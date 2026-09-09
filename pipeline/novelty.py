"""S6b 新規性評価: 「知らなかった祭りをどれだけ見つけたか」を測る。

`evaluate.py` の recall は `benchmarks/ibaraki-2026-09-06` に対する一致率だが、
そのベンチマークは**旧AI手法の出力そのもの**である。そこへの recall を上げる
チューニングは、定義上「すでに見つけたものを、また見つける」性能を上げている。
新しく発見した祭りは0点、むしろ誤検出として減点される。当初目的
(知らなかった祭り・マイナーな祭りを見つける) と指標が逆を向いていた。

そこで別の指標を置く:

    新規性 = 出力のうち、data/festivals.json にも Wikipedia にも無いものの数

recall を捨てるわけではない。recall は**壊れていないことの確認**として残し、
新規性を**前に進んでいることの確認**として使う。両方を並べて見る。

`evaluate.py` / `merge.py` と同じく、これは採点側なので既存データを読んでよい。
S1〜S4 は読んではならない (`check_leak.py` が検査する)。

使い方:
    python pipeline/novelty.py --input work/judged.tsv
    python pipeline/novelty.py --input work/normalized.tsv --prefecture 茨城県
    python pipeline/novelty.py --input work/judged.tsv --list 30
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gazetteer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FESTIVALS_JSON = REPO_ROOT / "data" / "festivals.json"

# 照合の前に落とす飾り。「第38回」「令和7年度」「2026年」など、
# 同じ祭りが年ごとに別名で出るのを1つに寄せる。
_LEADERS = re.compile(
    r"(第\s*[0-9０-９一二三四五六七八九十百]+\s*回|"
    r"令和\s*[0-9０-９]+\s*年度?|平成\s*[0-9０-９]+\s*年度?|"
    r"[0-9０-９]{4}\s*年度?)"
)
_DROP = re.compile(r"[\s　・･。、,，.．\-‐-―–—~〜～!！?？'\"“”'']")
# 「まつり」「祭り」「祭」は同じものを指す。末尾だけ寄せる。
_TAIL = re.compile(r"(まつり|マツリ|祭り|祭)$")


def norm(name: str) -> str:
    """照合用の正規化キー。表記ゆれを潰すが、別の祭りを混ぜない程度に留める。"""
    s = unicodedata.normalize("NFKC", name)
    s = _LEADERS.sub("", s)
    s = _DROP.sub("", s)
    s = _TAIL.sub("祭", s)
    return s.lower()


def read_tsv(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    return [dict(zip(header, ln.split("\t"))) for ln in lines[1:] if ln.strip()]


def existing_names() -> dict[str, set[str]]:
    """data/festivals.json の名称と別名を、都道府県ごとの正規化キーで返す。"""
    out: dict[str, set[str]] = {}
    if not FESTIVALS_JSON.exists():
        return out
    doc = json.loads(FESTIVALS_JSON.read_text(encoding="utf-8"))
    records = doc.get("festivals", doc) if isinstance(doc, dict) else doc
    if isinstance(records, dict):
        records = list(records.values())
    for rec in records:
        if not isinstance(rec, dict):
            continue
        pref = rec.get("prefecture") or ""
        slot = out.setdefault(pref, set())
        for key in ("name", "aliases"):
            v = rec.get(key)
            if isinstance(v, str):
                slot.add(norm(v))
            elif isinstance(v, list):
                slot.update(norm(x) for x in v if isinstance(x, str))
    return out


def wikipedia_names() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for pref in gazetteer.PREFECTURES:
        names = gazetteer.load(pref)
        if names:
            out[pref] = {norm(n) for n in names}
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, help="採点対象のTSV (name / prefecture 列を持つもの)")
    ap.add_argument("--prefecture", help="この県だけを見る")
    ap.add_argument("--list", type=int, default=0, metavar="N", help="新規のものをN件表示")
    args = ap.parse_args(argv)

    path = Path(args.input)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        print("入力が無い: %s" % path, file=sys.stderr)
        return 2

    rows = read_tsv(path)
    if args.prefecture:
        rows = [r for r in rows if r.get("prefecture") == args.prefecture]
    # 判定済みTSVなら drop を除く。未判定なら全行が対象。
    kept = [r for r in rows if r.get("s4_verdict", "keep") != "drop"]

    known_local = existing_names()
    known_wiki = wikipedia_names()
    if not known_wiki:
        print("警告: registry/gazetteer_wikipedia.tsv が無い。"
              "先に gazetteer.py --build を実行する", file=sys.stderr)

    stats: dict[str, list[int]] = {}
    novel: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for r in kept:
        pref = r.get("prefecture", "")
        key = norm(r.get("name", ""))
        if not key or (pref, key) in seen:
            continue
        seen.add((pref, key))
        s = stats.setdefault(pref, [0, 0, 0, 0])  # 対象 / 既存データ既知 / Wikipedia既知 / 新規
        s[0] += 1
        in_local = key in known_local.get(pref, set())
        in_wiki = key in known_wiki.get(pref, set())
        if in_local:
            s[1] += 1
        if in_wiki:
            s[2] += 1
        if not in_local and not in_wiki:
            s[3] += 1
            novel.append(r)

    total = [sum(v[i] for v in stats.values()) for i in range(4)]
    print("入力: %s  (%d行 -> 判定後 %d行 -> 名称で重複を除いて %d件)"
          % (path.name, len(rows), len(kept), total[0]))
    print()
    print("%-8s %6s %8s %10s %8s %7s" % ("都道府県", "対象", "既存data", "Wikipedia", "新規", "新規率"))
    for pref, v in sorted(stats.items(), key=lambda kv: -kv[1][3]):
        rate = (100.0 * v[3] / v[0]) if v[0] else 0.0
        print("%-8s %6d %8d %10d %8d %6.0f%%" % (pref, v[0], v[1], v[2], v[3], rate))
    if len(stats) > 1:
        rate = (100.0 * total[3] / total[0]) if total[0] else 0.0
        print("%-8s %6d %8d %10d %8d %6.0f%%" % ("合計", total[0], total[1], total[2], total[3], rate))

    if args.list:
        print()
        print("新規のもの (先頭%d件):" % min(args.list, len(novel)))
        for r in novel[: args.list]:
            print("  %-6s %-28s %s" % (r.get("prefecture", ""), r.get("name", "")[:28],
                                       (r.get("source_urls", "") or "").split("|")[0][:70]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
