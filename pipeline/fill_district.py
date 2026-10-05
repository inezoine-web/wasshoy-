"""政令市の行事に区名 (district) を入れる。

区役所の配下 (registry/ward_sites.tsv の scope) にある出典を持つ行事は、その区の
行事とみなす。出典が複数の区にまたがるもの・市のページだけのものは空のまま残す。
既に district があるもの (手で書いたもの) は触らない。

    python pipeline/fill_district.py --dry-run
    python pipeline/fill_district.py
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data" / "festivals.json"
WARDS = REPO_ROOT / "registry" / "ward_sites.tsv"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    with WARDS.open(encoding="utf-8", newline="") as fh:
        scopes = [(r["prefecture"], r["municipality"], r["ward"], r["scope"])
                  for r in csv.DictReader(fh, delimiter="\t") if r["scope"]]
    # discover.py --wards と同じ範囲: 区のパスの前に1段 (神戸市の /c63604/ など) を許す
    matchers = [
        (pref, city, ward, urllib.parse.urlparse(scope).netloc,
         re.compile(r"^(?:/[^/]+)?" + re.escape(urllib.parse.urlparse(scope).path)))
        for pref, city, ward, scope in scopes
    ]

    def in_ward(url: str, host: str, pat: re.Pattern) -> bool:
        p = urllib.parse.urlparse(url)
        return p.netloc == host and bool(pat.match(p.path))

    data = json.loads(DATA.read_text(encoding="utf-8"))
    filled = mixed = 0
    for f in data["festivals"]:
        if f.get("district"):
            continue
        wards = {
            ward
            for pref, city, ward, host, pat in matchers
            if f["prefecture"] == pref and f["municipality"] == city
            for s in f.get("sources", [])
            if in_ward(s.get("url") or "", host, pat)
        }
        if len(wards) == 1:
            f["district"] = wards.pop()
            filled += 1
        elif len(wards) > 1:
            mixed += 1
    print(f"district を埋めた: {filled} 件 / 複数の区にまたがり空のまま: {mixed} 件")
    if not args.dry_run and filled:
        DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
