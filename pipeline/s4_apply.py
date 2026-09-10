"""S4-2 判定結果の取り込み: AIの返答を機械チェックしてから適用する。

AIの返答をそのまま信じない。過去に、行の連番をリセットして返す、
ローマ字化せず漢字を残す、といった逸脱が実際に起きている。
ここで次を必ず確認する。

  - 入力の id が過不足なく1回ずつ現れるか
  - verdict が既定の語彙か
  - alias_of が同一市町村の keep 行を指しているか (循環していないか)
  - reading がひらがなだけか
  - 生成した slug が `^[a-z0-9-]+$` を満たし、重複しないか

**slug は romaji.py が生成する。** AIにはかなの読みだけを返させ、
ローマ字化は委託しない (識別子は後から直すと同一性が壊れるため)。

使い方:
    python pipeline/s4_apply.py --prefecture 茨城県
    python pipeline/s4_apply.py --prefecture 茨城県 --strict   # 未回答があれば失敗
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from normalize import dedup_key  # noqa: E402
from romaji import is_kana, romanize, slugify  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / "work"
REGISTRY_DIR = REPO_ROOT / "registry"
BATCH_DIR = WORK_DIR / "s4"

VERDICTS = {"keep", "drop", "unsure"}
SLUG_RE = re.compile(r"^[a-z0-9-]+$")
PREFECTURE_WIDE = "(県全域)"


def load_index() -> dict[str, dict[str, str]]:
    path = BATCH_DIR / "index.tsv"
    if not path.is_file():
        raise SystemExit("先に s4_prepare.py を実行してください")
    with path.open(encoding="utf-8", newline="") as fh:
        return {r["id"]: r for r in csv.DictReader(fh, delimiter="\t")}


_ID = re.compile(r"^\d{4,6}$")
_KANA = re.compile(r"^[ぁ-んゔー・\s]+$")
_MUNI = re.compile(r"[^\s]+[市町村区]$")
_VERDICT = re.compile(r"^(keep|drop|unsure)$", re.I)


def parse_verdict_line(cells: list[str]) -> dict[str, str] | None:
    """1行を (id, verdict, reason, alias_of, municipality, reading) に直す。

    **位置で読んではいけない。** 実測では同じ工程の返答で列数が 3〜7 に散った。
    末尾の空欄を落とすエージェント、alias_of を空けずに読みを前へ詰める
    エージェントが混在する。列数を揃えろと指示しても揃わないので、
    受け側で内容から判別する。判別は排他的で曖昧さが無い:

        5桁の数字        -> id か alias_of
        keep/drop/unsure -> verdict
        ひらがなだけ      -> reading
        末尾が市町村区    -> municipality
        それ以外の英字    -> reason
    """
    cells = [c.strip() for c in cells]
    if not cells or not _ID.match(cells[0]):
        return None
    out = {"id": cells[0], "verdict": "", "reason": "",
           "alias_of": "", "municipality": "", "reading": ""}
    for c in cells[1:]:
        if not c:
            continue
        if not out["verdict"] and _VERDICT.match(c):
            out["verdict"] = c.lower()
        elif _ID.match(c):
            out["alias_of"] = c
        elif _KANA.match(c):
            out["reading"] = c.strip()
        elif _MUNI.match(c):
            out["municipality"] = c
        elif not out["reason"]:
            out["reason"] = c
    return out


def load_verdicts() -> tuple[dict[str, dict[str, str]], list[str]]:
    verdicts: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    files = sorted(BATCH_DIR.glob("s4_verdict_*.tsv"))
    if not files:
        raise SystemExit(f"{BATCH_DIR} に s4_verdict_*.tsv がありません")
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            row = parse_verdict_line(line.split("\t"))
            if row is None:
                continue  # ヘッダ行や空行
            rid = row["id"]
            if rid in verdicts:
                problems.append(f"id {rid} が {path.name} で重複している")
                continue
            verdicts[rid] = row
    return verdicts, problems


# 一覧表や投稿ナビ由来の飾りが名称に残る。抽出時には行事名との区別が
# つかないが、S4で keep と判定できた後なら機械的に落とせる。
# 落とした元の名称は aliases に残すので情報は失われない。
_NAME_FIXES: list[tuple[re.Pattern[str], str]] = [
    # 「新しい投稿 …」「NEW! F …」のような投稿ナビの語
    (re.compile(r"^(?:新しい投稿|古い投稿|NEW!\s*\S?)\s*"), ""),
    # 一覧表の連番 「4.塚崎の獅子舞」「39 立延の盆綱」
    (re.compile(r"^\s*\d{1,3}\s*[.．、]\s*"), ""),
    (re.compile(r"^\s*\d{1,3}\s+(?=[^\d\s])"), ""),
    # 「県指定 富田のささら 所在地 石岡市国府5」→ 中身だけ残す
    (re.compile(r"^(?:国|県|市|町|村)指定\s+"), ""),
    (re.compile(r"\s+所在地\s+.*$"), ""),
    # 「ユネスコ無形文化遺産・国指定重要無形民俗文化財「日立風流物」」
    (re.compile(
        r"^(?:ユネスコ無形文化遺産|国指定重要無形民俗文化財|"
        r"茨城県指定無形民俗文化財|[^「]*?指定[^「]*?文化財)[・\s]*"
        r"[「『]([^」』]+)[」』].*$"), r"\1"),
    # 末尾の括弧注記 「三和祇園ばやし（無形民俗文化財）」
    (re.compile(
        r"\s*[（(](?:無形民俗文化財|[国県市町村]指定|"
        r"pdf[^)）]*|jpg[^)）]*)[)）]\s*$", re.I), ""),
]

# 「保存会」は行事そのものではないが行事の存在を示す。名称からは落として
# 行事名に寄せ、元の名称は aliases に残す (ユーザーの判断)。
_ORG_SUFFIX = re.compile(r"(?:保存|連合|振興)?(?:保存会|連合会|振興会|会)$")


def clean_display_name(name: str, is_organization: bool) -> str:
    """一覧表・投稿ナビ由来の飾りを落として行事名に寄せる。

    S2の段階では行事名との区別がつかないが、S4で keep と判定できた後なら
    落としてよい。短くなりすぎる場合は元の名称を保つ。
    """
    out = name
    for pattern, repl in _NAME_FIXES:
        out = pattern.sub(repl, out).strip()
    if is_organization:
        stripped = _ORG_SUFFIX.sub("", out).strip()
        if len(stripped) >= 3:
            out = stripped
    out = out.strip(" 　「」『』")
    return out if len(out) >= 2 else name


def stable_id(prefecture_romaji: str, municipality_romaji: str,
              name: str, reading: str | None) -> tuple[str, str]:
    """安定IDを決める。戻り値は (id, 由来)。

    読みが確認できればヘボン式ローマ字を使う。読めない漢字は珍しくないので
    （「化蘇沼」「鷲子」「安居」…）、確認できない場合は**名称から導いた
    ハッシュ**を使う。推測で読みを作らない。

    **一度決めたIDは変えない。** あとから読みが分かってもIDは据え置き、
    読みは別のフィールドに記録する。IDを変えると既存レコードとの突合や
    重複検出が破綻するため、可読性より同一性を優先する。

    ハッシュIDは `x` で始めるので、ローマ字由来かどうかが一目でわかる
    （日本語のローマ字は x では始まらない）。
    """
    if reading and is_kana(reading):
        romaji = romanize(reading)
        if romaji:
            slug = slugify(romaji)
            if slug:
                return f"{prefecture_romaji}-{municipality_romaji}-{slug}", "reading"
    # dedup_key を種にする。表記揺れ (第N回・年号・空白) では変わらない。
    digest = hashlib.sha1(
        f"{prefecture_romaji}/{municipality_romaji}/{dedup_key(name)}".encode("utf-8")
    ).hexdigest()[:8]
    return f"{prefecture_romaji}-{municipality_romaji}-x{digest}", "hash"


def _place(rid: str, index: dict, verdicts: dict) -> str:
    """S4で所在が与えられていればそれを、無ければ入力の市町村を返す。"""
    given = (verdicts.get(rid) or {}).get("municipality") or ""
    return given or index[rid]["municipality"]


def _same_place(rid: str, alias: str, index: dict, verdicts: dict) -> bool:
    """別名として結んでよい組み合わせか。

    県単位レンズで拾った `(県全域)` の行は、所在を確定したうえで
    市町村側の行の別名になるのが普通の流れなので許す
    (「国指定重要無形民俗文化財「綱火」」→ つくばみらい市の「綱火」)。
    それ以外は同じ市町村でなければならない。
    """
    a, b = _place(rid, index, verdicts), _place(alias, index, verdicts)
    if a == b:
        return True
    return PREFECTURE_WIDE in (index[rid]["municipality"], index[alias]["municipality"])


# --- 所在の機械的な推定 -------------------------------------------------
# 県単位レンズ (教育委員会・県公式サイト) で拾った行は市町村が (県全域) の
# ままで、slug に市町村のローマ字が要るため PENDING で止まる。S4 の指示では
# AIに「読み取れるなら市町村を書く」と頼んでいるが、茨城では18行中9行しか
# 埋まらなかった。残りは既にある台帳と突き合わせれば機械的に解ける。
_PLACE_CACHE: dict[str, dict] = {}


def _place_sources(prefecture: str) -> dict:
    """(指定台帳の名称->市町村, 市町村の語幹) を用意する。"""
    if prefecture in _PLACE_CACHE:
        return _PLACE_CACHE[prefecture]
    desig: dict[str, str] = {}
    for name in ("bunkazai_local.tsv", "bunkazai_ai.tsv"):
        path = REGISTRY_DIR / name
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            continue
        header = lines[0].split("\t")
        for ln in lines[1:]:
            if not ln.strip():
                continue
            r = dict(zip(header, ln.split("\t")))
            muni = r.get("municipality", "")
            if r.get("prefecture") == prefecture and muni and muni != PREFECTURE_WIDE:
                desig.setdefault(dedup_key(r.get("name", "")), muni)
    stems: dict[str, str] = {}
    muni_path = REGISTRY_DIR / "municipalities.tsv"
    if muni_path.exists():
        lines = muni_path.read_text(encoding="utf-8").splitlines()
        header = lines[0].split("\t")
        for ln in lines[1:]:
            if not ln.strip():
                continue
            r = dict(zip(header, ln.split("\t")))
            if r.get("prefecture") != prefecture:
                continue
            m = r.get("municipality", "")
            stem = re.sub(r"[市町村区]$", "", m)
            if len(stem) >= 2:
                stems[m] = stem
    out = {"desig": desig, "stems": stems}
    _PLACE_CACHE[prefecture] = out
    return out


def _guess_place(row: dict, prefecture: str) -> tuple[str, str]:
    """(市町村, 根拠) を返す。分からなければ ("", "")。

    根拠の強い順に見る。指定台帳との名称一致がいちばん堅い。
    """
    src = _place_sources(prefecture)
    key = dedup_key(row.get("name", ""))
    hit = src["desig"].get(key)
    if hit:
        return hit, "designation"
    blob = " ".join([row.get("name", ""), row.get("venue", ""),
                     row.get("date_note", "")])
    for muni, stem in src["stems"].items():
        if stem in blob:
            return muni, "name"
    return "", ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefecture")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)

    index = load_index()
    verdicts, problems = load_verdicts()

    missing = sorted(set(index) - set(verdicts))
    extra = sorted(set(verdicts) - set(index))
    if extra:
        problems.append(f"入力に無い id が {len(extra)} 件返ってきた: {extra[:5]}")
    if missing:
        problems.append(f"未回答が {len(missing)} 件: {missing[:5]}")

    src = WORK_DIR / "normalized.tsv"
    with src.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    if args.prefecture:
        rows = [r for r in rows if r["prefecture"] == args.prefecture]
    rows.sort(key=lambda r: (r["municipality"], r["name"]))
    by_id = {f"{i:05d}": r for i, r in enumerate(rows, start=1)}

    # --- 検証 ---
    kept_ids = {
        rid for rid, v in verdicts.items() if v.get("verdict") == "keep"
    }
    for rid, v in sorted(verdicts.items()):
        verdict = v.get("verdict", "")
        if verdict not in VERDICTS:
            problems.append(f"id {rid}: verdict が不正 ({verdict!r})")
        alias = v.get("alias_of", "")
        if alias:
            if alias not in index:
                problems.append(f"id {rid}: alias_of {alias} が存在しない")
            elif alias == rid:
                problems.append(f"id {rid}: alias_of が自分自身")
            elif not _same_place(rid, alias, index, verdicts):
                problems.append(
                    f"id {rid}: alias_of {alias} の市町村が違う "
                    f"({index[rid]['municipality']} vs {index[alias]['municipality']})"
                )
            elif alias not in kept_ids:
                problems.append(f"id {rid}: alias_of {alias} が keep されていない")
        reading = v.get("reading", "")
        if reading and not is_kana(reading):
            problems.append(f"id {rid}: reading がかなではない ({reading!r})")

    # alias の循環・多段を検出
    alias_map = {r: v["alias_of"] for r, v in verdicts.items() if v.get("alias_of")}
    for rid in alias_map:
        seen, cur = {rid}, alias_map.get(rid)
        while cur:
            if cur in seen:
                problems.append(f"id {rid}: alias_of が循環している")
                break
            seen.add(cur)
            cur = alias_map.get(cur)

    shown_problems = len(problems)
    if problems:
        print("=== 検証で問題が見つかった ===")
        for p in problems[:30]:
            print("  -", p)
        if len(problems) > 30:
            print(f"  ... 他 {len(problems) - 30} 件")
        if args.strict:
            return 1
        print("  (--strict でなければ、問題のある行は unsure として扱って続行する)\n")

    # --- 適用 ---
    out_rows = []
    slugs: dict[str, str] = {}
    stats = collections.Counter()
    for rid, base in sorted(by_id.items()):
        v = verdicts.get(rid, {})
        verdict = v.get("verdict", "")
        if verdict not in VERDICTS:
            verdict = "unsure"
        stats[verdict] += 1

        row = dict(base)
        # 所在の確定 (県全域 の行だけ上書きを許す)
        if base["municipality"] == PREFECTURE_WIDE and v.get("municipality"):
            row["municipality"] = v["municipality"]
            stats["municipality_resolved"] += 1
        elif base["municipality"] == PREFECTURE_WIDE and verdict == "keep":
            # AIが所在を書けなかった行を、機械的な手がかりで埋める。
            # 県レンズ (教育委員会・県公式) で拾った行は所在が空のままで、
            # slug に市町村のローマ字が要るため PENDING で止まる。
            guess, how = _guess_place(row, args.prefecture or base["prefecture"])
            if guess:
                row["municipality"] = guess
                stats["municipality_resolved"] += 1
                stats["place_from_" + how] += 1

        row["_reading"] = v.get("reading", "")

        # 名称の整形は keep 行だけに行う。元の名称は aliases に残す。
        row["s4_verdict"] = verdict
        row["s4_reason"] = v.get("reason", "")
        row["s4_alias_of"] = v.get("alias_of", "")
        if verdict == "keep":
            cleaned = clean_display_name(
                row["name"], v.get("reason") == "organization"
            )
            if cleaned != row["name"]:
                aliases = [a for a in row.get("aliases", "").split("|") if a]
                if row["name"] not in aliases:
                    aliases.append(row["name"])
                row["aliases"] = "|".join(aliases)
                row["name"] = cleaned
                stats["name_cleaned"] += 1
        out_rows.append(row)

    # 名称を整形すると、判定時には別名だと分からなかった重複が見えてくる
    # (「井草大杉囃子保存会」と「井草大杉囃子」)。同一市町村で整形後の名称が
    # 一致し、どちらも別名指定を持たない行は、機械的に統合してよい。
    by_name: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    id_of = {id(r): rid for rid, r in zip(sorted(by_id), out_rows)}
    for r in out_rows:
        if r["s4_verdict"] == "keep" and not r["s4_alias_of"]:
            # 表記揺れも畳んだキーで見る。整形で「○○まつり」と「○○祭り」が
            # 同じものになる場合があり、名称の完全一致だけでは取り逃す。
            by_name[(r["municipality"], dedup_key(r["name"]))].append(r)
    for group in by_name.values():
        if len(group) < 2:
            continue
        # 根拠の多いものを正とする。同数なら先に出てきたもの。
        canonical = max(group, key=lambda r: len(r["source_urls"].split("|")))
        for r in group:
            if r is canonical:
                continue
            r["s4_alias_of"] = id_of[id(canonical)]
            stats["merged_after_clean"] += 1
            merged = [a for a in canonical["aliases"].split("|") if a]
            for a in [r["name"], *r["aliases"].split("|")]:
                if a and a not in merged and a != canonical["name"]:
                    merged.append(a)
            canonical["aliases"] = "|".join(merged)
            urls = [u for u in canonical["source_urls"].split("|") if u]
            for u in r["source_urls"].split("|"):
                if u and u not in urls:
                    urls.append(u)
            canonical["source_urls"] = "|".join(urls[:8])
            canonical["source_count"] = str(len(urls))

    # --- ID付与 ---
    # 名称の整形と重複統合が終わってから、代表行 (別名でない keep) にだけ与える。
    # 整形前の名称でIDを作ると、統合されるはずの行に別のIDが振られてしまう。
    for rid, row in zip(sorted(by_id), out_rows):
        if row["s4_verdict"] != "keep" or row["s4_alias_of"]:
            row["slug"] = ""          # 別名行はIDを持たない
            continue
        if row["slug"] != "PENDING":  # S3で読みから決まっていたものはそのまま
            # ここでも衝突を見る。setdefault で黙って捨てていたため、
            # 笠間市の「流鏑馬 やぶさめ」と「流鏑馬（やぶさめ）」が同じIDのまま
            # 両方 data/festivals.json へ行き、S5の消費側検査で初めて落ちた。
            if row["slug"] in slugs:
                row["s4_alias_of"] = slugs[row["slug"]]
                row["slug"] = ""
                stats["merged_by_id"] += 1
                continue
            slugs[row["slug"]] = rid
            stats["id_reading"] += 1
            continue
        pref_r, muni_r = _romaji_for(row["prefecture"], row["municipality"])
        if not (pref_r and muni_r):
            problems.append(f"id {rid}: 市町村のローマ字が引けない ({row['municipality']})")
            continue
        candidate, origin = stable_id(pref_r, muni_r, row["name"], row.get("_reading"))
        if not SLUG_RE.match(candidate):
            problems.append(f"id {rid}: 生成IDの形式が不正 ({candidate})")
        elif candidate in slugs:
            # **同一市町村でIDが衝突するのは重複の証拠であって異常ではない。**
            # IDは (県, 市町村, 読み or dedup_key(名称)) から決まるので、
            # 衝突は「同じ市町村で読みまたは正規化名称が一致した」ことを意味する。
            # 実際に「つくばみらい市の綱火が2行」「北茨城市の常陸大津の御船祭が
            # 2行」といった、S4の別名判定が拾えなかった重複だった。
            # 以前はここでエラーにして PENDING のまま放置し、しかもその
            # 問題文は表示されていなかったので34行が黙って落ちていた。
            row["s4_alias_of"] = slugs[candidate]
            row["slug"] = ""
            stats["merged_by_id"] += 1
        else:
            row["slug"] = candidate
            slugs[candidate] = rid
            stats[f"id_{origin}"] += 1
    # slug生成で出た問題は、上の検証ブロックより後に積まれる。以前は
    # 一度も表示されず、42行が黙って PENDING のまま残っていた。ここで出す。
    if len(problems) > shown_problems:
        print()
        print("=== ID生成で問題が見つかった ===")
        for p in problems[shown_problems:][:30]:
            print(f"  - {p}")
        if len(problems) - shown_problems > 30:
            print(f"  ... 他 {len(problems) - shown_problems - 30} 件")

    for row in out_rows:
        row.pop("_reading", None)

    out = WORK_DIR / "judged.tsv"
    fields = list(out_rows[0].keys()) if out_rows else ["prefecture"]
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.DictWriter(fh, delimiter="\t", lineterminator="\n", fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    print(f"judged.tsv: {len(out_rows)} 件")
    print(f"  keep {stats['keep']} / drop {stats['drop']} / unsure {stats['unsure']}")
    print(f"  所在を確定できた   : {stats['municipality_resolved']}")
    print(f"  ID: 読み由来       : {stats['id_reading']}")
    print(f"  ID: ハッシュ由来   : {stats['id_hash']}")
    print(f"  名称を整形         : {stats['name_cleaned']}  (元の名称は aliases に残す)")
    print(f"  整形で判明した重複 : {stats['merged_after_clean']}  (機械的に統合した)")
    print(f"  別名として統合     : {sum(1 for r in out_rows if r['s4_alias_of'])}")
    canon = [r for r in out_rows if r["s4_verdict"] == "keep" and not r["s4_alias_of"]]
    print(f"  代表行 (別名を除く) : {len(canon)}")
    still = sum(1 for r in canon if r["slug"] in ("", "PENDING"))
    print(f"  ID 未確定          : {still}  (0であるべき)")
    ids = [r["slug"] for r in canon if r["slug"]]
    print(f"  ID の重複          : {len(ids) - len(set(ids))}  (0であるべき)")
    return 0


_ROMAJI_CACHE: dict[tuple[str, str], tuple[str, str]] = {}


def _romaji_for(prefecture: str, municipality: str) -> tuple[str, str]:
    if not _ROMAJI_CACHE:
        path = REPO_ROOT / "registry" / "municipalities.tsv"
        if path.is_file():
            with path.open(encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh, delimiter="\t"):
                    _ROMAJI_CACHE[(r["prefecture"], r["municipality"])] = (
                        r["pref_romaji"],
                        r["muni_romaji"],
                    )
    return _ROMAJI_CACHE.get((prefecture, municipality), ("", ""))


if __name__ == "__main__":
    sys.exit(main())
