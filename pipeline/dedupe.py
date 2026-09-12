"""S5c 重複統合: 公開データの同一市町村・同一照合キーのレコードを1件にまとめる。

マージ時の防護 (merge.py の ID・dedup_key の突合) が入る前に取り込んだ県では、
同じ名前がハッシュIDで複数入っている (三嶋大祭り×2、いわた大祭り 遠州大名行列・舞車×3)。
2026-09-12 の監査で 52組 106件。

統合の規則 (判断を要しないものだけ):
- 同じ (都道府県, 市町村, dedup_key(name)) のパイプライン由来レコードをまとめる
- 手書きのレコード (merge.is_pipeline_record が偽) が混ざる組は触らない
- 残す ID は、読み由来 (ハッシュ "-x" でない) → 出典が多い → 先頭の順
- name は残す側のもの。他の name は aliases へ。sources は URL で重複除去して合算。
  usual_schedule / categories / cultural_property などは空の方を埋める (上書きしない)

--strong を付けると、年・第N回・先頭の日付・指定ラベル・保存会を落とした強いキーでも
まとめる。ただし括弧の中身が異なる組 (神幸祭（初日）/ 還幸祭（最終日）) は別物なので、
括弧を落とす前の本文が一致するものだけを統合する。

使い方:
    python pipeline/dedupe.py --dry-run
    python pipeline/dedupe.py
    python pipeline/dedupe.py --strong --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from merge import check_consumers, is_pipeline_record  # noqa: E402
from normalize import dedup_key  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data" / "festivals.json"

_HASH_ID = re.compile(r"-x[0-9a-f]{8}$")
_DECOR = re.compile(
    r"(?:令和|平成|R|H)\s?\d+年?度?|20\d\d年?度?"
    r"|第\s?[0-9０-９一二三四五六七八九十百]+回"
    r"|^[0-9０-９]{1,2}月(?:[0-9０-９]{1,2}日)?(?:[（(][^）)]*[）)])?"
    r"|^[0-9０-９]{1,2}/[0-9０-９]{1,2}"
    r"|【[^】]*(?:指定|登録|選択|文化財)[^】]*】"
    r"|[（(][^）)]*(?:指定|登録|選択|文化財)[^）)]*[）)]"
    r"|(?:保存会|愛好会|同好会|連合会|振興会)$"
)


def strong_key(name: str) -> str:
    """飾りを落とした照合キー。括弧の中身は残す (初日/最終日 を区別するため)。"""
    k = _DECOR.sub("", name)
    return dedup_key(k)


def _score(f: dict) -> tuple[int, int]:
    return (0 if _HASH_ID.search(f["id"]) else 1, len(f.get("sources") or []))


def merge_group(group: list[dict]) -> dict:
    group = sorted(group, key=_score, reverse=True)
    head, rest = group[0], group[1:]
    aliases = list(head.get("aliases") or [])
    seen_urls = {s.get("url") for s in head.get("sources") or []}
    for f in rest:
        for a in [f["name"]] + list(f.get("aliases") or []):
            if a != head["name"] and a not in aliases:
                aliases.append(a)
        for s in f.get("sources") or []:
            if s.get("url") not in seen_urls:
                head.setdefault("sources", []).append(s)
                seen_urls.add(s.get("url"))
        for k, v in f.items():
            if k in ("id", "name", "aliases", "sources", "notes"):
                continue
            if not head.get(k) and v:
                head[k] = v
    head["aliases"] = aliases
    return head


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--strong", action="store_true", help="飾りを落としたキーでもまとめる")
    args = ap.parse_args(argv)

    doc = json.loads(DATA.read_text(encoding="utf-8"))
    rows = doc["festivals"]
    keyf = strong_key if args.strong else dedup_key

    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["prefecture"], r["municipality"], keyf(r["name"]))].append(r)

    merged_ids: set[str] = set()
    skipped = 0
    out_by_id: dict[str, dict] = {}
    n_groups = 0
    for key, group in groups.items():
        if len(group) < 2:
            continue
        if not all(is_pipeline_record(f) for f in group):
            skipped += 1
            continue
        n_groups += 1
        head = merge_group(group)
        print("  [%s] %s  <- %s" % (
            key[1], head["name"],
            " / ".join(f["name"] + ("" if f is head else "") for f in group if f is not head)))
        for f in group:
            if f is not head:
                merged_ids.add(f["id"])
        out_by_id[head["id"]] = head

    keep = [out_by_id.get(r["id"], r) for r in rows if r["id"] not in merged_ids]
    print("\n%d 組 %d 件を統合 (手書きが混ざる %d 組は触らない): %d -> %d 件"
          % (n_groups, len(merged_ids) + n_groups, skipped, len(rows), len(keep)))
    if not merged_ids:
        return 0
    if args.dry_run:
        print("--dry-run のため書き込まない")
        return 0
    # 手書きの古いレコードには sources が空のものが2件ある。触った行だけ検査する。
    problems = check_consumers(list(out_by_id.values()))
    ids = [r["id"] for r in keep]
    if len(ids) != len(set(ids)):
        problems.append("id が重複")
    if problems:
        print("消費側チェックに失敗:", problems)
        return 1
    doc["festivals"] = keep
    DATA.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    print("書いた ->", DATA)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
