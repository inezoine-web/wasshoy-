"""S4-1 判定バッチの作成: AIに渡す最小限のTSVを切り出す。

S4 はパイプラインで唯一AIが要る工程。コード化できない判断だけを渡す。

  1. 対象/対象外  — 行事か、断片・団体名・一般語か (AGENTS.md §2)
  2. 別名統合    — 同一市町村内で同じ行事を指しているか (AGENTS.md §8)
  3. 所在の帰属  — 県単位レンズで拾った `(県全域)` に市町村を与える
  4. 読み       — slug用のかな。**ローマ字化はさせない**

読みをかなで返させ、ローマ字化と slug 生成は romaji.py が行う。
IDは同一性を決める値であり、過去にローマ字化を委託して漢字が残ったまま
返ってきた事故がある。判断は委託してよいが、識別子は委託しない。

コストを抑えるため、渡すのは判断に要る列だけにする。本文・URL全体・
context は渡さない (ホスト名だけ残して出所の種類がわかるようにする)。

使い方:
    python pipeline/s4_prepare.py --prefecture 茨城県 --batch-size 120
"""

from __future__ import annotations

import argparse
import csv
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / "work"
BATCH_DIR = WORK_DIR / "s4"

FIELDS = ["id", "municipality", "name", "categories", "venue", "schedule", "source"]

INSTRUCTIONS = """\
# S4 判定バッチ
#
# 各行について次を判定し、s4_verdict_<n>.tsv に**同じ順序・同じ件数**で返す。
# 列: id / verdict / reason / alias_of / municipality / reading
#
# verdict:
#   keep   … 地域の祭り・祭礼・伝統行事・花火・市民祭・季節の行事として記録する価値がある
#   drop   … 行事ではない (団体名・会場名・一般語・文の断片・グッズ・交通情報・他県の事例)
#   unsure … 判断できない。理由を reason に書く
# reason: 短い語 (festival / shrine / temple / folk / fireworks / citizen / seasonal /
#         market / fragment / organization / venue_only / generic / out_of_area / commercial)
# alias_of: 同一市町村内の別行に対する別名なら、その id。なければ空
# municipality: 入力が (県全域) の行だけ、確認できる場合に市町村名を書く。不明なら空
# reading: 名称全体の読みを**ひらがなだけ**で。自信がなければ空。
#          ローマ字にしない。一部だけの読みを書かない。
#
# 推測で埋めない。分からないものは unsure か空欄にする。
"""


def compact_source(urls: str) -> str:
    """出所はホスト名だけ渡す。URL全文はトークンの無駄。"""
    first = (urls or "").split("|")[0]
    return urllib.parse.urlparse(first).netloc if first else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefecture")
    ap.add_argument("--batch-size", type=int, default=120)
    ap.add_argument("--only-pending-municipality", action="store_true",
                    help="所在未確定 (県全域) の行だけを対象にする")
    args = ap.parse_args(argv)

    src = WORK_DIR / "normalized.tsv"
    if not src.is_file():
        raise SystemExit("先に normalize.py を実行してください")
    with src.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    if args.prefecture:
        rows = [r for r in rows if r["prefecture"] == args.prefecture]
    if args.only_pending_municipality:
        rows = [r for r in rows if r["municipality"] == "(県全域)"]

    # 別名判定ができるよう、同一市町村の行は同じバッチに固める
    rows.sort(key=lambda r: (r["municipality"], r["name"]))

    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    for old in BATCH_DIR.glob("s4_batch_*.tsv"):
        old.unlink()

    index_path = BATCH_DIR / "index.tsv"
    with index_path.open("w", encoding="utf-8", newline="\n") as idx:
        iw = csv.writer(idx, delimiter="\t", lineterminator="\n")
        iw.writerow(["id", "prefecture", "municipality", "name"])

        batch_no = 0
        written = 0
        for start in range(0, len(rows), args.batch_size):
            batch = rows[start : start + args.batch_size]
            batch_no += 1
            path = BATCH_DIR / f"s4_batch_{batch_no:03d}.tsv"
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(INSTRUCTIONS)
                w = csv.writer(fh, delimiter="\t", lineterminator="\n")
                w.writerow(FIELDS)
                for i, r in enumerate(batch, start=start + 1):
                    rid = f"{i:05d}"
                    w.writerow([
                        rid,
                        r["municipality"],
                        r["name"],
                        r["categories"],
                        r["venue"],
                        r["usual_schedule"] or r["date_note"][:40],
                        compact_source(r["source_urls"]),
                    ])
                    iw.writerow([rid, r["prefecture"], r["municipality"], r["name"]])
                    written += 1

    print(f"s4/ に {batch_no} バッチ、計 {written} 行を書き出した")
    print(f"  1バッチ {args.batch_size} 行、索引: {index_path}")
    print("  AIは s4/s4_batch_NNN.tsv を読み、s4/s4_verdict_NNN.tsv を同じ順序・件数で返す")
    return 0


if __name__ == "__main__":
    sys.exit(main())
