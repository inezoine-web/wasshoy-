"""S0.5c-2 AI判定の取り込み: 返ってきたTSVを機械検査してから台帳に足す。

AIの回答をそのまま信じない。`bunkazai_local.py` が持っている門番
(分類語そのもの・住所・散文の断片・長すぎる名称) をここでも通す。過去に
文脈から両方を補って614件のゴミを作った経緯があるので、検査は入口に置く。

**識別子は委託していない。** AIが返すのは page_id と名称だけで、
ローマ字化・slug・IDの生成はこの後の機械工程が行う。

使い方:
    python pipeline/bunkazai_ai_apply.py --prefecture 茨城県 --dry-run
    python pipeline/bunkazai_ai_apply.py --prefecture 茨城県
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bunkazai_local as B  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BATCH_DIR = REPO_ROOT / "work" / "bunkazai_ai"

VALID_DESIG = {"国指定", "国登録", "県指定", "都指定", "道指定", "府指定",
               "市指定", "町指定", "村指定"}
NONE_ROW = "NONE"
# 種別は必ず民俗のものであること。無形文化財(工芸技術)や有形は通さない。
VALID_KIND = re.compile(r"(?<!有形)(?:無形民俗文化財|民俗文化財|無形民俗|民俗芸能|風俗慣習|民俗行事)")

# `bunkazai_local.PROSE` はここでは使えない。あれは平文から正規表現で切り出した
# 断片が文の途中かを見るためのもので、「〜し で終わる」を文の継続とみなす。
# 行事名は「神田ばやし」「猿島ばやし」「石岡ばやし」のように **し で終わるものが
# 多く**、実際に最初の実行で正しい5件を全部弾いた。AIは既に「これは名前だ」と
# 判断しているので、ここで見るのは名前の体を成しているかだけにする。
AI_PROSE = re.compile(
    # 助詞・接続で始まる = 前の文の続きを写している
    r"^(?:に|は|が|を|で|と|も|や|へ|から|より|など|また|なお|この|その|"
    r"である|であり|として|における|に関する|について)|"
    # サイトの定型文
    r"https?|www\.|本文へ|お問い合わせ|問合せ|ダウンロード|ページの先頭|"
    r"[。、！？]"
)


def read_index() -> dict[str, dict[str, str]]:
    path = BATCH_DIR / "index.tsv"
    if not path.exists():
        raise SystemExit("先に bunkazai_ai_prepare.py を実行してください")
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    return {r["page_id"]: r for r in
            (dict(zip(header, ln.split("\t"))) for ln in lines[1:] if ln.strip())}


def read_verdicts() -> list[tuple[str, list[str]]]:
    files = sorted(BATCH_DIR.glob("bz_verdict_*.tsv"))
    if not files:
        raise SystemExit("%s に bz_verdict_*.tsv がありません" % BATCH_DIR)
    out = []
    for f in files:
        for ln in f.read_text(encoding="utf-8").splitlines():
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            out.append((f.name, ln.split("\t")))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefecture", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    index = read_index()
    rows = read_verdicts()

    accepted: list[dict[str, str]] = []
    problems: list[str] = []
    answered: set[str] = set()
    dropped = {"designation": 0, "kind": 0, "name": 0, "unknown_id": 0, "shape": 0}

    for fname, cells in rows:
        if len(cells) < 4:
            dropped["shape"] += 1
            problems.append("%s: 列が足りない: %r" % (fname, cells[:4]))
            continue
        pid, desig, kind, name = (c.strip() for c in cells[:4])
        rec = index.get(pid)
        if rec is None:
            dropped["unknown_id"] += 1
            problems.append("%s: 入力に無い page_id: %s" % (fname, pid))
            continue
        answered.add(pid)
        if desig == NONE_ROW or kind == NONE_ROW or name == NONE_ROW:
            continue
        if desig not in VALID_DESIG:
            dropped["designation"] += 1
            problems.append("%s: 指定区分が不正: %r (%s)" % (fname, desig, name[:20]))
            continue
        if not VALID_KIND.search(kind):
            dropped["kind"] += 1
            continue
        clean = B.clean_name(name)
        if not (2 <= len(clean) <= B.MAX_NAME):
            dropped["name"] += 1
            continue
        if B.NOT_NAME.search(clean) or AI_PROSE.search(clean) or B._ADDRESS_CELL.search(clean):
            dropped["name"] += 1
            problems.append("%s: 名称が門番に掛かった: %r" % (fname, clean))
            continue
        accepted.append({
            "prefecture": rec["prefecture"],
            "municipality": rec["municipality"],
            "designation": desig,
            "kind": kind,
            "name": clean,
            "source_url": rec["url"],
        })

    missing = [p for p in index if p not in answered]

    print("返答 %d 行 / 入力 %d ページ" % (len(rows), len(index)))
    print("  採用            : %d" % len(accepted))
    print("  未回答のページ  : %d %s" % (len(missing), missing[:5]))
    for k, v in dropped.items():
        if v:
            print("  除外(%s) : %d" % (k, v))
    if problems:
        print()
        print("--- 検査に掛かったもの (先頭20) ---")
        for p in problems[:20]:
            print("  " + p)

    if args.dry_run:
        print()
        print("--- 採用分 (先頭30) ---")
        for r in accepted[:30]:
            print("  [%s %s] %-8s %s" % (r["designation"], r["kind"][:8],
                                         r["municipality"], r["name"]))
        return 0

    existing = [r for r in B.read_tsv_rows()] if hasattr(B, "read_tsv_rows") else []
    if not existing and B.OUT_TSV.exists():
        lines = B.OUT_TSV.read_text(encoding="utf-8").splitlines()
        header = lines[0].split("\t")
        existing = [dict(zip(header, ln.split("\t"))) for ln in lines[1:] if ln.strip()]

    seen = {(r.get("prefecture", ""), r.get("name", ""), r.get("kind", "")) for r in existing}
    added = 0
    for r in accepted:
        key = (r["prefecture"], r["name"], r["kind"])
        if key in seen:
            continue
        seen.add(key)
        existing.append(r)
        added += 1

    existing.sort(key=lambda r: (r.get("prefecture", ""), r.get("designation", ""),
                                 r.get("name", "")))
    body = B.TAB.join(B.COLUMNS) + B.NL
    body += "".join(B.TAB.join(r.get(c, "").replace(B.TAB, " ") for c in B.COLUMNS) + B.NL
                    for r in existing)
    tmp = B.OUT_TSV.with_suffix(".tsv.tmp")
    tmp.write_text(body, encoding="utf-8", newline=B.NL)
    tmp.replace(B.OUT_TSV)
    print()
    print("台帳に %d 件追加 (計 %d 件) -> %s" % (added, len(existing), B.OUT_TSV))
    print("次: python pipeline/vocab.py --build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
