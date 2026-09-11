"""S4b 読みの補完: keep なのに読みが無い行だけを、軽い任務でAIに聞き直す。

S4 の判定エージェント (haiku) は、バッチによっては reading 列を丸ごと
空で返すことがある (東京の19本中6本がほぼ全滅)。読みが無い行は ID が
ハッシュ採番になり、ID は同一性そのものなので後から直しにくい。
そこで keep かつ reading 空の行だけを抜き出し、名称と市町村だけを渡して
ひらがなの読みを返してもらう。1本120行で約4万トークン。

戻りは機械検査する: ひらがな以外 (数字・記号) が残る読みは採用しない。
「第34回…」のような数字入りの名称は読み下されないことが多く、
そのままハッシュ採番に残る。

使い方:
    python pipeline/s4_readings.py --prepare       # work/s4/reading_batch_NNN.tsv
    #   AIが reading_verdict_NNN.tsv (id / reading) を同じ順序・件数で返す
    python pipeline/s4_readings.py --apply         # s4_verdict_*.tsv の reading 列へ書き戻す
    python pipeline/s4_apply.py --prefecture 東京都   # その後で判定を適用し直す
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
S4_DIR = REPO_ROOT / "work" / "s4"

CARD = """# 読みバッチ
# 各行の name (祭り・行事の名称) の読みを、**ひらがなだけ**で reading 列に書き、
# reading_verdict_<n>.tsv に同じ順序・同じ件数で返す。列: id / reading
# 名称全体の読みを書く。一部だけの読みは書かない。ローマ字にしない。
# 数字は読み下す (第3回 -> だいさんかい)。長音は「ー」のまま。
# 自信がなければ reading を空にする。推測で埋めない。
id\tmunicipality\tname
"""

_ID = re.compile(r"\d{5}")
_KANA = re.compile(r"^[ぁ-ゖー]+$")
_STRIP = re.compile(r"[\s　・「」『』（）()〜～!！?？、。]")


def batch_rows() -> dict[str, tuple[str, str]]:
    """id -> (市町村, 名称) を S4 のバッチファイルから引く。"""
    info: dict[str, tuple[str, str]] = {}
    for bf in sorted(glob.glob(str(S4_DIR / "s4_batch_*.tsv"))):
        lines = [l for l in Path(bf).read_text(encoding="utf-8").splitlines()
                 if l and not l.startswith("#")]
        hdr = lines[0].split("\t")
        for l in lines[1:]:
            r = dict(zip(hdr, l.split("\t")))
            info[r["id"]] = (r["municipality"], r["name"])
    return info


def verdict_files() -> list[Path]:
    return [Path(p) for p in sorted(glob.glob(str(S4_DIR / "s4_verdict_*.tsv")))]


def missing_readings() -> list[tuple[str, str, str]]:
    info = batch_rows()
    todo = []
    for vf in verdict_files():
        for l in vf.read_text(encoding="utf-8-sig").splitlines()[1:]:
            c = l.split("\t")
            if (len(c) >= 2 and c[1].strip() == "keep" and c[0] in info
                    and (len(c) < 6 or not c[5].strip())):
                todo.append((c[0],) + info[c[0]])
    return todo


def prepare(size: int) -> int:
    todo = missing_readings()
    for old in glob.glob(str(S4_DIR / "reading_batch_*.tsv")):
        Path(old).unlink()
    for i in range(0, len(todo), size):
        n = i // size + 1
        body = CARD + "".join("\t".join(t) + "\n" for t in todo[i:i + size])
        (S4_DIR / f"reading_batch_{n:03d}.tsv").write_text(body, encoding="utf-8", newline="\n")
    print(f"keep で読みが無い行 {len(todo)} -> reading_batch_*.tsv {-(-len(todo) // size) if todo else 0} 本")
    return len(todo)


def read_readings() -> tuple[dict[str, str], list[str]]:
    readings: dict[str, str] = {}
    rejected: list[str] = []
    for f in sorted(glob.glob(str(S4_DIR / "reading_verdict_*.tsv"))):
        for l in Path(f).read_text(encoding="utf-8-sig").splitlines():
            c = l.split("\t")
            if not _ID.fullmatch(c[0].strip()) or len(c) < 2:
                continue
            r = _STRIP.sub("", c[1])
            if not r:
                continue
            if _KANA.match(r):
                readings[c[0].strip()] = r
            else:
                rejected.append(r)
    return readings, rejected


def apply() -> int:
    readings, rejected = read_readings()
    filled = 0
    for vf in verdict_files():
        out = []
        for l in vf.read_text(encoding="utf-8-sig").splitlines():
            c = l.split("\t")
            if (len(c) >= 2 and c[1].strip() == "keep" and c[0] in readings
                    and (len(c) < 6 or not c[5].strip())):
                c = (c + [""] * 6)[:6]
                c[5] = readings[c[0]]
                filled += 1
            out.append("\t".join(c))
        vf.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    print(f"読み {len(readings)} 件 (かな以外で不採用 {len(rejected)}: {rejected[:3]}…)")
    print(f"s4_verdict_*.tsv に書き戻した: {filled}")
    print("次: python pipeline/s4_apply.py --prefecture <県>")
    return filled


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch-size", type=int, default=120)
    args = ap.parse_args(argv)
    if args.prepare:
        prepare(args.batch_size)
    if args.apply:
        apply()
    if not (args.prepare or args.apply):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
