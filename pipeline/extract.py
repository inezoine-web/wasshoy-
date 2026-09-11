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
    r"フェスティバル|フェスタ|カーニバル|"
    # 実データで取りこぼした語 (茨城パイロットの評価から追加)
    r"大道芸|人形浄瑠璃|綱火|盆綱|わらじ|流鏑馬|献灯|燈明"
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

# 県別の行事語彙 (registry/vocab_regional.tsv)。main() が --prefecture から
# 読み込む。既存の FESTIVAL_WORD は既知の祭りから育てた語彙なので、
# 地方の行事名をほとんど拾えない (沖縄81%・その他地方92%が不一致)。
# 民俗文化財の指定名称から機械生成した語をここで足す。
REGIONAL_WORD: re.Pattern | None = None


def set_regional_vocab(prefecture: str) -> int:
    """県別語彙を読み込む。読み込んだ語数を返す。"""
    global REGIONAL_WORD
    try:
        import vocab
        REGIONAL_WORD = vocab.regional_pattern(prefecture)
        return len(vocab.load_terms(prefecture))
    except Exception:
        REGIONAL_WORD = None
        return 0

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
    # 「〜を踊る」「〜をかつごう」「〜をお見逃しなく」= 文であって名前ではない。
    # 公開データで3件見つかり、3件とも断片だった (誤検出は0)。
    r"を[ぁ-ん一-龥々]{1,6}(?:る|う|く|す|つ|ぬ|ぶ|む|ます|した|する|なく)$|"
    r"お問い合わせ|ご案内|詳しくは|こちら|ホームページ|公式サイト|"
    r"募集|選挙|委員会|協議会|審議会|懇談会|推進|計画|方針|"
    r"公民館 |センター で|▼|"
    r"祝祭日|祭日|祝日|休祭日|葬祭|冠婚|"
    r"公募|作品募集|掲載|更新$|開会式|閉会式|表彰式|"
    # 名称が一般語そのもの (「盆踊り」「例大祭」「お祭り」)。どの祭りかを
    # 指せないので記録にならない。名称全体が一般語の場合だけなので誤爆は無い。
    # 公開データに30件、東京の判定済みに15件あった (2026-09-11)。
    # 種目名 (神楽・獅子舞・囃子・盆踊り) は入れない。東文研の指定名称に
    # 「神楽」197件「獅子舞」35件「盆踊り」5件が、そのままの名で存在する。
    r"^(?:お|御)?(?:祭り|まつり|祭|祭典|祭礼|例祭|例大祭|大祭|夏祭り|秋祭り|"
    r"春祭り|花火大会|花火|イベント|フェスティバル|フェスタ)$"
)
# 末尾の一般的な括弧書き。行事名の一部ではない
_PAREN_SUFFIX = re.compile(
    r"\s*[（(](?:フォトギャラリー|ギャラリー|写真|動画|画像|"
    r"外部リンク|新しいウィンドウで開きます|PDF[^)）]*|"
    r"終了|終了しました|中止|延期|予定)[)）]\s*$"
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
    r"が行われました|を行いました|は?終了しました|終了|レポート|"
    r"を開催します|を開催しました|イベント|"
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
    # 「…を開催します！」の「！」を先に落とさないと、末尾を見る _TRAILERS が効かない
    name = name.rstrip("！!？?。 　")
    name = _TRAILERS.sub("", name)
    name = name.rstrip("！!？?。 　")
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
    if not (2 <= len(name) <= 30):
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
    if REGIONAL_WORD is not None and REGIONAL_WORD.search(probe):
        return "regional_vocab"
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

    # 告知文の中で鉤括弧に入っている固有名。自治体サイトは行事名を
    # 「常陸大津の御船祭」がユネスコ…、国指定重要無形民俗文化財「綱火」が…
    # のように文中へ埋める。文ごと弾くと名称まで失われる。
    for m in re.finditer(r"[「『]([^「」『』\n]{2,30})[」』]", clean(text)):
        out.append((m.group(1), "quoted"))

    return out


def context_for(flat: str, needle: str, width: int = 160) -> str:
    """名称の周辺テキスト。日付・会場の手掛かりを拾うために使う。

    引数は整形済みの平文。ページ全体の整形は1ページにつき1回だけ行う。
    候補ごとに整形し直していたため、1ページに数十候補あると二乗的に
    遅くなり、6850ページの抽出が10分以上かかっていた。
    """
    idx = flat.find(needle)
    if idx < 0:
        return ""
    return flat[max(0, idx - width) : idx + len(needle) + width]


# --- 所在地の列を持つ表 ------------------------------------------------------
# 県サイトには「名称 | 所在地 | 概要」の形で行事を並べた一覧がある。千葉県の
# 伝統芸能一覧 (kkbunka/b-shigen/06dentou/) は266行あり、県内の民俗芸能を
# 市町村つきで網羅している。これを平文として読むと2つの不具合が起きる:
#
#   - 所在が落ちる。`assign_municipality` は文脈160字に市町村名が1つだけの
#     ときしか割り当てず、密な表では隣の行の市町村まで窓に入って判断不能になる。
#     千葉で181件が (県全域) のまま残り、IDが付かず公開できなかった
#   - 「概要」列の文が候補になる。「10歳前後の少年によって奉納」
#     「20数基の御輿が繰り出す勇壮な祭り」がそのまま名称として出た
#
# 表として読めば両方消える。所在地はセルから取り、名称の列だけを候補にする。
_TABLE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_PLACE_HEADER = re.compile(r"^(?:所在地|所在|市町村|市町村名|市町名|市区町村|開催地|地域)$")
_NAME_HEADER = re.compile(r"^(?:名称|名前|行事名|祭り名|祭礼名|芸能名|文化財名|指定文化財名|"
                          r"行事|祭り|まつり|祭礼)$")


def _cell_text(fragment: str) -> str:
    return normalize_space(clean(fragment))


def _place_to_municipality(place: str, muni_names: list[str]) -> str:
    """所在地セルの文字列を市町村名に寄せる。「市原市」「市原市能満」「市原」を許す。"""
    p = place.strip()
    if not p or p in ("-", "－", "―", "ー"):
        return ""
    for n in muni_names:
        if p == n or p.startswith(n):
            return n
    for n in muni_names:
        stem = n[:-1] if n[-1] in "市町村区" else n
        if len(stem) >= 2 and (p == stem or p.startswith(stem + " ") or p.startswith(stem + "・")):
            return n
    return ""


def attributed_table_rows(text: str, muni_names: list[str]) -> tuple[list[tuple[str, str, str]], str]:
    """(名称, 市町村, 行の文脈) の並びと、それらの表を除いた本文を返す。

    見出し行に「名称」系と「所在地」系の両方がある表だけを対象にする。
    対象の表は本文から取り除き、平文経路が概要列の文を拾わないようにする。
    """
    if not muni_names:
        return [], text
    found: list[tuple[str, str, str]] = []
    keep = text
    for m in _TABLE.finditer(text):
        trs = _TR.findall(m.group(0))
        if len(trs) < 3:
            continue
        head = [_cell_text(c) for c in _TD.findall(trs[0])]
        ni = next((i for i, c in enumerate(head) if _NAME_HEADER.match(c)), None)
        pi = next((i for i, c in enumerate(head) if _PLACE_HEADER.match(c)), None)
        if ni is None or pi is None:
            continue
        got = 0
        for tr in trs[1:]:
            cells = [_cell_text(c) for c in _TD.findall(tr)]
            if len(cells) <= max(ni, pi):
                continue
            name, muni = cells[ni], _place_to_municipality(cells[pi], muni_names)
            if name and muni:
                found.append((name, muni, " ".join(cells)))
                got += 1
        if got:
            keep = keep.replace(m.group(0), " ")
    return found, keep


def extract_page(text: str, page_url: str, municipality: str = "",
                 muni_names: list[str] | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    # 所在地つきの表を先に読む。ここで取れた行は所在が確定している。
    table_rows, text = attributed_table_rows(text, muni_names or [])
    for raw, muni, ctx_row in table_rows:
        name = clean_name(raw)
        if name in seen:
            continue
        reason = looks_like_festival(name, muni)
        if reason is None:
            continue
        seen.add(name)
        dates = DATE_PATTERN.findall(ctx_row)
        venues = [v for v in VENUE_PATTERN.findall(ctx_row) if v not in name]
        rows.append(
            {
                "name": name,
                "name_raw": normalize_space(raw)[:120],
                "origin": "table",
                "reason": reason,
                "date_text": normalize_space(best_date([normalize_space(d) for d in dates])),
                "venue": venues[0] if venues else "",
                "source_url": page_url,
                "context": ctx_row[:300],
                "municipality": muni,
            }
        )

    flat = normalize_space(clean(text))  # ページ全体の整形は1回だけ
    for raw, origin in candidate_strings(text):
        name = clean_name(raw)
        if name in seen:
            continue
        reason = looks_like_festival(name, municipality)
        if reason is None:
            continue
        seen.add(name)
        ctx = context_for(flat, name) or normalize_space(raw)
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


PREFECTURE_WIDE = "(県全域)"


def load_municipality_names(prefecture: str) -> list[str]:
    path = REPO_ROOT / "registry" / "municipalities.tsv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return [
            r["municipality"]
            for r in csv.DictReader(fh, delimiter="\t")
            if r["prefecture"] == prefecture
        ]


def assign_municipality(context: str, names: list[str]) -> str:
    """県単位レンズで拾った行事に所在市町村を割り当てる。

    県教育委員会の文化財一覧には所在欄が無いことがある。旧手法では
    これが理由で19件を登録できずに落としていた (前回レポート §7-7)。
    周辺テキストに市町村名が1つだけ現れる場合にのみ採用し、
    複数現れる場合は判断できないので割り当てない。推測しない。
    """
    hits = {n for n in names if n in context}
    if len(hits) == 1:
        return hits.pop()
    # 「大子町の…」と「大子」だけの表記が混ざるため、接尾辞なしでも見る
    bare = {n for n in names if len(n) > 2 and n[:-1] in context}
    if len(bare) == 1:
        return bare.pop()
    return ""


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
    # 県単位レンズで拾った行事に所在市町村を割り当てるための名簿
    muni_names = load_municipality_names(args.prefecture) if args.prefecture else []
    if args.prefecture:
        n = set_regional_vocab(args.prefecture)
        print("県別語彙: %s %d語" % (args.prefecture, n))
    pref_wide_rows = 0
    pref_wide_assigned = 0
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
            # キャッシュはリクエストURLで引く (リダイレクト後のURLでは当たらない)
            doc = fetcher.get(page.get("fetch_url") or page["url"])
            if doc is None:
                missing += 1
                continue
            muni = page["municipality"]
            for row in extract_page(doc.text, page["url"], "" if muni == PREFECTURE_WIDE else muni,
                                    muni_names):
                resolved = muni
                if muni != PREFECTURE_WIDE:
                    row.pop("municipality", None)
                if muni == PREFECTURE_WIDE:
                    pref_wide_rows += 1
                    # 所在地の列を持つ表から取れた行は、そのセルの値が確定値。
                    # 文脈からの推定 (assign_municipality) より優先する。
                    resolved = row.pop("municipality", "") or assign_municipality(
                        f"{row['context']} {page['title']}", muni_names
                    ) or PREFECTURE_WIDE
                    if resolved != PREFECTURE_WIDE:
                        pref_wide_assigned += 1
                row.update(
                    prefecture=page["prefecture"],
                    municipality=resolved,
                    page_title=page["title"],
                )
                for k in fields:
                    row[k] = str(row.get(k, "")).replace("\t", " ").replace("\n", " ")
                w.writerow({k: row[k] for k in fields})
                total += 1

    print(f"candidates.tsv: {total} 行 (ページ {len(pages)} 件、キャッシュ無し {missing} 件)")
    if pref_wide_rows:
        print(
            f"  県単位レンズ: {pref_wide_rows} 行、うち所在市町村を割り当てられた "
            f"{pref_wide_assigned} 行 (残りは {PREFECTURE_WIDE} のまま S4 へ)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
