"""S5c 名称の整形を公開データに当てる。

`s4_apply.clean_display_name` の規則は S4 の keep 行にしか掛からない。規則を足したときに
過去の県へ遡って適用するための工程。ID は読み由来なので変えない。元の名称は aliases に残す。

手書きのレコード (merge.is_pipeline_record が偽) は触らない。

使い方:
    python pipeline/rename.py --dry-run
    python pipeline/rename.py
続けて重複統合と整理を行う:
    python pipeline/dedupe.py && python pipeline/dedupe.py --strong && python pipeline/prune.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from merge import check_consumers, is_pipeline_record  # noqa: E402
from s4_apply import _PRESERVATION_SUFFIX, clean_display_name  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data" / "festivals.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", type=int, default=40, help="表示する変更の件数")
    args = ap.parse_args(argv)

    doc = json.loads(DATA.read_text(encoding="utf-8"))
    rows = doc["festivals"]
    changed: list[tuple[str, str, str]] = []
    touched: list[dict] = []
    for r in rows:
        if not is_pipeline_record(r):
            continue
        is_org = bool(_PRESERVATION_SUFFIX.search(r["name"]))
        new = clean_display_name(r["name"], is_org, r.get("municipality", ""))
        if new == r["name"] or len(new) < 2:
            continue
        aliases = [a for a in (r.get("aliases") or []) if a and a != new]
        if r["name"] not in aliases:
            aliases.append(r["name"])
        changed.append((r["municipality"], r["name"], new))
        r["aliases"] = aliases
        r["name"] = new
        touched.append(r)

    print("公開データ %d 件のうち、名称を整形するもの %d 件" % (len(rows), len(changed)))
    for mu, a, b in changed[: args.show]:
        print("   [%s] %s  ->  %s" % (mu, a, b))
    if len(changed) > args.show:
        print("   ... (--show N で増やせる)")
    if not changed:
        return 0
    if args.dry_run:
        print("\n--dry-run のため書き込まない")
        return 0
    problems = check_consumers(touched)
    if problems:
        print("消費側チェックに失敗:", problems[:5])
        return 1
    DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    print("\n%d 件の名称を整形して書いた -> %s" % (len(changed), DATA))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
