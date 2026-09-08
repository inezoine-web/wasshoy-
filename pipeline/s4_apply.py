"""S4-2 判定結果の取り込み: AIの返答を機械チェックしてから適用する。

AIの返答をそのまま信じない。過去に、行の連番をリセットして返す、
ローマ字化せず漢字を残す、といった逸脱が実際に起きている。
ここで次を必ず確認する。

  - 入力の id が過不足なく1回ずつ現れるか
  - verdict が既定の語彙か
  - alias_of が同一市町村の keep 行を指しているか (循環していないか)
  - reading がひらがなだけか
  - 生成した slug が `^[a-z0-9-]+$` を満たし、重複しないか

**slug は romaji.py が生成する。** AIにはかなの読みだけを返させ、
ローマ字化は委託しない (識別子は後から直すと同一性が壊れるため)。

使い方:
    python pipeline/s4_apply.py --prefecture 茨城県
    python pipeline/s4_apply.py --prefecture 茨城県 --strict   # 未回答があれば失敗
"""

from __future__ import annotations

import argparse
import collections
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from romaji import is_kana, romanize, slugify  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / "work"
BATCH_DIR = WORK_DIR / "s4"

VERDICTS = {"keep", "drop", "unsure"}
SLUG_RE = re.compile(r"^[a-z0-9-]+$")
PREFECTURE_WIDE = "(県全域)"


def load_index() -> dict[str, dict[str, str]]:
    path = BATCH_DIR / "index.tsv"
    if not path.is_file():
        raise SystemExit("先に s4_prepare.py を実行してください")
    with path.open(encoding="utf-8", newline="") as fh:
        return {r["id"]: r for r in csv.DictReader(fh, delimiter="\t")}


def load_verdicts() -> tuple[dict[str, dict[str, str]], list[str]]:
    verdicts: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    files = sorted(BATCH_DIR.glob("s4_verdict_*.tsv"))
    if not files:
        raise SystemExit(f"{BATCH_DIR} に s4_verdict_*.tsv がありません")
    for path in files:
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(
                (line for line in fh if not line.startswith("#")), delimiter="\t"
            )
            for row in reader:
                rid = (row.get("id") or "").strip()
                if not rid:
                    continue
                if rid in verdicts:
                    problems.append(f"id {rid} が {path.name} で重複している")
                    continue
                verdicts[rid] = {k: (v or "").strip() for k, v in row.items()}
    return verdicts, problems


def _place(rid: str, index: dict, verdicts: dict) -> str:
    """S4で所在が与えられていればそれを、無ければ入力の市町村を返す。"""
    given = (verdicts.get(rid) or {}).get("municipality") or ""
    return given or index[rid]["municipality"]


def _same_place(rid: str, alias: str, index: dict, verdicts: dict) -> bool:
    """別名として結んでよい組み合わせか。

    県単位レンズで拾った `(県全域)` の行は、所在を確定したうえで
    市町村側の行の別名になるのが普通の流れなので許す
    (「国指定重要無形民俗文化財「綱火」」→ つくばみらい市の「綱火」)。
    それ以外は同じ市町村でなければならない。
    """
    a, b = _place(rid, index, verdicts), _place(alias, index, verdicts)
    if a == b:
        return True
    return PREFECTURE_WIDE in (index[rid]["municipality"], index[alias]["municipality"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefecture")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)

    index = load_index()
    verdicts, problems = load_verdicts()

    missing = sorted(set(index) - set(verdicts))
    extra = sorted(set(verdicts) - set(index))
    if extra:
        problems.append(f"入力に無い id が {len(extra)} 件返ってきた: {extra[:5]}")
    if missing:
        problems.append(f"未回答が {len(missing)} 件: {missing[:5]}")

    src = WORK_DIR / "normalized.tsv"
    with src.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    if args.prefecture:
        rows = [r for r in rows if r["prefecture"] == args.prefecture]
    rows.sort(key=lambda r: (r["municipality"], r["name"]))
    by_id = {f"{i:05d}": r for i, r in enumerate(rows, start=1)}

    # --- 検証 ---
    kept_ids = {
        rid for rid, v in verdicts.items() if v.get("verdict") == "keep"
    }
    for rid, v in sorted(verdicts.items()):
        verdict = v.get("verdict", "")
        if verdict not in VERDICTS:
            problems.append(f"id {rid}: verdict が不正 ({verdict!r})")
        alias = v.get("alias_of", "")
        if alias:
            if alias not in index:
                problems.append(f"id {rid}: alias_of {alias} が存在しない")
            elif alias == rid:
                problems.append(f"id {rid}: alias_of が自分自身")
            elif not _same_place(rid, alias, index, verdicts):
                problems.append(
                    f"id {rid}: alias_of {alias} の市町村が違う "
                    f"({index[rid]['municipality']} vs {index[alias]['municipality']})"
                )
            elif alias not in kept_ids:
                problems.append(f"id {rid}: alias_of {alias} が keep されていない")
        reading = v.get("reading", "")
        if reading and not is_kana(reading):
            problems.append(f"id {rid}: reading がかなではない ({reading!r})")

    # alias の循環・多段を検出
    alias_map = {r: v["alias_of"] for r, v in verdicts.items() if v.get("alias_of")}
    for rid in alias_map:
        seen, cur = {rid}, alias_map.get(rid)
        while cur:
            if cur in seen:
                problems.append(f"id {rid}: alias_of が循環している")
                break
            seen.add(cur)
            cur = alias_map.get(cur)

    if problems:
        print("=== 検証で問題が見つかった ===")
        for p in problems[:30]:
            print("  -", p)
        if len(problems) > 30:
            print(f"  ... 他 {len(problems) - 30} 件")
        if args.strict:
            return 1
        print("  (--strict でなければ、問題のある行は unsure として扱って続行する)\n")

    # --- 適用 ---
    out_rows = []
    slugs: dict[str, str] = {}
    stats = collections.Counter()
    for rid, base in sorted(by_id.items()):
        v = verdicts.get(rid, {})
        verdict = v.get("verdict", "")
        if verdict not in VERDICTS:
            verdict = "unsure"
        stats[verdict] += 1

        row = dict(base)
        # 所在の確定 (県全域 の行だけ上書きを許す)
        if base["municipality"] == PREFECTURE_WIDE and v.get("municipality"):
            row["municipality"] = v["municipality"]
            stats["municipality_resolved"] += 1

        # slug は必ずこちらで生成する。AIのローマ字は使わない。
        reading = v.get("reading", "")
        if row["slug"] == "PENDING" and reading and is_kana(reading):
            romaji = romanize(reading)
            if romaji:
                pref_r, muni_r = _romaji_for(row["prefecture"], row["municipality"])
                if pref_r and muni_r:
                    candidate = f"{pref_r}-{muni_r}-{slugify(romaji)}"
                    if SLUG_RE.match(candidate) and candidate not in slugs:
                        row["slug"] = candidate
                        slugs[candidate] = rid
                        stats["slug_resolved"] += 1
                    elif candidate in slugs:
                        problems.append(
                            f"id {rid}: slug {candidate} が id {slugs[candidate]} と衝突"
                        )

        row["s4_verdict"] = verdict
        row["s4_reason"] = v.get("reason", "")
        row["s4_alias_of"] = v.get("alias_of", "")
        out_rows.append(row)

    out = WORK_DIR / "judged.tsv"
    fields = list(out_rows[0].keys()) if out_rows else ["prefecture"]
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.DictWriter(fh, delimiter="\t", lineterminator="\n", fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    print(f"judged.tsv: {len(out_rows)} 件")
    print(f"  keep {stats['keep']} / drop {stats['drop']} / unsure {stats['unsure']}")
    print(f"  所在を確定できた   : {stats['municipality_resolved']}")
    print(f"  slug を確定できた  : {stats['slug_resolved']}")
    print(f"  別名として統合     : {sum(1 for r in out_rows if r['s4_alias_of'])}")
    still = sum(1 for r in out_rows if r["slug"] == "PENDING")
    print(f"  slug PENDING 残り  : {still}")
    return 0


_ROMAJI_CACHE: dict[tuple[str, str], tuple[str, str]] = {}


def _romaji_for(prefecture: str, municipality: str) -> tuple[str, str]:
    if not _ROMAJI_CACHE:
        path = REPO_ROOT / "registry" / "municipalities.tsv"
        if path.is_file():
            with path.open(encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh, delimiter="\t"):
                    _ROMAJI_CACHE[(r["prefecture"], r["municipality"])] = (
                        r["pref_romaji"],
                        r["muni_romaji"],
                    )
    return _ROMAJI_CACHE.get((prefecture, municipality), ("", ""))


if __name__ == "__main__":
    sys.exit(main())
