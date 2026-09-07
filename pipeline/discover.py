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
from collections import deque
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
SKIP_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|jpe?g|png|gif|svg|mp4|mp3)$", re.I)

FEED_HINT = re.compile(r"rss|atom|feed", re.I)


def clean_text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def page_title(text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    return re.sub(r"\s+", " ", clean_text(m.group(1))) if m else ""


def iter_links(text: str, base_url: str) -> list[tuple[str, str]]:
    """(絶対URL, アンカーテキスト) を返す。"""
    out = []
    for m in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, re.S | re.I):
        href = html.unescape(m.group(1)).strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        label = re.sub(r"\s+", " ", clean_text(m.group(2)))
        out.append((urllib.parse.urljoin(base_url, href).split("#", 1)[0], label))
    return out


def find_feeds(text: str, base_url: str) -> list[str]:
    feeds = []
    for m in re.finditer(
        r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*href="([^"]+)"', text, re.I
    ):
        feeds.append(urllib.parse.urljoin(base_url, html.unescape(m.group(1))))
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
                urls.append(urllib.parse.urljoin(base_url, value))
        elif tag == "guid" and (el.text or "").startswith("http"):
            urls.append(el.text.strip())
    return urls


def same_site(url: str, hosts: set[str]) -> bool:
    return urllib.parse.urlparse(url).netloc in hosts


def link_priority(url: str, label: str) -> int:
    """探索順の優先度。小さいほど先に見る。"""
    blob = f"{label} {urllib.parse.unquote(url)}"
    if NAV_SKIP.search(blob):
        return 99
    score = 5
    if re.search(r"祭|まつり|マツリ|花火|盆踊|神輿|山車|囃子|獅子舞|文化財|民俗|芸能", blob):
        score = 0
    elif re.search(r"観光|イベント|催し|行事|年間|カレンダー|歳時|季節", blob):
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
    queue: deque[tuple[str, int, str]] = deque()
    for s in seeds:
        if s:
            queue.append((s, 0, "seed"))
            # sitemap.xml は無い自治体が多いが、あれば一番安い経路
            queue.append((urllib.parse.urljoin(s, "/sitemap.xml"), 0, "sitemap"))

    while queue and len(records) < max_pages:
        url, depth, kind = queue.popleft()
        if url in visited or not same_site(url, hosts) or SKIP_EXT.search(url):
            continue
        visited.add(url)
        doc = fetcher.get(url)
        if doc is None or doc.status != 200:
            continue

        body = doc.text.lstrip()
        is_xml = body.startswith("<?xml") or "<rss" in body[:400] or "<urlset" in body[:400] or "<feed" in body[:400]
        records.append(
            {
                "url": doc.final_url,
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
                    queue.append((child, depth + 1, "sitemap" if kind == "sitemap" else "feed"))
            continue

        if depth == 0:
            for feed in find_feeds(doc.text, url):
                if feed not in visited and same_site(feed, hosts):
                    queue.appendleft((feed, depth + 1, "feed"))

        scored = []
        for child, label in iter_links(doc.text, url):
            if child in visited or not same_site(child, hosts) or SKIP_EXT.search(child):
                continue
            p = link_priority(child, label)
            if p < 99:
                scored.append((p, child))
        scored.sort(key=lambda t: t[0])
        for _p, child in scored[:60]:
            queue.append((child, depth + 1, "index"))

    return records


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
    ap.add_argument("--max-pages", type=int, default=40, help="1自治体あたりの取得上限")
    ap.add_argument("--max-depth", type=int, default=2)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args(argv)

    sites = load_sites(args.prefecture, args.municipality)
    if not sites:
        raise SystemExit("対象がありません")

    fetcher = Fetcher(delay=args.delay, offline=args.offline)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    out = WORK_DIR / "pages.tsv"
    written = 0
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.DictWriter(
            fh,
            delimiter="\t",
            lineterminator="\n",
            fieldnames=["prefecture", "municipality", "url", "kind", "depth", "title"],
        )
        w.writeheader()
        for site in sites:
            seeds = [u for u in (site["official_url"], site["tourism_url"]) if u]
            recs = discover_site(fetcher, seeds, args.max_pages, args.max_depth)
            for r in recs:
                r.update(prefecture=site["prefecture"], municipality=site["municipality"])
                # タブと改行はTSVを壊すので落とす
                r["title"] = r["title"].replace("\t", " ")
                w.writerow(r)
            written += len(recs)
            fh.flush()
            print(f"  {site['municipality']:12s} {len(recs):3d} pages  (net={fetcher.stats['network']} cache={fetcher.stats['cache']})")

    print(f"\npages.tsv: {written} ページ")
    print("fetcher stats:", fetcher.stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
