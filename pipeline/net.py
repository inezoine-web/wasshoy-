"""取得基盤: robots.txt 遵守・レート制限・ローカルキャッシュ・日本語エンコーディング判定。

このモジュールは既存データ (data/festivals.json, benchmarks/) を一切参照しない。
標準ライブラリのみで動作する (certifi は「あれば使う」任意扱い)。

デバッグ:
    python pipeline/net.py https://www.city.hokota.lg.jp/
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 既定はリポジトリ直下の cache/ (クローンしてすぐ動くように)。
# ただしリポジトリがクラウド同期フォルダ (OneDrive/Dropbox 等) の中にあると、
# 数千個の小ファイルを同期しようとして実行中にメモリを食い潰す。
# 実際にこの環境でクロールが2回強制終了した。その場合は
# WASSHOY_CACHE_DIR に同期対象外の場所を指定する。
CACHE_DIR = Path(os.environ.get("WASSHOY_CACHE_DIR") or (REPO_ROOT / "cache"))

# 連絡先を含める。自治体サイトの管理者が誰の巡回か判別できるようにするため。
USER_AGENT = (
    "wasshoy-research/0.1 (+https://github.com/inezoine-web/wasshoy-; "
    "festival research crawler; contact via GitHub issues)"
)

DEFAULT_DELAY = 1.0  # 同一ホストへの最小間隔(秒)
DEFAULT_TIMEOUT = 20


def build_ssl_context() -> ssl.SSLContext:
    """CAバンドルを明示的に解決する。

    このWindows環境では既定の CA ストア
    (C:\\Program Files\\Common Files\\SSL\\cert.pem) が期限切れで、
    一部ホストが CERTIFICATE_VERIFY_FAILED になる。サイト側ではなく
    ローカルの問題なので、使えるバンドルを探して差し替える。
    証明書検証は決して無効化しない。
    """
    for cafile in (
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("REQUESTS_CA_BUNDLE"),
    ):
        if cafile and Path(cafile).is_file():
            return ssl.create_default_context(cafile=cafile)
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    # Git for Windows が同梱するバンドル。既定ストアが期限切れの環境で実際に
    # Wikidata への接続を復旧できることを確認済み。Linux/CI には存在しないので
    # その場合は既定にフォールバックする (CIでは既定で問題ない)。
    for candidate in (
        r"C:\Program Files\Git\mingw64\etc\ssl\certs\ca-bundle.crt",
        r"C:\Program Files (x86)\Git\mingw64\etc\ssl\certs\ca-bundle.crt",
    ):
        if Path(candidate).is_file():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I
)
_ENCODING_ALIASES = {
    "shift_jis": "cp932",
    "shift-jis": "cp932",
    "sjis": "cp932",
    "x-sjis": "cp932",
    "windows-31j": "cp932",
    "euc_jp": "euc-jp",
    "utf8": "utf-8",
}


def decode_html(body: bytes, content_type: str = "") -> tuple[str, str]:
    """日本語サイト向けにデコードする。戻り値は (text, encoding)。

    自治体サイトには cp932 / euc-jp がまだ残っているため、
    utf-8 決め打ちにしない。
    """
    candidates: list[str] = []
    m = re.search(r"charset=([\w\-]+)", content_type, re.I)
    if m:
        candidates.append(m.group(1).lower())
    m2 = _META_CHARSET.search(body[:4096])
    if m2:
        candidates.append(m2.group(1).decode("ascii", "ignore").lower())
    candidates += ["utf-8", "cp932", "euc-jp"]

    seen: set[str] = set()
    for raw in candidates:
        enc = _ENCODING_ALIASES.get(raw, raw)
        if enc in seen:
            continue
        seen.add(enc)
        try:
            return body.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "replace"), "utf-8/replace"


@dataclass
class Doc:
    url: str
    final_url: str
    status: int
    text: str
    encoding: str
    from_cache: bool
    fetched_at: str
    content_type: str = ""
    body: bytes = b""

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.final_url).netloc


class Blocked(Exception):
    """robots.txt もしくは取得上限による拒否。"""


class Fetcher:
    """1回の実行を通じて使い回す取得器。

    - 同一ホストへは直列・最小間隔つき
    - robots.txt を取得して遵守
    - 取得結果は cache/ に保存し、再実行時は再取得しない
      (試行錯誤で自治体サイトを繰り返し叩かないため)
    """

    def __init__(
        self,
        cache_dir: Path | str = CACHE_DIR,
        delay: float = DEFAULT_DELAY,
        timeout: int = DEFAULT_TIMEOUT,
        max_per_host: int | None = None,
        offline: bool = False,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.delay = delay
        self.timeout = timeout
        self.max_per_host = max_per_host
        self.offline = offline
        self.user_agent = user_agent
        self.ctx = build_ssl_context()
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._host_count: dict[str, int] = {}
        self.stats = {"network": 0, "cache": 0, "blocked": 0, "error": 0}
        # 複数スレッドから使えるようにする。礼儀 (1秒間隔) はホスト単位の
        # 制約なので、別ホストへは同時にアクセスしてよい。同一ホストへは
        # ホストごとのロックで直列を保つ。
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}

    def _lock_for(self, host: str) -> threading.Lock:
        with self._lock:
            if host not in self._host_locks:
                self._host_locks[host] = threading.Lock()
            return self._host_locks[host]

    def _bump(self, key: str) -> None:
        with self._lock:
            self.stats[key] += 1

    # ------------------------------------------------------------------ cache

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        parsed = urllib.parse.urlparse(url)
        host = re.sub(r"[^A-Za-z0-9.\-]", "_", parsed.netloc) or "_"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        base = self.cache_dir / host / digest
        return base.with_suffix(".body"), base.with_suffix(".meta.json")

    def _read_cache(self, url: str) -> Doc | None:
        body_path, meta_path = self._cache_paths(url)
        if not (body_path.is_file() and meta_path.is_file()):
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        body = body_path.read_bytes()
        text, enc = decode_html(body, meta.get("content_type", ""))
        return Doc(
            url=url,
            final_url=meta.get("final_url", url),
            status=meta.get("status", 0),
            text=text,
            encoding=enc,
            from_cache=True,
            fetched_at=meta.get("fetched_at", ""),
            content_type=meta.get("content_type", ""),
            body=body,
        )

    def _write_cache(self, url: str, body: bytes, meta: dict) -> None:
        body_path, meta_path = self._cache_paths(url)
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(body)
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # ----------------------------------------------------------------- robots

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            if origin in self._robots:
                return self._robots[origin]

        rp: urllib.robotparser.RobotFileParser | None = None
        robots_url = origin + "/robots.txt"
        try:
            raw = self._raw_get(robots_url, respect_robots=False)
            if raw is not None and raw[0] == 200:
                rp = urllib.robotparser.RobotFileParser()
                text, _ = decode_html(raw[1])
                rp.parse(text.splitlines())
        except Exception:
            rp = None
        # robots.txt が無い自治体サイトは多い (鉾田市・龍ケ崎市等はいずれも404)。
        # その場合 rp は None = 明示的な禁止なしとして扱うが、
        # レート制限と取得上限は同じように適用する。
        with self._lock:
            self._robots[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True

    # ------------------------------------------------------------------ fetch

    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._last_hit.get(host)
        if last is not None:
            wait = self.delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        with self._lock:
            self._last_hit[host] = time.monotonic()

    def _raw_get(
        self, url: str, respect_robots: bool = True
    ) -> tuple[int, bytes, str, str] | None:
        """生の取得。戻り値は (status, body, content_type, final_url)。"""
        host = urllib.parse.urlparse(url).netloc
        self._throttle(host)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return (
                resp.status,
                body,
                resp.headers.get("Content-Type", ""),
                resp.geturl(),
            )

    def get(self, url: str) -> Doc | None:
        """1URLを取得する。キャッシュ優先。取得できなければ None。"""
        url = url.split("#", 1)[0]
        cached = self._read_cache(url)
        if cached is not None:
            self._bump("cache")
            return cached
        if self.offline:
            return None

        host = urllib.parse.urlparse(url).netloc
        with self._lock:
            over = (
                self.max_per_host is not None
                and self._host_count.get(host, 0) >= self.max_per_host
            )
        if over:
            self._bump("blocked")
            return None
        if not self.allowed(url):
            self._bump("blocked")
            return None

        # 同一ホストへの並行アクセスを禁じる。別ホストへは並行してよい。
        with self._lock_for(host):
            try:
                raw = self._raw_get(url)
            except urllib.error.HTTPError as exc:
                self._bump("error")
                self._note_failure(url, f"HTTP {exc.code}")
                return None
            except Exception as exc:  # ネットワーク/TLS/タイムアウト
                self._bump("error")
                self._note_failure(url, f"{type(exc).__name__}: {exc}")
                return None

        if raw is None:
            return None
        status, body, content_type, final_url = raw
        self._bump("network")
        with self._lock:
            self._host_count[host] = self._host_count.get(host, 0) + 1
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._write_cache(
            url,
            body,
            {
                "url": url,
                "final_url": final_url,
                "status": status,
                "content_type": content_type,
                "fetched_at": fetched_at,
                "bytes": len(body),
            },
        )
        text, enc = decode_html(body, content_type)
        return Doc(
            url, final_url, status, text, enc, False, fetched_at, content_type, body
        )

    def get_bytes(self, url: str) -> bytes | None:
        """バイナリ資料 (xlsx, PDF 等) を生バイトで取得する。"""
        doc = self.get(url)
        return None if doc is None else doc.body

    def _note_failure(self, url: str, reason: str) -> None:
        """失敗も記録する。静かに消えると探索漏れの原因が追えなくなるため。"""
        log = self.cache_dir / "failures.tsv"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d')}\t{url}\t{reason}\n")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    f = Fetcher()
    doc = f.get(argv[1])
    if doc is None:
        print("取得できませんでした:", argv[1])
        print("stats:", f.stats)
        return 1
    print(f"status={doc.status} encoding={doc.encoding} cache={doc.from_cache} bytes={len(doc.text)}")
    print(f"final_url={doc.final_url}")
    print("stats:", f.stats)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
