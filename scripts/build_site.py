#!/usr/bin/env python3
"""Build the dependency-free GitHub Pages site into ``_site``."""

from __future__ import annotations

import html
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "_site"


def inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\[([^]]+)]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', escaped)
    return escaped


def markdown(text: str) -> str:
    """Render the small Markdown subset used by research reports."""
    parts: list[str] = []
    list_type: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        match = re.match(r"^(#{1,4})\s+(.+)$", line)
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        numbered = re.match(r"^\d+\.\s+(.+)$", line)
        wanted = "ul" if bullet else "ol" if numbered else None
        if list_type and wanted != list_type:
            parts.append(f"</{list_type}>")
            list_type = None
        if match:
            level = len(match.group(1))
            parts.append(f"<h{level}>{inline(match.group(2))}</h{level}>")
        elif wanted:
            if not list_type:
                parts.append(f"<{wanted}>")
                list_type = wanted
            item = bullet.group(1) if bullet else numbered.group(1)
            parts.append(f"<li>{inline(item)}</li>")
        elif line:
            parts.append(f"<p>{inline(line)}</p>")
    if list_type:
        parts.append(f"</{list_type}>")
    return "\n".join(parts)


def page(title: str, body: str, root: str = "") -> str:
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} | Wasshoy!</title><link rel="stylesheet" href="{root}assets/style.css"></head>
<body><header><a href="{root}index.html" class="brand">Wasshoy!</a><span>祭り調査アーカイブ</span></header>
<main>{body}</main><footer>根拠をたどれる、日本の祭りデータセット。</footer></body></html>"""


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets").mkdir(parents=True)
    (OUT / "reports").mkdir()
    (OUT / "data").mkdir()
    shutil.copy(ROOT / "data/festivals.json", OUT / "data/festivals.json")
    shutil.copy(ROOT / "site/style.css", OUT / "assets/style.css")
    data = json.loads((ROOT / "data/festivals.json").read_text())
    cards = []
    for festival in data["festivals"]:
        tags = "".join(f"<span>{html.escape(tag)}</span>" for tag in festival["categories"])
        cards.append(f"""<article><p class="place">{festival['municipality']}・{festival.get('district') or '市内'}</p>
<h3>{html.escape(festival['name'])}</h3><p>{html.escape(festival['summary'])}</p>
<div class="tags">{tags}</div><p class="meta">{festival['status']} / 確信度 {festival['confidence']}</p></article>""")
    reports = []
    for path in sorted((ROOT / "reports").glob("*.md"), reverse=True):
        if path.name == "README.md":
            continue
        target = OUT / "reports" / f"{path.stem}.html"
        target.write_text(page(path.stem, markdown(path.read_text()), "../"))
        reports.append(f'<li><a href="reports/{path.stem}.html">{path.stem}</a></li>')
    body = f"""<section class="hero"><p class="eyebrow">OPEN RESEARCH ARCHIVE</p><h1>近くの祭りを、<br>根拠といっしょに。</h1>
<p>自治体・主催者などの公開情報をたどり、まだ知らない地域行事に出会えるデータを育てています。</p></section>
<section><div class="section-head"><h2>今回の調査</h2><strong>{len(cards)}件</strong></div><div class="grid">{''.join(cards)}</div></section>
<section class="reports"><h2>調査レポート</h2><ul>{''.join(reports)}</ul><p><a href="data/festivals.json">JSONデータを開く →</a></p></section>"""
    (OUT / "index.html").write_text(page("祭り調査アーカイブ", body))


if __name__ == "__main__":
    main()
