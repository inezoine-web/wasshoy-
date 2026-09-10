"""S5b 整理: 公開済みデータに、後から分かった名称の規則を当てて落とす。

S4 の判定ファイル (work/s4/) は県ごとに上書きされる一時物なので、
名称の規則を1つ足すたびに全県のS4を回し直すことはできない。
そこで `extract.py` の `NOT_A_NAME` (名前ではない形) を
`data/festivals.json` に直接当て、該当するレコードを外す。

**規則を足す前に実データで全件確認すること。** 過去に、正当な名前を
巻き込みかけた例:

    「ひらがな1字＋漢字で始まる」 → お吉祭り・お楽しみ縁日 など19件が正当
    「先頭が助詞」                → やまゆり祭り・もみじまつり など大半が正当

`merge.py` と同じ消費側チェックを通してから書く。

使い方:
    python pipeline/prune.py --dry-run
    python pipeline/prune.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract import NOT_A_NAME  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data" / "festivals.json"

REQUIRED = ("name", "status", "confidence", "prefecture", "municipality", "categories")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    doc = json.loads(DATA.read_text(encoding="utf-8"))
    rows = doc["festivals"] if isinstance(doc, dict) and "festivals" in doc else doc

    keep, drop = [], []
    for r in rows:
        (drop if NOT_A_NAME.search(r.get("name", "")) else keep).append(r)

    print("公開データ %d 件のうち、名前の形をしていないもの %d 件:" % (len(rows), len(drop)))
    for r in drop:
        print("   [%s] %s" % (r.get("municipality", ""), r.get("name", "")[:50]))

    if not drop:
        return 0
    if args.dry_run:
        print("\n--dry-run のため書き込まない")
        return 0

    # 消費側チェック (merge.py と同じ観点)
    bad = [r for r in keep if not all(r.get(k) for k in REQUIRED)]
    ids = [r.get("id") for r in keep]
    if bad or len(ids) != len(set(ids)):
        print("消費側チェックに失敗: 必須列のnull %d / id重複 %d" % (len(bad), len(ids) - len(set(ids))))
        return 1

    if isinstance(doc, dict) and "festivals" in doc:
        doc["festivals"] = keep
    else:
        doc = keep
    DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    print("\n%d 件を外して %d 件を書いた -> %s" % (len(drop), len(keep), DATA))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
