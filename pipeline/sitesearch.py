"""S1b サイト内検索レンズ: 指定が1件も取れなかった自治体だけ、検索窓から直行する。

文化財レンズ (`discover.py` 第2段) は文化財の**区画**には届くが、その中の
無形民俗の頁に届かない自治体が残る。茨城で17自治体が指定0件のまま残り、
中身を見たところ原因は「文化財ページが無い」ではなかった:

    取手市 461ページ中461が「民俗」も「無形」も1文字も含まない
    大洗町 340ページ中335が同上
    利根町 328ページ中294が同上

URLに `bunkazai` を含むページは取れているが、それは史跡・埋蔵文化財・資料館
つまり**有形**の頁だった。無形民俗の一覧へリンクが伸びていない。

一方、実サイトで確かめると鉾田市には「市内の指定文化財」に
「無形民俗文化財 / 1 市 鉾田囃子 鉾田囃子連合保存会」が載っており、
**サイト内検索1回でそこへ直行できた**。

初日に「サイト内検索は14自治体中2つしか使えない」と測ったが、あれは無作為
抽出だった。指定0件の自治体に絞ると 5/17 で使える。1自治体あたり数フェッチ
なので、当たれば安く、外れれば「本当に指定が無い」ことの消極的な裏付けになる。

残る5自治体 (取手市・東海村・神栖市・つくば市・鹿嶋市) は検索結果がJS描画で
この方法では届かない。ヘッドレスブラウザが正当に効くのはそこだけである。

既存データ (data/festivals.json / benchmarks) は読まない。

使い方:
    python pipeline/sitesearch.py --prefecture 茨城県 --dry-run
    python pipeline/sitesearch.py --prefecture 茨城県
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bunkazai_local as B  # noqa: E402
from net import Fetcher  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = REPO_ROOT / "registry"
WORK_DIR = REPO_ROOT / "work"

# 自治体CMSでよくある検索のURL形。上から順に試し、当たった時点で止める。
TEMPLATES = [
    "search.php?keyword={q}",
    "?s={q}",
    "search.html?q={q}",
    "sitesearch.html?q={q}",
    "search/?q={q}",
    "?q={q}",
]
QUERIES = ["無形民俗文化財", "指定文化財 一覧"]

TAG = re.compile(r"<[^>]+>")
LINK = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
HOT = re.compile(r"無形民俗|民俗芸能|風俗慣習")
# 結果ページから拾うリンクの見出し。
# **「一覧」単独は入れない。** 入れたところ「工業団地企業 一覧」「公共施設 一覧」
# 「新着情報一覧」「親子・子育て・教育の一覧へ」を拾った。文化財の語が要る。
# 「市 指定文化財 一覧」は文化財で当たるので取りこぼさない。
WANT_LABEL = re.compile(r"文化財|民俗|芸能|囃子|ばやし|神楽|獅子|田楽|ささら|"
                        r"神輿|山車|保存会|祭|まつり|踊")
# 検索ページ自身・ナビへ戻るリンクは辿らない
SKIP_URL = re.compile(r"search|keyword|\?s=|sitemap|privacy|policy", re.I)
SKIP_LABEL = re.compile(r"^(?:くらし|健康|福祉|子育て|事業者|市政|町政|村政|"
                        r"組織|課所|ホーム|トップ|前へ|次へ|検索)")

MAX_LINKS_PER_MUNI = 8


def text_of(fragment: str) -> str:
    import html as _html
    return re.sub(r"\s+", " ", _html.unescape(TAG.sub(" ", fragment))).strip()


def zero_municipalities(prefecture: str) -> list[dict[str, str]]:
    """その県で、指定が1件も取れていない自治体の台帳行を返す。

    出所は2つある。機械抽出 (`bunkazai_local.tsv`) と AI由来
    (`bunkazai_ai.tsv`)。片方しか見ないと、AIが既に拾った自治体へ
    無駄に検索を投げることになる。
    """
    have: set[str] = set()
    for path in (B.OUT_TSV, REGISTRY_DIR / "bunkazai_ai.tsv",
                 REGISTRY_DIR / "bunkazai_tobunken.tsv"):
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            continue
        header = lines[0].split("\t")
        for ln in lines[1:]:
            if not ln.strip():
                continue
            r = dict(zip(header, ln.split("\t")))
            if r.get("prefecture") == prefecture:
                have.add(r.get("municipality", ""))
    out = []
    sites = REGISTRY_DIR / "sites.tsv"
    lines = sites.read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    for ln in lines[1:]:
        if not ln.strip():
            continue
        r = dict(zip(header, ln.split("\t")))
        if r.get("prefecture") != prefecture:
            continue
        if r.get("municipality") in have or not r.get("official_url"):
            continue
        out.append(r)
    return out


def search_once(fetcher: Fetcher, base: str, query: str) -> tuple[str, str] | None:
    """検索結果ページを返す。(使ったURL, 本文)。当たらなければ None。"""
    for tmpl in TEMPLATES:
        url = urllib.parse.urljoin(base, tmpl.format(q=urllib.parse.quote(query)))
        doc = fetcher.get(url)
        if doc is None or doc.status != 200:
            continue
        flat = text_of(doc.text)
        # 問い合わせ語の反射だけでは当たりとしない
        if len(HOT.findall(flat)) >= 2 or "文化財" in flat:
            return url, doc.text
    return None


def result_links(page_html: str, base: str) -> list[tuple[str, str]]:
    """結果ページから、同一ホストの文化財らしきリンクを拾う。

    「見つかりませんでした」だけで打ち切ってはいけない。鉾田市の結果は
    「インデックス（索引） 見つかりませんでした。 ページ 市内の指定文化財 …」
    という形で、索引が0件でも本文の結果はある。
    """
    host = urllib.parse.urlparse(base).netloc
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in LINK.finditer(page_html):
        try:
            url = urllib.parse.urljoin(base, m.group(1))
        except ValueError:
            continue
        if urllib.parse.urlparse(url).netloc != host or SKIP_URL.search(url):
            continue
        label = text_of(m.group(2))
        if not label or len(label) > 60 or SKIP_LABEL.match(label):
            continue
        if not WANT_LABEL.search(label):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((label, url))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefecture", required=True)
    ap.add_argument("--dry-run", action="store_true", help="取得せず、届くURLだけ出す")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args(argv)

    targets = zero_municipalities(args.prefecture)
    if not targets:
        print("指定0件の自治体は無い")
        return 0
    print("指定0件の自治体: %d" % len(targets))

    fetcher = Fetcher(delay=args.delay)
    usable = empty = unusable = 0
    fetched = 0
    rows: list[tuple[str, str, str]] = []

    for site in targets:
        muni, base = site["municipality"], site["official_url"]
        hit = None
        for q in QUERIES:
            hit = search_once(fetcher, base, q)
            if hit:
                links = result_links(hit[1], base)
                if links:
                    break
                hit = None
        if hit is None:
            unusable += 1
            print("  %-10s ×  検索が使えない (JS描画等)" % muni, flush=True)
            continue
        links = result_links(hit[1], base)[:MAX_LINKS_PER_MUNI]
        if not links:
            empty += 1
            print("  %-10s 0  検索は動くが結果リンク無し" % muni, flush=True)
            continue
        usable += 1
        got = 0
        for label, url in links:
            rows.append((muni, label, url))
            if not args.dry_run:
                doc = fetcher.get(url)
                if doc is not None and doc.status == 200:
                    got += 1
                    fetched += 1
        print("  %-10s ○  リンク%d 取得%d  例: %s"
              % (muni, len(links), got, links[0][0][:26]), flush=True)

    out = WORK_DIR / "sitesearch_hits.tsv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\t".join(["municipality", "label", "url"]) + "\n"
                   + "".join("\t".join(r) + "\n" for r in rows),
                   encoding="utf-8", newline="\n")
    print()
    print("使えた %d / 結果なし %d / 使えない %d  (計 %d)"
          % (usable, empty, unusable, len(targets)))
    print("取得したページ: %d   -> %s" % (fetched, out))
    print("fetcher:", fetcher.stats)
    if not args.dry_run and fetched:
        print()
        print("次: python pipeline/bunkazai_local.py --build --prefecture %s"
              % args.prefecture)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
