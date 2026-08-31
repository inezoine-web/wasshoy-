#!/usr/bin/env python3
"""Build the dependency-free GitHub Pages site into ``_site``."""

from __future__ import annotations

import html
import json
import re
import shutil
from collections import defaultdict
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
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        line = raw.rstrip()
        table = (
            line.startswith("|")
            and index + 1 < len(lines)
            and re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", lines[index + 1].strip())
        )
        match = re.match(r"^(#{1,4})\s+(.+)$", line)
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        numbered = re.match(r"^\d+\.\s+(.+)$", line)
        wanted = "ul" if bullet else "ol" if numbered else None
        if list_type and wanted != list_type:
            parts.append(f"</{list_type}>")
            list_type = None
        if table:
            headers = [cell.strip() for cell in line.strip("|").split("|")]
            rows = []
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append([cell.strip() for cell in lines[index].strip().strip("|").split("|")])
                index += 1
            head = "".join(f"<th>{inline(cell)}</th>" for cell in headers)
            body = "".join(
                "<tr>" + "".join(f"<td>{inline(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            parts.append(f"<div class=\"table-wrap\"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")
            continue
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
        index += 1
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
    regions: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for festival in data["festivals"]:
        tags = "".join(f"<span>{html.escape(tag)}</span>" for tag in festival["categories"])
        card = f"""<article><p class="place">{html.escape(festival.get('district') or '市区町村内')}</p>
<h3>{html.escape(festival['name'])}</h3><p>{html.escape(festival['summary'])}</p>
<div class="tags">{tags}</div><p class="meta">{html.escape(festival['status'])} / 確信度 {html.escape(festival['confidence'])}</p></article>"""
        regions[festival["prefecture"]][festival["municipality"]].append(card)
    region_sections = []
    for prefecture in sorted(regions):
        municipalities = regions[prefecture]
        prefecture_count = sum(len(items) for items in municipalities.values())
        municipality_sections = []
        for municipality in sorted(municipalities):
            cards = municipalities[municipality]
            municipality_sections.append(
                f'<section class="municipality"><div class="municipality-head"><h3>{html.escape(municipality)}</h3>'
                f'<span>{len(cards)}件</span></div><div class="grid">{"".join(cards)}</div></section>'
            )
        region_sections.append(
            f'<section class="prefecture"><div class="prefecture-head"><h2>{html.escape(prefecture)}</h2>'
            f'<strong>{prefecture_count}件</strong></div>{"".join(municipality_sections)}</section>'
        )
    reports = []
    for path in sorted((ROOT / "reports").glob("*.md"), reverse=True):
        if path.name == "README.md":
            continue
        target = OUT / "reports" / f"{path.stem}.html"
        target.write_text(page(path.stem, markdown(path.read_text()), "../"))
        reports.append(f'<li><a href="reports/{path.stem}.html">{path.stem}</a></li>')
    body = f"""<section class="hero"><p class="eyebrow">OPEN RESEARCH ARCHIVE</p><h1>近くの祭りを、<br>根拠といっしょに。</h1>
<p>自治体・主催者などの公開情報をたどり、まだ知らない地域行事に出会えるデータを育てています。</p></section>
<section><div class="section-head"><h2>地域から祭りを探す</h2><strong>{len(data['festivals'])}件</strong></div>{''.join(region_sections)}</section>
<section class="reports"><h2>調査レポート</h2><ul>{''.join(reports)}</ul><p><a href="data/festivals.json">JSONデータを開く →</a></p></section>"""
    (OUT / "index.html").write_text(page("祭り調査アーカイブ", body))


if __name__ == "__main__":
    main()
