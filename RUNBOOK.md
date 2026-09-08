# RUNBOOK — 調査パイプラインの回し方

`AGENTS.md` は**何を良い調査とするか**の方針。この RUNBOOK は**どう回すか**の手順。
クローンしたエージェントはこの手順に従えば、AIの判断を使わずに S0〜S3 を実行できる。

## 前提

- Python 3.10 以上。**外部パッケージは不要**（標準ライブラリのみ）
- ネットワークが必要なのは S0・S1 だけ。S2・S3 は `cache/` を読むだけでネットに出ない
- `cache/` と `work/` は `.gitignore` 済み。コミットしない

## なぜスクリプトなのか

前回の茨城県調査（`reports/2026-09-06-ibaraki-40-municipalities-stage1.md`）は
Haikuサブエージェント9本で約55万トークンを使い、AI利用枠5時間をほぼ使い切って
`verified` 0件で終わった。レポートを読み返すと、AIが担っていた工程の大半は
**日本語で明文化できるルール**だった（日付の3分岐、会場名からのカテゴリ推定、
重複統合、slug生成）。明文化できているならコードで足りる。

このパイプラインは、その定型工程をゼロトークンのスクリプトへ移し、
**AIの判断を S4 だけに残す**ための構成である。

## 工程

| 段階 | スクリプト | ネット | AI | 役割 |
| --- | --- | --- | --- | --- |
| S0 | `registry.py` | 要 | 不要 | 全国市区町村コード表と、公式サイト/観光協会URLの台帳 |
| S1 | `discover.py` | 要 | 不要 | 台帳のトップページから祭り情報のあるページを幅優先で収集 |
| S2 | `extract.py` | 不要 | 不要 | キャッシュ済みページから候補行を抽出 |
| S3 | `normalize.py` | 不要 | 不要 | 重複統合・日付仕分け・カテゴリ推定・slug生成 |
| S4 | `s4_prepare.py` / `s4_apply.py` | 不要 | **要** | 対象/対象外の判断、別名統合、所在の帰属、読み。**AIが要るのはここだけ** |
| S5 | （未実装） | 不要 | 不要 | `data/festivals.json` へマージ |
| S6 | `evaluate.py` | 不要 | 不要 | 凍結ベンチマークとの突合 |

## 実行

```bash
# S0-1: 全国市区町村コード表 (一度だけ。結果はコミットする)
python pipeline/registry.py --build-municipalities

# S0-2: 対象都道府県の公式サイト/観光協会URLを解決 (結果はコミットする)
python pipeline/registry.py --resolve-sites --prefecture 茨城県

# S1: ページ収集 (時間がかかる。1市町村40ページ上限、同一ホスト1秒間隔)
python pipeline/discover.py --prefecture 茨城県 --max-pages 40

# S2-S3: 抽出と正規化 (ネットに出ない。何度でもやり直せる)
python pipeline/extract.py   --prefecture 茨城県
python pipeline/normalize.py --prefecture 茨城県

# S4: AI判定 (唯一AIが要る工程)
python pipeline/s4_prepare.py --prefecture 茨城県 --batch-size 120
#   -> work/s4/s4_batch_NNN.tsv ができる。AIがこれを読み、
#      work/s4/s4_verdict_NNN.tsv を「同じ順序・同じ件数」で返す
python pipeline/s4_apply.py --prefecture 茨城県     # 機械チェックしてから適用
python pipeline/s4_apply.py --prefecture 茨城県 --strict   # 未回答があれば失敗させる

# S6: 評価 (ベンチマークのある都道府県のみ)
python pipeline/evaluate.py --benchmark benchmarks/ibaraki-2026-09-06
```

### S4 でAIに任せること / 任せないこと

渡すのは判断に要る列だけ (id・市町村・名称・カテゴリ・会場・日程・ホスト名)。
本文もURL全文も渡さない。実測で **1行あたり約90トークン**。

| AIに任せる | 任せない |
| --- | --- |
| 行事か、断片・団体名・一般語か | slugの生成 |
| 同一行事の別名かどうか | ローマ字化 |
| 県レンズで拾った行事の所在市町村 | カテゴリの機械推定 |
| 名称の**読み（ひらがな）** | 日付の仕分け |

**読みはかなで返させ、ローマ字化と slug 生成は `romaji.py` が行う。**
過去にローマ字化を委託して漢字が残ったまま返ってきた事故があるため、
識別子そのものは委託しない。`s4_apply.py` は適用前に必ず次を確認する。

- 入力の id が過不足なく1回ずつ返ってきたか
- `verdict` が `keep` / `drop` / `unsure` のいずれかか
- `alias_of` が実在し、自分自身でなく、`keep` された行を指し、循環していないか
- `reading` がひらがなだけか
- 生成した slug が `^[a-z0-9-]+$` を満たし、重複しないか

`(県全域)` の行だけは、所在を確定したうえで市町村側の行の別名にしてよい
（「国指定重要無形民俗文化財「綱火」」→ つくばみらい市の「綱火」）。

新しい都道府県を調べるときは `--prefecture` を差し替えるだけでよい。
`registry.py --build-municipalities` は全国分を一度に作るので再実行は不要。

### 個別のデバッグ

```bash
python pipeline/net.py https://www.city.hokota.lg.jp/   # 1URLの取得確認
python pipeline/romaji.py                               # ローマ字変換の自己テスト
python pipeline/discover.py --prefecture 茨城県 --municipality 鉾田市 --max-pages 15
```

## クロールの作法（変更しないこと）

`net.py` が強制している。緩めてはいけない。

- `robots.txt` を取得して遵守する
- 同一ホストへは直列、1リクエストあたり最低1秒
- 連絡先を含む User-Agent を名乗る
- 取得済みは `cache/` に保存し、再実行時は再取得しない（試行錯誤で自治体サイトを叩かないため）
- 1自治体あたりの取得ページ数に上限を置く

抽出ルールを直したいときは **S2 からやり直す**。`cache/` があるのでネットには出ない。

なお robots.txt は実際に効いている。Wikidata（`/w/api.php` と `query.wikidata.org/sparql`）は
このUAに対して拒否を返すため、台帳の情報源には使っていない。

## 守るべき設計上の約束

### 1. ベンチマークにリークさせない

**S1〜S4 は `data/festivals.json` と `benchmarks/` を読んではならない。**

既存データがあるのは一部の都道府県だけなので、それを入力に使うと
その県でのスコアだけが上がり、他県で再現しない。既存データを読んでよいのは
`merge.py`（重複回避）と `evaluate.py`（採点）だけ。

機械的に確認できる:

```bash
grep -rn "open(\|read_text(\|json.load" pipeline/*.py | grep -i "festivals\|snapshot\|gold"
```

ファイルを実際に開いている箇所だけを見る (説明コメントに拾われないため)。
`merge.py` と `evaluate.py` 以外がヒットしたらリークである。

### 2. 推測で埋めない（AGENTS.md §1）

コード側でも同じ規律を守る。

- **日付**: `classify_date()` が3分岐で仕分ける。特定年の日付を例年規則へ言い換えない。
  判別できない表記は `usual_schedule` を空にし、原文を `date_note` に残す
- **カテゴリ**: 名称と、情報源に書かれていた会場名からのみ推定する。
  どちらからも判断できなければ `unclassified`
- **slug**: 読みが確認できる場合（名称がかな／括弧内にかながある）だけ生成する。
  漢字の読みを推測しない。確定できないものは `PENDING` として S4 へ回す
- **URL**: 台帳のURLは組み立てただけでは採用しない。取得してページ本文に
  自治体名があることを確認したものだけを書く

### 3. 同一性に関わる値を委託しない

slug・ID・主キーは `romaji.py` が決定的に生成する。安価なモデルに投げない。
過去に祭り名212件のローマ字化を委託して、漢字が残ったまま返ってきた事故がある
（`真鍋のまつり` → `真鍋nomatsuri`）。誤りの訂正コストが非対称に大きい値は委託しない。

S4 で PENDING を埋める場合も、返ってきた値は必ず機械チェックする
（`^[a-z0-9-]+$`、件数、既存IDとの重複）。

### 4. データを足したら消費側を確認する

`data/festivals.json` に `null` を含むレコードを追加したら、
そのフィールドをガードなしに参照しているコードがないか確認する。
過去に `summary: null` を212件追加して GitHub Pages のビルドが落ちている
（`scripts/build_site.py` が `festival['summary']` を直接参照していた）。

```bash
grep -rn "festival\['" scripts/ | grep -v "\.get("
```

## 環境依存の落とし穴

| 症状 | 原因と対処 |
| --- | --- |
| `UnicodeDecodeError: 'cp932' codec` | Windows の既定エンコーディングが cp932。ファイル入出力には必ず `encoding="utf-8"` を明示する。`scripts/build_site.py` は未修正でローカルでは落ちる（CIのLinuxでは通る） |
| `CERTIFICATE_VERIFY_FAILED: certificate has expired` | Windows の既定CAストアが古い。`net.py` の `build_ssl_context()` が `SSL_CERT_FILE` → `certifi` → Git for Windows 同梱バンドルの順に解決する。**証明書検証を無効化しないこと** |
| コンソールの日本語が化ける | `PYTHONIOENCODING=utf-8` を付けて実行する |
| `sed` / `awk` が CRLF ファイルの CR を落とす | 加工は Python で行う |
| `git push` が応答しない | Git Credential Manager のGUI待ち。`git -c credential.helper='!gh auth git-credential' push -u origin <branch>` |
| クロールがメモリ不足で強制終了する | **リポジトリがクラウド同期フォルダ (OneDrive 等) の中にある場合、`cache/` の数千個の小ファイルを同期しようとして落ちる。** 茨城の実走で実際に2回強制終了した (7000ファイル/129MB)。`WASSHOY_CACHE_DIR` に同期対象外の場所を指定する:<br>`export WASSHOY_CACHE_DIR="C:/Users/<user>/AppData/Local/wasshoy-cache"` |

### キャッシュの置き場所

`cache/` は既定でリポジトリ直下（クローンしてすぐ動くように）。環境変数
`WASSHOY_CACHE_DIR` で移せる。**同期フォルダの中で長時間のクロールを回さないこと。**
移動しても取得済みページはそのまま使えるので、途中で移して再開してよい。

## GitHub Actions で回す

`.github/workflows/research.yml` を手動実行（workflow_dispatch）すると、
指定した都道府県の S0〜S3 を CI 上で実行し、`work/` の成果物を
アーティファクトとして取得できる。ローカルに Python が無くても回せる。

CI では `cache/` が毎回空なので、実行のたびに実際に取得が走る。
同じ都道府県を何度も回さないこと。
