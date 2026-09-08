"""S6 評価: パイプラインの出力を凍結ベンチマークと突き合わせる。

ここと merge.py だけが既存データを読んでよい (benchmarks/README.md のリーク防止方針)。

出すのは「回帰の下限に対する再現率」であって、網羅率ではない。
ベンチマーク自身が不完全であることは benchmarks/*/README.md に書いてある。

使い方:
    python pipeline/evaluate.py --benchmark benchmarks/ibaraki-2026-09-06
    python pipeline/evaluate.py --benchmark benchmarks/ibaraki-2026-09-06 --show-misses
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from normalize import dedup_key  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / "work"


def load_output(judged: bool = False) -> list[dict[str, str]]:
    """S3の出力、または S4 判定後の出力を読む。

    judged=True では `keep` の行だけを対象にする。別名として統合された行は
    残す — 同じ行事を別名で拾っていること自体が発見の成果であり、
    ベンチマークとの照合ではどちらの表記で当たっても構わないため。
    """
    if judged:
        path = WORK_DIR / "judged.tsv"
        if not path.is_file():
            raise SystemExit("先に s4_apply.py を実行してください")
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))
        return [r for r in rows if r.get("s4_verdict") == "keep"]
    path = WORK_DIR / "normalized.tsv"
    if not path.is_file():
        raise SystemExit("先に normalize.py を実行してください")
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def index_output(rows: list[dict[str, str]]) -> tuple[dict, dict, set[str]]:
    """(市町村+名前) と 名前のみ の索引、および全URLを返す。"""
    by_muni_name: dict[tuple[str, str], dict] = {}
    by_name: dict[str, list[dict]] = {}
    urls: set[str] = set()
    for r in rows:
        key = dedup_key(r["name"])
        by_muni_name[(r["municipality"], key)] = r
        by_name.setdefault(key, []).append(r)
        for alias in filter(None, r.get("aliases", "").split("|")):
            akey = dedup_key(alias)
            by_muni_name.setdefault((r["municipality"], akey), r)
            by_name.setdefault(akey, []).append(r)
        for u in filter(None, r.get("source_urls", "").split("|")):
            urls.add(u)
    return by_muni_name, by_name, urls


def match(
    name: str,
    municipality: str,
    source_url: str,
    by_muni_name: dict,
    by_name: dict,
    urls: set[str],
) -> tuple[bool, str]:
    key = dedup_key(name)
    if (municipality, key) in by_muni_name:
        return True, "名称+市町村"
    if key in by_name:
        return True, f"名称のみ (市町村不一致: {by_name[key][0]['municipality']})"
    if source_url and source_url in urls:
        return True, "根拠URLのみ"
    return False, ""


def loose_match(name: str, municipality: str, rows: list[dict[str, str]]) -> str:
    """参考値用のゆるい照合。同一市町村内で4文字以上の共通部分を持つか。

    厳密な照合では取りこぼすが実際には発見できている例がある。
    守谷市の「守谷祇園祭」に対しパイプラインは「八坂神社祇園祭」を
    見つけており、同じ行事の別名だが名称キーは一致しない。
    **この値は再現率として使わない。** 別名で拾えているのか、
    まったく届いていないのかを切り分けるための診断値である。
    """
    key = dedup_key(name)
    for r in rows:
        if r["municipality"] != municipality:
            continue
        other = dedup_key(r["name"])
        if key in other or other in key:
            return r["name"]
        for size in range(len(key), 3, -1):
            for i in range(len(key) - size + 1):
                if key[i : i + size] in other:
                    return r["name"]
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmark", required=True)
    ap.add_argument("--show-misses", action="store_true")
    ap.add_argument("--sample-new", type=int, default=30)
    ap.add_argument("--judged", action="store_true",
                    help="S4判定後 (judged.tsv の keep 行) を評価する")
    args = ap.parse_args(argv)

    bench = Path(args.benchmark)
    snapshot = json.loads((bench / "snapshot.json").read_text(encoding="utf-8"))
    with (bench / "gold.tsv").open(encoding="utf-8", newline="") as fh:
        gold = list(csv.DictReader(fh, delimiter="\t"))

    rows = load_output(args.judged)
    prefecture = snapshot["prefecture"]
    rows = [r for r in rows if r["prefecture"] == prefecture]
    by_muni_name, by_name, urls = index_output(rows)

    print(f"=== ベンチマーク: {snapshot['benchmark_id']} ({prefecture}) ===")
    print(f"パイプライン出力: {len(rows)} 件 / {len({r['municipality'] for r in rows})} 市町村\n")

    # ---- gold
    print("--- gold.tsv (必ず取れるべき集合) ---")
    gold_hits: list[tuple[dict, str]] = []
    gold_misses: list[dict] = []
    for g in gold:
        ok, how = match(g["name"], g["municipality"], g["source_url"], by_muni_name, by_name, urls)
        (gold_hits if ok else gold_misses).append((g, how) if ok else g)
    for tier in ("core", "notable"):
        total = sum(1 for g in gold if g["tier"] == tier)
        hit = sum(1 for g, _ in gold_hits if g["tier"] == tier)
        print(f"  {tier:8s}: {hit}/{total}  ({hit * 100 // max(1, total)}%)")
    total_hit = len(gold_hits)
    print(f"  {'合計':8s}: {total_hit}/{len(gold)}  ({total_hit * 100 // max(1, len(gold))}%)")
    if gold_misses:
        print("  取れなかったもの:")
        for g in gold_misses:
            flag = "" if g["in_snapshot"] == "yes" else "  [旧手法でも未登録]"
            print(f"    - {g['name']} ({g['municipality'] or '所在不明'}){flag}")

    # ---- snapshot
    print("\n--- snapshot.json (旧手法の全出力) ---")
    snap_hits, snap_misses = [], []
    for f in snapshot["festivals"]:
        url = (f.get("sources") or [{}])[0].get("url", "")
        ok, _ = match(f["name"], f["municipality"], url, by_muni_name, by_name, urls)
        (snap_hits if ok else snap_misses).append(f)
    n = len(snapshot["festivals"])
    print(f"  再現: {len(snap_hits)}/{n}  ({len(snap_hits) * 100 // max(1, n)}%)")

    loose = [f for f in snap_misses if loose_match(f["name"], f["municipality"], rows)]
    print(f"  参考値: 厳密には外れたが同一市町村内に名称の一部が一致する候補がある: {len(loose)}")
    print("    (別名で拾えている可能性。再現率としては数えない)")

    per_muni: dict[str, list[int]] = {}
    for f in snapshot["festivals"]:
        per_muni.setdefault(f["municipality"], [0, 0])[1] += 1
    for f in snap_hits:
        per_muni[f["municipality"]][0] += 1
    zero = [m for m, (h, _t) in per_muni.items() if h == 0]
    print(f"  1件も再現できなかった市町村: {len(zero)}" + (f"  {'、'.join(zero)}" if zero else ""))

    covered = {r["municipality"] for r in rows}
    snap_munis = set(per_muni)
    unreached = sorted(snap_munis - covered)
    print(f"  パイプラインが1件も候補を出せなかった市町村: {len(unreached)}"
          + (f"  {'、'.join(unreached)}" if unreached else ""))

    if args.show_misses and snap_misses:
        print("\n  再現できなかったもの (先頭40件):")
        for f in snap_misses[:40]:
            print(f"    - {f['municipality']:10s} {f['name']}")

    # ---- 新規
    snap_keys = {(f["municipality"], dedup_key(f["name"])) for f in snapshot["festivals"]}
    new_rows = [r for r in rows if (r["municipality"], dedup_key(r["name"])) not in snap_keys]
    print(f"\n--- snapshot に無い候補: {len(new_rows)} 件 ---")
    print("  (これは「新規発見」と「ノイズ」の混在。機械的には区別できない。")
    print("   下のサンプルを目視して内訳を報告すること)")
    for r in new_rows[: args.sample_new]:
        cats = r["categories"]
        print(f"    {r['municipality']:10s} {r['name'][:28]:30s} {cats[:28]:30s} {r['source_urls'].split('|')[0][:60]}")

    # ---- 健全性
    print("\n--- 健全性チェック ---")
    no_url = [r for r in rows if not r["source_urls"]]
    print(f"  根拠URLなし     : {len(no_url)}  (0であるべき)")
    pending = [r for r in rows if r["slug"] == "PENDING"]
    print(f"  slug PENDING    : {len(pending)}  ({len(pending) * 100 // max(1, len(rows))}% がS4のAI判定を要する)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
