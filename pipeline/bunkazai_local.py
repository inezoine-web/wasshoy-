"""S0.5b 県・市町村指定の民俗文化財を、取得済みページから拾う。

`bunkazai.py` が取るのは**国指定だけ**で全国966件しかない。語彙の種としては
効くが、県指定・市町村指定はその数十倍あり、そこにこそマイナーな行事がいる。

国指定と違って県・市町村指定を全国一括で持つ、使える口が無い:

  - 文化遺産オンライン (bunka.nii.ac.jp) は135,397件を持つが、検索結果が
    JS描画で、フォームが宣言する POST /heritages/relatedsearch は 405 を返す。
    robots.txt は全許可だが Crawl-Delay:3 なので、仮に引けても全件で113時間。
  - 自治体の「指定文化財一覧」PDFは形が悪い。実測では2段組が交互に出る
    (蒲郡市)、フォントに ToUnicode が無く日本語0文字になる (半田市) など。

一方、自治体・教育委員会のHTMLページには「県指定 無形民俗文化財 ○○」の形で
素直に載っている。**クロール済みのキャッシュに既にある**ので、ネットに出ずに
拾える。取れるのはクロールした県だけだが、語彙は県ごとに作るものなので、
その県を回す前に一度クロールすればよい。

既存データ (data/festivals.json / benchmarks) は読まない。

使い方:
    python pipeline/bunkazai_local.py --build
    python pipeline/bunkazai_local.py --build --prefecture 茨城県
    python pipeline/bunkazai_local.py --build --resume   # 落ちた続きから
"""

from __future__ import annotations

import argparse
import gc
import html
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from net import decode_html  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = REPO_ROOT / "registry"
CACHE_DIR = Path(os.environ.get("WASSHOY_CACHE_DIR") or (REPO_ROOT / "cache"))
OUT_TSV = REGISTRY_DIR / "bunkazai_local.tsv"
STATE = REPO_ROOT / "work" / "bunkazai_local.state"

COLUMNS = ["prefecture", "municipality", "designation", "kind", "name", "source_url"]

TAB = "\t"
NL = "\n"

# 指定区分。「国」も拾っておく (bunkazai.py と突き合わせて漏れを見るため)。
DESIGNATION = r"(?:国|県|都|道|府|市|町|村|市町村)\s*指定"
# 無形の民俗文化財だけ。有形は「もの」であって行事ではない。
# 「無形文化財」は入れない。能・狂言・工芸技術が対象で、実際に回したところ
# 「粟野春慶塗」のような工芸品が混ざった。ここで欲しいのは行事の名前。
KIND = r"(?:重要)?無形民俗文化財|無形民俗|民俗芸能"

# 平文に「県指定無形民俗文化財 富田のささら」の形で出るもの。
FLAT = re.compile(
    r"(" + DESIGNATION + r")\s*[・：:]?\s*(" + KIND + r")\s*[「『（(]?\s*"
    r"([^\s「」『』（）()\[\]。、,，:：|｜/／\t]{2,30})"
)
# 逆順「富田のささら（県指定無形民俗文化財）」
FLAT_REV = re.compile(
    r"([^\s「」『』（）()\[\]。、,，:：|｜/／\t]{2,30})\s*[（(]\s*"
    r"(" + DESIGNATION + r")\s*(" + KIND + r")\s*[）)]"
)

TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
TD = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
DESIG_ONLY = re.compile(r"^" + DESIGNATION + r"$")
KIND_ONLY = re.compile(r"^(?:" + KIND + r")$")

# 名称として不適当なもの
NOT_NAME = re.compile(
    r"^(?:名称|種別|区分|指定|所在地|所有者|員数|時代|年月日|備考|一覧|合計|"
    r"その他|ページ|関連|詳細|リンク|お問い合わせ)$|"
    r"[。、！？]|^\d+$|^[〇○◯×]+$|"
    r"(?:について|ください|します|ました|しています)$|"
    # 分類語そのもの。名称の位置に見出しが入り込んだ場合に出る。
    r"^(?:[国県都道府市町村]?指定)?(?:重要)?(?:有形|無形)?(?:民俗)?文化財[】）)]?$|"
    r"^(?:史跡|名勝|天然記念物|建造物|絵画|彫刻|工芸品|書跡|典籍|古文書|"
    r"考古資料|歴史資料|登録有形文化財)$"
)

# 平文から拾った断片が散文かどうか。表と違って平文には切れ目が無いので、
# 「県指定無形民俗文化財に指定されており」のような続き文をそのまま拾う。
# 実際に出た誤りをそのまま条件にしている。
PROSE = re.compile(
    # 助詞・活用で始まる = 直前の文の続き
    r"^(?:に|は|が|を|で|と|も|の|や|へ|から|より|など|また|なお|この|その|"
    r"である|であり|として|における|に関する|について)|"
    # 用言・接続・助詞で終わる = 文の途中で切れている
    r"(?:し|して|され|されて|される|されており|など|ため|こと|もの|とき|"
    r"ます|です|ある|あり|いる|おり|ない|れる|られ|ており|ながら|つつ|"
    r"が|は|を|に|で|と|も|へ|の|や)$|"
    # サイトの定型文・URL・数量表現
    r"https?|www\.|本文|ページ|お問い合わせ|問合せ|閲覧|ダウンロード|"
    r"^第?\d+(?:件|行事|号|回|年|月|日)|"
    # 記事本文にしか出ない言い回し
    r"以上前|当地|地域|現在|今から|例祭日$|文化財の$"
)
# 名称の最大長。これを超えるものは説明文が食い込んでいる。
MAX_NAME = 20
# 「所在地」等の見出し語が名称に食い込んだものを切る
CUT = re.compile(r"\s*(?:所在地|所有者|員数|指定年月日|時代|備考|管理者).*$")

# 復号は重い。生バイトの段階で「民俗」も「無形」も含まないページを落とす。
# キャッシュのHTMLは UTF-8 と CP932 が混在するので両方のバイト列で見る。
_PREFILTER = (
    "民俗".encode("utf-8"), "無形".encode("utf-8"),
    "民俗".encode("cp932"), "無形".encode("cp932"),
)
# 極端に大きいページは文化財一覧ではないうえ、正規表現でメモリを食う。
MAX_BODY = 600_000


def text_of(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG.sub(" ", fragment))).strip()


def clean_name(raw: str) -> str:
    n = CUT.sub("", raw).strip(" 　・:：|｜/／")
    # 一覧の行頭に付く記号・番号を落とす (「※浅川のささら」「1 舘獅子」)
    n = re.sub(r"^[※＊*・□■○●◇◆▲△\-–—\d０-９.．)）]+\s*", "", n)
    n = re.sub(r"^(?:" + DESIGNATION + r")\s*", "", n)
    n = re.sub(r"^(?:" + KIND + r")\s*", "", n)
    return n.strip()


def rows_from_table(page_text: str) -> list[tuple[str, str, str]]:
    """表から (指定区分, 種別, 名称) を拾う。

    自治体の文化財一覧は「区分 / 種別 / 名称 / 所在地」の表が多い。
    セルのどれかが指定区分、どれかが種別なら、残りで一番名称らしいものを採る。
    """
    out = []
    for row_match in TR.finditer(page_text):
        cells = [text_of(c) for c in TD.findall(row_match.group(1))]
        if len(cells) < 2:
            continue
        desig = next((c for c in cells if DESIG_ONLY.match(c)), "")
        kind = next((c for c in cells if KIND_ONLY.match(c)), "")
        if not (desig and kind):
            continue
        for c in cells:
            if c in (desig, kind) or not c:
                continue
            name = clean_name(c)
            if 2 <= len(name) <= 30 and not NOT_NAME.search(name):
                out.append((desig, kind, name))
                break
    return out


def ok_flat_name(name: str) -> bool:
    """平文から拾った断片を名称として認めるか。表より厳しくする。"""
    if not (2 <= len(name) <= MAX_NAME):
        return False
    if NOT_NAME.search(name) or PROSE.search(name):
        return False
    # 日本語を含まない断片 (URL の残骸など) は名称ではない
    return bool(re.search(r"[぀-ヿ一-龥]", name))


def rows_from_flat(page_text: str) -> list[tuple[str, str, str]]:
    flat = text_of(page_text)
    out = []
    for m in FLAT.finditer(flat):
        name = clean_name(m.group(3))
        if ok_flat_name(name):
            out.append((m.group(1), m.group(2), name))
    for m in FLAT_REV.finditer(flat):
        name = clean_name(m.group(1))
        if ok_flat_name(name):
            out.append((m.group(2), m.group(3), name))
    return out


def host_map() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for tsv, cols in ((REGISTRY_DIR / "sites.tsv", ("official_url", "tourism_url")),
                      (REGISTRY_DIR / "pref_sites.tsv", ("official_url", "education_url"))):
        if not tsv.exists():
            continue
        lines = tsv.read_text(encoding="utf-8").splitlines()
        header = lines[0].split(TAB)
        for ln in lines[1:]:
            if not ln.strip():
                continue
            r = dict(zip(header, ln.split(TAB)))
            who = (r.get("prefecture", ""), r.get("municipality", "(県全域)"))
            for c in cols:
                u = r.get(c) or ""
                if u:
                    out[urllib.parse.urlparse(u).netloc] = who
    return out


def rows_of_page(raw: bytes, content_type: str, src: str,
                 who: tuple[str, str]) -> list[dict[str, str]]:
    if not any(k in raw for k in _PREFILTER):
        return []
    try:
        text, _ = decode_html(raw, content_type)
    except Exception:
        return []
    out = []
    for desig, kind, name in rows_from_table(text) + rows_from_flat(text):
        out.append({
            "prefecture": who[0],
            "municipality": who[1],
            "designation": re.sub(r"\s+", "", desig),
            "kind": re.sub(r"\s+", "", kind),
            "name": name,
            "source_url": src,
        })
    return out


def append_rows(rows: list[dict[str, str]]) -> None:
    """1ホスト分を追記する。落ちても進捗が消えないように毎回書く。"""
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    fresh = not OUT_TSV.exists()
    with OUT_TSV.open("a", encoding="utf-8", newline=NL) as fh:
        if fresh:
            fh.write(TAB.join(COLUMNS) + NL)
        for r in rows:
            fh.write(TAB.join(r[c].replace(TAB, " ") for c in COLUMNS) + NL)


def done_hosts() -> set[str]:
    if not STATE.exists():
        return set()
    return {ln.strip() for ln in STATE.read_text(encoding="utf-8").splitlines() if ln.strip()}


def mark_done(host: str) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.open("a", encoding="utf-8", newline=NL) as fh:
        fh.write(host + NL)


def dedupe_output() -> list[dict[str, str]]:
    """追記でできた重複を落として書き直す。"""
    if not OUT_TSV.exists():
        return []
    lines = OUT_TSV.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    header = lines[0].split(TAB)
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for ln in lines[1:]:
        if not ln.strip():
            continue
        r = dict(zip(header, ln.split(TAB)))
        key = (r.get("prefecture", ""), r.get("name", ""), r.get("kind", ""))
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
    rows.sort(key=lambda r: (r.get("prefecture", ""), r.get("designation", ""), r.get("name", "")))
    body = TAB.join(COLUMNS) + NL
    body += "".join(TAB.join(r.get(c, "").replace(TAB, " ") for c in COLUMNS) + NL for r in rows)
    tmp = OUT_TSV.with_suffix(".tsv.tmp")
    tmp.write_text(body, encoding="utf-8", newline=NL)
    tmp.replace(OUT_TSV)
    return rows


def build(prefectures: list[str] | None, resume: bool) -> None:
    """ホスト単位で走査し、1ホスト終わるごとに追記する。

    このPCは実装6GBで空きが数百MBしかなく、全ページ分を抱えると
    OOMで強制終了される (最初の実装がそれで落ちた)。件数を溜めない作りにする。
    """
    h2m = host_map()
    skip = done_hosts() if resume else set()
    if not resume:
        if OUT_TSV.exists():
            OUT_TSV.unlink()
        if STATE.exists():
            STATE.unlink()

    hosts = []
    for d in sorted(CACHE_DIR.iterdir()):
        if not d.is_dir() or d.name not in h2m:
            continue
        if prefectures and h2m[d.name][0] not in prefectures:
            continue
        if d.name in skip:
            continue
        hosts.append(d)
    print("対象ホスト %d (スキップ済み %d)" % (len(hosts), len(skip)), flush=True)

    for i, host_dir in enumerate(hosts, 1):
        who = h2m[host_dir.name]
        rows: list[dict[str, str]] = []
        pages = 0
        for meta_path in host_dir.glob("*.meta.json"):
            body_path = meta_path.with_suffix("").with_suffix(".body")
            if not body_path.is_file():
                continue
            try:
                if body_path.stat().st_size > MAX_BODY:
                    continue
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            ctype = meta.get("content_type", "") or ""
            if "html" not in ctype.lower():
                continue
            try:
                raw = body_path.read_bytes()
            except Exception:
                continue
            pages += 1
            rows.extend(rows_of_page(raw, ctype, meta.get("final_url") or "", who))
            del raw
        append_rows(rows)
        mark_done(host_dir.name)
        gc.collect()
        print("[%3d/%3d] %-32s %-8s ページ%-6d 抽出%d"
              % (i, len(hosts), host_dir.name[:32], who[1], pages, len(rows)), flush=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--prefecture", action="append")
    ap.add_argument("--resume", action="store_true", help="済んだホストを飛ばして続きから")
    args = ap.parse_args(argv)
    if not args.build:
        ap.print_help()
        return 0

    build(args.prefecture, args.resume)
    rows = dedupe_output()

    per: dict[str, dict[str, int]] = {}
    for r in rows:
        slot = per.setdefault(r.get("prefecture", ""), {})
        d = r.get("designation", "")
        slot[d] = slot.get(d, 0) + 1
    print()
    print("計 %d件 -> %s" % (len(rows), OUT_TSV))
    for pref, slot in sorted(per.items(), key=lambda kv: -sum(kv[1].values())):
        parts = " ".join("%s=%d" % (k, v) for k, v in sorted(slot.items(), key=lambda kv: -kv[1]))
        print("  %-8s %4d  (%s)" % (pref, sum(slot.values()), parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
