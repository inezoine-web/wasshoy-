"""S2 抽出: 取得済みページから祭り候補の行を起こす。

ネットワークへは出ない。discover.py が cache/ に落としたページだけを読む。

抽出は「構造化された位置」からのみ行う (ページタイトル・見出し・アンカー
テキスト・表のセル・RSS項目)。本文の流し読みから名前を切り出すと境界を
誤るため、既に区切られている文字列だけを候補にする。

推測で値を埋めない:
  - 日付は原文のまま date_text に置く。例年規則への言い換えは normalize.py が
    判定し、判定できないものは null にする (AGENTS.md §5)
  - 会場は本文に現れた施設名だけ。名称から神社等を推測しない

既存の festivals.json / benchmarks を参照しない。

使い方:
    python pipeline/extract.py --prefecture 茨城県
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from net import Fetcher  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / "work"

# AGENTS.md §3.1 の語形展開。祭りそのものを指す語。
FESTIVAL_WORD = re.compile(
    r"祭|まつり|マツリ|大祭|例大祭|祭礼|縁日|花火大会|盆踊り|盆踊|"
    r"囃子|ばやし|ばやし|獅子舞|ささら|田楽|神楽|風流|万灯|燈籠|灯籠|"
    r"山車|神輿|みこし|曳山|屋台|山鉾|鉾曳|奉納|渡御|行列|パレード|"
    r"どんど焼き|どんと祭|裸祭|火渡|柴燈|梯子乗|太鼓|踊り|おどり|"
    r"フェスティバル|フェスタ|カーニバル"
)
# 単独では祭りと言い切れないが、地域行事の入口になる語 (AGENTS.md §3.1 レンズ6)
SEASONAL_WORD = re.compile(
    r"桜|さくら|梅|うめ|桃|藤|つつじ|ツツジ|ぼたん|牡丹|菖蒲|あやめ|"
    r"紫陽花|あじさい|朝顔|ほおずき|風鈴|七夕|紅葉|もみじ|雪|氷|"
    r"ひなまつり|雛|こいのぼり|鯉のぼり|菊|萩|蓮|ホタル|ほたる|"
    r"いちょう|コスモス|ひまわり|チューリップ|ゆり|ラベンダー"
)
# 「市」単独は地名 (岐阜市) と卸売市場を大量に拾うので使わない。
# 市(いち)を指す複合語だけを対象にする。
MARKET_WORD = re.compile(r"朝市|大市|達磨市|だるま市|骨董市|植木市|六斎市|楽市")

# サイトのナビゲーション見出し。行事名ではない。
NAV_LABEL = re.compile(
    r"^(観光|イベント|祭り?|まつり|催し|行事|文化|スポーツ|グルメ|自然|"
    r"歴史|見どころ|最新|一覧|トップ|ホーム|情報)"
    r"([・／/｜|,、]\s*(観光|イベント|祭り?|まつり|催し|行事|文化|スポーツ|"
    r"グルメ|自然|歴史|見どころ|最新|一覧|トップ|ホーム|情報))+$"
)
# 添付ファイルや更新日の注記
_NOISE = re.compile(
    r"\s*\[[^\]]*(PDF|KB|MB|形式)[^\]]*\]\s*|"
    r"^\s*(?:\d{4}\s*年)?\s*\d{1,2}\s*月\s*\d{1,2}\s*日更新\s*|"
    r"^\s*\d{4}[./-]\d{1,2}[./-]\d{1,2}\s*|"
    r"\s*（外部リンク）\s*|\s*\(外部リンク\)\s*"
)

# 明らかに祭りではないもの (AGENTS.md §2 の「単なる商業キャンペーン、展示会」)
EXCLUDE = re.compile(
    r"申請|届出|手続|募集要項|入札|契約|採用|職員|議会|条例|例規|"
    r"予防接種|健診|検診|相談会|説明会|講座|教室|セミナー|研修|"
    r"ごみ|廃棄物|下水|上水|工事|通行止|休館|閉館|開館時間|休業|"
    r"納税|課税|保険料|年金|補助金|給付金|助成|証明書|マイナンバー|"
    r"避難|防災訓練|不審者|注意報|警報|感染症|ワクチン|"
    r"結果|中止のお知らせ$|アンケート|パブリックコメント|"
    r"議事録|要綱|規則|計画書|報告書|answers?|検索結果"
)

# 文になっているもの・行政文書の断片は名称ではない
NOT_A_NAME = re.compile(
    r"[。、！？]|"
    r"(です|ます|ました|います|ください|しています|について|に関する)$|"
    r"お問い合わせ|ご案内|詳しくは|こちら|ホームページ|公式サイト|"
    r"募集|選挙|委員会|協議会|審議会|懇談会|推進|計画|方針|"
    r"公民館 |センター で|▼|"
    r"祝祭日|祭日|祝日|休祭日|葬祭|冠婚|"
    r"公募|作品募集|掲載|更新$|開会式|閉会式|表彰式"
)
# 末尾の一般的な括弧書き。行事名の一部ではない
_PAREN_SUFFIX = re.compile(
    r"\s*[（(](?:フォトギャラリー|ギャラリー|写真|動画|画像|"
    r"外部リンク|新しいウィンドウで開きます|PDF[^)）]*)[)）]\s*$"
)
# 「詩吟・三味線・民謡・尺八・舞・お囃子…」のような列挙は名称ではない
ENUMERATION = re.compile(r"([^・]+・){3,}")
# 名称の後ろに付く説明を切る位置
_CUT_AT = re.compile(r"\s*[~～〜:：\-−―–—]\s*|\s*\|\s*|\s*｜\s*")

# 名称の前後に付く飾りを落とす
_BRACKETS = "「」『』【】《》〈〉（）()［］[]｛｝{}"
_TRAILERS = re.compile(
    r"(の開催について|を開催します|が開催されます|を開催!?|開催のお知らせ|"
    r"のお知らせ|のご案内|の御案内|について|情報|ページ|一覧|最新|"
    r"が行われました|を行いました|は終了しました|レポート|"
    r"の中止について|中止のお知らせ|開催|"
    r"とは|のみどころ|の見どころ|を見る|の詳細を見る|ホーム|"
    r"当日チラシ|チラシ|日程|部門日程|スケジュール|会場案内|交通規制)+$"
)
_LEADERS = re.compile(
    r"^(令和\s*\d+\s*年度?|平成\s*\d+\s*年度?|\d{4}\s*年度?|"
    r"第\s*[0-9０-９一二三四五六七八九十百]+\s*回|"
    r"【[^】]*】)+\s*"
)

# 原文のままの日付表現を拾う (仕分けは normalize.py)
DATE_PATTERN = re.compile(
    r"(?:令和\s*\d+\s*年|平成\s*\d+\s*年|\d{4}\s*年)?\s*"
    r"(?:\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"
    # 「毎年」だけでは日程ではない (「毎年行われている市民の祭典」等)。
    # 実際の日付語に到達する場合だけ拾う。
    r"|毎年[^、。\n]{0,12}?"
    r"(?:\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?|第\s*[0-9０-９一二三四五六七八九]\s*[週]?[月火水木金土日]曜日?|上旬|中旬|下旬)"
    # 「毎年7月中旬から8月中旬」のような範囲を途中で切らない
    r"(?:\s*(?:上旬|中旬|下旬))?"
    r"(?:\s*(?:から|まで|〜|～|~|-|・)\s*[^、。\n]{0,10}?"
    r"(?:\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?(?:\s*(?:上旬|中旬|下旬))?"
    r"|上旬|中旬|下旬|[月火水木金土日]曜日?))?"
    r"|旧暦[^、。\n]{0,12}?\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"
    r"|[1-9]?[0-9]?月?第\s*[0-9０-９一二三四五六七八九]\s*[週]?[月火水木金土日]曜日?"
    r"|(?:春|夏|秋|冬)季"
    r"|(?:1[0-2]|[1-9])月(?:上旬|中旬|下旬))"
)
# 例年規則を示す表現。同じ文脈に複数の日付があるとき、こちらを優先する。
# 単に最初のマッチを採ると、ナビの「夏」を拾って
# 「毎年7月第4土曜」を取り逃す。
_RECURRING_HINT = re.compile(r"毎年|例年|第\s*[0-9０-９一二三四五六七八九]|旧暦|上旬|中旬|下旬|[月火水木金土日]曜")


def best_date(candidates: list[str]) -> str:
    for c in candidates:
        if _RECURRING_HINT.search(c):
            return c
    for c in candidates:
        if "月" in c:
            return c
    return candidates[0] if candidates else ""
VENUE_PATTERN = re.compile(
    r"[一-龥ぁ-んァ-ヶA-Za-z0-9ヶケヵノ]{2,12}"
    r"(?:神社|神宮|八幡宮|稲荷|大社|天満宮|東照宮|寺|院|大師|観音|不動尊|"
    r"公園|広場|会館|文化センター|市民会館|商店街|海岸|河川敷|球場|"
    r"駅前|通り|城址|城跡|旧跡)"
)


def clean(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment))


def normalize_space(text: str) -> str:
    return re.sub(r"[\s　]+", " ", text).strip()


_PAIRS = {"「": "」", "『": "』", "【": "】", "《": "》", "〈": "〉",
          "（": "）", "(": ")", "［": "］", "[": "]", "｛": "｝", "{": "}"}


def strip_wrapping(name: str) -> str:
    """外側を包んでいる括弧だけを剥がす。閉じ括弧だけを落とさない。"""
    changed = True
    while changed and len(name) >= 2:
        changed = False
        if name[0] in _PAIRS and name[-1] == _PAIRS[name[0]]:
            name, changed = name[1:-1].strip(), True
        elif name[-1] in _PAIRS.values() and _PAIRS.get(name[0]) != name[-1]:
            # 閉じ括弧が余っている = 対応する開き括弧が名称内にある。落とさない
            break
    return name


def clean_name(raw: str) -> str:
    name = _NOISE.sub(" ", normalize_space(raw))
    name = normalize_space(name)
    # 「祭り名 | サイト名」「祭り名 ～説明～」の後半を落とす
    head = _CUT_AT.split(name)[0].strip()
    if len(head) >= 3:
        name = head
    name = _LEADERS.sub("", name)
    # 年を落とすと「8月17日更新 …」が先頭に出てくるので、もう一度かける
    name = normalize_space(_NOISE.sub(" ", name))
    name = strip_wrapping(name.strip(" 　・:：-−―"))
    name = _PAREN_SUFFIX.sub("", name)
    name = _TRAILERS.sub("", name)
    name = strip_wrapping(name.strip(" 　・:：-−―"))
    # 開き括弧が閉じないまま終わっていたら、その手前で切る
    for opener, closer in _PAIRS.items():
        if opener in name and closer not in name:
            name = name.split(opener)[0].strip()
    return name


def looks_like_festival(name: str, municipality: str = "") -> str | None:
    """候補として拾う理由を返す。拾わないなら None。

    レンズごとに理由を残すのは、後で「どの経路で見つかったか」を
    カバレッジ表に出すため。
    """
    if not (3 <= len(name) <= 30):
        return None
    if EXCLUDE.search(name) or NOT_A_NAME.search(name) or NAV_LABEL.match(name):
        return None
    if ENUMERATION.match(name):
        return None
    # 「第57回水戸市芸術祭 映像部門日程」のような日程ページは行事そのものではない
    if re.search(r"部門|日程$|年報|統計|市場", name):
        return None
    if not re.search(r"[一-龥ぁ-んァ-ヶ]", name):
        return None
    # 自治体名そのものが祭り語を含むことがある (鉾田市の「鉾」)。
    # 判定は自治体名を除いた残りに対して行う。
    probe = name.replace(municipality, "")
    if municipality.endswith(("市", "町", "村", "区")):
        probe = probe.replace(municipality[:-1], "")
    if not probe.strip():
        return None
    if FESTIVAL_WORD.search(probe):
        return "festival_word"
    if SEASONAL_WORD.search(probe) and re.search(r"祭|まつり|展|会|ウィーク", probe):
        return "seasonal"
    if MARKET_WORD.search(probe):
        return "market"
    return None


def candidate_strings(text: str) -> list[tuple[str, str]]:
    """(文字列, 出所) を構造化された位置から集める。"""
    out: list[tuple[str, str]] = []

    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if m:
        out.append((clean(m.group(1)), "title"))

    for m in re.finditer(r"<h([1-4])[^>]*>(.*?)</h\1>", text, re.S | re.I):
        out.append((clean(m.group(2)), f"h{m.group(1)}"))

    for m in re.finditer(r"<a\b[^>]*>(.*?)</a>", text, re.S | re.I):
        out.append((clean(m.group(1)), "anchor"))

    for m in re.finditer(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", text, re.S | re.I):
        out.append((clean(m.group(1)), "table"))

    for m in re.finditer(r"<(?:item|entry)\b.*?</(?:item|entry)>", text, re.S | re.I):
        t = re.search(r"<title[^>]*>(.*?)</title>", m.group(0), re.S | re.I)
        if t:
            out.append((clean(t.group(1)), "feed_item"))

    for m in re.finditer(r"<li\b[^>]*>(.*?)</li>", text, re.S | re.I):
        frag = m.group(1)
        if len(frag) < 200 and "<li" not in frag:
            out.append((clean(frag), "list_item"))

    return out


def context_for(text: str, needle: str, width: int = 160) -> str:
    """名称の周辺テキスト。日付・会場の手掛かりを拾うために使う。"""
    flat = normalize_space(clean(text))
    idx = flat.find(needle)
    if idx < 0:
        return ""
    return flat[max(0, idx - width) : idx + len(needle) + width]


def extract_page(text: str, page_url: str, municipality: str = "") -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw, origin in candidate_strings(text):
        name = clean_name(raw)
        if name in seen:
            continue
        reason = looks_like_festival(name, municipality)
        if reason is None:
            continue
        seen.add(name)
        ctx = context_for(text, name) or normalize_space(raw)
        dates = DATE_PATTERN.findall(ctx)
        venues = [v for v in VENUE_PATTERN.findall(ctx) if v not in name]
        rows.append(
            {
                "name": name,
                "name_raw": normalize_space(raw)[:120],
                "origin": origin,
                "reason": reason,
                "date_text": normalize_space(best_date([normalize_space(d) for d in dates])),
                "venue": venues[0] if venues else "",
                "source_url": page_url,
                "context": ctx[:300],
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefecture")
    ap.add_argument("--municipality")
    args = ap.parse_args(argv)

    pages_path = WORK_DIR / "pages.tsv"
    if not pages_path.is_file():
        raise SystemExit("先に discover.py を実行してください")
    with pages_path.open(encoding="utf-8", newline="") as fh:
        pages = list(csv.DictReader(fh, delimiter="\t"))
    if args.prefecture:
        pages = [p for p in pages if p["prefecture"] == args.prefecture]
    if args.municipality:
        pages = [p for p in pages if p["municipality"] == args.municipality]

    fetcher = Fetcher(offline=True)  # ネットへは出ない
    out_path = WORK_DIR / "candidates.tsv"
    fields = [
        "prefecture", "municipality", "name", "name_raw", "origin", "reason",
        "date_text", "venue", "source_url", "page_title", "context",
    ]
    total = 0
    missing = 0
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.DictWriter(fh, delimiter="\t", lineterminator="\n", fieldnames=fields)
        w.writeheader()
        for page in pages:
            doc = fetcher.get(page["url"])
            if doc is None:
                missing += 1
                continue
            for row in extract_page(doc.text, page["url"], page["municipality"]):
                row.update(
                    prefecture=page["prefecture"],
                    municipality=page["municipality"],
                    page_title=page["title"],
                )
                for k in fields:
                    row[k] = str(row.get(k, "")).replace("\t", " ").replace("\n", " ")
                w.writerow({k: row[k] for k in fields})
                total += 1

    print(f"candidates.tsv: {total} 行 (ページ {len(pages)} 件、キャッシュ無し {missing} 件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
