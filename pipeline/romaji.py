"""かな → ヘボン式ローマ字の決定的変換。

安定IDの生成に使う。IDは同一性を決める値なので、モデルに推測させず
ここで決定的に生成する。読みが取れない文字が残った場合は変換せず
None を返し、呼び出し側が PENDING として保留する。

自己テスト:
    python pipeline/romaji.py
"""

from __future__ import annotations

import re
import sys

# 2文字の拗音を先に引くため、長いキーから順にマッチさせる
_TABLE: dict[str, str] = {
    "きゃ": "kya", "きゅ": "kyu", "きょ": "kyo",
    "しゃ": "sha", "しゅ": "shu", "しょ": "sho",
    "ちゃ": "cha", "ちゅ": "chu", "ちょ": "cho",
    "にゃ": "nya", "にゅ": "nyu", "にょ": "nyo",
    "ひゃ": "hya", "ひゅ": "hyu", "ひょ": "hyo",
    "みゃ": "mya", "みゅ": "myu", "みょ": "myo",
    "りゃ": "rya", "りゅ": "ryu", "りょ": "ryo",
    "ぎゃ": "gya", "ぎゅ": "gyu", "ぎょ": "gyo",
    "じゃ": "ja", "じゅ": "ju", "じょ": "jo",
    "ぢゃ": "ja", "ぢゅ": "ju", "ぢょ": "jo",
    "びゃ": "bya", "びゅ": "byu", "びょ": "byo",
    "ぴゃ": "pya", "ぴゅ": "pyu", "ぴょ": "pyo",
    "ふぁ": "fa", "ふぃ": "fi", "ふぇ": "fe", "ふぉ": "fo",
    "うぁ": "wa", "うぃ": "wi", "うぇ": "we", "うぉ": "wo",
    "てぃ": "ti", "でぃ": "di", "とぅ": "tu", "どぅ": "du",
    "しぇ": "she", "ちぇ": "che", "じぇ": "je",
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "ゐ": "i", "ゑ": "e", "を": "o",
    "ん": "n",
    "ゔ": "vu",
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
    "ゃ": "ya", "ゅ": "yu", "ょ": "yo",
}

_MAX_KEY = 2
_KANA_ONLY = re.compile(r"^[ぁ-ゖァ-ヺーゝゞ・　\s]+$")


_HALFWIDTH = (
    "｡｢｣､･ｦｧｨｩｪｫｬｭｮｯｰｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝﾞﾟ"
)
_FULLWIDTH = (
    "。「」、・ヲァィゥェォャュョッーアイウエオカキクケコサシスセソタチツテト"
    "ナニヌネノハヒフヘホマミムメモヤユヨラリルレロワン゛゜"
)
_HW_MAP = {h: f for h, f in zip(_HALFWIDTH, _FULLWIDTH)}
_DAKUTEN = {
    "カ": "ガ", "キ": "ギ", "ク": "グ", "ケ": "ゲ", "コ": "ゴ",
    "サ": "ザ", "シ": "ジ", "ス": "ズ", "セ": "ゼ", "ソ": "ゾ",
    "タ": "ダ", "チ": "ヂ", "ツ": "ヅ", "テ": "デ", "ト": "ド",
    "ハ": "バ", "ヒ": "ビ", "フ": "ブ", "ヘ": "ベ", "ホ": "ボ",
    "ウ": "ヴ",
}
_HANDAKUTEN = {"ハ": "パ", "ヒ": "ピ", "フ": "プ", "ヘ": "ペ", "ホ": "ポ"}


def from_halfwidth_katakana(text: str) -> str:
    """半角カナを全角カナへ。濁点・半濁点は前の文字に合成する。

    総務省の市区町村コード表は読みを半角カナで持っている (ﾎｯｶｲﾄﾞｳ)。
    """
    out: list[str] = []
    for ch in text:
        full = _HW_MAP.get(ch, ch)
        if full == "゛" and out and out[-1] in _DAKUTEN:
            out[-1] = _DAKUTEN[out[-1]]
        elif full == "゜" and out and out[-1] in _HANDAKUTEN:
            out[-1] = _HANDAKUTEN[out[-1]]
        else:
            out.append(full)
    return "".join(out)


def to_hiragana(text: str) -> str:
    """カタカナをひらがなに寄せる。長音符はそのまま残す。"""
    out = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:  # ァ-ヶ
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return "".join(out)


def _collapse_long_vowels(kana: str) -> str:
    """長音を短母音に畳む。

    自治体名・祭り名のローマ字表記の慣行に合わせる。
    既存IDが実際にこの形になっている:
      常陸太田 ひたちおおた -> hitachiota
      常陸大宮 ひたちおおみや -> hitachiomiya
      龍ケ崎   りゅうがさき   -> ryugasaki
    「えい」は畳まない (例大祭 れいたいさい -> reitaisai)。
    """
    kana = kana.replace("おう", "お").replace("おお", "お")
    kana = kana.replace("こう", "こ").replace("そう", "そ").replace("とう", "と")
    kana = kana.replace("のう", "の").replace("ほう", "ほ").replace("もう", "も")
    kana = kana.replace("よう", "よ").replace("ろう", "ろ").replace("ごう", "ご")
    kana = kana.replace("ぞう", "ぞ").replace("どう", "ど").replace("ぼう", "ぼ")
    kana = kana.replace("ぽう", "ぽ").replace("しょう", "しょ").replace("じょう", "じょ")
    kana = kana.replace("ちょう", "ちょ").replace("きょう", "きょ").replace("ぎょう", "ぎょ")
    kana = kana.replace("ひょう", "ひょ").replace("びょう", "びょ").replace("りょう", "りょ")
    kana = kana.replace("みょう", "みょ").replace("にょう", "にょ").replace("ぴょう", "ぴょ")
    kana = kana.replace("しゅう", "しゅ").replace("じゅう", "じゅ").replace("ちゅう", "ちゅ")
    kana = kana.replace("きゅう", "きゅ").replace("ぎゅう", "ぎゅ").replace("りゅう", "りゅ")
    kana = kana.replace("ひゅう", "ひゅ").replace("びゅう", "びゅ").replace("みゅう", "みゅ")
    kana = kana.replace("にゅう", "にゅ").replace("ぴゅう", "ぴゅ").replace("ゅう", "ゅ")
    kana = kana.replace("うう", "う")
    return kana


def is_kana(text: str) -> bool:
    return bool(text) and bool(_KANA_ONLY.match(text))


def romanize(kana: str) -> str | None:
    """かな文字列をローマ字へ。かな以外が混じっていれば None。

    None は「読みが確定できない」の意味であり、推測で埋めない。
    """
    if not is_kana(kana):
        return None
    kana = to_hiragana(kana)
    kana = re.sub(r"[・　\s]+", "", kana)
    kana = re.sub(r"[ゝゞ]", "", kana)
    kana = _collapse_long_vowels(kana)

    out: list[str] = []
    i = 0
    n = len(kana)
    while i < n:
        if kana[i] == "ー":  # 長音符は直前の母音を伸ばさずに落とす
            i += 1
            continue
        if kana[i] == "っ":
            # 促音: 次の子音を重ねる
            nxt = None
            for size in (_MAX_KEY, 1):
                if kana[i + 1 : i + 1 + size] in _TABLE:
                    nxt = _TABLE[kana[i + 1 : i + 1 + size]]
                    break
            if nxt:
                # ch- は tch- とするのがヘボン式
                out.append("t" if nxt.startswith("ch") else nxt[0])
            i += 1
            continue
        matched = False
        for size in (_MAX_KEY, 1):
            chunk = kana[i : i + size]
            if chunk in _TABLE:
                out.append(_TABLE[chunk])
                i += size
                matched = True
                break
        if not matched:
            return None

    text = "".join(out)
    # ん + b/m/p は m に (ヘボン式)
    text = re.sub(r"n(?=[bmp])", "m", text)
    return text


def slugify(romaji: str) -> str:
    """ローマ字をslug用に整える。"""
    s = romaji.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# --------------------------------------------------------------------- 自己テスト

_CASES = [
    ("ほこた", "hokota"),
    ("もりや", "moriya"),
    ("こが", "koga"),
    ("ひたちおおた", "hitachiota"),
    ("ひたちおおみや", "hitachiomiya"),
    ("りゅうがさき", "ryugasaki"),
    ("つくばみらい", "tsukubamirai"),
    ("ひたちなか", "hitachinaka"),
    ("かすみがうら", "kasumigaura"),
    ("つちうら", "tsuchiura"),
    ("まつり", "matsuri"),
    ("ぎおん", "gion"),
    ("ばやし", "bayashi"),
    ("ささら", "sasara"),
    ("れいたいさい", "reitaisai"),
    ("はなび", "hanabi"),
    ("ぼんおどり", "bonodori"),
    ("しんぶ", "shimbu"),
    ("にっこう", "nikko"),
    ("ホコタ", "hokota"),
    ("さっぽろ", "sapporo"),
    ("ちよだ", "chiyoda"),
    ("しょうぶ", "shobu"),
]

_HW_CASES = [
    ("ﾎｯｶｲﾄﾞｳ", "ホッカイドウ"),
    ("ｻｯﾎﾟﾛｼ", "サッポロシ"),
    ("ｵｵﾀﾞﾃｼ", "オオダテシ"),
    ("ﾘｭｳｶﾞｻｷｼ", "リュウガサキシ"),
]


def _self_test() -> int:
    bad = 0
    for kana, want in _CASES:
        got = romanize(kana)
        flag = "ok " if got == want else "NG "
        if got != want:
            bad += 1
        print(f"  {flag}{kana} -> {got}  (want {want})")
    for hw, want in _HW_CASES:
        got = from_halfwidth_katakana(hw)
        if got != want:
            bad += 1
            print(f"  NG {hw} -> {got} (want {want})")
        else:
            print(f"  ok {hw} -> {got}")
    # かな以外は None を返すこと
    for bad_input in ("鉾田", "鉾田まつり", "", "abc"):
        got = romanize(bad_input)
        if got is not None:
            bad += 1
            print(f"  NG {bad_input!r} -> {got} (want None)")
    print(f"failures: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_self_test())
