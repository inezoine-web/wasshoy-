"""S0.5c-1 AI判定バッチの作成: 機械抽出が0件だったページを整形して切り出す。

`bunkazai_local.py` は書式に依存しない規則
(「指定区分と種別のうち文脈から補ってよいのは片方だけ」) までは機械化できたが、
そこから先は**任意の表の意味を読む**仕事で正規表現の担当ではない。実測では
同じCMSの自治体どうしで指定区分と種別の置き場所が逆になっており、書式ごとに
規則を足す方法は破綻する (茨城だけで4通り、途中2回巻き戻した)。

そこで残余だけをAIに渡す。対象は機械的に定義できる:

    「無形民俗」等を含むのに `bunkazai_local` が1件も取れなかったページ

**AIに探索させない。** 渡すのは確定した小さな入力で、やることは
「この表から指定されている行事名を書き出す」だけ。指示は
`pipeline/bunkazai_ai_card.md` にあり、バッチ先頭にそのまま埋め込む。

整形はここで行う。実測では素のHTMLを渡すと WordPress の inline JS と CSS が
そのまま入り、1ページ1500文字のうち大半がゴミだった。script/style/nav を落とし、
見出しと表だけを残す。

使い方:
    python pipeline/bunkazai_ai_prepare.py --prefecture 茨城県
    python pipeline/bunkazai_ai_prepare.py --prefecture 茨城県 --limit 20
    -> work/bunkazai_ai/bz_batch_NNN.txt と index.tsv ができる
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bunkazai_local as B  # noqa: E402
from net import decode_html  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / "work"
BATCH_DIR = WORK_DIR / "bunkazai_ai"
CARD = Path(__file__).resolve().parent / "bunkazai_ai_card.md"

INDEX_FIELDS = ["page_id", "prefecture", "municipality", "url"]

# ページに「無形民俗」等が実際にあるか。`bunkazai_local` の粗い前段より厳しい。
HOT = re.compile(r"無形民俗|民俗文化財|民俗芸能|風俗慣習")

# 整形で落とすもの。ここを削らないと inline JS と CSS が本文を埋める。
DROP_BLOCK = re.compile(
    r"<(script|style|noscript|svg|head)\b.*?</\1>|<!--.*?-->", re.S | re.I)
DROP_NAV = re.compile(
    r"<(nav|header|footer|form|select)\b.*?</\1>", re.S | re.I)
TABLE = re.compile(r"<table\b.*?</table>", re.S | re.I)
TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
TD = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
HEAD = re.compile(r"<(h[1-4]|caption)\b[^>]*>(.*?)</\1>", re.S | re.I)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

MAX_PAGE_CHARS = 2500
DEFAULT_BATCH = 12


def clean_page(html_text: str) -> str:
    """AIに渡す形に整える。見出しと表だけを残す。"""
    body = DROP_BLOCK.sub(" ", html_text)
    body = DROP_NAV.sub(" ", body)

    parts: list[str] = []
    m = TITLE.search(html_text)
    if m:
        t = B.text_of(m.group(1))
        if t:
            parts.append("TITLE: " + t)

    # 見出しと表を、文書順に並べ直す
    events: list[tuple[int, str]] = []
    for m in HEAD.finditer(body):
        h = B.text_of(m.group(2))
        if h and len(h) <= 60:
            events.append((m.start(), "H: " + h))
    for m in TABLE.finditer(body):
        rows = []
        for row in TR.finditer(m.group(0)):
            cells = [B.text_of(c) for c in TD.findall(row.group(1))]
            cells = [c for c in cells if c]
            if cells:
                rows.append("| " + " | ".join(c[:40] for c in cells) + " |")
        if rows:
            events.append((m.start(), "\n".join(rows[:60])))
    events.sort()
    parts.extend(text for _pos, text in events)

    out = "\n".join(parts)
    if not TABLE.search(body):
        # 表が無いページは、種別の語の周辺だけを添える
        flat = B.text_of(body)
        near = []
        for m in HOT.finditer(flat):
            near.append(flat[max(0, m.start() - 120):m.start() + 200])
            if len(near) >= 4:
                break
        if near:
            out += "\n本文抜粋: " + " … ".join(near)
    return out[:MAX_PAGE_CHARS].strip()


def page_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def collect(prefecture: str, limit: int) -> list[dict[str, str]]:
    h2m = B.host_map()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for host_dir in sorted(B.CACHE_DIR.iterdir()):
        if len(out) >= limit:
            break
        if not host_dir.is_dir() or host_dir.name not in h2m:
            continue
        pref, muni = h2m[host_dir.name]
        if pref != prefecture:
            continue
        taken_here = 0
        for meta_path in host_dir.glob("*.meta.json"):
            if len(out) >= limit or taken_here >= 8:
                break
            body_path = meta_path.with_suffix("").with_suffix(".body")
            if not body_path.is_file():
                continue
            try:
                if body_path.stat().st_size > B.MAX_BODY:
                    continue
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            ctype = meta.get("content_type", "") or ""
            if "html" not in ctype.lower():
                continue
            try:
                raw = body_path.read_bytes()
            except Exception:
                continue
            if not any(k in raw for k in B._PREFILTER):
                continue
            try:
                text, _ = decode_html(raw, ctype)
            except Exception:
                continue
            url = meta.get("final_url") or ""
            if HOT.search(text) and url and url not in seen:
                # 機械抽出が取れているページは渡さない
                if not B.rows_of_page(raw, ctype, url, (pref, muni)):
                    cleaned = clean_page(text)
                    if len(cleaned) >= 120:
                        seen.add(url)
                        taken_here += 1
                        out.append({
                            "page_id": page_id(url), "prefecture": pref,
                            "municipality": muni, "url": url, "body": cleaned,
                        })
            del text, raw
            gc.collect()
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefecture", required=True)
    ap.add_argument("--limit", type=int, default=120, help="切り出すページ数の上限")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    args = ap.parse_args(argv)

    if not CARD.is_file():
        raise SystemExit("任務カードが無い: %s" % CARD)
    card = CARD.read_text(encoding="utf-8").rstrip()

    pages = collect(args.prefecture, args.limit)
    if not pages:
        print("対象ページが無い")
        return 0

    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    for old in BATCH_DIR.glob("bz_batch_*.txt"):
        old.unlink()

    idx = BATCH_DIR / "index.tsv"
    idx.write_text(
        "\t".join(INDEX_FIELDS) + "\n"
        + "".join("\t".join(p[f] for f in INDEX_FIELDS) + "\n" for p in pages),
        encoding="utf-8", newline="\n")

    n = 0
    for i in range(0, len(pages), args.batch_size):
        n += 1
        chunk = pages[i:i + args.batch_size]
        body = [card, "", "=" * 70,
                "# このバッチ: %d ページ。全ページについて1行以上返すこと。" % len(chunk),
                "=" * 70, ""]
        for p in chunk:
            body.append("### page_id=%s  (%s %s)" % (p["page_id"], p["prefecture"], p["municipality"]))
            body.append(p["body"])
            body.append("")
        path = BATCH_DIR / ("bz_batch_%03d.txt" % n)
        path.write_text("\n".join(body), encoding="utf-8", newline="\n")

    chars = sum(len(p["body"]) for p in pages)
    print("ページ %d 枚 / バッチ %d 本 -> %s" % (len(pages), n, BATCH_DIR))
    print("  本文の総文字数 : %s" % format(chars, ","))
    print("  概算トークン   : 約 %s (カード分は別途 1本あたり約900)"
          % format(int(chars * 1.1), ","))
    print("  1ページ平均    : %d 文字" % (chars // len(pages)))
    print()
    print("AIは各バッチを読み、%s/bz_verdict_NNN.tsv を返す" % BATCH_DIR.name)
    print("その後: python pipeline/bunkazai_ai_apply.py --prefecture %s" % args.prefecture)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
