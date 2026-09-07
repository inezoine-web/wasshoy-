"""S3 正規化: 重複統合・日付の仕分け・カテゴリ推定・slug生成。

前回の調査 (reports/2026-09-06-ibaraki-40-municipalities-stage1.md) で
親モデルが手作業でやっていた工程を、そのままルールとしてコード化したもの。
ルールが日本語で明文化できていた工程は、モデルに再実行させる必要がない。

推測で埋めない:
  - 日付は AGENTS.md §5 の3分岐で仕分ける。特定年の日付を例年規則へ
    言い換えることはしない。判別できない表記は usual_schedule を null にし、
    原文を note に退避する
  - カテゴリは名称と、情報源に書かれていた会場名からのみ推定する。
    どちらからも判断できなければ unclassified にする
  - slug は読みが確定できる場合のみ生成する。漢字の読みを推測しない。
    確定できないものは PENDING とし、S4 (AI判定) へ回す

既存の festivals.json / benchmarks を参照しない。

使い方:
    python pipeline/normalize.py --prefecture 茨城県
"""

from __future__ import annotations

import argparse
import collections
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from romaji import is_kana, romanize, slugify, to_hiragana  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / "work"

# --------------------------------------------------------- 日付 (AGENTS.md §5)

# 例年の規則を示す語。これらがあれば usual_schedule に入れてよい。
RECURRING = re.compile(
    r"毎年|例年|第\s*[0-9０-９一二三四五六七八九]|旧暦|"
    r"上旬|中旬|下旬|春季|夏季|秋季|冬季|"
    r"[月火水木金土日]曜"
)
# 特定年を示す語。あれば「特定年の日付」として usual_schedule は null。
SPECIFIC_YEAR = re.compile(r"令和\s*\d+\s*年|平成\s*\d+\s*年|\d{4}\s*年")
BARE_DATE = re.compile(r"\d{1,2}\s*月(\s*\d{1,2}\s*日)?")


def classify_date(date_text: str) -> tuple[str | None, str]:
    """(usual_schedule, note) を返す。

    前回レポート §5「日程の扱い」の仕分けをそのまま実装する:
      「毎年」「第N」「旧暦」「上旬/中旬/下旬」「春夏秋冬」等を含む -> usual_schedule
      年 (西暦・元号) を含む -> null、原文を note へ
      裸の月日のみ           -> null、原文を note へ
    """
    text = date_text.strip()
    if not text:
        return None, ""
    if SPECIFIC_YEAR.search(text):
        return None, f"情報源に特定年の日付として記載: {text}"
    if RECURRING.search(text):
        # 「毎年」「例年」があっても、実際の日付語を伴わなければ日程ではない
        # (「毎年行われている市民の祭典」のような文を拾ってしまうため)
        if not re.search(r"[月火水木金土日]曜|\d{1,2}\s*月|上旬|中旬|下旬|旧暦|[春夏秋冬]季", text):
            return None, f"例年の語はあるが日付が特定できない: {text}"
        return text, ""
    if BARE_DATE.search(text):
        return None, f"特定年の日程か例年の規則かは未確認: {text}"
    return None, f"日付表記を判別できず: {text}"


# ------------------------------------------------------ カテゴリ (report §3)

CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("shrine_festival", re.compile(r"神社|神宮|八幡宮|八幡|稲荷|大社|天満宮|東照宮|祇園|例大祭|祭礼")),
    ("temple_festival", re.compile(r"寺|観音|大師|不動尊|地蔵|薬師|開帳|法要|縁日")),
    ("fireworks", re.compile(r"花火")),
    ("bon_odori", re.compile(r"盆踊")),
    ("lion_dance", re.compile(r"獅子舞|ささら獅子")),
    ("traditional_performing_art", re.compile(r"ささら|田楽|神楽|風流|囃子|ばやし|太鼓|万灯|巫女舞|奉納相撲|人形")),
    ("dashi", re.compile(r"山車|曳山|屋台|鉾")),
    ("mikoshi", re.compile(r"神輿|みこし")),
    ("ichi_fair", re.compile(r"達磨市|だるま市|骨董市|植木市|朝市|大市")),
    ("citizen_festival", re.compile(r"市民祭|市民まつり|ふるさとまつり|フェスティバル|フェスタ|産業祭|商工祭")),
    ("seasonal_flower_nature", re.compile(
        r"桜|さくら|梅|藤|つつじ|ぼたん|牡丹|菖蒲|あやめ|紫陽花|あじさい|"
        r"朝顔|ほおずき|風鈴|七夕|紅葉|もみじ|雪|氷|雛|ひなまつり|鯉のぼり|"
        r"菊|萩|蓮|ホタル|ほたる|コスモス|ひまわり|チューリップ|ゆり|ラベンダー")),
    ("fire_festival", re.compile(r"火祭|柴燈|どんど焼|どんと祭|松明")),
    ("dance", re.compile(r"踊り|おどり|よさこい|阿波踊")),
]


def infer_categories(name: str, venue: str, municipality: str = "") -> list[str]:
    """名称と会場名からのみ推定する。どちらからも判断できなければ unclassified。

    自治体名は判定材料から除く。鉾田市の「鉾」が山車系に、
    〇〇市の「市」が市(いち)に一致してしまうため。
    """
    blob = f"{name} {venue}"
    if municipality:
        blob = blob.replace(municipality, "")
        if municipality.endswith(("市", "町", "村", "区")):
            blob = blob.replace(municipality[:-1], "")
    cats = [key for key, pattern in CATEGORY_RULES if pattern.search(blob)]
    return cats or ["unclassified"]


# --------------------------------------------------------------- 重複統合

_ROUND = re.compile(r"第\s*[0-9０-９一二三四五六七八九十百]+\s*回\s*")
_YEAR = re.compile(r"(令和|平成)\s*\d+\s*年度?|\d{4}\s*年度?")
# 括弧の中身が全部かなならふりがな。名称の一部ではないので照合前に落とす。
# 「東金砂神社田楽舞（ひがしかなさじんじゃでんがくまい）」と
# 「東金砂神社田楽舞」は同じものだが、落とさないと別レコードになる。
# 中身がかな以外を含む場合 (「延方相撲（鹿嶋吉田神社祭礼）」) は落とさない。
_FURIGANA = re.compile(r"[（(]\s*[ぁ-ゖァ-ヺーー\s]+\s*[)）]")


def dedup_key(name: str) -> str:
    """表記揺れを吸収した照合キー。

    AGENTS.md §8 が挙げる揺れのうち、機械的に確実なものだけを畳む。
    「同一と判断できない場合は無理に統合しない」ため、
    漢字とかなの対応 (祇園祭 / ぎおんさい 等) は畳まない。
    """
    key = _FURIGANA.sub("", name)
    key = _ROUND.sub("", key)
    key = _YEAR.sub("", key)
    # 「〜」は名称の飾りとして使われる (守谷市商工まつり～きらめき…～)。
    # 照合キーからは落とす。長音符「ー」は語の一部なので落とさない。
    key = re.sub(r"[\s　・,、。!！?？'\"()（）「」【】〜～~]", "", key)
    key = to_hiragana(key)
    key = key.replace("祭り", "祭").replace("まつり", "祭").replace("マツリ", "祭")
    return key


def merge_prefix_variants(
    groups: dict[tuple[str, str, str], list[dict[str, str]]],
) -> dict[tuple[str, str, str], list[dict[str, str]]]:
    """同一市町村内で、名前が他の候補の先頭一致になっているものを畳む。

    自治体サイトは同じ行事の関連ページを大量に並べる:
      日立さくらまつり / 日立さくらまつりホーム /
      日立さくらまつり「交通規制」 / 日立さくらまつり当日チラシ
    これらは同じ行事なので、最も短い名前へ寄せて別名として保持する。
    先頭一致に限るのは、部分一致まで許すと別の行事を巻き込むため。
    """
    merged: dict[tuple[str, str, str], list[dict[str, str]]] = collections.defaultdict(list)
    by_muni: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for pref, muni, key in groups:
        by_muni[(pref, muni)].append(key)

    for (pref, muni), keys in by_muni.items():
        shortest_first = sorted(keys, key=len)
        for key in keys:
            target = key
            for base in shortest_first:
                # 4文字未満の断片を親にすると無関係な行事まで巻き込む
                if base != key and len(base) >= 4 and key.startswith(base):
                    target = base
                    break
            merged[(pref, muni, target)].extend(groups[(pref, muni, key)])
    return merged


# ------------------------------------------------------------------ slug

_PAREN_KANA = re.compile(r"[（(]\s*([ぁ-ゖァ-ヺーー\s]{2,30})\s*[)）]")
_RUBY = re.compile(r"<rt[^>]*>([^<]{1,20})</rt>", re.I)


def reading_for(name: str, context: str) -> str | None:
    """名称の読みを、確認できる範囲でだけ得る。推測はしない。

    確認できる経路:
      1. 名称そのものがかな
      2. 名称の直後の括弧内のかな (撞舞（つくまい） の形)
    """
    bare = re.sub(r"[（(].*?[)）]", "", name).strip()
    if is_kana(bare.replace(" ", "")):
        return bare.replace(" ", "")
    m = _PAREN_KANA.search(name)
    if m:
        return m.group(1).replace(" ", "")
    idx = context.find(bare)
    if idx >= 0:
        tail = context[idx + len(bare) : idx + len(bare) + 40]
        m = _PAREN_KANA.match(tail.lstrip())
        if m:
            return m.group(1).replace(" ", "")
    return None


def make_slug(prefecture_romaji: str, municipality_romaji: str, name: str, context: str) -> str:
    reading = reading_for(name, context)
    if reading is None:
        return "PENDING"
    romaji = romanize(reading)
    if romaji is None:
        return "PENDING"
    return f"{prefecture_romaji}-{municipality_romaji}-{slugify(romaji)}"


# ------------------------------------------------------------------- main


def load_romaji_map() -> dict[tuple[str, str], tuple[str, str]]:
    path = REPO_ROOT / "registry" / "municipalities.tsv"
    out: dict[tuple[str, str], tuple[str, str]] = {}
    if not path.is_file():
        return out
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            out[(row["prefecture"], row["municipality"])] = (
                row["pref_romaji"],
                row["muni_romaji"],
            )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefecture")
    args = ap.parse_args(argv)

    src = WORK_DIR / "candidates.tsv"
    if not src.is_file():
        raise SystemExit("先に extract.py を実行してください")
    with src.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    if args.prefecture:
        rows = [r for r in rows if r["prefecture"] == args.prefecture]

    romaji_map = load_romaji_map()
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = collections.defaultdict(list)
    for r in rows:
        groups[(r["prefecture"], r["municipality"], dedup_key(r["name"]))].append(r)
    groups = merge_prefix_variants(groups)

    out_rows = []
    for (pref, muni, key), members in groups.items():
        # 代表名は、統合キーと一致する表記を優先し、次に出現回数、次に短さ。
        # 先頭一致で畳んだ結果、長い派生名が代表になるのを防ぐ。
        counts = collections.Counter(m["name"] for m in members)
        name = min(
            counts,
            key=lambda n: (dedup_key(n) != key, -counts[n], len(n)),
        )
        aliases = sorted({m["name"] for m in members} - {name})

        venue = next((m["venue"] for m in members if m["venue"]), "")
        date_text = next((m["date_text"] for m in members if m["date_text"]), "")
        usual_schedule, date_note = classify_date(date_text)
        context = " ".join(m["context"] for m in members)[:600]
        pref_r, muni_r = romaji_map.get((pref, muni), ("", ""))

        urls = sorted({m["source_url"] for m in members})
        origins = sorted({m["origin"] for m in members})
        reasons = sorted({m["reason"] for m in members})

        out_rows.append(
            {
                "prefecture": pref,
                "municipality": muni,
                "slug": make_slug(pref_r, muni_r, name, context) if pref_r else "PENDING",
                "name": name,
                "aliases": "|".join(aliases),
                "categories": "|".join(infer_categories(name, venue, muni)),
                "venue": venue,
                "usual_schedule": usual_schedule or "",
                "date_note": date_note,
                "source_count": str(len(urls)),
                "source_urls": "|".join(urls[:5]),
                "origins": "|".join(origins),
                "reasons": "|".join(reasons),
            }
        )

    out_rows.sort(key=lambda r: (r["municipality"], r["name"]))
    out_path = WORK_DIR / "normalized.tsv"
    fields = list(out_rows[0].keys()) if out_rows else ["prefecture"]
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.DictWriter(fh, delimiter="\t", lineterminator="\n", fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    pending = sum(1 for r in out_rows if r["slug"] == "PENDING")
    with_schedule = sum(1 for r in out_rows if r["usual_schedule"])
    unclassified = sum(1 for r in out_rows if r["categories"] == "unclassified")
    no_url = sum(1 for r in out_rows if not r["source_urls"])
    print(f"normalized.tsv: {len(out_rows)} 件 (候補行 {len(rows)} から統合)")
    print(f"  市町村数            : {len({r['municipality'] for r in out_rows})}")
    print(f"  slug PENDING        : {pending}  ({pending * 100 // max(1, len(out_rows))}% — S4のAI判定へ回す分)")
    print(f"  usual_schedule あり : {with_schedule}")
    print(f"  unclassified        : {unclassified}")
    print(f"  根拠URLなし         : {no_url}  (0であるべき)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
