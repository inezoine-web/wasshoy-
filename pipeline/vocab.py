"""S0.6 県別語彙生成: 民俗文化財の名称から、県ごとの行事語彙を機械的に作る。

`extract.py` の `FESTIVAL_WORD` は既知の祭りから育てた語彙なので、既知の
祭りしか見つけられない (沖縄で81%、その他地方で92%が不一致)。ここでは
`registry/bunkazai.tsv` の指定名称から語を取り出し、**その県で祭りを指す語**
を県別に用意する。指定の枠から引いているので、語彙を先に知っている必要が無い。

名称は「<地名>の<行事名>」の形が圧倒的に多い:

    宮古島のパーントゥ  -> パーントゥ
    塩屋湾のウンガミ    -> ウンガミ
    春採のアイヌ古式舞踊 -> アイヌ古式舞踊
    多良間の豊年祭      -> 豊年祭 (既存語彙が「祭」で拾えるので covered)

そこで「最後の『の』より後ろ」を語の候補とし、地名と一般語を落とす。
**地名は必ず落とす。** 過去に「鉾田市」の鉾で全件が山車扱いになった事故がある
ので、全国の市区町村名とその語幹を除外する。

出力 `registry/vocab_regional.tsv` の `covered` 列は、その語が既存の
`FESTIVAL_WORD` で既に拾えるかどうか。`covered=no` の語が、この工程で
新しく見えるようになった分である。

使い方:
    python pipeline/vocab.py --build
    python pipeline/vocab.py --summary
    python pipeline/vocab.py --show 沖縄県
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract import FESTIVAL_WORD, SEASONAL_WORD  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = REPO_ROOT / "registry"
BUNKAZAI_TSV = REGISTRY_DIR / "bunkazai.tsv"
BUNKAZAI_LOCAL_TSV = REGISTRY_DIR / "bunkazai_local.tsv"
BUNKAZAI_AI_TSV = REGISTRY_DIR / "bunkazai_ai.tsv"
BUNKAZAI_TOBUNKEN_TSV = REGISTRY_DIR / "bunkazai_tobunken.tsv"
MUNI_TSV = REGISTRY_DIR / "municipalities.tsv"
OUT_TSV = REGISTRY_DIR / "vocab_regional.tsv"

COLUMNS = ["prefecture", "term", "covered", "source_count", "categories", "example"]

# 名称の並列を切る区切り。「盆、結願祭、種子取祭の芸能」のような列挙をほどく。
_SPLIT = re.compile(r"[、，,・／/]|及び|および|並びに")
# 「<地名>の<行事名>」の地名部分。最後の「の」で切る。
_NO_PREFIX = re.compile(r"^.{1,12}の(?=.)")
# 語の前後に付く飾り
_TRIM = re.compile(r"^[（(【「『\s]+|[）)】」』\s]+$")

# 行事名ではない一般語。これ単体では祭りを指さない。
STOPWORD = {
    "芸能", "習俗", "行事", "年中行事", "風俗", "慣習", "民俗", "技術", "儀礼",
    "用具", "製作", "製作技術", "工程", "資料", "文書", "記録", "伝承", "文化",
    "生産", "生業", "信仰", "衣食住", "人生", "儀礼用具", "生活", "民具",
    "collection", "その他", "分類", "名称", "所在地", "都道府県",
    "行事の芸能", "芸能の習俗", "習俗の芸能",
}
# 語幹が短すぎると誤爆する。「舞」「踊」「祭」単独は使わない。
MIN_LEN = 2

# 民俗文化財のうち、行事ではないもの。生業・衣食住・人生儀礼は「習俗」で
# あって祭りではないので、ここから語彙を作ると漁撈・建築・婚礼の語が混ざる。
SKIP_SUBCATEGORY = re.compile(r"生産|生業|衣食住|人生|交通|運搬|交易")


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    return [dict(zip(header, ln.split("\t"))) for ln in lines[1:] if ln.strip()]


def place_names() -> set[str]:
    """全国の市区町村名と、接尾辞を落とした語幹。

    県をまたいで除外する。「府中」は東京にも広島にもあるので、対象県の
    分だけ落としても足りない。
    """
    names: set[str] = set()
    for row in read_tsv(MUNI_TSV):
        muni = row.get("municipality", "")
        pref = row.get("prefecture", "")
        for n in (muni, pref):
            if not n:
                continue
            names.add(n)
            stem = re.sub(r"(都|道|府|県|市|区|町|村|郡)$", "", n)
            if len(stem) >= 2:
                names.add(stem)
    return names


# 名称に添えられた読み仮名。「元町みろく（もとまちみろく）」の括弧を
# 末尾だけ落とすと「元町みろく（もとまちみろく」という開き括弧の残った
# 語ができるので、括弧ごと消す。
_READING = re.compile(r"[（(][ぁ-んァ-ヶー・\s]{2,24}[）)]")
# 読み以外の注記が閉じないまま残った場合は、開き括弧で切る。
_UNBALANCED = re.compile(r"[（(][^）)]*$")


def terms_of(name: str) -> list[str]:
    """1つの指定名称から語の候補を返す。"""
    name = _READING.sub("", name)
    out: list[str] = []
    for seg in _SPLIT.split(name):
        seg = _UNBALANCED.sub("", seg)
        seg = _TRIM.sub("", seg)
        if not seg:
            continue
        # 「<地名>の<行事名>」なら後ろだけ採る。地名を含んだままの
        # 「宮古島のパーントゥ」を語彙に入れると、その1件しか拾えない。
        # **両方は採らない。** 両方入れると「安田のシヌグ」と「シヌグ」が
        # 並び、長い方は決して余分な1件も拾わないまま語彙を膨らませる。
        # 「の」で切った結果が一般語 (小浜島の芸能 -> 芸能) や1文字
        # (小浜島の盆 -> 盆) なら、その断片は語彙にしない。
        cand = _TRIM.sub("", _NO_PREFIX.sub("", seg))
        # _TRIM は末尾の閉じ括弧を落とすので、ここで初めて釣り合いが崩れる
        # (「プーリィ（豊年祭）」-> 「プーリィ（豊年祭」)。開き括弧で切り直す。
        cand = _UNBALANCED.sub("", cand).strip()
        if len(cand) >= MIN_LEN and cand not in STOPWORD:
            out.append(cand)
    return out


def source_rows() -> list[dict[str, str]]:
    """国指定 (bunkazai.tsv) と県・市町村指定 (bunkazai_local.tsv) を揃えて返す。

    列の形が違うので、県・市町村指定側は category に種別を移して合わせる。
    国指定は全国966件しかなく、語彙の種としては薄い。県・市町村指定を足せる
    県では、そちらが主な供給源になる。
    """
    rows = read_tsv(BUNKAZAI_TSV)
    # 県・市町村指定は出所が2つある。機械抽出 (bunkazai_local.tsv) は
    # キャッシュから作り直されるたび上書きされるので、AI由来 (bunkazai_ai.tsv)
    # は別ファイルに分けてある。両方読む。
    # 東文研DB (bunkazai_tobunken.tsv) は全国分を一度に持つ。市区町村指定と
    # 一部の未指定まで入るので、県によっては上の2つを合わせたより多い。
    for path in (BUNKAZAI_LOCAL_TSV, BUNKAZAI_AI_TSV, BUNKAZAI_TOBUNKEN_TSV):
        for r in read_tsv(path):
            rows.append({
                "prefecture": r.get("prefecture", ""),
                "name": r.get("name", ""),
                "category": r.get("kind", ""),
                "subcategory": "",
            })
    return rows


def build() -> list[dict[str, str]]:
    rows = source_rows()
    if not rows:
        raise SystemExit("registry/bunkazai.tsv が無い。先に bunkazai.py --build を実行する")
    places = place_names()

    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        pref, name = r.get("prefecture", ""), r.get("name", "")
        if not pref or not name:
            continue
        if SKIP_SUBCATEGORY.search(r.get("subcategory", "")):
            continue
        for term in terms_of(name):
            if term in places:
                continue
            # 地名を含む語 (「宮古島のパーントゥ」全体) は、地名を除いた
            # 語が別に立つので落としてよい。
            if any(p in term for p in places if len(p) >= 3):
                continue
            key = (pref, term)
            slot = agg.setdefault(key, {
                "prefecture": pref, "term": term,
                "covered": "yes" if FESTIVAL_WORD.search(term) else "no",
                "source_count": 0, "cats": set(), "example": name,
            })
            slot["source_count"] += 1
            if r.get("category"):
                slot["cats"].add(r["category"])

    out = []
    for slot in agg.values():
        out.append({
            "prefecture": slot["prefecture"],
            "term": slot["term"],
            "covered": slot["covered"],
            "source_count": str(slot["source_count"]),
            "categories": "|".join(sorted(slot["cats"])),
            "example": slot["example"],
        })
    out.sort(key=lambda r: (r["prefecture"], r["covered"], -int(r["source_count"]), r["term"]))
    return out


def write(rows: list[dict[str, str]]) -> None:
    body = "\t".join(COLUMNS) + "\n"
    body += "".join("\t".join(r[c].replace("\t", " ") for c in COLUMNS) + "\n" for r in rows)
    tmp = OUT_TSV.with_suffix(".tsv.tmp")
    tmp.write_text(body, encoding="utf-8", newline="\n")
    tmp.replace(OUT_TSV)


def load_terms(prefecture: str, only_new: bool = True) -> list[str]:
    """extract.py から使う。県の語彙を長い順で返す。"""
    rows = read_tsv(OUT_TSV)
    terms = [r["term"] for r in rows
             if r["prefecture"] == prefecture and (not only_new or r["covered"] == "no")]
    return sorted(set(terms), key=len, reverse=True)


def regional_pattern(prefecture: str) -> re.Pattern | None:
    terms = load_terms(prefecture)
    if not terms:
        return None
    return re.compile("|".join(re.escape(t) for t in terms))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--show", metavar="都道府県")
    args = ap.parse_args(argv)

    if args.build:
        rows = build()
        write(rows)
        new = sum(1 for r in rows if r["covered"] == "no")
        print("語 %d件 (うち既存語彙で未カバー %d件) -> %s" % (len(rows), new, OUT_TSV))

    if args.summary:
        rows = read_tsv(OUT_TSV)
        per: dict[str, list[int]] = {}
        for r in rows:
            s = per.setdefault(r["prefecture"], [0, 0])
            s[0] += 1
            if r["covered"] == "no":
                s[1] += 1
        print("%-8s %5s %5s" % ("都道府県", "語数", "新規"))
        for pref, (total, new) in sorted(per.items(), key=lambda kv: -kv[1][1]):
            print("%-8s %5d %5d" % (pref, total, new))

    if args.show:
        rows = [r for r in read_tsv(OUT_TSV)
                if r["prefecture"] == args.show and r["covered"] == "no"]
        print("%s の新規語 %d件:" % (args.show, len(rows)))
        for r in rows:
            print("  %-16s x%-3s %-12s %s" % (r["term"], r["source_count"],
                                              r["categories"][:12], r["example"]))

    if not (args.build or args.summary or args.show):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
