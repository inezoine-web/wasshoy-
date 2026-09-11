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
import os
import re
import sys
import time
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
        # 表の一部の行 (後から市制した 白岡市・滝沢市・富谷市・大網白里市・
        # 那珂川市 など7行) は名称セルに読みが連結されている
        # (「白岡市シラオカシ」「滝沢市シ」)。末尾のカタカナを落とす。
        muni = re.sub(r"[ァ-ヶー]+$", "", muni)
        pref = re.sub(r"[ァ-ヶー]+$", "", pref)  # 那珂川市の行は都道府県側も同じ
        pref_kana = from_halfwidth_katakana(pref_kana_hw)
        muni_kana = from_halfwidth_katakana(muni_kana_hw)
        # 都道府県の接尾辞 (ケン/フ/ト/ドウ) は**末尾だけ**落とす。
        # 以前は replace で全文字を消していたため、トチギ→チギ (chigi)、
        # フクイ→クイ (kui)、トットリ→リ (rri)、フクシマ→クシマ (徳島と衝突)
        # になっていた。茨城・静岡・愛知は該当文字を含まず気づけなかった。
        pref_romaji = romanize(re.sub(r"(?:ケン|フ|ト|ドウ)$", "", strip_suffix_kana(pref_kana))) or ""
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
    # 町村の読みは「モリマチ」「オヤマチョウ」のように種別を含むが、
    # ローマ字列からは落としてある。ドメインには残している自治体があるので
    # (森町 www.town.morimachi.shizuoka.jp)、接尾辞つきも候補に出す。
    # マチ/チョウ、ムラ/ソン のどちらを読むかは表から判らないため両方試す。
    _SUFFIXES = {"町": ("machi", "cho"), "村": ("mura", "son"), "市": ("shi",)}
    names: list[str] = []
    for variant in romaji_variants(muni_romaji):
        forms = [variant]
        forms += [variant + suf for suf in _SUFFIXES.get(muni_type, ())]
        for form in forms:
            names.append(form)
            if pref_romaji:
                # 同名市がある場合は都道府県名を前置する (古河市 -> ibaraki-koga)
                names.append(f"{pref_romaji}-{form}")
    urls: list[str] = []
    for name in names:
        for host in (
            f"www.{label}.{name}.lg.jp",
            f"www.{label}.{name}.{pref_romaji}.jp",
            f"{label}.{name}.lg.jp",
            f"{label}.{name}.{pref_romaji}.jp",
            # 裸の .jp を使う自治体がある (名古屋市 www.city.nagoya.jp)。
            # 政令市など古くからドメインを持つところに多い。
            # 取得して自治体名を確認するので、誤って他人のドメインを
            # 拾うことはない。
            f"www.{label}.{name}.jp",
            f"{label}.{name}.jp",
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


def safe_join(base_url: str, href: str) -> str:
    """urljoin の例外を潰す。壊れた href は空文字を返す。

    自治体サイトには href="//＃Jump01" のような全角文字を含むリンクがあり、
    urljoin が NFKC 正規化の検査で ValueError を投げる。
    """
    try:
        return urllib.parse.urljoin(base_url, href)
    except ValueError:
        return ""


_TOURISM_LABEL = re.compile(r"観光協会|観光物産|観光コンベンション|観光局|観光振興|観光情報|観光サイト")
# リンク文字列が「観光情報」のように一般的でも、ホスト名で判別できることが多い
# (日立市 -> www.kankou-hitachi.jp)
_TOURISM_HOST = re.compile(r"kanko|kankou|tourism|-tpa\.|tpa\.|kankokyokai", re.I)
# SNS・動画・地図サービスは観光協会サイトではない。
# 「観光協会Facebook」のようなリンクを起点に採ってしまい、探索枠を
# 無駄にした上に robots.txt で拒否される (龍ケ崎市・常陸大宮市で実際に発生)。
_NOT_A_SITE = re.compile(
    r"(^|\.)(facebook|instagram|twitter|x|youtube|youtu\.be|tiktok|line|"
    r"google|goo\.gl|maps\.google|pinterest|note|ameblo|jimdo|wixsite)\.",
    re.I,
)


def _scan_for_tourism(text: str, official_host: str, muni_romaji: str) -> str:
    """1ページ分の外部リンクから観光協会サイトを探す。

    リンク文字列だけでは足りない。実データで確認できた形:
      - リンク名が空で alt に「（一社）古河市観光協会こがナビ」
      - リンク名が「観光情報」でホストが kankou-hitachi.jp
      - 画像リンクで alt が空、**title 属性**に「観光協会Youtube」(北茨城市)
    """
    fallback = ""
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', text, re.S):
        href, inner = m.group(1), m.group(2)
        parsed = urllib.parse.urlparse(href)
        host = parsed.netloc
        if not host or host == official_host or host.endswith(".lg.jp"):
            continue
        if _NOT_A_SITE.search(host):
            continue
        label = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        attrs = " ".join(
            html.unescape(x) for x in re.findall(r'(?:alt|title)="([^"]*)"', inner)
        )
        blob = f"{label} {attrs}"
        if _TOURISM_LABEL.search(blob):
            return f"{parsed.scheme}://{host}/"
        if fallback:
            continue
        # ホスト名が観光協会を示し、かつ自治体名のローマ字を含むなら、
        # リンク文字列に頼らず採用してよい
        # (kitaibarakishi-kankokyokai.gr.jp のような形)
        if _TOURISM_HOST.search(host) and (muni_romaji and muni_romaji in host.lower()):
            fallback = f"{parsed.scheme}://{host}/"
        elif _TOURISM_HOST.search(host) and "観光" in blob:
            fallback = f"{parsed.scheme}://{host}/"
    return fallback


# 観光系のカテゴリページ。トップに観光協会へのリンクが無い自治体で、
# もう1階層だけ辿るための入口。
_TOURISM_SECTION = re.compile(r"観光|見どころ|楽しむ|遊ぶ|魅力|イベント")


def find_tourism_site(
    fetcher: Fetcher, official_url: str, municipality: str, muni_romaji: str = ""
) -> str:
    """公式サイトから観光協会サイトを拾う。

    旧手法では検索で当てていた部分。公式サイトからのリンクで置き換える。
    トップページに無い自治体があるため (水戸市・常総市・下妻市・境町で確認)、
    見つからなければ観光カテゴリのページを1つだけ辿る。
    """
    doc = fetcher.get(official_url)
    if doc is None:
        return ""
    official_host = urllib.parse.urlparse(doc.final_url).netloc
    found = _scan_for_tourism(doc.text, official_host, muni_romaji)
    if found:
        return found

    # トップに無い場合だけ、観光カテゴリのページを最大3つ辿る
    seen: set[str] = set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', doc.text, re.S):
        label = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if not _TOURISM_SECTION.search(label):
            continue
        child = safe_join(doc.final_url, html.unescape(m.group(1))).split("#")[0]
        if not child:
            continue
        if urllib.parse.urlparse(child).netloc != official_host or child in seen:
            continue
        seen.add(child)
        sub = fetcher.get(child)
        if sub is not None:
            found = _scan_for_tourism(sub.text, official_host, muni_romaji)
            if found:
                return found
        if len(seen) >= 3:
            break
    return ""


_SITE_FIELDS = ["code", "prefecture", "municipality", "official_url", "tourism_url", "resolved"]


def _write_sites(out: Path, rows_by_code: dict[str, dict[str, str]]) -> None:
    """1件ごとに書き出すために切り出した。長時間の解決中に落とされても
    進捗が消えないようにするため（このPCは実装6GBで実際に強制終了される）。"""
    # 一時ファイルへ書いてから置き換える。同じファイルを何度も開くと、
    # クラウド同期フォルダ (OneDrive) にロックされて PermissionError になる。
    # 置換なら開いている時間が短く、書きかけの内容が残ることもない。
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.DictWriter(
            fh, delimiter="\t", lineterminator="\n", fieldnames=_SITE_FIELDS
        )
        w.writeheader()
        w.writerows(sorted(rows_by_code.values(), key=lambda r: r["code"]))
    for attempt in range(5):
        try:
            os.replace(tmp, out)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.5)


def _pref_official(prefecture: str) -> str:
    """pref_sites.tsv から県公式サイトのURLを引く。無ければ空。"""
    path = REGISTRY_DIR / "pref_sites.tsv"
    if not path.is_file():
        return ""
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="	"):
            if row["prefecture"] == prefecture:
                return row.get("official_url", "")
    return ""


# 県公式サイトが持つ市町村リンク集。命名規則から外れたドメインは、
# 読み仮名からは原理的に導けない (小山町 www.fuji-oyama.jp)。
# 手で書けば直るが、それを47都道府県ぶん積むのは避けたい。
# 県は自分の市町村へのリンクを必ず持っているので、そこから引く。
_MUNI_LINK_SECTION = re.compile(r"市町村|市区町村|市町|各市|リンク|一覧|ホームページ")
_PREF_LINK_PAGES = 6


def _collect_external_links(text: str, self_host: str) -> list[tuple[str, str]]:
    """(リンク文字列, URL) の一覧。自ホストとSNSは除く。"""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', text, re.S):
        href, inner = m.group(1), m.group(2)
        host = urllib.parse.urlparse(href).netloc
        if not host or host == self_host or _NOT_A_SITE.search(host):
            continue
        label = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        attrs = " ".join(
            html.unescape(x) for x in re.findall(r'(?:alt|title)="([^"]*)"', inner)
        )
        out.append((f"{label} {attrs}".strip(), href))
    return out


def prefecture_link_index(fetcher: Fetcher, pref_official_url: str) -> list[tuple[str, str]]:
    """県公式サイトから外部リンクを集める。市町村サイトを引くための索引。

    トップページに市町村一覧を置いている県は少ないので、リンク集らしい
    ページを数枚だけ辿る。県ごとに1回作れば全市町村で使い回せる。
    """
    if not pref_official_url:
        return []
    doc = fetcher.get(pref_official_url)
    if doc is None or doc.status != 200:
        return []
    self_host = urllib.parse.urlparse(doc.final_url).netloc
    links = _collect_external_links(doc.text, self_host)
    # 同一ホスト内のリンク集ページを数枚だけ辿る
    seen: set[str] = {doc.final_url}
    followed = 0
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', doc.text, re.S):
        if followed >= _PREF_LINK_PAGES:
            break
        href = safe_join(doc.final_url, m.group(1))
        if not href:
            continue
        if urllib.parse.urlparse(href).netloc != self_host or href in seen:
            continue
        label = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if not _MUNI_LINK_SECTION.search(label):
            continue
        seen.add(href)
        followed += 1
        sub = fetcher.get(href)
        if sub is not None and sub.status == 200:
            links.extend(_collect_external_links(sub.text, self_host))
    return links


def find_via_prefecture_links(
    fetcher: Fetcher, links: list[tuple[str, str]], municipality: str
) -> tuple[str, str]:
    """リンク集から1市町村の公式サイトを引き当てる。

    リンク文字列に自治体名が入っていることだけを手掛かりにし、採用前に
    本文を取得して自治体名を確認する。検証は命名規則の場合と同じなので、
    ここで推測が混ざることはない。
    """
    # 「町」「村」を落とした形も見る (リンク集が「小山」と書く県がある)
    short = re.sub(r"(市|区|町|村)$", "", municipality)
    for label, href in links:
        if municipality not in label and (len(short) < 2 or short not in label):
            continue
        parsed = urllib.parse.urlparse(href)
        url = f"{parsed.scheme}://{parsed.netloc}/"
        ok, why = verify_site(fetcher, url, municipality)
        if ok:
            return url, f"県公式サイトのリンク集から ({why})"
    return "", ""


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

    # 県公式サイトのリンク集。命名規則が外れた市町村のためだけに使うので、
    # 実際に外れるまで作らない。
    pref_links: dict[str, list[tuple[str, str]]] = {}

    for m in munis:
        pref_romaji = _PREF_DOMAIN_OVERRIDE.get(m["prefecture"], m["pref_romaji"])
        official, note = "", "候補URLがすべて外れ"
        for url in candidate_domains(m["muni_romaji"], m["type"], pref_romaji):
            ok, why = verify_site(fetcher, url, m["municipality"])
            if ok:
                official, note = url, why
                break
        if not official:
            pref = m["prefecture"]
            if pref not in pref_links:
                pref_links[pref] = prefecture_link_index(fetcher, _pref_official(pref))
            found, why = find_via_prefecture_links(
                fetcher, pref_links[pref], m["municipality"]
            )
            if found:
                official, note = found, why
        tourism = (
            find_tourism_site(fetcher, official, m["municipality"], m["muni_romaji"])
            if official
            else ""
        )
        existing[m["code"]] = {
            "code": m["code"],
            "prefecture": m["prefecture"],
            "municipality": m["municipality"],
            "official_url": official,
            "tourism_url": tourism,
            "resolved": note,
        }
        print(f"  {m['municipality']:12s} {official or '-':45s} {tourism or '-':35s} {note}")
        # 1件ごとに書き出す。長時間の解決中に落とされても進捗が消えないように
        # (このPCは実装6GBで、実際に何度か強制終了されている)。
        _write_sites(out, existing)

    rows = sorted(existing.values(), key=lambda r: r["code"])
    _write_sites(out, existing)
    hit = sum(1 for r in rows if r["official_url"])
    tour = sum(1 for r in rows if r["tourism_url"])
    print(f"\nsites.tsv: {len(rows)} 件 / 公式サイト解決 {hit} / 観光協会 {tour}")
    print("fetcher stats:", fetcher.stats)
    return out


# ------------------------------------------------- 都道府県レベルの入口

# 都道府県の公式サイトと教育委員会。文化財一覧は市町村サイトではなく
# ここにあることが多い (旧手法で最も費用対効果が高かった経路)。
_PREF_HOST_PATTERNS = (
    "www.pref.{r}.jp",
    "www.pref.{r}.lg.jp",
    "pref.{r}.jp",
    "www.metro.{r}.lg.jp",  # 東京都
)
_EDU_HOST_PATTERNS = (
    "kyoiku.pref.{r}.jp",
    "www.edu.pref.{r}.jp",
    "www.pref.{r}.ed.jp",
    "edu.pref.{r}.jp",
    "www.kyoiku.metro.{r}.lg.jp",
)


def resolve_prefecture_sites(fetcher: Fetcher, prefecture: str | None) -> Path:
    """都道府県の公式サイトと教育委員会サイトを解決する。

    市町村と同じく、ドメイン規則で候補を組み立てて取得し、
    ページ本文に都道府県名があることを確認してから採用する。
    """
    src = REGISTRY_DIR / "municipalities.tsv"
    if not src.is_file():
        raise SystemExit("先に --build-municipalities を実行してください")
    with src.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))

    prefs: dict[str, str] = {}
    for r in rows:
        prefs.setdefault(r["prefecture"], _PREF_DOMAIN_OVERRIDE.get(r["prefecture"], r["pref_romaji"]))
    if prefecture:
        prefs = {k: v for k, v in prefs.items() if k == prefecture}
    if not prefs:
        raise SystemExit(f"該当する都道府県がありません: {prefecture}")

    out = REGISTRY_DIR / "pref_sites.tsv"
    existing: dict[str, dict[str, str]] = {}
    if out.is_file():
        with out.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                existing[row["prefecture"]] = row

    for pref, romaji in prefs.items():
        official, education = "", ""
        for pattern in _PREF_HOST_PATTERNS:
            url = f"https://{pattern.format(r=romaji)}/"
            ok, _why = verify_site(fetcher, url, pref)
            if ok:
                official = url
                break
        for pattern in _EDU_HOST_PATTERNS:
            url = f"https://{pattern.format(r=romaji)}/"
            ok, _why = verify_site(fetcher, url, pref)
            if ok:
                education = url
                break
        existing[pref] = {
            "prefecture": pref,
            "official_url": official,
            "education_url": education,
            "resolved": "本文に都道府県名を確認" if official else "候補URLがすべて外れ",
        }
        print(f"  {pref:8s} {official or '-':40s} {education or '-'}")

    with out.open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.DictWriter(
            fh, delimiter="\t", lineterminator="\n",
            fieldnames=["prefecture", "official_url", "education_url", "resolved"],
        )
        w.writeheader()
        w.writerows(sorted(existing.values(), key=lambda r: r["prefecture"]))
    hit = sum(1 for r in existing.values() if r["official_url"])
    edu = sum(1 for r in existing.values() if r["education_url"])
    print(f"\npref_sites.tsv: {len(existing)} 件 / 公式 {hit} / 教育委員会 {edu}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-municipalities", action="store_true")
    ap.add_argument("--resolve-sites", action="store_true")
    ap.add_argument("--resolve-prefecture-sites", action="store_true")
    ap.add_argument("--prefecture")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args(argv)

    fetcher = Fetcher(offline=args.offline, delay=args.delay)
    if args.build_municipalities:
        build_municipalities(fetcher)
    if args.resolve_sites:
        resolve_sites(fetcher, args.prefecture)
    if args.resolve_prefecture_sites:
        resolve_prefecture_sites(fetcher, args.prefecture)
    if not (args.build_municipalities or args.resolve_sites or args.resolve_prefecture_sites):
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
