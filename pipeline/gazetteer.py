"""S0.7 既知台帳: Wikipedia から「すでに知られている祭り」の一覧を作る。

**これはシードではなく除外リストである。** 当初目的は「知らなかった祭り・
マイナーな祭りを見つけること」なので、Wikipedia に載っている祭りは目的から
すると *既知* にあたる。件数が少ない (茨城40件・愛知52件) ことは、シードには
不足だが除外リストとしては問題にならない。

`novelty.py` がこれを使って「新規性」を測る。従来の recall は
`benchmarks/ibaraki-2026-09-06` (旧AI手法の出力そのもの) に対する一致率で、
定義上「すでに見つけたものを、また見つける」性能しか測っていない。新しく
発見した祭りは0点になるので、当初目的と指標が逆を向いていた。

Wikipedia のカテゴリ「<都道府県>の祭り」とその下位カテゴリを辿る。
API は robots.txt で禁じられていない (禁止対象は特定のbotのみ)。

使い方:
    python pipeline/gazetteer.py --build
    python pipeline/gazetteer.py --build --prefecture 沖縄県
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from net import USER_AGENT, build_ssl_context  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = REPO_ROOT / "registry"
OUT_TSV = REGISTRY_DIR / "gazetteer_wikipedia.tsv"

API = "https://ja.wikipedia.org/w/api.php"
DELAY = 1.0
MAX_DEPTH = 2

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]

# 県ごとに辿る起点カテゴリ。「祭り」だけだと年中行事や民俗芸能を取り逃す。
# 「<県>の文化」は入れない。試したところ映画・音楽・飲食店・スポーツまで
# 引きずり込み、沖縄で1407件になった (祭りは実際には数十件)。
ROOT_TEMPLATES = [
    "Category:{pref}の祭り",
    "Category:{pref}の年中行事",
    "Category:{pref}の民俗芸能",
]
# 下位カテゴリを辿ってよいかの判定。「那覇市の祭り」は辿るが、
# 途中に紛れる「沖縄県の観光地」のようなカテゴリで脱線しない。
SUBCAT_OK = re.compile(r"祭|まつり|行事|芸能|民俗|神楽|踊|囃子|山車|神輿|花火")

COLUMNS = ["prefecture", "name", "category", "url"]

_CTX: ssl.SSLContext | None = None
_LAST = [0.0]


def api(**params) -> dict:
    global _CTX
    if _CTX is None:
        _CTX = build_ssl_context()
    params.setdefault("action", "query")
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    wait = DELAY - (time.monotonic() - _LAST[0])
    if wait > 0:
        time.sleep(wait)
    _LAST[0] = time.monotonic()
    url = API + "?" + urllib.parse.urlencode(params, encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
        return json.load(r)


def members(category: str, kind: str) -> list[str]:
    """カテゴリの直下メンバー。kind は "page" か "subcat"。"""
    out: list[str] = []
    cont: dict[str, str] = {}
    while True:
        try:
            d = api(list="categorymembers", cmtitle=category, cmlimit="500",
                    cmtype=kind, **cont)
        except Exception as exc:
            print("  ! %s (%s): %r" % (category, kind, exc), file=sys.stderr)
            return out
        out.extend(m["title"] for m in d.get("query", {}).get("categorymembers", []))
        if "continue" not in d:
            return out
        cont = d["continue"]


def walk(root: str, depth: int, seen_cats: set[str]) -> list[tuple[str, str]]:
    """(記事名, 直接の所属カテゴリ) を返す。"""
    if depth > MAX_DEPTH or root in seen_cats:
        return []
    seen_cats.add(root)
    found = [(title, root) for title in members(root, "page")]
    if depth < MAX_DEPTH:
        for sub in members(root, "subcat"):
            if not SUBCAT_OK.search(sub):
                continue
            found.extend(walk(sub, depth + 1, seen_cats))
    return found


def build(prefectures: list[str]) -> list[dict[str, str]]:
    rows: dict[tuple[str, str], dict[str, str]] = {}
    for i, pref in enumerate(prefectures, 1):
        seen_cats: set[str] = set()
        got = 0
        for tmpl in ROOT_TEMPLATES:
            for title, cat in walk(tmpl.format(pref=pref), 0, seen_cats):
                if title.startswith(("Category:", "Template:", "Portal:", "Wikipedia:")):
                    continue
                key = (pref, title)
                if key in rows:
                    continue
                rows[key] = {
                    "prefecture": pref,
                    "name": title,
                    "category": cat.replace("Category:", ""),
                    "url": "https://ja.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
                }
                got += 1
        print("[%2d/%2d] %-5s %4d件" % (i, len(prefectures), pref, got), flush=True)
    return list(rows.values())


def write(rows: list[dict[str, str]]) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    order = {p: i for i, p in enumerate(PREFECTURES)}
    rows = sorted(rows, key=lambda r: (order.get(r["prefecture"], 99), r["name"]))
    body = "\t".join(COLUMNS) + "\n"
    body += "".join("\t".join(r[c].replace("\t", " ") for c in COLUMNS) + "\n" for r in rows)
    tmp = OUT_TSV.with_suffix(".tsv.tmp")
    tmp.write_text(body, encoding="utf-8", newline="\n")
    tmp.replace(OUT_TSV)


def load(prefecture: str | None = None) -> set[str]:
    if not OUT_TSV.exists():
        return set()
    lines = OUT_TSV.read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    out = set()
    for ln in lines[1:]:
        if not ln.strip():
            continue
        r = dict(zip(header, ln.split("\t")))
        if prefecture is None or r["prefecture"] == prefecture:
            out.add(r["name"])
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--prefecture", action="append")
    args = ap.parse_args(argv)
    if not args.build:
        ap.print_help()
        return 0
    rows = build(args.prefecture or PREFECTURES)
    write(rows)
    print()
    print("合計 %d件 -> %s" % (len(rows), OUT_TSV))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
