"""S0.5d 東文研DB: 無形文化遺産総合データベースから全国の無形民俗文化財を引く。

東京文化財研究所「無形文化遺産総合データベース」
(https://mukeinet.tobunken.go.jp/index.php?gid=10027) は、国・都道府県・
市区町村が指定・選択した無形の文化財と、一部の県では未指定の行事まで、
全国分を1つの表で持っている。2026-09-11 時点で 9,991 行 (うち無形民俗 9,350)。

自治体サイトを1つずつ掘る S0.5b/c より網羅的で、AIトークンを使わない。
既に処理した5県で測ったところ、festivals.json は指定済みの無形民俗の
31〜64% しか捕まえていなかった。ここで取れる指定済みの層を種と語彙に
使い、クロールは未指定の層を探す役に回す。

取り方は2経路で、どちらも1リクエスト:
  - 印刷用一覧 (get.php mode=getdata type=print): 行ID・都道府県・市区町村
    が列で付く。XMLHttpRequest ヘッダが要る
  - CSV (csv.php type=csv): 解説・種別・指定情報・期日・保存団体。cp932。
    所在住所に都道府県名が無い行が 86% あるので、県は一覧側から取る
両方とも市区町村昇順で同じ並びなので、行番号で突き合わせて名称一致を検査する。

既存の festivals.json / benchmarks は参照しない。

使い方:
    python pipeline/tobunken.py --fetch            # 取得してキャッシュへ
    python pipeline/tobunken.py --build            # registry/bunkazai_tobunken.tsv
    python pipeline/tobunken.py --seed --prefecture 東京都   # candidates.tsv に種を足す
    python pipeline/tobunken.py --stats --prefecture 東京都
"""

from __future__ import annotations

import argparse
import collections
import csv
import html
import io
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from net import CACHE_DIR, USER_AGENT, build_ssl_context  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = REPO_ROOT / "registry"
WORK_DIR = REPO_ROOT / "work"
OUT_TSV = REGISTRY_DIR / "bunkazai_tobunken.tsv"
CACHE = CACHE_DIR / "tobunken"

DB_PAGE = "https://mukeinet.tobunken.go.jp/index.php?gid=10027"
API = "https://mukeinet.tobunken.go.jp/modules/cultural_map/"
EID = "55862"

# bunkazai_local.tsv と同じ6列を先頭に置く。vocab.py / s4_apply.py は
# ヘッダで引くので、後ろに列が増えても壊れない。
COLUMNS = [
    "prefecture", "municipality", "designation", "kind", "name", "source_url",
    "municipality_raw", "tobunken_id", "level", "kana", "kind1", "kind2", "address", "date_note",
    "organization", "ref_url",
]

# 祭り・行事ではない中分類。語彙にも種にも使わない。
NOT_FESTIVAL_KIND2 = {
    "生産・生業", "社会生活（民俗知識）", "衣食住", "その他（民俗技術）",
    "その他（工芸技術）", "手漉和紙", "能楽", "雅楽", "その他（古典芸能）",
}

_TAG = re.compile(r"<[^>]+>")
_ROW = re.compile(
    r'<tr data-id=(m\d+)>(.*?)</tr>', re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_LIST_COLUMNS = ("name", "prefecture", "municipality", "detail", "date")


# --------------------------------------------------------------- 取得


def _get(url: str, xhr: bool, timeout: int = 300) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, context=build_ssl_context(), timeout=timeout) as r:
        return r.read()


def _query(params: dict[str, str]) -> str:
    return "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in params.items())


def fetch(force: bool = False) -> tuple[Path, Path]:
    """印刷用一覧とCSVを1回ずつ取ってキャッシュに置く。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    list_path = CACHE / "print_list.html"
    csv_path = CACHE / "all.csv"
    empty = json.dumps({"region": [], "cat": [], "kind_l": [], "date": []})
    if force or not list_path.exists():
        url = API + "get.php?" + _query({
            "eid": EID, "mode": "getdata", "type": "print", "sort": "asc",
            "field": "city", "conditions": empty, "key": "",
        })
        outer = json.loads(_get(url, xhr=True).decode("utf-8"))
        inner = json.loads(outer["data"])
        list_path.write_text(inner["list"], encoding="utf-8")
        print(f"一覧: {len(inner['list']):,} 文字 -> {list_path}")
    if force or not csv_path.exists():
        url = API + "csv.php?" + _query({
            "eid": EID, "module": "cultural_map", "mode": "getdata", "type": "csv",
            "region": "[]", "cat": "[]", "kind_l": "[]", "date": "[]", "key": "",
            "field": "city", "sort": "asc", "userStatus": "false",
        })
        body = _get(url, xhr=False)
        csv_path.write_bytes(body)
        print(f"CSV: {len(body):,} bytes -> {csv_path}")
    return list_path, csv_path


# --------------------------------------------------------------- 解析


def parse_list(text: str) -> list[dict[str, str]]:
    rows = []
    for m in _ROW.finditer(text):
        # 印刷用の表は <td> に見出しを持たない。列順は 名称/都道府県/市区町村/町・字/期日
        cells = [re.sub(r"\s+", " ", html.unescape(_TAG.sub("", v))).strip()
                 for v in _CELL.findall(m.group(2))]
        row = dict(zip(_LIST_COLUMNS, cells + [""] * (len(_LIST_COLUMNS) - len(cells))))
        row["tobunken_id"] = m.group(1)
        rows.append(row)
    return rows


def parse_csv(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("cp932", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def level_of(designation: str) -> str:
    s = designation
    if "国指定" in s or "重要無形民俗" in s:
        return "国指定"
    if "選択" in s or "記録" in s:
        return "国選択"
    if re.search(r"(都|道|府|県)指定", s):
        return "都道府県指定"
    if re.search(r"(市|区|町|村)指定", s):
        return "市区町村指定"
    if "未指定" in s:
        return "未指定"
    return "不明"


def _norm(s: str) -> str:
    return re.sub(r"[\s　]", "", s)


# extract.py と同じ印。所在が市区町村単位で確定しない行に付ける。
PREFECTURE_WIDE = "(県全域)"

# 北海道だけ地域名で来る (道南 / 道央・道北 / 道東)
_HOKKAIDO = {"道南", "道央・道北", "道東", "道北", "道央"}

# 自治体名の異体字。CSV側 (cp932) と一覧側で字体が違うことがある。
# 篠山市は 2019 年に丹波篠山市へ改称、白岡町は 2012 年に市制。
_MUNI_ALIAS = {
    "貝塚市": "貝塚市",  # 塚 (U+FA10) -> 塚
    "篠山市": "丹波篠山市",
}


def load_registry() -> tuple[set[tuple[str, str]], set[str]]:
    path = REGISTRY_DIR / "municipalities.tsv"
    pairs: set[tuple[str, str]] = set()
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            pairs.add((r["prefecture"], r["municipality"]))
    return pairs, {p for p, _ in pairs}


def resolve_place(pref: str, muni: str, registry: tuple[set[tuple[str, str]], set[str]]) -> tuple[str, str]:
    """一覧の (都道府県, 市区町村) を台帳の表記に揃える。

    確定できない所在 (「全域」「会津地方」「千代田区,江戸川区」等) は
    (県全域) にする。推測で1つの自治体に寄せない。
    """
    pairs, prefs = registry
    if pref in _HOKKAIDO:
        pref = "北海道"
    muni = unicodedata.normalize("NFKC", muni).strip()
    muni = _MUNI_ALIAS.get(muni, muni)
    if (pref, muni) in pairs:
        return pref, muni
    return pref, PREFECTURE_WIDE


# 一覧の名称は「小中野囃子(こなかのばやし)」と読みを括弧で持つ (43%)。
# CSV の名称ふりがな列とは別に埋まっている行があるので、両方から取る。
# 読みの中に括弧が入れ子になる (六斎念仏（鹿野）(ろくさいねんぶつ（しかの）))
# ので正規表現では切らず、CSV の名称を先頭一致で外して残りを読みとする。
# CSV は cp932 なので「蒅」「衹」「髹」が ? に化ける。名称は UTF-8 の一覧側を
# 正とし、照合では ? を任意の1字として扱う。
def split_name(listed: str, csv_name: str) -> tuple[str, str] | None:
    a, b = _norm(listed), _norm(csv_name)
    pat = "".join("." if ch == "?" else re.escape(ch) for ch in b)
    m = re.match(pat, a)
    if not m:
        return None
    name, rest = a[: m.end()], a[m.end():]
    m2 = re.fullmatch(r"[（(](.*)[)）]", rest)
    kana = re.sub(r"[（()）・]", "", m2.group(1)) if m2 else ""
    return name, kana


def build() -> list[dict[str, str]]:
    list_path, csv_path = fetch()
    lst = parse_list(list_path.read_text(encoding="utf-8"))
    tbl = parse_csv(csv_path.read_bytes())
    if len(lst) != len(tbl):
        raise SystemExit(f"一覧 {len(lst)} 行と CSV {len(tbl)} 行で数が合わない。"
                         "--fetch --force で両方を取り直す")
    # 行番号で突き合わせる。名称が違う行が1つでもあれば並びがずれている。
    mismatch = [(i, a["name"], b["名称"]) for i, (a, b) in enumerate(zip(lst, tbl))
                if split_name(a["name"], b["名称"]) is None]
    if mismatch:
        for i, a, b in mismatch[:5]:
            print(f"  行 {i}: 一覧「{a}」 CSV「{b}」")
        raise SystemExit(f"一覧とCSVの名称が {len(mismatch)} 行で一致しない")

    registry = load_registry()
    out = []
    skipped = {"not_folk": 0, "not_festival": 0, "no_pref": 0}
    unresolved: collections.Counter[str] = collections.Counter()
    for a, b in zip(lst, tbl):
        if b["相当する国分類"] != "無形民俗文化財":
            skipped["not_folk"] += 1
            continue
        kind2 = b["中分類（文化庁種別2）"]
        if kind2 in NOT_FESTIVAL_KIND2:
            skipped["not_festival"] += 1
            continue
        if not a["prefecture"]:
            skipped["no_pref"] += 1
            continue
        desig = re.sub(r"\s+", " ", b["指定情報（表示用）"]).strip()
        name, kana_listed = split_name(a["name"], b["名称"])
        pref, muni = resolve_place(a["prefecture"], a["municipality"], registry)
        if muni == PREFECTURE_WIDE:
            unresolved[f"{a['prefecture']} {a['municipality']}"] += 1
        out.append({
            "prefecture": pref,
            "municipality": muni,
            "municipality_raw": a["municipality"],
            "designation": desig,
            "kind": "無形民俗文化財",
            "name": name,
            "source_url": DB_PAGE,
            "tobunken_id": a["tobunken_id"],
            "level": level_of(desig),
            "kana": _norm(b["名称ふりがな"]) or kana_listed,
            "kind1": b["大分類（文化庁種別1）"],
            "kind2": kind2,
            "address": re.sub(r"\s+", " ", b["所在住所"]).strip(),
            "date_note": re.sub(r"\s+", " ", b["公開情報（表示用）"]).strip(),
            "organization": re.sub(r"\s+", " ", b["保存団体"]).strip(),
            "ref_url": b["参考URL"].strip(),
        })
    print(f"全 {len(lst)} 行 -> 採用 {len(out)} 行 "
          f"(無形民俗でない {skipped['not_folk']} / 祭りでない種別 {skipped['not_festival']} / "
          f"県なし {skipped['no_pref']})")
    n_wide = sum(unresolved.values())
    print(f"  市区町村を台帳で確定できず {PREFECTURE_WIDE} にした: {n_wide} 行 "
          f"({', '.join(f'{k} {v}' for k, v in unresolved.most_common(6))} …)")
    return out


def write(rows: list[dict[str, str]]) -> None:
    body = "\t".join(COLUMNS) + "\n"
    body += "".join(
        # CSV 由来の値は改行を含む (名称「土佐和紙\n（…）」)。1行1件を守る
        "\t".join(re.sub(r"[\t\r\n]+", " ", r.get(c, "")) for c in COLUMNS) + "\n" for r in rows
    )
    tmp = OUT_TSV.with_suffix(".tsv.tmp")
    tmp.write_text(body, encoding="utf-8", newline="\n")
    tmp.replace(OUT_TSV)
    print(f"-> {OUT_TSV} ({len(rows)} 行)")


def read_rows() -> list[dict[str, str]]:
    if not OUT_TSV.exists():
        raise SystemExit("先に tobunken.py --build を実行してください")
    with OUT_TSV.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


# --------------------------------------------------------------- 種


_VENUE = re.compile(r"(?:に|で)([^、。にで]{2,30}?(?:神社|神宮|八幡宮|寺|院|堂|境内|会館|公園|広場|公民館|集会所))で公開")


def seed(prefecture: str) -> int:
    """東文研の行を S2 の候補形式で work/candidates.tsv に足す。

    origin=tobunken で区別する。読み仮名があれば「名称（かな）」の形で
    文脈に入れ、S3 の reading_for がそのまま拾えるようにする。
    """
    path = WORK_DIR / "candidates.tsv"
    if not path.exists():
        raise SystemExit("先に extract.py を実行してください (candidates.tsv が無い)")
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        fields = reader.fieldnames or []
        existing = list(reader)
    existing = [r for r in existing if r.get("origin") != "tobunken" or r["prefecture"] != prefecture]

    added = []
    for r in read_rows():
        if r["prefecture"] != prefecture:
            continue
        kana = r["kana"]
        context = f"{r['name']}（{kana}）" if kana else r["name"]
        context += f" {r['designation']}"
        if r["date_note"]:
            context += f" {r['date_note']}"
        m = _VENUE.search(r["date_note"])
        added.append({
            "prefecture": r["prefecture"],
            "municipality": r["municipality"],
            "name": r["name"],
            "name_raw": r["name"],
            "origin": "tobunken",
            "reason": f"tobunken:{r['level']}:{r['tobunken_id']}",
            "date_text": r["date_note"],
            "venue": m.group(1) if m else "",
            "source_url": r["source_url"],
            "page_title": "無形文化遺産総合データベース (東京文化財研究所)",
            "context": context[:300],
        })
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.DictWriter(fh, delimiter="\t", lineterminator="\n", fieldnames=fields,
                           extrasaction="ignore")
        w.writeheader()
        for r in existing + added:
            w.writerow(r)
    print(f"{prefecture}: 東文研の種 {len(added)} 行を足した (candidates.tsv 計 {len(existing) + len(added)} 行)")
    return len(added)


def stats(prefecture: str | None) -> None:
    import collections
    rows = read_rows()
    if prefecture:
        rows = [r for r in rows if r["prefecture"] == prefecture]
    levels = collections.Counter(r["level"] for r in rows)
    munis = collections.Counter(r["municipality"] for r in rows)
    print(f"{prefecture or '全国'}: {len(rows)} 行 / {len(munis)} 市区町村 / 読み仮名あり "
          f"{sum(1 for r in rows if r['kana'])}")
    for k, v in levels.most_common():
        print(f"  {k:8s} {v}")
    print("  自治体上位:", ", ".join(f"{m} {n}" for m, n in munis.most_common(8)))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--force", action="store_true", help="キャッシュを無視して取り直す")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--prefecture")
    args = ap.parse_args(argv)

    if args.fetch:
        fetch(force=args.force)
    if args.build:
        write(build())
    if args.seed:
        if not args.prefecture:
            raise SystemExit("--seed には --prefecture が要る")
        seed(args.prefecture)
    if args.stats:
        stats(args.prefecture)
    if not (args.fetch or args.build or args.seed or args.stats):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
