"""アクセス台帳の集計: どのサイトに・いつ・何回アクセスしたかを reports/ に残す。

クローラーがネットに出た全リクエストは net.py が
`$WASSHOY_CACHE_DIR/access.tsv` に追記している (キャッシュ命中は含まない)。
このスクリプトはそれをホスト単位に集計して、リポジトリにコミットできる形にする。

    python pipeline/access_report.py            # reports/access-hosts.tsv と access-log.md を更新
    python pipeline/access_report.py --backfill # 台帳導入前の取得分をキャッシュから復元 (一度だけ)

--backfill について:
    2026-09-07〜12 の取得は台帳導入前だったので、キャッシュの *.meta.json
    (fetched_at, status, bytes) と failures.tsv から復元する。robots.txt の
    取得はキャッシュされていないため復元できない。復元行は note 列に
    backfill-from-cache / backfill-from-failures と印を付ける。
    すでに台帳にある URL は二重に書かない。

このモジュールは data/festivals.json と benchmarks/ を参照しない。
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from net import ACCESS_LOG_COLUMNS, ACCESS_LOG_NAME, CACHE_DIR, REPO_ROOT  # noqa: E402

REPORT_TSV = REPO_ROOT / "reports" / "access-hosts.tsv"
REPORT_MD = REPO_ROOT / "reports" / "access-log.md"
SITES_TSV = REPO_ROOT / "registry" / "sites.tsv"
PREF_SITES_TSV = REPO_ROOT / "registry" / "pref_sites.tsv"

KINDS = ("fetch", "robots", "fail", "denied", "capped")


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def read_log(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(ACCESS_LOG_COLUMNS):
                parts += [""] * (len(ACCESS_LOG_COLUMNS) - len(parts))
            rows.append(dict(zip(ACCESS_LOG_COLUMNS, parts)))
    return rows


# ---------------------------------------------------------------- backfill


def backfill(cache_dir: Path) -> int:
    log = cache_dir / ACCESS_LOG_NAME
    known = {(r["kind"], r["url"]) for r in read_log(log)}
    lines: list[str] = []

    for host_dir in sorted(cache_dir.iterdir()):
        if not host_dir.is_dir():
            continue
        for meta_path in host_dir.glob("*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            url = meta.get("url", "")
            if not url or ("fetch", url) in known:
                continue
            final_url = meta.get("final_url", "")
            lines.append(
                "\t".join(
                    [
                        meta.get("fetched_at", ""),
                        "fetch",
                        url,
                        str(meta.get("status", "")),
                        str(meta.get("bytes", "")),
                        final_url if final_url != url else "",
                        "backfill-from-cache",
                    ]
                )
            )
            known.add(("fetch", url))

    failures = cache_dir / "failures.tsv"
    if failures.is_file():
        with failures.open(encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 3:
                    continue
                date, url, reason = parts[0], parts[1], parts[2]
                if ("fail", url) in known:
                    continue
                status = reason[5:] if reason.startswith("HTTP ") and reason[5:].isdigit() else ""
                lines.append(
                    "\t".join([date, "fail", url, status, "", "", f"backfill-from-failures {reason}"])
                )
                known.add(("fail", url))

    # 時系列順に並ぶよう、復元分は日時でソートしてから追記する
    lines.sort()
    with log.open("a", encoding="utf-8", newline="\n") as fh:
        for ln in lines:
            fh.write(ln + "\n")
    return len(lines)


# ------------------------------------------------------------------ report


def _load_host_labels() -> dict[str, tuple[str, str]]:
    """host -> (都道府県, 自治体/種別)。registry から引く。"""
    labels: dict[str, tuple[str, str]] = {}
    for path, fallback in ((SITES_TSV, ""), (PREF_SITES_TSV, "県庁")):
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as fh:
            header = fh.readline().rstrip("\n").split("\t")
            idx = {name: i for i, name in enumerate(header)}
            for line in fh:
                p = line.rstrip("\n").split("\t")
                pref = p[idx["prefecture"]] if "prefecture" in idx and idx["prefecture"] < len(p) else ""
                muni = p[idx["municipality"]] if "municipality" in idx and idx["municipality"] < len(p) else fallback
                for col, suffix in (
                    ("official_url", ""),
                    ("tourism_url", "観光"),
                    ("education_url", "教委"),
                ):
                    if col in idx and idx[col] < len(p) and p[idx[col]]:
                        labels.setdefault(_host(p[idx[col]]), (pref, muni + suffix))
    return labels


def build_report(cache_dir: Path) -> tuple[int, int]:
    rows = read_log(cache_dir / ACCESS_LOG_NAME)
    labels = _load_host_labels()

    per_host: dict[str, dict] = {}
    for r in rows:
        h = _host(r["url"])
        d = per_host.setdefault(
            h, {"first": "", "last": "", "bytes": 0, **{k: 0 for k in KINDS}}
        )
        kind = r["kind"] if r["kind"] in KINDS else "fail"
        d[kind] += 1
        day = r["ts"][:10]
        if day:
            d["first"] = min(d["first"] or day, day)
            d["last"] = max(d["last"], day)
        if r["bytes"].isdigit():
            d["bytes"] += int(r["bytes"])

    REPORT_TSV.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_TSV.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "host\tprefecture\tsite\tfirst\tlast\tfetch\trobots\tfail\tdenied\tcapped\tbytes\n"
        )
        for h in sorted(per_host, key=lambda x: (-per_host[x]["fetch"], x)):
            d = per_host[h]
            pref, who = labels.get(h, ("", ""))
            fh.write(
                f"{h}\t{pref}\t{who}\t{d['first']}\t{d['last']}\t{d['fetch']}\t{d['robots']}"
                f"\t{d['fail']}\t{d['denied']}\t{d['capped']}\t{d['bytes']}\n"
            )

    # ---- 要約 (Markdown)
    total = collections.Counter(r["kind"] for r in rows)
    by_day: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in rows:
        by_day[r["ts"][:10]][r["kind"]] += 1
    # 応答のあったホスト (fetch か robots が1回でもある) と、問い合わせただけで
    # 応答が無かったホスト (URL推測の外れ = DNS不在が大半) を分けて数える。
    # 後者もアクセスは試みているので台帳には残すが、「巡回したサイト」とは別物。
    responded = {h for h, d in per_host.items() if d["fetch"] or d["robots"]}
    by_pref: dict[str, dict] = collections.defaultdict(lambda: {"hosts": 0, "fetch": 0})
    unlabeled = {"hosts": 0, "fetch": 0}
    for h in responded:
        d = per_host[h]
        pref = labels.get(h, ("", ""))[0]
        tgt = by_pref[pref] if pref else unlabeled
        tgt["hosts"] += 1
        tgt["fetch"] += d["fetch"]
    net_requests = total["fetch"] + total["robots"] + total["fail"]

    md = [
        "# クローラーのアクセス記録",
        "",
        f"最終更新: {time.strftime('%Y-%m-%d')} / 台帳: `$WASSHOY_CACHE_DIR/{ACCESS_LOG_NAME}`"
        f" ({len(rows):,} 行) / 集計: `python pipeline/access_report.py`",
        "",
        "`net.py` がネットに出た全リクエストを1行ずつ台帳に追記し、このファイルと",
        "`access-hosts.tsv` (ホスト別) はそれを集計したもの。キャッシュ命中は含まない。",
        "取得の作法 (robots.txt 遵守・同一ホスト1秒間隔・連絡先入り UA) は RUNBOOK.md 参照。",
        "",
        "## 全体",
        "",
        "| | 件数 |",
        "|---|---:|",
        f"| ネットに出たリクエスト | {net_requests:,} |",
        f"| ├ ページ取得 (fetch) | {total['fetch']:,} |",
        f"| ├ robots.txt 取得 | {total['robots']:,} |",
        f"| └ 失敗 (HTTPエラー/接続失敗) | {total['fail']:,} |",
        f"| robots.txt により取得しなかった (denied) | {total['denied']:,} |",
        f"| 1ホスト上限で取得しなかった (capped) | {total['capped']:,} |",
        f"| 応答のあったホスト数 | {len(responded):,} |",
        f"| 問い合わせのみで応答の無かったホスト数 (DNS不在等) | {len(per_host) - len(responded):,} |",
        "",
        "## 日別",
        "",
        "| 日付 | fetch | robots | fail | denied | capped |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for day in sorted(by_day):
        c = by_day[day]
        md.append(
            f"| {day} | {c['fetch']:,} | {c['robots']:,} | {c['fail']:,} | {c['denied']:,} | {c['capped']:,} |"
        )
    md += [
        "",
        "## 都道府県別 (応答のあったホスト。都道府県は registry/sites.tsv から)",
        "",
        "| 都道府県 | ホスト | 取得ページ |",
        "|---|---:|---:|",
    ]
    for pref, d in sorted(by_pref.items(), key=lambda kv: -kv[1]["fetch"]):
        md.append(f"| {pref} | {d['hosts']:,} | {d['fetch']:,} |")
    md.append(f"| (台帳未登録のホスト) | {unlabeled['hosts']:,} | {unlabeled['fetch']:,} |")
    md += [
        "",
        "## 補足",
        "",
        "- 2026-09-07〜12 の分は台帳導入前だったため、キャッシュの meta.json と failures.tsv",
        "  から復元した (`--backfill`)。この期間の robots.txt 取得はキャッシュされておらず復元できない。",
        "- 失敗の大半は DNS 解決失敗。自治体サイトの URL をドメイン規則から推測して当たりを",
        "  探す方式 (S0-2) のため、存在しないホスト名への問い合わせが多く出る。",
        "- 個別 URL まで知りたいときは台帳を直接 grep する:",
        "  `grep 'www.city.mito.lg.jp' \"$WASSHOY_CACHE_DIR/access.tsv\"`",
        "",
    ]
    REPORT_MD.write_text("\n".join(md), encoding="utf-8")
    return len(rows), len(per_host)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--backfill", action="store_true", help="キャッシュから台帳導入前の分を復元する")
    ap.add_argument("--cache-dir", default=str(CACHE_DIR))
    args = ap.parse_args(argv[1:])
    cache_dir = Path(args.cache_dir)

    if args.backfill:
        n = backfill(cache_dir)
        print(f"backfill: {n:,} 行を {cache_dir / ACCESS_LOG_NAME} に追記")

    rows, hosts = build_report(cache_dir)
    print(
        f"report: {rows:,} 行 / {hosts:,} ホスト -> "
        f"{REPORT_TSV.relative_to(REPO_ROOT)}, {REPORT_MD.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
