"""S1 発見: 自治体公式サイト・観光協会サイトから、祭り情報のありそうなページを集める。

検索エンジンを使わない。台帳のトップページを起点に、
  - サイトマップ (sitemap.xml)
  - RSS/Atom フィード
  - 観光・イベント・文化財等のカテゴリインデックスの階層
を辿る幅優先探索でページURLを集め、本文を cache/ に落とす。

事前調査で確認した設計根拠:
  自治体サイトの多くに sitemap.xml が無い (鉾田市・龍ケ崎市・常陸大宮市・
  笠間市はいずれも404)。一方でトップページからは、イベントRSS
  (rss.php?mode=news&type=1) とカテゴリ階層 (page/dir000006.html) に到達できる。
  ニュース一覧だけを見ると、掲載期間の切れた常設ページ (文化財の
  「鉾田ばやし」等) に届かないため、カテゴリ階層の探索が要る。

既存の festivals.json / benchmarks を参照しない。

使い方:
    python pipeline/discover.py --prefecture 茨城県
    python pipeline/discover.py --prefecture 茨城県 --municipality 鉾田市 --max-pages 40
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
import heapq
import itertools
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from net import Fetcher  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = REPO_ROOT / "registry"
WORK_DIR = REPO_ROOT / "work"

# AGENTS.md §3.1 の探索レンズを、リンクを辿る手掛かりに落としたもの。
# 祭りそのものの語だけでなく、祭りへ到達する入口の語を含める。
NAV_HINT = re.compile(
    r"観光|イベント|催し|行事|まつり|マツリ|祭|文化財|民俗|芸能|保存会|"
    r"カレンダー|年間|季節|歳時|名所|見どころ|遊ぶ|楽しむ|魅力|特産|"
    r"公園|神社|寺|史跡|歴史|伝統|花|市民|コミュニティ|地区|自治会|商店街"
)
# 明らかに祭りへ繋がらない行政手続き系。探索枠を食うので落とす。
NAV_SKIP = re.compile(
    r"申請|届出|手続|税|保険|年金|ごみ|廃棄物|入札|契約|採用|例規|議会|"
    r"予算|決算|統計|人事|給与|条例|パブリックコメント|個人情報|"
    r"サイトポリシー|アクセシビリティ|プライバシー|よくある質問|"
    r"新型コロナ|ワクチン|防災|避難|水道|下水|道路工事|求人|入札結果"
)
# 自治体CMSのURLはセクションをローマ字で切る。日本語の語彙では
# URLから何も読み取れないので、同じ判断をローマ字でも行う。
# リンク文字列側の祭り語彙。extract.py の FESTIVAL_WORD とほぼ同じだが、
# こちらは「探索を優先するか」の判断にだけ使う。
_FESTIVAL_LABEL = re.compile(
    r"祭|まつり|マツリ|花火|盆踊|神輿|山車|囃子|ばやし|獅子舞|"
    r"文化財|民俗|芸能|保存会|縁日|だるま市|達磨市|朝市|"
    r"ささら|田楽|神楽|綱火|盆綱|流鏑馬|万灯|大道芸|不動尊"
)
_ROMAJI_FESTIVAL = re.compile(
    r"matsuri|maturi|omatsuri|bunkazai|minzoku|geino|geinou|"
    r"hanabi|mikoshi|dashi|shishimai|bonodori|saiten|reitaisai"
)
_ROMAJI_TOURISM = re.compile(
    r"kanko|kankou|kanko_|tanoshimu|tanosimu|asobu|miru|event|ivent|"
    r"sightsee|tourism|guide|meisho|midokoro|bunka|rekishi|shiseki|"
    r"kyodo|kyoudo|calendar|nenkan|gyoji|gyouji|shizen|koen|kouen|"
    # 文化財は生涯学習・教育委員会の配下に置かれていることが多い
    r"shogaigakushu|syogaigakusyu|shougaigakushuu|gakushu|kyoiku|kyouiku"
)
NAV_SKIP_ROMAJI = re.compile(
    r"gomi|haikibutsu|zeikin|/zei|kokuho|hoken|nenkin|nyusatsu|keiyaku|"
    r"saiyo|saiyou|jinji|kyuyo|jorei|reiki|gikai|yosan|kessan|tokei|"
    r"bosai|hinan|suido|gesui|kosodate|fukushi|kaigo|kenshin|yobou|"
    r"privacy|sitemap_policy|accessibility|faq|toiawase|shinsei|"
    r"todokede|tetsuzuki|koho_|kouhou_|pubcom|corona|vaccine"
)
SKIP_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|jpe?g|png|gif|svg|mp4|mp3)$", re.I)

FEED_HINT = re.compile(r"rss|atom|feed", re.I)

# 1自治体あたりの探索キューの上限。取得上限が決まっているので
# これ以上抱えても使われない。並列数を掛けた分だけメモリを食うので
# 控えめにする (このPCは実装容量6GB、空き数百MBで動かしている)。
MAX_QUEUE = 5000

# 行政手続き系リンクの優先度。捨てるのではなく最後尾に回す。
DEPRIORITIZED = 90

# URLのローマ字パスを「優先」の根拠に使うか。除外(優先度下げ)には常に使う。
# 茨城の実測では優先側を有効にすると再現率が 66% -> 64% に下がったため、
# 既定では無効。bunka/event/koen のような広い語が大きな低収量の部分木を
# 引き上げてしまうのが原因と見ている。
USE_ROMAJI_BOOST = False


def clean_text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def page_title(text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    return re.sub(r"\s+", " ", clean_text(m.group(1))) if m else ""


def safe_join(base_url: str, href: str) -> str:
    """urljoin の例外を潰す。壊れた href は空文字を返す。

    href="//＃Jump01" のような全角文字を含むリンクで urljoin が
    NFKC 正規化の検査に引っかかって ValueError を投げる。静岡県では
    これ1本で県全体の実行が落ちた。自治体サイトのHTMLは手書きが多く、
    壊れたリンクは想定内として扱う。
    """
    try:
        return urllib.parse.urljoin(base_url, href)
    except ValueError:
        return ""


def iter_links(text: str, base_url: str) -> list[tuple[str, str]]:
    """(絶対URL, アンカーテキスト) を返す。"""
    out = []
    for m in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, re.S | re.I):
        href = html.unescape(m.group(1)).strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        label = re.sub(r"\s+", " ", clean_text(m.group(2)))
        absolute = safe_join(base_url, href)
        if not absolute:
            continue
        out.append((absolute.split("#", 1)[0], label))
    return out


def find_feeds(text: str, base_url: str) -> list[str]:
    feeds = []
    for m in re.finditer(
        r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*href="([^"]+)"', text, re.I
    ):
        feed = safe_join(base_url, html.unescape(m.group(1)))
        if feed:
            feeds.append(feed)
    for url, _label in iter_links(text, base_url):
        if FEED_HINT.search(urllib.parse.urlparse(url).path + "?" + (urllib.parse.urlparse(url).query or "")):
            feeds.append(url)
    seen: set[str] = set()
    return [f for f in feeds if not (f in seen or seen.add(f))]


def urls_from_xml(text: str, base_url: str) -> list[str]:
    """sitemap.xml / RSS / Atom から URL を取り出す。"""
    urls: list[str] = []
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError:
        return urls
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag in ("loc", "link"):
            value = (el.text or "").strip() or el.get("href", "")
            if value:
                child = safe_join(base_url, value)
                if child:
                    urls.append(child)
        elif tag == "guid" and (el.text or "").startswith("http"):
            urls.append(el.text.strip())
    return urls


_FEED_ROOT = re.compile(r"<\s*(urlset|sitemapindex|rss|feed)\b", re.I)


def is_feed_or_sitemap(text: str) -> bool:
    """sitemap.xml / RSS / Atom かどうか。

    XML宣言の有無で判定してはいけない。XHTMLで書かれた自治体サイトは
    `<?xml version="1.0"?>` で始まるため、サイトマップと誤認して
    本文のリンクを一切辿らなくなる。龍ケ崎市と取手市が実際にこれで
    トップページ1枚しか取得できていなかった。
    ルート要素で判定し、HTMLなら除外する。
    """
    head = text.lstrip()[:600]
    if re.search(r"<\s*(!DOCTYPE\s+html|html)\b", head, re.I):
        return False
    return bool(_FEED_ROOT.search(head))


def same_site(url: str, hosts: set[str]) -> bool:
    return urllib.parse.urlparse(url).netloc in hosts


def link_priority(url: str, label: str) -> int:
    """探索順の優先度。小さいほど先に見る。

    リンク文字列だけでなく URL のローマ字パスも見る。自治体CMSは
    セクションをローマ字で切っており (/tanoshimu/ /kankyo_gomi/ /shisei/)、
    日本語の語彙だけで採点するとURLから何も読み取れない。実際に守谷市では
    ごみ収集の配下に19ページを使い、祇園祭のある /tanoshimu/ に
    1ページも入れていなかった。
    """
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path).lower()
    blob = f"{label} {path}"
    if NAV_SKIP.search(blob) or NAV_SKIP_ROMAJI.search(path):
        # 捨てずに最後尾へ回す。トップページから数本しかリンクが無い
        # 自治体があり (下妻市はJS駆動のナビで同一ホスト5本のみ)、
        # 除外語で弾くと探索が手詰まりになって1ページも進まない。
        return DEPRIORITIZED
    score = 5
    if _FESTIVAL_LABEL.search(blob) or (USE_ROMAJI_BOOST and _ROMAJI_FESTIVAL.search(path)):
        score = 0
    elif re.search(r"観光|イベント|催し|行事|年間|カレンダー|歳時|季節", blob) or (USE_ROMAJI_BOOST and _ROMAJI_TOURISM.search(path)):
        score = 1
    elif NAV_HINT.search(blob):
        score = 3
    return score


def discover_site(
    fetcher: Fetcher,
    seeds: list[str],
    max_pages: int,
    max_depth: int,
) -> list[dict[str, str]]:
    """1自治体分の探索。訪問したページの記録を返す。"""
    hosts = {urllib.parse.urlparse(s).netloc for s in seeds if s}
    visited: set[str] = set()
    records: list[dict[str, str]] = []
    # 優先度付きキュー。1自治体あたりの取得数には上限があるので、
    # 単純な幅優先だと優先度の低い浅いページが枠を食い潰し、
    # 祭り一覧のような深い高優先ページに届かない。
    queue: list[tuple[int, int, int, str, str]] = []
    order = itertools.count()

    def push(url: str, depth: int, kind: str, priority: int) -> None:
        # 深さを増やすとキューが青天井に伸びる。取得上限が決まっている以上、
        # 優先度の低い末尾を抱え続けても使われないので捨てる。
        if len(queue) >= MAX_QUEUE:
            if priority >= queue[-1][0]:
                return
            queue.pop()
            heapq.heapify(queue)
        heapq.heappush(queue, (priority, depth, next(order), url, kind))

    for s in seeds:
        if s:
            push(s, 0, "seed", -1)
            # sitemap.xml は無い自治体が多いが、あれば一番安い経路
            push(urllib.parse.urljoin(s, "/sitemap.xml"), 0, "sitemap", -1)

    while queue and len(records) < max_pages:
        _priority, depth, _seq, url, kind = heapq.heappop(queue)
        if url in visited or not same_site(url, hosts) or SKIP_EXT.search(url):
            continue
        visited.add(url)
        doc = fetcher.get(url)
        if doc is None or doc.status != 200:
            continue

        is_xml = is_feed_or_sitemap(doc.text)
        records.append(
            {
                # url は出典として残す最終URL、fetch_url はキャッシュの引き当てキー。
                # リダイレクトがあると両者が食い違い、final_url で引くと
                # キャッシュに当たらず本文が抽出から静かに漏れる (実測144ページ)。
                "url": doc.final_url,
                "fetch_url": url,
                "kind": kind,
                "depth": str(depth),
                "title": page_title(doc.text),
            }
        )

        if depth >= max_depth:
            continue

        if is_xml:
            for child in urls_from_xml(doc.text, url)[:200]:
                if child not in visited and same_site(child, hosts):
                    # sitemap/フィードの項目はURLしか手掛かりが無いので、
                    # URL自体を採点する
                    push(child, depth + 1,
                         "sitemap" if kind == "sitemap" else "feed",
                         link_priority(child, ""))
            continue

        if depth == 0:
            for feed in find_feeds(doc.text, url):
                if feed not in visited and same_site(feed, hosts):
                    push(feed, depth + 1, "feed", 0)

        for child, label in iter_links(doc.text, url):
            if child in visited or not same_site(child, hosts) or SKIP_EXT.search(child):
                continue
            push(child, depth + 1, "index", link_priority(child, label))

    return records


PREFECTURE_WIDE = "(県全域)"


def load_prefecture_sites(prefecture: str | None) -> list[dict[str, str]]:
    """都道府県の公式サイト・教育委員会を、県単位の探索レンズとして返す。

    文化財・無形民俗文化財の一覧は市町村サイトではなく県教育委員会に
    まとまっていることが多い。市町村ごとに回すより、県で1本にまとめた
    ほうが安く網羅的になる (AGENTS.md §3.1 レンズ7)。
    このレンズで見つけた行事は所在市町村が自明でないため、
    municipality を PREFECTURE_WIDE にしておき extract.py で割り当てる。
    """
    path = REGISTRY_DIR / "pref_sites.tsv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    if prefecture:
        rows = [r for r in rows if r["prefecture"] == prefecture]
    out = []
    for r in rows:
        if r["official_url"] or r["education_url"]:
            out.append(
                {
                    "prefecture": r["prefecture"],
                    "municipality": PREFECTURE_WIDE,
                    # 文化財一覧を持つ教育委員会を先に見る
                    "official_url": r["education_url"] or r["official_url"],
                    "tourism_url": r["official_url"] if r["education_url"] else "",
                }
            )
    return out


def already_done(path: Path) -> set[tuple[str, str]]:
    """pages.tsv に既に記録がある (都道府県, 市町村) を返す。

    このPCは実装6GBで、長時間のクロールが何度も強制終了されている。
    やり直すたびに最初から歩き直すのは無駄なので、済んだ市町村は飛ばす。
    キャッシュがあるので再取得は起きないが、それでも数分は縮む。
    """
    if not path.is_file():
        return set()
    with path.open(encoding="utf-8", newline="") as fh:
        return {(r["prefecture"], r["municipality"]) for r in csv.DictReader(fh, delimiter="\t")}


def load_sites(prefecture: str | None, municipality: str | None) -> list[dict[str, str]]:
    path = REGISTRY_DIR / "sites.tsv"
    if not path.is_file():
        raise SystemExit("先に registry.py --resolve-sites を実行してください")
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    if prefecture:
        rows = [r for r in rows if r["prefecture"] == prefecture]
    if municipality:
        rows = [r for r in rows if r["municipality"] == municipality]
    return [r for r in rows if r["official_url"] or r["tourism_url"]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefecture")
    ap.add_argument("--municipality")
    ap.add_argument("--max-pages", type=int, default=40,
                    help="1自治体あたりの取得上限。増やしても割に合わない。"
                         "茨城の実測では 40 と 150 で gold 25/34 対 23/34、"
                         "snapshot 再現 62%% 対 66%% であり、"
                         "4倍近いページ数の見返りが4ポイントしかない。"
                         "情報価値は上位20ページに8割が集中している")
    ap.add_argument("--pref-lens-pages", type=int, default=250,
                    help="県単位レンズ(教育委員会の文化財一覧等)の取得上限")
    ap.add_argument("--no-prefecture-lens", action="store_true",
                    help="県単位レンズを使わない")
    ap.add_argument("--workers", type=int, default=4,
                    help="並行して探索する自治体数。礼儀はホスト単位の制約なので"
                         "別ホストへは同時にアクセスしてよいが、並列数だけ"
                         "メモリを食う。空きメモリが少ない環境では下げる")
    ap.add_argument("--max-depth", type=int, default=2)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--resume", action="store_true",
                    help="pages.tsv に既に記録がある市町村を飛ばして追記する")
    args = ap.parse_args(argv)

    sites = load_sites(args.prefecture, args.municipality)
    pref_sites = (
        []
        if (args.no_prefecture_lens or args.municipality)
        else load_prefecture_sites(args.prefecture)
    )
    targets = sites + pref_sites
    done: set = set()
    if args.resume:
        done = already_done(WORK_DIR / "pages.tsv")
        before = len(targets)
        targets = [t for t in targets if (t["prefecture"], t["municipality"]) not in done]
        print(f"--resume: {before - len(targets)} 市町村は済んでいるので飛ばす")
    if not targets:
        raise SystemExit("対象がありません" if not done else "すべて済んでいる")

    fetcher = Fetcher(delay=args.delay, offline=args.offline)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    out = WORK_DIR / "pages.tsv"
    written = 0
    # --resume で既に済んだ分があるときだけ追記。それ以外は作り直す。
    mode = "a" if (args.resume and done) else "w"
    with out.open(mode, encoding="utf-8", newline="\n") as fh:
        w = csv.DictWriter(
            fh,
            delimiter="\t",
            lineterminator="\n",
            fieldnames=["prefecture", "municipality", "url", "fetch_url", "kind", "depth", "title"],
        )
        if mode == "w":
            w.writeheader()
            fh.flush()

        def crawl(site: dict[str, str]):
            seeds = [u for u in (site["official_url"], site["tourism_url"]) if u]
            budget = (
                args.pref_lens_pages
                if site["municipality"] == PREFECTURE_WIDE
                else args.max_pages
            )
            try:
                return site, discover_site(fetcher, seeds, budget, args.max_depth)
            except Exception as exc:  # noqa: BLE001
                # 1自治体で落ちても他の43件を巻き添えにしない。
                # 静岡県では壊れた href 1本で県全体が失敗した。
                print(
                    f"  {site['municipality']:12s} 失敗: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                return site, []

        # 自治体ごとに別ホストなので並行して回せる。同一ホストへの直列
        # アクセスと1秒間隔は Fetcher がホスト単位のロックで保証する。
        # 直列に回すと待ち時間が積み上がり、150ページ×44市町村で
        # 10時間近くかかっていた。
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            for site, recs in pool.map(crawl, targets):
                for r in recs:
                    r.update(prefecture=site["prefecture"], municipality=site["municipality"])
                    # タブと改行はTSVを壊すので落とす
                    r["title"] = r["title"].replace("\t", " ")
                w.writerows(recs)
                fh.flush()
                written += len(recs)
                print(
                    f"  {site['municipality']:12s} {len(recs):3d} pages  "
                    f"(net={fetcher.stats['network']} cache={fetcher.stats['cache']})",
                    flush=True,
                )

    print(f"\npages.tsv: {written} ページ")
    print("fetcher stats:", fetcher.stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
