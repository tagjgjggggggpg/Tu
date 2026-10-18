# openverse-sfx リファレンス

## 依存ツール

| ツール | 用途 | 導入 |
|---|---|---|
| Python 3.8+ | スクリプト本体（標準ライブラリのみ、pip 不要） | 通常は導入済み |
| ffmpeg / ffprobe | 解析・コンタクトシート・ミックス | Ubuntu/Debian: `sudo apt-get install -y ffmpeg` / macOS: `brew install ffmpeg` / Windows: `winget install Gyan.FFmpeg` |

確認: `python3 .claude/skills/openverse-sfx/scripts/sfx.py check`
（ffmpeg の有無、Openverse への疎通、残りの匿名 API 回数を表示）

環境変数（任意）:

- `OPENVERSE_TOKEN` — Openverse の OAuth アクセストークン（登録すると上限が少し上がる）。
- `OPENVERSE_SFX_CACHE` — キャッシュ場所（既定 `~/.cache/openverse-sfx`、検索結果は7日保持、音源は無期限）。
- `OPENVERSE_SFX_UA` — User-Agent。Wikimedia は連絡先入りの UA を求めるため、自分の URL/メールを入れると良い。

## 処理の流れとファイル

```
<video>                        元動画（読み取りのみ）
<stem>_sfx_work/
  analysis.json                尺・解像度・カット・動きのピーク・音声区間
  sheet.jpg                    ショットごと1枚のコンタクトシート（Claude が見る唯一の画像）
  cues.json                    効果音の配置計画（編集用）
  manifest.json                選ばれた音源・ライセンス・候補・ピーク音量
  sfx/                         ダウンロードした音源のコピー
<stem>_sfx.<ext>               完成動画（映像は無劣化コピー、音声は AAC 192k）
credits.txt                    使用音源の出典・ライセンス（出力先フォルダ。--credits で変更可）
```

サブコマンド: `check` / `analyze` / `plan` / `fetch` / `mix` / `run`（analyze→plan→fetch→mix を一括）。
主なオプション: `--work DIR`、`--threshold 0.3`（カット検出感度）、`--per-seconds 2.5`・`--min-gap 1.2`（密度上限）、
`--motion`（動きのピークにも swish を置く）、`--sources freesound,wikimedia_audio`、`-o OUT`、`--credits PATH`、`--force`。

cues.json の例:

```json
{"cues": [
  {"t": 1.75, "query": "whoosh", "max_dur": 1.0, "reason": "カット(2.0s)への転換"},
  {"t": 4.10, "query": "pop", "max_dur": 0.5, "gain_db": -2, "reason": "テロップ出現"},
  {"t": 6.00, "query": "impact", "sound_id": "eab8a6e2-...", "reason": "決めカット"}
]}
```

## 自動化できる範囲と制約

| 工程 | 自動でできること | 制約・人/Claude の判断が要るところ |
|---|---|---|
| 動画解析 | FFmpeg の scene スコアでカット検出、カット未満の大きな変化を「動きのピーク」として抽出、silencedetect で音声が鳴っている区間、volumedetect で平均音量 | 意味（何が起きているか）は数値からは分からない。コンタクトシート1枚を Claude が見て判断する。テロップ出現・口パク・物の衝突など細かい瞬間は検出しないので、必要なら手で cue を足す |
| 選音 | タイトル/タグ一致・長さの近さ・ライセンス段位・不要語（loop, music, pack 等）でスコア付けし最上位を採用。候補3件を manifest に残す | 音そのものは聴いていない（タイトルとタグで判断）。違和感があれば alternatives から差し替え |
| ライセンス | CC0/PDM を優先し、不足時のみ CC BY。API の license フィールドで再フィルタ。出典・帰属文を credits.txt と manifest.json に保存 | Openverse はライセンスを検証していない（利用規約 §3）。公開前に credits.txt の Source URL を確認すること |
| 配置 | 先頭無音の除去、長さトリム+フェードアウト、音量正規化、ナレーション区間での減衰、リミッター、映像は再エンコードなし | 本格的なサイドチェーン・ダッキング（BGM を下げる）はしない。効果音側を下げる方式 |
| 出力 | 元動画を上書きしない、既存出力は `--force` が無いと拒否、`credits.txt` を出力 | — |

### 無人モード（`run`）の振る舞い

カットの 0.25 秒前に `whoosh` を置くだけの保守的なルール（2.5 秒に1つまで、強いカット優先）。
内容に合った音にしたい場合は通常モード（Claude が sheet.jpg を見て cues.json を編集）を使う。
ショート動画パイプラインでは「analyze → Claude が cues.json を生成 → fetch → mix」を1動画ずつ回すのが
品質とトークンのバランスが良い（画像は1枚、JSON は数百トークン）。

## ライセンス方針

- **CC0 / PDM**: 帰属不要・商用可。最優先。
- **CC BY**: 商用可だが帰属必須。credits.txt の Attribution 行を概要欄・エンドロール等に記載する。
- **BY-SA は除外**: CC 4.0 では音を映像に同期させると「翻案物」となり、動画全体を BY-SA で公開する義務が生じるため。
- **NC / ND / sampling+ / nc-sampling+ は除外**: 商用不可、または改変（トリム・音量変更）不可。

## Openverse API 利用条件（2026-10 時点で確認）

- エンドポイント: `GET https://api.openverse.org/v1/audio/`（認証不要）。
- 匿名レート制限（レスポンスヘッダで確認）: **20 回/分、200 回/日**。超えると 429。
  スクリプトは 3.1 秒間隔、429 時は Retry-After 待機、残り 20 回未満で警告。
- 匿名は `page_size` 最大 20、1クエリあたり取得できる総件数にも上限あり。スクレイピングは禁止。
- 利用規約（https://docs.openverse.org/terms_of_service.html）の要点: レート制限遵守、複数マシンでの回避禁止、
  CC 作品への適切な帰属、ライセンス状態は利用者が自分で確認、Openverse を使った旨と「Openverse による保証・承認ではない」旨の表示。
- 使用パラメータ: `q`, `license`（例 `cc0,pdm` / `by`）, `source=freesound`（効果音の主供給元）,
  `length=shortest,short`（短い音源のみ）, `page_size=20`。`category=sound_effect` は Freesound の結果に
  カテゴリが付いていないため 0 件になるので使わない。

### 呼び出し回数

ユニークな query ごとに最大 2 回（CC0/PDM → 不足時のみ BY）× ソース数。検索結果は7日キャッシュされるため、
同じ語彙を使い回すほど API 消費はゼロに近づく。典型的な 30 秒ショート（query 3〜4種）で 3〜8 回。

## クエリ語彙

英語・1〜3語が最もヒットする。

| 演出 | query 例 |
|---|---|
| カット・場面転換 | `whoosh`, `swoosh`, `transition swoosh`, `riser` |
| 速い動き・スワイプ | `swish`, `swipe` |
| テロップ・アイコン出現 | `pop`, `bubble pop`, `click`, `ui click` |
| 衝撃・決めカット | `impact`, `punch`, `hit`, `thud` |
| 驚き・強調 | `ding`, `bell`, `chime` |
| キラキラ・魔法 | `sparkle`, `magic chime`, `shimmer` |
| 失敗・ボケ | `cartoon boing`, `buzzer`, `fail` |
| 写真・スクショ | `camera shutter` |
| タイピング | `keyboard typing` |
| グリッチ | `glitch`, `static` |

## トラブルシュート

- **`cdn.freesound.org returned 403`**: Freesound はクラウド/データセンターの IP を遮断することがある。
  ローカル PC では通常そのまま動く。クラウド環境では Wikimedia へフォールバックするが、効果音は少ない。
- **`upload.wikimedia.org returned 429`**: Wikimedia の原本ファイル配信の制限。時間を置くか、`OPENVERSE_SFX_UA` に連絡先を入れる。
- **`no usable cues; nothing to mix`**: 全 cue で音源が取れなかった。query を言い換えるか、ネットワーク環境を確認。
- **効果音が大きすぎ/小さすぎ**: cue に `gain_db`（例 -4 / +3）を付けて `mix --force` だけ再実行（API 呼び出し不要）。
