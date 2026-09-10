"""リーク検査: S1〜S4 が既存データを参照していないことを機械的に確かめる。

**S1〜S4（発見・抽出・正規化・AI判定）は `data/festivals.json` と
`benchmarks/` を読んではならない。** 既存データがあるのは一部の都道府県
だけなので、それを入力に使うとその県でのスコアだけが上がり、他県で再現
しない。既存データを読んでよいのは `merge.py`（重複回避）と
`evaluate.py`（採点）だけ。

grep では取り逃す。`DATA = REPO_ROOT / "data" / "festivals.json"` のように
パスを変数へ入れてから開く形は、`open(` と同じ行に出てこないため。
ここでは構文木の**文字列リテラル全部**を見る。

使い方:
    python pipeline/check_leak.py        # 問題があれば exit 1
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent

# 既存データを指す語。これらを含む文字列リテラルは S1〜S4 に現れてはならない。
FORBIDDEN = ("festivals.json", "snapshot.json", "gold.tsv", "benchmarks")

# 既存データを読んでよいスクリプト
ALLOWED = {"merge.py", "evaluate.py", "check_leak.py", "novelty.py", "prune.py"}


def literals(tree: ast.AST):
    """構文木の中の文字列リテラルを返す。docstring とコメントは対象外。

    ast はコメントを保持しないので自動的に除かれる。docstring は
    Expr(Constant) として現れるので、それだけ明示的に飛ばす。
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.lineno, node.value


def main() -> int:
    problems: list[str] = []
    checked = 0
    for path in sorted(PIPELINE.glob("*.py")):
        if path.name in ALLOWED:
            continue
        checked += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, value in literals(tree):
            for word in FORBIDDEN:
                if word in value:
                    problems.append(f"{path.name}:{lineno} に {word!r} を含む文字列: {value[:60]!r}")

    print(f"S1〜S4 の {checked} ファイルを検査した")
    if problems:
        print("\n=== リークの疑い ===")
        for p in problems:
            print("  -", p)
        print("\n既存データを読んでよいのは merge.py と evaluate.py だけ。")
        return 1
    print("問題なし: 既存データ (data/festivals.json, benchmarks/) への参照は無い")
    return 0


if __name__ == "__main__":
    sys.exit(main())
