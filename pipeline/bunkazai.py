"""S0.5 民俗文化財台帳: 文化庁「国指定文化財等データベース」から
無形の民俗文化財を種別×都道府県で全国分取得する。

**なぜこの工程を足したか。**
`extract.py` の `FESTIVAL_WORD` は茨城・愛知・静岡の実データから育てた語彙で、
本州の標準的な祭り名 (祇園祭・ねぶた・阿波おどり) は取れるが、地方の行事名を
ほとんど取れない。実測では

    沖縄       27語中 22語が不一致 (エイサー ハーリー パーントゥ シヌグ …)
    北海道     13語中 11語が不一致 (イオマンテ カムイノミ アイヌ古式舞踊 …)
    その他地方 26語中 24語が不一致 (なまはげ 御柱 六斎念仏 田遊び …)
    本州の有名どころ 7語中 0語が不一致

つまり**語彙は既知の祭りから作ったので、既知の祭りしか見つけられない**。
「知らなかった祭り・マイナーな祭りを探す」という当初目的に対して、これは
クロール予算ではなく検出器そのものが天井になっている。

そこで名前で祭りを判定するのをやめ、**指定の枠**で引く。「重要無形民俗
文化財」という枠は名前に依存しないので、パーントゥもアイヌ古式舞踊も
語彙を知らないまま出てくる。ここで得た名称群を県別語彙の**種**として使う
(語彙生成は `vocab.py`)。

国指定は全国で千件規模しかない。これは網羅の材料ではなく語彙の種である。

使い方:
    python pipeline/bunkazai.py --build          # 全種別×全都道府県
    python pipeline/bunkazai.py --build --prefecture 沖縄県
    python pipeline/bunkazai.py --build --resume  # 既存TSVに無い県だけ追加
"""

from __future__ import annotations

import argparse
import html
import http.cookiejar
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from net import USER_AGENT, build_ssl_context  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = REPO_ROOT / "registry"
OUT_TSV = REGISTRY_DIR / "bunkazai.tsv"

BASE = "https://kunishitei.bunka.go.jp"
INDEX_URL = BASE + "/bsys/index"
SEARCH_URL = BASE + "/bsys/searchlist"

# robots.txt は 404 (= 制約なし) を確認済み。それでも同一ホスト1秒は守る。
DELAY = 1.0

# 引く種別。無形の民俗文化財だけを対象にする。有形民俗 (301/311) は
# 「もの」であって行事ではないので入れない。
KINDS = {
    "302": "重要無形民俗文化財",
    "312": "記録作成等の措置を講ずべき無形の民俗文化財",
    "322": "登録無形民俗文化財",
}

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]

COLUMNS = ["kind_code", "kind_name", "prefecture", "name", "category", "subcategory", "detail_url"]

_TOKEN = re.compile(r'name="_csrfToken"[^>]*value="([^"]+)"')
_ROW = re.compile(r"<tr[^>]*class=\"result(?:back)?\d*\"[^>]*>(.*?)</tr>", re.S | re.I)
_DETAIL = re.compile(r'href="(/heritage/detail/(\d+)/(\d+))"[^>]*>(.*?)</a>', re.S)
_TD = re.compile(r'<td[^>]*class="result-td"[^>]*>(.*?)</td>', re.S)
# 「9 件中 1 件から 9 件のデータです。」
_COUNT = re.compile(r"([\d,]+)\s*件中\s*([\d,]+)\s*件から\s*([\d,]+)\s*件")


def text_of(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


class Session:
    """CSRFトークン付きのPOST検索。トークンはセッションクッキーと対になる。"""

    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=build_ssl_context()),
        )
        self.token = ""
        self._last = 0.0

    def _throttle(self) -> None:
        wait = DELAY - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def open_index(self) -> None:
        self._throttle()
        req = urllib.request.Request(INDEX_URL, headers={"User-Agent": USER_AGENT})
        body = self.opener.open(req, timeout=60).read().decode("utf-8", "replace")
        m = _TOKEN.search(body)
        if not m:
            raise RuntimeError("CSRFトークンが取れない。フォームの形が変わった可能性がある")
        self.token = m.group(1)

    def search(self, kind: str, prefecture: str, page: int) -> str:
        if not self.token:
            self.open_index()
        data = {
            "_method": "POST",
            "_csrfToken": self.token,
            "screen_id": "index",
            "page_no": str(page),
            "freeword": "",
            "register_sub_id": kind,
            "seat_pref_" + kind: prefecture,
        }
        body = urllib.parse.urlencode(data, encoding="utf-8").encode()
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": INDEX_URL,
        }
        for attempt in (1, 2):
            self._throttle()
            try:
                req = urllib.request.Request(SEARCH_URL, data=body, headers=headers)
                return self.opener.open(req, timeout=60).read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                # 403 はトークン切れ。1度だけ取り直す。
                if exc.code == 403 and attempt == 1:
                    self.open_index()
                    data["_csrfToken"] = self.token
                    body = urllib.parse.urlencode(data, encoding="utf-8").encode()
                    continue
                raise
        raise RuntimeError("unreachable")


def parse_rows(page_html: str, kind: str, prefecture: str) -> list[dict]:
    """結果表の行を返す。

    同じ行が2度出る (PC用とモバイル用のマークアップ)。detail の URL で
    重複を落とす。名称セルに含まれる img の alt は text_of で消える。
    """
    out: dict[str, dict] = {}
    for row in _ROW.findall(page_html):
        # 1行に detail へのリンクが2本ある。1本目は矢印画像だけで
        # テキストを持たないので、名称のある方を採る。
        m = None
        for cand in _DETAIL.finditer(row):
            if text_of(cand.group(4)):
                m = cand
                break
        if m is None:
            continue
        name = text_of(m.group(4))
        cells = [text_of(c) for c in _TD.findall(row)]
        # セル並び: 種別 / 分類 / 小分類 / 都道府県
        kind_name = cells[0] if len(cells) > 0 else KINDS.get(kind, "")
        category = cells[1] if len(cells) > 1 else ""
        subcategory = cells[2] if len(cells) > 2 else ""
        url = BASE + m.group(1)
        out[url] = {
            "kind_code": kind,
            "kind_name": kind_name or KINDS.get(kind, ""),
            "prefecture": prefecture,
            "name": name,
            "category": category,
            "subcategory": subcategory,
            "detail_url": url,
        }
    return list(out.values())


def total_of(page_html: str) -> tuple[int, int]:
    """(全件数, このページの最終件番) を返す。取れなければ (0, 0)。"""
    m = _COUNT.search(text_of(page_html))
    if not m:
        return (0, 0)
    return (int(m.group(1).replace(",", "")), int(m.group(3).replace(",", "")))


def harvest(session: Session, kind: str, prefecture: str) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    page = 1
    while page <= 40:  # 1県1種別で40ページ (数千件) を超えることはない
        body = session.search(kind, prefecture, page)
        total, last = total_of(body)
        fresh = [r for r in parse_rows(body, kind, prefecture) if r["detail_url"] not in seen]
        for r in fresh:
            seen.add(r["detail_url"])
        rows.extend(fresh)
        if not fresh or total == 0 or last >= total:
            break
        page += 1
    return rows


def read_existing() -> list[dict]:
    if not OUT_TSV.exists():
        return []
    lines = OUT_TSV.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    return [dict(zip(header, ln.split("\t"))) for ln in lines[1:] if ln.strip()]


def write_rows(rows: list[dict]) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    order = {p: i for i, p in enumerate(PREFECTURES)}
    rows = sorted(rows, key=lambda r: (order.get(r["prefecture"], 99), r["kind_code"], r["name"]))
    body = "\t".join(COLUMNS) + "\n"
    body += "".join("\t".join(r.get(c, "").replace("\t", " ") for c in COLUMNS) + "\n" for r in rows)
    tmp = OUT_TSV.with_suffix(".tsv.tmp")
    tmp.write_text(body, encoding="utf-8", newline="\n")
    tmp.replace(OUT_TSV)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", action="store_true", help="取得して registry/bunkazai.tsv を書く")
    ap.add_argument("--prefecture", action="append", help="対象都道府県 (既定: 全国)")
    ap.add_argument("--resume", action="store_true", help="既存TSVにある県を飛ばして追記")
    args = ap.parse_args(argv)

    if not args.build:
        ap.print_help()
        return 0

    targets = args.prefecture or PREFECTURES
    unknown = [p for p in targets if p not in PREFECTURES]
    if unknown:
        print("不明な都道府県: " + ", ".join(unknown), file=sys.stderr)
        return 2

    existing = read_existing() if args.resume else []
    done = {r["prefecture"] for r in existing}
    if args.resume:
        targets = [p for p in targets if p not in done]
        print("再開: %d県は取得済み、残り %d県" % (len(done), len(targets)))

    session = Session()
    session.open_index()

    collected = list(existing)
    for i, pref in enumerate(targets, 1):
        got = 0
        for kind in KINDS:
            try:
                rows = harvest(session, kind, pref)
            except Exception as exc:  # 1県の失敗で全体を止めない
                print("  ! %s %s: %r" % (pref, KINDS[kind], exc), file=sys.stderr)
                continue
            collected.extend(rows)
            got += len(rows)
        print("[%2d/%2d] %-5s %3d件" % (i, len(targets), pref, got), flush=True)
        write_rows(collected)  # 落ちても進捗が消えないよう毎県書く

    by_kind: dict[str, int] = {}
    for r in collected:
        by_kind[r["kind_name"]] = by_kind.get(r["kind_name"], 0) + 1
    print()
    print("合計 %d件 -> %s" % (len(collected), OUT_TSV))
    for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print("  %-30s %4d" % (k, n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
