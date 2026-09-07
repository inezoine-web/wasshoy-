"""S0 台帳生成: 全国市区町村コード表と、公式サイト/観光協会サイトのURL。

既存の festivals.json を一切参照しない。公開された一次情報だけから作る。

  1. 総務省「都道府県コード及び市区町村コード」(xlsx) を解析 -> municipalities.tsv
  2. 読み仮名からローマ字を起こし、日本の自治体ドメインの命名規則で
     候補URLを組み立て、実際に取得してページ内に自治体名があるかで検証
     -> sites.tsv
  3. 検証できた公式サイトの外部リンクから観光協会サイトを拾う

推測でURLを書かない。検証できなかったものは空欄のまま残し、
`resolved` 列に理由を書く。

使い方:
    python pipeline/registry.py --build-municipalities
    python pipeline/registry.py --resolve-sites --prefecture 茨城県
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from net import Fetcher  # noqa: E402
from romaji import from_halfwidth_katakana, romanize, to_hiragana  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = REPO_ROOT / "registry"

# 総務省「都道府県コード及び市区町村コード」(令和6年1月1日更新)
# soumu.go.jp/denshijiti/code.html からリンクされている現行版
SOUMU_CODE_XLSX = "https://www.soumu.go.jp/main_content/000925835.xlsx"
SOUMU_CODE_PAGE = "https://www.soumu.go.jp/denshijiti/code.html"

_XL_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# 市区町村名の末尾。ローマ字化の前に落とす (ドメインには含まれないため)
_SUFFIX_KANA = ("シ", "チョウ", "マチ", "ムラ", "ソン", "ク")
_SUFFIX_KANJI = ("市", "町", "村", "区")


# --------------------------------------------------------------- xlsx 解析


def parse_xlsx(data: bytes, sheet: str = "xl/worksheets/sheet1.xml") -> list[list[str]]:
    """xlsx を標準ライブラリだけで行列に開く。"""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = [
        "".join(t.text or "" for t in si.iter(_XL_NS + "t"))
        for si in ET.fromstring(z.read("xl/sharedStrings.xml"))
    ]
    rows: list[list[str]] = []
    for row in ET.fromstring(z.read(sheet)).iter(_XL_NS + "row"):
        cells: list[str] = []
        for c in row.iter(_XL_NS + "c"):
            v = c.find(_XL_NS + "v")
            val = "" if v is None else (v.text or "")
            if c.get("t") == "s":
                val = shared[int(val)]
            cells.append(val)
        rows.append(cells)
    return rows


def strip_suffix_kana(kana: str) -> str:
    for suf in _SUFFIX_KANA:
        if kana.endswith(suf) and len(kana) > len(suf):
            return kana[: -len(suf)]
    return kana


def strip_suffix_kanji(name: str) -> str:
    for suf in _SUFFIX_KANJI:
        if name.endswith(suf) and len(name) > 1:
            return name[: -len(suf)]
    return name


def municipality_type(name: str) -> str:
    for suf in _SUFFIX_KANJI:
        if name.endswith(suf):
            return suf
    return "?"


def build_municipalities(fetcher: Fetcher) -> Path:
    data = fetcher.get_bytes(SOUMU_CODE_XLSX)
    if data is None:
        raise SystemExit(f"取得できませんでした: {SOUMU_CODE_XLSX}")
    rows = parse_xlsx(data)

    out_rows = []
    for cells in rows[1:]:
        if len(cells) < 5:
            continue
        code, pref, muni, pref_kana_hw, muni_kana_hw = (cells + [""] * 5)[:5]
        if not code or not pref:
            continue
        if not muni:
            continue  # 都道府県そのものの行は市区町村台帳には入れない
        pref_kana = from_halfwidth_katakana(pref_kana_hw)
        muni_kana = from_halfwidth_katakana(muni_kana_hw)
        pref_romaji = romanize(strip_suffix_kana(pref_kana).replace("ケン", "").replace("フ", "").replace("ト", "").replace("ドウ", "")) or ""
        muni_romaji = romanize(strip_suffix_kana(muni_kana)) or ""
        out_rows.append(
            [
                code,
                pref,
                muni,
                municipality_type(muni),
                pref_kana,
                muni_kana,
                pref_romaji,
                muni_romaji,
            ]
        )

    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    out = REGISTRY_DIR / "municipalities.tsv"
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(
            ["code", "prefecture", "municipality", "type", "pref_kana",
             "muni_kana", "pref_romaji", "muni_romaji"]
        )
        w.writerows(out_rows)
    print(f"municipalities.tsv: {len(out_rows)} 件")
    unresolved = [r for r in out_rows if not r[7]]
    if unresolved:
        print(f"  読みからローマ字を起こせなかった: {len(unresolved)} 件")
        for r in unresolved[:10]:
            print(f"    {r[1]} {r[2]} (kana={r[5]!r})")
    return out


# ------------------------------------------------------- 公式サイトの解決

# 都道府県ローマ字は municipalities.tsv から引くが、慣行的に異なるものだけ上書き
_PREF_DOMAIN_OVERRIDE = {
    "北海道": "hokkaido",
    "東京都": "tokyo",
    "京都府": "kyoto",
    "大阪府": "osaka",
}

_TYPE_LABEL = {"市": "city", "区": "city", "町": "town", "村": "vill"}


def romaji_variants(romaji: str) -> list[str]:
    """ローマ字の綴りの揺れを列挙する。

    読み仮名だけでは、母音の重なりが長音 (遠野 トオノ -> tono) なのか
    形態素の境界 (広尾 ヒロオ -> hiroo) なのか判別できない。また総務省の
    表には拗音を大書きした表記が混ざる (竜王 リユウオウ)。
    どちらが正しいかを推測せず、両方を候補に出して HTTP 検証に決めさせる。
    """
    variants = [romaji]
    contracted = re.sub(r"i(y[auo])", r"\1", romaji)  # riyuu -> ryuu
    if contracted != romaji:
        variants.append(contracted)
    for base in list(variants):
        collapsed = re.sub(r"([aiueo])\1", r"\1", base)
        if collapsed != base:
            variants.append(collapsed)
    seen: set[str] = set()
    return [v for v in variants if v and not (v in seen or seen.add(v))]


def candidate_domains(muni_romaji: str, muni_type: str, pref_romaji: str) -> list[str]:
    """日本の自治体ドメインの命名規則から候補を組み立てる。

    実データで確認できた形:
      www.city.kasama.lg.jp          (市 + lg.jp)
      www.city.ryugasaki.ibaraki.jp  (市 + 都道府県ドメイン)
      www.city.ibaraki-koga.lg.jp    (同名市があるため都道府県名を前置)
      www.town.daigo.ibaraki.jp      (町)
    """
    if not muni_romaji:
        return []
    label = _TYPE_LABEL.get(muni_type)
    if label is None:
        return []
    names: list[str] = []
    for variant in romaji_variants(muni_romaji):
        names.append(variant)
        if pref_romaji:
            # 同名市がある場合は都道府県名を前置する (古河市 -> ibaraki-koga)
            names.append(f"{pref_romaji}-{variant}")
    urls: list[str] = []
    for name in names:
        for host in (
            f"www.{label}.{name}.lg.jp",
            f"www.{label}.{name}.{pref_romaji}.jp",
            f"{label}.{name}.lg.jp",
            f"{label}.{name}.{pref_romaji}.jp",
        ):
            if pref_romaji or ".lg.jp" in host:
                urls.append(f"https://{host}/")
    # 重複除去 (順序は維持)
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def verify_site(fetcher: Fetcher, url: str, municipality: str) -> tuple[bool, str]:
    """取得して、そのページが本当にその自治体のサイトかを確認する。

    ドメインが当たっただけでは採用しない。ページ本文に自治体名が
    現れることを確認する (推測でURLを書かないため)。
    """
    doc = fetcher.get(url)
    if doc is None:
        return False, "取得できず"
    if doc.status != 200:
        return False, f"HTTP {doc.status}"
    text = re.sub(r"<[^>]+>", " ", doc.text)
    if municipality in text:
        return True, "本文に自治体名を確認"
    bare = strip_suffix_kanji(municipality)
    if bare and bare in text:
        return True, "本文に自治体名(接尾辞なし)を確認"
    return False, "本文に自治体名が見つからない"


_TOURISM_LABEL = re.compile(r"観光協会|観光物産|観光コンベンション|観光局|観光振興|観光情報|観光サイト")
# リンク文字列が「観光情報」のように一般的でも、ホスト名で判別できることが多い
# (日立市 -> www.kankou-hitachi.jp)
_TOURISM_HOST = re.compile(r"kanko|kankou|tourism|-tpa\.|tpa\.|kankokyokai", re.I)


def find_tourism_site(fetcher: Fetcher, official_url: str, municipality: str) -> str:
    """公式サイトの外部リンクから観光協会サイトを拾う。

    旧手法では検索で当てていた部分。公式サイトからのリンクで置き換える。
    リンク文字列だけでなく alt 属性とホスト名も見る。実データでは、
    リンク名が空で alt に「（一社）古河市観光協会こがナビ」が入っていたり、
    リンク名が「観光情報」でホストが kankou-hitachi.jp だったりする。
    """
    doc = fetcher.get(official_url)
    if doc is None:
        return ""
    official_host = urllib.parse.urlparse(doc.final_url).netloc
    fallback = ""
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', doc.text, re.S):
        href, inner = m.group(1), m.group(2)
        parsed = urllib.parse.urlparse(href)
        host = parsed.netloc
        if not host or host == official_host or host.endswith(".lg.jp"):
            continue
        label = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        alt = " ".join(re.findall(r'alt="([^"]*)"', inner))
        blob = f"{label} {alt}"
        if _TOURISM_LABEL.search(blob):
            return f"{parsed.scheme}://{host}/"
        if not fallback and _TOURISM_HOST.search(host) and "観光" in blob:
            fallback = f"{parsed.scheme}://{host}/"
    return fallback


def resolve_sites(fetcher: Fetcher, prefecture: str | None) -> Path:
    src = REGISTRY_DIR / "municipalities.tsv"
    if not src.is_file():
        raise SystemExit("先に --build-municipalities を実行してください")
    with src.open(encoding="utf-8", newline="") as fh:
        munis = list(csv.DictReader(fh, delimiter="\t"))
    if prefecture:
        munis = [m for m in munis if m["prefecture"] == prefecture]
    if not munis:
        raise SystemExit(f"該当する市区町村がありません: {prefecture}")

    out = REGISTRY_DIR / "sites.tsv"
    existing: dict[str, dict[str, str]] = {}
    if out.is_file():
        with out.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                existing[row["code"]] = row

    for m in munis:
        pref_romaji = _PREF_DOMAIN_OVERRIDE.get(m["prefecture"], m["pref_romaji"])
        official, note = "", "候補URLがすべて外れ"
        for url in candidate_domains(m["muni_romaji"], m["type"], pref_romaji):
            ok, why = verify_site(fetcher, url, m["municipality"])
            if ok:
                official, note = url, why
                break
        tourism = find_tourism_site(fetcher, official, m["municipality"]) if official else ""
        existing[m["code"]] = {
            "code": m["code"],
            "prefecture": m["prefecture"],
            "municipality": m["municipality"],
            "official_url": official,
            "tourism_url": tourism,
            "resolved": note,
        }
        print(f"  {m['municipality']:12s} {official or '-':45s} {tourism or '-':35s} {note}")

    rows = sorted(existing.values(), key=lambda r: r["code"])
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.DictWriter(
            fh,
            delimiter="\t",
            lineterminator="\n",
            fieldnames=["code", "prefecture", "municipality", "official_url", "tourism_url", "resolved"],
        )
        w.writeheader()
        w.writerows(rows)
    hit = sum(1 for r in rows if r["official_url"])
    tour = sum(1 for r in rows if r["tourism_url"])
    print(f"\nsites.tsv: {len(rows)} 件 / 公式サイト解決 {hit} / 観光協会 {tour}")
    print("fetcher stats:", fetcher.stats)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-municipalities", action="store_true")
    ap.add_argument("--resolve-sites", action="store_true")
    ap.add_argument("--prefecture")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args(argv)

    fetcher = Fetcher(offline=args.offline, delay=args.delay)
    if args.build_municipalities:
        build_municipalities(fetcher)
    if args.resolve_sites:
        resolve_sites(fetcher, args.prefecture)
    if not (args.build_municipalities or args.resolve_sites):
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
