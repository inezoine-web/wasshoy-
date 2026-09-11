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

## 語彙が天井になっている (2026-09-09に判明)

`extract.py` の `FESTIVAL_WORD` は茨城・愛知・静岡の実データから育てた語彙で、
**既知の祭りから作ったので既知の祭りしか見つけられない**。実測:

| 対象 | 不一致 |
| --- | --- |
| 本州の有名どころ (祇園祭・ねぶた・阿波おどり…) | 0/7 (0%) |
| 沖縄 (エイサー・パーントゥ・シヌグ…) | 22/27 (81%) |
| 北海道・アイヌ (イオマンテ・カムイノミ…) | 11/13 (85%) |
| その他の地方 (なまはげ・御柱・六斎念仏・田遊び…) | 24/26 (92%) |

沖縄・北海道の特別チューニングの問題ではない。「その他の地方」が最も悪く、
なまはげも御柱もユネスコ登録の有名な行事である。当初目的
(知らなかった祭り・マイナーな祭りを見つける) に対して、天井はクロール予算
ではなく**検出器そのもの**だった。

対策は名前で判定するのをやめ、**指定の枠**で引くこと。「重要無形民俗文化財」
という枠は名前に依存しないので、語彙を知らないままパーントゥもアイヌ古式舞踊も
出てくる (S0.5)。そこから県別語彙を機械生成する (S0.6)。

あわせて**指標も差し替えた**。`evaluate.py` の recall は
`benchmarks/ibaraki-2026-09-06` (旧AI手法の出力そのもの) への一致率なので、
「すでに見つけたものを、また見つける」性能しか測らない。新規に発見した祭りは
0点になる。`novelty.py` (S6b) で「既存データにもWikipediaにも無い件数」を測る。
recall は**壊れていないことの確認**として残す。

### 県・市町村指定をどこから取るか

**2026-09-11 追記: 見つかった。** 東京文化財研究所の「無形文化遺産総合データベース」
(https://mukeinet.tobunken.go.jp/index.php?gid=10027) が、国・都道府県・市区町村の
指定・選択と、一部の県では未指定の行事まで、全国 9,991 行を**1リクエストのCSV**で
返す (`tobunken.py`, S0.5d)。無形民俗で祭り・行事の種別に絞ると 8,614 行、
1,229 市区町村。公開範囲は県ごとに A〜E で、東京・神奈川・島根は A (未指定まで)、
山梨・京都は未収集、青森は非公開。

これを正解の近似にして、処理済み5県の再現率を測った:

```
県      指定件数   festivals.json が持つ   再現率
茨城       149            70               47%
静岡       171            56               33%
愛知       315           119               38%
栃木       228            70               31%
千葉       273           174               64%   <- 県の悉皆表を読めた分
```

存在が確実で市町村も分かっている指定済みの行事でも、クロールでは3〜6割しか
拾えていなかった。以後は東文研DBの行を**候補の種** (`tobunken.py --seed`) と
**語彙** (`vocab.py`) に使い、クロールは未指定の層を探す役に回す。
S4 の所在解決 (`s4_apply._place_sources`) と S1b の零自治体判定も、この台帳を見る。

注意点: 所在住所に都道府県名が無い行が 86% あるので県は一覧側の列から取る。
CSV は cp932 で「蒅」「衹」等が `?` になるので名称は UTF-8 の一覧側を正とする。
所在が「全域」「会津地方」「千代田区,江戸川区」のような行 (321) は `(県全域)` に
する。北海道は「道南/道央・道北/道東」で来る。読み仮名の充足は 42%。

以下は、それ以前に試して外れた記録。

国指定は `bunkazai.py` で全国966件が取れるが、県指定・市町村指定を全国一括で
持つ、使える口は当初**見つからなかった**。試した結果:

- **文化遺産オンライン** (bunka.nii.ac.jp) — 135,397件を持ち robots.txt も全許可
  だが、検索結果がJS描画。フォームが宣言する `POST /heritages/relatedsearch` は
  405 を返し、GET では結果が出ない。`Crawl-Delay:3` なので仮に引けても全件113時間
- **自治体の「指定文化財一覧」PDF** — 形が悪い。2段組が交互に出る (蒲郡市)、
  フォントに ToUnicode が無く pdftotext で日本語0文字になる (半田市)

結局、自治体・教育委員会の**HTMLページ**が一番素直だった。「県指定 無形民俗
文化財 ○○」の形で載っており、S1でクロール済みならキャッシュにあるので
ネットに出ずに拾える (`bunkazai_local.py`)。取れるのはクロールした県だけだが、
語彙は県ごとに作るものなので順序としては問題ない。

**ただし現状の頭打ちは抽出器ではなくクロールの射程である。** 茨城で実測:

```
国指定 (bunkazai.py)              17件
県・市町村指定 (bunkazai_local.py) 29件   -> 1.7倍。「数十倍」には遠い
茨城県の新規語                    5語 -> 15語

理由: クロール済み6850ページのうち文化財らしきページは409枚 (6.0%)。
      1自治体あたり中央値9枚で、7自治体 (日立市 石岡市 笠間市 結城市
      茨城町 行方市 鉾田市) は0枚。指定は存在するがキャッシュに無い。
```

S1 は1自治体40ページの一般クロールで、文化財セクションを狙っていない。
そこで `discover.py` に文化財レンズ (第2段, `--bunkazai-pages`) を足した。

### 文化財レンズの予算は増やしても伸びない (2026-09-10に実測)

`--bunkazai-pages` を 15 から 45 に上げて茨城で測った。レンズが取る
文化財ページ自体は増える:

```
 N   文化財ページ  一覧ページ  レンズ総ページ  取得効率
  5      57          7            155         37%
 15     178         16            457         39%
 20     239         18            607         39%   <- ここまで効率が保つ
 25     288         18            757         38%
 30     324         18            907         36%
 45     401         26           1334         30%
```

**しかし県・市町村指定の件数は 69件のまま1件も増えなかった。**
語彙も26語のまま。ネットに535回余計に出た見返りがゼロである。

増えた分の中身は個別の物件ページ・補助金の案内・「有形文化財一覧」
「登録有形文化財一覧」「市史資料目録」で、種別列に無形民俗を持つ一覧では
なかった。**予算は律速ではない。**

律速は別のところにある。69件に寄与しているのは **12/45自治体** だけで、
残り33自治体は0件。文化財ページが1枚も取れない自治体も10残る (予算を
増やしても10のまま減らない)。これらのサイトは文化財の区画へリンクが
届いていないか、別ホスト(教育委員会・資料館)に置いている。
次に効くのはそこの解決であって、予算ではない。

既定値は 15 のままにしてある。20 まで上げても効率は落ちないが、
指定件数への寄与が確認できていないので既定は動かさない。

### 書式チューニングは打ち切った (2026-09-10)

`bunkazai_local.py` に書式の型を足していく方法は**スケールしない**。実測:

- 変動の単位は県でもCMSでもなく**自治体ごと**。石岡市・結城市・常総市・潮来市は
  同じCMS (`page\d+.html`) だが、石岡市は「見出しに指定区分/セルに種別」、
  結城市は「セルに指定区分/見出しに種別」と**逆**だった。CMSはページ編集機能を
  与えるだけで、表は各自治体の担当者が手で組んでいる
- 茨城1県だけで4通りの型が出て、実装は2回巻き戻した (614件→255件→83件)

書式に依存せず成立した規則は1つだけ:

> **指定区分と種別のうち、文脈から補ってよいのは片方だけ。両方とも
> 書いていない行は根拠にならない。**

これで83件・14/45自治体。残りは任意の表の意味を読む仕事なので、そこだけ
AIに渡す (S0.5c)。対象は機械的に定義できる ——「無形民俗等を含むのに
`bunkazai_local` が0件だったページ」。**AIに探索させない。**渡すのは確定した
小さな入力で、やるのは「この表から指定されている行事名を書き出す」だけ。

整形は `bunkazai_ai_prepare.py` が行う。素のHTMLを渡すと WordPress の
inline JS と CSS が本文を埋めるため、script/style/nav を落として見出しと表
だけを残す。実測で **1ページ930文字 → 375文字**。

任務カードは `pipeline/bunkazai_ai_card.md` に置き、バッチ先頭へそのまま
埋め込む。サブエージェントには**カード以外の規約ファイルを配らない**
(AGENTS.md 全文を配ると枠を食う)。戻り値は4列固定
`page_id / designation / kind / name` で、ページ本文が親のコンテキストへ
流れ込まないようにする。**IDとローマ字は委託しない** —
`bunkazai_ai_apply.py` が受け取るのは名称までで、slug生成は機械工程が行う。

## 工程

| 段階 | スクリプト | ネット | AI | 役割 |
| --- | --- | --- | --- | --- |
| S0 | `registry.py` | 要 | 不要 | 全国市区町村コード表と、公式サイト/観光協会URLの台帳 |
| S0.5 | `bunkazai.py` | 要 | 不要 | 文化庁DBから無形の民俗文化財を種別×県で取得 (全国966件) |
| S0.5b | `bunkazai_local.py` | 不要 | 不要 | 県・市町村指定をキャッシュ済みHTMLから拾う (クロール済みの県のみ) |
| S0.5c | `bunkazai_ai_prepare.py` / `bunkazai_ai_apply.py` | 不要 | **要** | 機械抽出が0件だったページだけAIに読ませる |
| S0.5d | `tobunken.py` | 要 | 不要 | 東文研DBから全国の無形民俗を取得 (8,614行)。語彙と候補の種にする |
| S0.6 | `vocab.py` | 不要 | 不要 | 指定名称から**県別の行事語彙**を生成 (6,851語) |
| S0.7 | `gazetteer.py` | 要 | 不要 | Wikipediaの祭り記事一覧 = **既知の除外リスト** (全国1609件) |
| S1 | `discover.py` | 要 | 不要 | 台帳のトップページから祭り情報のあるページを幅優先で収集 |
| S2 | `extract.py` | 不要 | 不要 | キャッシュ済みページから候補行を抽出 |
| S3 | `normalize.py` | 不要 | 不要 | 重複統合・日付仕分け・カテゴリ推定・slug生成 |
| S4 | `s4_prepare.py` / `s4_apply.py` | 不要 | **要** | 対象/対象外の判断、別名統合、所在の帰属、読み。**AIが要るのはここだけ** |
| S5 | `merge.py` | 不要 | 不要 | `data/festivals.json` へマージ。**既存データを読んでよいのはここと `evaluate.py` だけ** |
| S6 | `evaluate.py` | 不要 | 不要 | 凍結ベンチマークとの突合 |
| S6b | `novelty.py` | 不要 | 不要 | **新規性**の測定。既存データにもWikipediaにも無い件数 |

## 実行

```bash
# S0-1: 全国市区町村コード表 (一度だけ。結果はコミットする)
python pipeline/registry.py --build-municipalities

# S0-2: 対象都道府県の公式サイト/観光協会URLを解決 (結果はコミットする)
python pipeline/registry.py --resolve-sites --prefecture 茨城県

# S0-3: 民俗文化財の台帳と県別語彙 (一度だけ。結果はコミットする)
python pipeline/bunkazai.py --build          # 全国966件、約8分
python pipeline/bunkazai_local.py --build    # 県・市町村指定 (ネット不要、S1の後で)

# S0.5c: 残余をAIに渡す (S0.5b の後。唯一AIが要る工程その2)
python pipeline/bunkazai_ai_prepare.py --prefecture 茨城県 --limit 120
#   -> work/bunkazai_ai/bz_batch_NNN.txt (先頭に任務カードが入っている)
#      AIはこれを読み work/bunkazai_ai/bz_verdict_NNN.tsv を返す
python pipeline/bunkazai_ai_apply.py --prefecture 茨城県 --dry-run
python pipeline/bunkazai_ai_apply.py --prefecture 茨城県
# S0.5d: 東文研DB (一度だけ。全国分。結果はコミットする)
python pipeline/tobunken.py --fetch --build  # registry/bunkazai_tobunken.tsv
python pipeline/vocab.py --build --summary   # registry/vocab_regional.tsv
python pipeline/gazetteer.py --build         # 全国1609件、約12分

# S1: ページ収集 (時間がかかる。1市町村40ページ上限、同一ホスト1秒間隔)
python pipeline/discover.py --prefecture 茨城県 --max-pages 40

# S2-S3: 抽出と正規化 (ネットに出ない。何度でもやり直せる)
python pipeline/extract.py   --prefecture 茨城県
python pipeline/tobunken.py --seed --prefecture 茨城県   # 東文研の行を候補に足す (origin=tobunken)
python pipeline/normalize.py --prefecture 茨城県

# S4: AI判定 (唯一AIが要る工程)
python pipeline/s4_prepare.py --prefecture 茨城県 --batch-size 120
#   -> work/s4/s4_batch_NNN.tsv ができる。AIがこれを読み、
#      work/s4/s4_verdict_NNN.tsv を「同じ順序・同じ件数」で返す
python pipeline/s4_apply.py --prefecture 茨城県     # 機械チェックしてから適用
python pipeline/s4_apply.py --prefecture 茨城県 --strict   # 未回答があれば失敗させる

# S5: マージ (既存を入れ替える場合は --replace)
python pipeline/merge.py --prefecture 茨城県 --replace --dry-run   # まず確認
python pipeline/merge.py --prefecture 茨城県 --replace

# S6: 評価 (ベンチマークのある都道府県のみ)
python pipeline/evaluate.py --benchmark benchmarks/ibaraki-2026-09-06
python pipeline/evaluate.py --benchmark benchmarks/ibaraki-2026-09-06 --judged  # S4判定後

# S6b: 新規性 (知らなかった祭りをどれだけ見つけたか)
python pipeline/novelty.py --input work/judged.tsv --list 30
```

`merge.py` は書き込み前に**消費側チェック**を必ず通す。
`scripts/build_site.py` がガードなしで参照する
`name` / `status` / `confidence` / `prefecture` / `municipality` / `categories`
が非nullであること、`sources` が空でないこと、id が重複していないこと
(他県のレコードとの衝突も含む) を検査し、1つでも引っかかれば書き込まない。
過去に `summary: null` を212件追加してPagesのビルドを落としているため。

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

`s4_apply.py` は検証のあと、`keep` 行の名称を機械的に整形する（AI不要）。
一覧表の連番、指定区分の前置、「所在地 …」、末尾の括弧注記、投稿ナビの語、
`保存会` の接尾辞を落とす。**元の名称は `aliases` に残す。**
整形すると判定時には見えなかった重複が現れるので（「井草大杉囃子保存会」と
「井草大杉囃子」）、同一市町村で整形後の名称が一致する行は機械的に統合する。

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
python pipeline/check_leak.py     # 問題があれば exit 1
```

構文木の文字列リテラルを全部見る。grep では
`DATA = REPO_ROOT / "data" / "festivals.json"` のようにパスを変数へ
入れてから開く形を取り逃すため。既存データを読んでよいのは
`merge.py` と `evaluate.py` だけ。

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
| `UnicodeDecodeError: 'cp932' codec` | Windows の既定エンコーディングが cp932。ファイル入出力には必ず `encoding="utf-8"` を明示する。`scripts/build_site.py` は 2026-09-08 に修正済みで、ローカルでも `python scripts/build_site.py` が通る |
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
