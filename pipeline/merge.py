"""S5 マージ: 判定済みレコードを data/festivals.json へ書き込む。

`evaluate.py` とこのスクリプトだけが既存データを読んでよい
(benchmarks/README.md のリーク防止方針)。

推測で埋めない。確認できていない項目は null のまま残す
(`summary` `history` `cultural_property` `location` `last_verified` 等)。

**データを足したら消費側を確認する。** 過去に `summary: null` を212件追加して
GitHub Pages のビルドが落ちている。`scripts/build_site.py` がガードなしで
参照するのは name / status / confidence / prefecture / municipality /
categories の6つで、ここは必ず非nullで埋め、書き込み前に検査する。

使い方:
    python pipeline/merge.py --prefecture 茨城県 --replace
    python pipeline/merge.py --prefecture 茨城県 --replace --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / "work"
DATA = REPO_ROOT / "data" / "festivals.json"

# build_site.py がガードなしで参照するフィールド。null にしてはいけない。
REQUIRED = ("name", "status", "confidence", "prefecture", "municipality", "categories")

EMPTY_SCALE = {
    "attendance": None,
    "floats": None,
    "mikoshi": None,
    "days": None,
    "participating_districts": None,
    "other": [],
}


def build_record(row: dict[str, str], accessed: str) -> dict:
    """判定済みの1行を schema.md のレコードへ写す。

    確認できたものだけを入れる。会場・日程・カテゴリは S2/S3 が情報源から
    取ったものだけで、推測は含まない。
    """
    urls = [u for u in row["source_urls"].split("|") if u]
    sources = [
        {
            "url": url,
            "title": None,
            "publisher": None,
            "accessed": accessed,
            # ページ本文は取得済みだが、行事の詳細まで裏取りしていない。
            # 「名称がこのページに載っている」以上のことは主張しない。
            "supports": ["name_mentioned"],
        }
        for url in urls
    ]

    notes = []
    if row.get("date_note"):
        notes.append(row["date_note"])
    notes.append(
        "2026-09-08 のパイプライン (S1-S4) による収集。"
        "自治体・観光協会・県教育委員会のページ本文から名称を抽出し、"
        "対象judgment・別名統合・所在の帰属をAIが行った。"
        "個別ページによる裏取りは未実施のため status は candidate。"
    )
    if row.get("s4_reason") == "organization":
        notes.append(
            "情報源では保存会・連合会の名称として現れた。行事名に整形してあるが、"
            "行事そのものの名称としての確認は未実施。"
        )
    if row["slug"].rsplit("-", 1)[-1].startswith("x"):
        notes.append("名称の読みを確認できなかったため、IDはハッシュで採番している。")

    categories = [c for c in row["categories"].split("|") if c] or ["unclassified"]
    aliases = [a for a in row.get("aliases", "").split("|") if a and a != row["name"]]

    return {
        "id": row["slug"],
        "name": row["name"],
        "aliases": aliases,
        "status": "candidate",
        "confidence": "low",
        "prefecture": row["prefecture"],
        "municipality": row["municipality"],
        "district": None,
        "venue": row.get("venue") or None,
        "location": None,
        "usual_schedule": row.get("usual_schedule") or None,
        "event_dates": [],
        "categories": categories,
        "features": [],
        "history": None,
        "cultural_property": None,
        "scale": dict(EMPTY_SCALE),
        "summary": None,
        "sources": sources,
        "notes": " / ".join(notes),
        "last_verified": None,
    }


def check_consumers(records: list[dict]) -> list[str]:
    """消費側が落ちない形になっているかを書き込み前に確認する。"""
    problems = []
    for r in records:
        for field in REQUIRED:
            value = r.get(field)
            if value is None or value == "" or value == []:
                problems.append(f"{r.get('id')}: {field} が空 ({value!r})")
        if not isinstance(r.get("categories"), list):
            problems.append(f"{r.get('id')}: categories がリストでない")
        if not isinstance(r.get("sources"), list) or not r["sources"]:
            problems.append(f"{r.get('id')}: sources が空")
    ids = [r["id"] for r in records]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        problems.append(f"id が重複: {sorted(dup)[:5]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefecture", required=True)
    ap.add_argument("--add", action="store_true",
                    help="既存レコードを残し、IDが重ならないものだけ足す。"
                         "一部のページだけ判定し直したときに使う")
    ap.add_argument("--replace", action="store_true",
                    help="対象都道府県の既存レコードを入れ替える")
    ap.add_argument("--accessed", default="2026-09-08")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    src = WORK_DIR / "judged.tsv"
    if not src.is_file():
        raise SystemExit("先に s4_apply.py を実行してください")
    with src.open(encoding="utf-8", newline="") as fh:
        judged = list(csv.DictReader(fh, delimiter="\t"))
    # slug が PENDING の行は入れない。IDが決まっていないものを入れると、
    # 全部が同じ "PENDING" というIDで衝突する。所在が (県全域) のまま
    # 残ったものがこれにあたる (茨城で8件)。捨てるのではなく、
    # 所在が決まってから入る。
    pending = [
        r for r in judged
        if r["s4_verdict"] == "keep" and not r["s4_alias_of"]
        and r["prefecture"] == args.prefecture and r["slug"] == "PENDING"
    ]
    new_rows = [
        r for r in judged
        if r["s4_verdict"] == "keep"
        and not r["s4_alias_of"]
        and r["prefecture"] == args.prefecture
        and r["slug"]
        and r["slug"] != "PENDING"
    ]
    if pending:
        print(f"ID未確定のため見送る: {len(pending)} 件 "
              f"({', '.join(r['name'][:14] for r in pending[:5])}…)")
    if not new_rows:
        raise SystemExit(f"{args.prefecture} の判定済みレコードがありません")

    data = json.loads(DATA.read_text(encoding="utf-8"))
    existing = data["festivals"]
    removed = [f for f in existing if f["prefecture"] == args.prefecture]
    kept = [f for f in existing if f["prefecture"] != args.prefecture]

    if args.add:
        # 追記: その県の既存行も残す。IDが既にあるものは足さない。
        # 名称 (別名を含む) が同じ県の既存行と一致するものも足さない。
        # 東京には手で精査した22件 (要約つき) があり、クロール由来の
        # 「三社祭」を別IDで並べると同じ祭りが2枚になる。
        from normalize import dedup_key  # 既存データは読まない側のモジュール
        have = {f["id"] for f in existing}
        have_names = {
            (f["prefecture"], dedup_key(n))
            for f in existing for n in [f["name"], *f.get("aliases", [])]
        }
        before = len(new_rows)
        new_rows = [r for r in new_rows if r["slug"] not in have]
        by_id = before - len(new_rows)
        new_rows = [r for r in new_rows
                    if (r["prefecture"], dedup_key(r["name"])) not in have_names]
        kept = existing
        removed = []
        print(f"--add: 既存 {len(existing)} 件を残し、ID重複 {by_id} 件・名称一致 "
              f"{before - by_id - len(new_rows)} 件を除いて {len(new_rows)} 件を足す")
    if removed and not args.replace:
        raise SystemExit(
            f"{args.prefecture} に既存 {len(removed)} 件がある。"
            "入れ替えるなら --replace を付ける"
        )

    records = [build_record(r, args.accessed) for r in new_rows]

    problems = check_consumers(records)
    # 他県のレコードとID衝突していないか
    other_ids = {f["id"] for f in kept}
    clash = sorted({r["id"] for r in records} & other_ids)
    if clash:
        problems.append(f"他県のレコードとID衝突: {clash[:5]}")

    print(f"既存 {len(existing)} 件 → {args.prefecture} の {len(removed)} 件を外し、"
          f"{len(records)} 件を追加")
    print(f"  他県は {len(kept)} 件のまま")
    if problems:
        print("\n=== 書き込み前の検査で問題 ===")
        for p in problems[:20]:
            print("  -", p)
        return 1
    print("  消費側チェック: 問題なし "
          f"({'/'.join(REQUIRED)} はすべて非null、id重複なし)")

    if args.dry_run:
        print("\n--dry-run のため書き込まない")
        return 0

    data["festivals"] = kept + records
    data["updated_at"] = args.accessed
    DATA.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\ndata/festivals.json: 計 {len(data['festivals'])} 件を書き込んだ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
