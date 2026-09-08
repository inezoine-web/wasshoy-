# Festival data schema

`data/festivals.json` は調査済み・調査中の祭りを保存する軽量なデータセットです。初期段階ではDBを導入せず、Git diffで変更を追えるJSONを使います。

## Root

```json
{
  "schema_version": 1,
  "updated_at": null,
  "festivals": []
}
```

## Festival record

主なフィールド:

| field | description |
| --- | --- |
| `id` | 安定した一意ID。`都道府県-市町村-<名称部>`（下記） |
| `name` | 最も公的・正式と判断できる名称 |
| `aliases` | 通称、別表記、観光向け名称 |
| `status` | `candidate` / `verified` / `inactive` |
| `confidence` | `low` / `medium` / `high` |
| `prefecture` | 都道府県 |
| `municipality` | 市区町村 |
| `district` | 地区・町名等。確認できる場合 |
| `venue` | 主会場、神社寺院、通り等 |
| `location` | 緯度経度。確認できた場合のみ |
| `usual_schedule` | 例年の時期・規則。推測禁止 |
| `event_dates` | 年ごとの確認済み開催日 |
| `categories` | 祭礼、山車、神輿、火祭り等 |
| `features` | 特徴を短い語句で列挙 |
| `history` | 起源・歴史に関する確認済み概要 |
| `cultural_property` | 文化財指定情報 |
| `scale` | 規模を示す客観データ |
| `summary` | 根拠の範囲内での短い概要 |
| `sources` | 出典一覧 |
| `notes` | 食い違い・注意点・未解決事項 |
| `last_verified` | 最終確認日 `YYYY-MM-DD` |

## id

`<都道府県ローマ字>-<市町村ローマ字>-<名称部>` の3節。都道府県と市町村の
ローマ字は総務省コード表の読み仮名から機械的に起こす（`pipeline/romaji.py`）。

名称部は2通りある。

| 形 | 条件 | 例 |
| --- | --- | --- |
| ヘボン式ローマ字 | 名称の**読みが確認できた**とき | `ibaraki-hitachi-hitachinosasara` |
| `x` + SHA-1 先頭8桁 | 読みが確認できないとき | `ibaraki-sakai-x01a1086f` |

日本の行事名には「知らなければ読めない」漢字が多い（化蘇沼、鷲子、安居、
金砂…）。**読みを推測してIDを作らない。** 読めないものはハッシュに落とす。
ハッシュの種は `都道府県/市町村/名称の照合キー`（`第N回`・年号・空白・
`祭り`/`まつり`/`祭` の揺れを畳んだもの）なので、表記の揺れでは変わらない。

`x` で始まるかどうかで由来が一目でわかる（日本語のローマ字は `x` で始まらない）。

### 一度決めたIDは変えない

あとから読みが判明しても**IDは据え置く**。読みは別途記録すればよい。
IDを変えると既存レコードとの突合や重複検出が破綻するため、
可読性より同一性を優先する。

同様に、名称を整形したときも**IDは変えない**。整形前の名称は `aliases` に残す。

## Sources

```json
{
  "url": "https://example.jp/...",
  "title": "ページタイトル",
  "publisher": "○○市",
  "accessed": "2026-08-31",
  "supports": ["existence", "usual_schedule", "history"]
}
```

`supports` には、そのページで実際に確認できた事項だけを書く。

## event_dates

```json
[
  {
    "year": 2026,
    "start": "2026-09-12",
    "end": "2026-09-13",
    "status": "scheduled",
    "source_url": "https://example.jp/..."
  }
]
```

特定年の日付から例年の開催規則を推測しない。

## location

```json
{
  "latitude": 35.0000,
  "longitude": 140.0000,
  "precision": "venue"
}
```

`precision` は `venue` / `district` / `municipality` 等。座標を推測で作らない。

## cultural_property

```json
{
  "designated": true,
  "level": "national",
  "designation_name": "指定名称",
  "designation_type": "重要無形民俗文化財"
}
```

不明なら `null`。文化財指定がないと推測して `false` にしない。

## scale

初期段階では独自の星評価を保存しない。確認できた生データを残す。

```json
{
  "attendance": null,
  "floats": null,
  "mikoshi": null,
  "days": null,
  "participating_districts": null,
  "other": []
}
```

将来、これらの根拠値から別途スコアを計算できるようにする。

## categories の例

- `shrine_festival`
- `temple_festival`
- `mikoshi`
- `dashi`
- `yatai_float`
- `hikiyama`
- `fire_festival`
- `dance`
- `lion_dance`
- `traditional_performing_art`
- `bon_odori`
- `fireworks`
- `citizen_festival`
- `unusual_festival`
- `seasonal_flower_nature`
- `ichi_fair`
- `unclassified`

分類語彙は調査を進めながら必要に応じて追加する。既存カテゴリとの重複を避ける。
