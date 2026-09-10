#!/usr/bin/env python3
"""Build the dependency-free GitHub Pages site into ``_site``."""

from __future__ import annotations

import html
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

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


def page(title: str, body: str, root: str = "", script: bool = False) -> str:
    tail = f'<script src="{root}assets/app.js" defer></script>' if script else ""
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} | Wasshoy!</title><link rel="stylesheet" href="{root}assets/style.css"></head>
<body><header><a href="{root}index.html" class="brand">Wasshoy!</a><span>祭り調査アーカイブ</span></header>
<main>{body}</main><footer>根拠をたどれる、日本の祭りデータセット。</footer>{tail}</body></html>"""


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets").mkdir(parents=True)
    (OUT / "reports").mkdir()
    (OUT / "data").mkdir()
    shutil.copy(ROOT / "data/festivals.json", OUT / "data/festivals.json")
    shutil.copy(ROOT / "site/style.css", OUT / "assets/style.css")
    shutil.copy(ROOT / "site/app.js", OUT / "assets/app.js")
    data = json.loads((ROOT / "data/festivals.json").read_text(encoding="utf-8"))
    regions: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for festival in data["festivals"]:
        tags = "".join(f"<span>{html.escape(tag)}</span>" for tag in festival["categories"])
        name = html.escape(festival["name"])
        # 概要が未確認でも、出典へ飛べれば「なんだこれ?」を確かめられる。
        # 祭り名そのものをリンクにして、行き先のホスト名を meta に出す。
        sources = festival.get("sources") or []
        heading = f"<h3>{name}</h3>"
        source_line = ""
        if sources and sources[0].get("url"):
            url = html.escape(sources[0]["url"], quote=True)
            heading = (
                f'<h3><a href="{url}" target="_blank" rel="noopener noreferrer">{name}</a></h3>'
            )
            host = html.escape(urlparse(sources[0]["url"]).netloc)
            more = f"　ほか{len(sources) - 1}件" if len(sources) > 1 else ""
            source_line = f'<p class="src">{host}{more}</p>'
        # 概要が無いカードが大半なので、定型文は出さず高さを詰める。
        summary = festival.get("summary")
        summary_line = f"<p class=\"summary\">{html.escape(summary)}</p>" if summary else ""
        district = festival.get("district")
        place_line = f"<p class=\"place\">{html.escape(district)}</p>" if district else ""
        pref_attr = html.escape(festival["prefecture"], quote=True)
        muni_attr = html.escape(festival["municipality"], quote=True)
        card = f"""<article data-pref="{pref_attr}" data-muni="{muni_attr}" data-name="{name}">{place_line}
{heading}{summary_line}<div class="tags">{tags}</div>{source_line}<p class="meta">{html.escape(festival['status'])} / {html.escape(festival['confidence'])}</p></article>"""
        regions[festival["prefecture"]][festival["municipality"]].append(card)
    region_sections = []
    for prefecture in sorted(regions):
        municipalities = regions[prefecture]
        prefecture_count = sum(len(items) for items in municipalities.values())
        pref_attr = html.escape(prefecture, quote=True)
        municipality_sections = []
        for municipality in sorted(municipalities):
            cards = municipalities[municipality]
            muni_attr = html.escape(municipality, quote=True)
            municipality_sections.append(
                f'<section class="municipality" data-pref="{pref_attr}" data-muni="{muni_attr}">'
                f'<div class="municipality-head"><h3>{html.escape(municipality)}</h3>'
                f'<span class="count">{len(cards)}件</span></div><div class="grid">{"".join(cards)}</div></section>'
            )
        region_sections.append(
            f'<section class="prefecture" data-pref="{pref_attr}"><div class="prefecture-head"><h2>{html.escape(prefecture)}</h2>'
            f'<strong class="count">{prefecture_count}件</strong></div>{"".join(municipality_sections)}</section>'
        )
    # 絞り込みのセレクト。市町村は県に応じて JS が入れ替えるので、
    # 全組み合わせを data 属性に持たせておく。
    pref_options = "".join(
        f'<option value="{html.escape(p, quote=True)}">{html.escape(p)}</option>' for p in sorted(regions)
    )
    muni_map = {p: sorted(regions[p]) for p in regions}
    muni_json = html.escape(json.dumps(muni_map, ensure_ascii=False), quote=True)
    filter_bar = f"""<div class="filter" id="filter" data-munis="{muni_json}">
<label>都道府県<select id="f-pref"><option value="">すべて</option>{pref_options}</select></label>
<label>市区町村<select id="f-muni" disabled><option value="">すべて</option></select></label>
<label class="grow">名称<input id="f-text" type="search" placeholder="祭りの名前で絞り込む" autocomplete="off"></label>
<button type="button" id="f-clear">解除</button>
<span class="filter-count" id="f-count"></span>
</div>"""
    reports = []
    for path in sorted((ROOT / "reports").glob("*.md"), reverse=True):
        if path.name == "README.md":
            continue
        target = OUT / "reports" / f"{path.stem}.html"
        target.write_text(
            page(path.stem, markdown(path.read_text(encoding="utf-8")), "../"),
            encoding="utf-8",
        )
        reports.append(f'<li><a href="reports/{path.stem}.html">{path.stem}</a></li>')
    body = f"""<section class="hero"><p class="eyebrow">OPEN RESEARCH ARCHIVE</p><h1>近くの祭りを、<br>根拠といっしょに。</h1>
<p>自治体・主催者などの公開情報をたどり、まだ知らない地域行事に出会えるデータを育てています。</p></section>
<section><div class="section-head"><h2>地域から祭りを探す</h2><strong id="total">{len(data['festivals'])}件</strong></div>{filter_bar}{''.join(region_sections)}</section>
<section class="reports"><h2>調査レポート</h2><ul>{''.join(reports)}</ul><p><a href="data/festivals.json">JSONデータを開く →</a></p></section>"""
    (OUT / "index.html").write_text(
        page("祭り調査アーカイブ", body, script=True), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
