---
name: openverse-sfx
description: 動画のカット・動き・演出を解析し、Openverse API から商用利用可能な効果音（CC0優先、CC BYは帰属表示付き）を検索・取得して FFmpeg で配置し、別ファイルの完成動画と credits.txt を出力する。動画に効果音(SFX)を付けたい、ショート動画の効果音を自動化したいときに使う。
argument-hint: <video> [--auto] [指示（例: ポップで控えめに）]
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/sfx.py *) Read Edit Write
---

# openverse-sfx

引数: `$ARGUMENTS`（先頭が動画パス。`--auto` があれば無人モード。残りはユーザーの演出指示）

スクリプトは `${CLAUDE_SKILL_DIR}/scripts/sfx.py`（標準ライブラリ + ffmpeg のみ）。
以下の `$SFX` は省略表記で、実行時は毎回 `python3 ${CLAUDE_SKILL_DIR}/scripts/sfx.py` と
フルに書く（シェル変数は呼び出し間で保持されない）。各ステップは
動画の隣の `<stem>_sfx_work/` に JSON を残すので、途中から再実行できる。

## 無人モード（`--auto`、パイプライン組み込み用）

`$SFX run <video>` を1回実行して、出力パスと credits.txt の要約を報告するだけ。
（カット検出 → カット直前に whoosh → 検索・取得 → ミックス）

## 通常モード（内容に合わせて選音する）

1. **依存確認**: `$SFX check`。ffmpeg が無ければ [reference.md](reference.md#依存ツール) の手順を案内して止まる。
2. **解析**: `$SFX analyze <video>`。出力 JSON（尺・カット・動きのピーク・音声の有無/音量/鳴っている区間）を読み、
   `sheet.jpg`（各ショット1枚のコンタクトシート、`#番号 秒`付き）を Read で**1回だけ**見る。
   フレームを個別に何枚も抽出しない（トークン節約）。
3. **計画**: `$SFX plan <video>` で `cues.json` の叩き台を作り、シートの内容に合わせて Edit する。
   - `t`: 音の開始秒。トランジション系はカットの 0.2〜0.3 秒前、衝撃・ポップ系はその瞬間。
   - `query`: 英語の短い SFX キーワード（1〜3語）。語彙は [reference.md](reference.md#クエリ語彙) 参照。
     同じ種類の演出は**同じ query に揃える**（API 呼び出しと音のばらつきが減る）。
   - `max_dur`（秒、既定 1.2）、`gain_db`（相対、既定 0）、`reason`（なぜ鳴らすか）。
   - 入れすぎない: 目安は 2.5 秒に1つ以下、ナレーション中は重要な瞬間だけ。意味のない cue は削除する。
   - ユーザーの指示（雰囲気・量）があれば優先する。
4. **取得**: `$SFX fetch <video>`。ログの `[query] -> 'タイトル' (license, source)` を確認し、
   タイトルが明らかに場面に合わなければ `manifest.json` の `alternatives` から `sound_id` を選び、
   その cue に `"sound_id": "..."` を足して再度 fetch（検索・音源はキャッシュされるので安い）。
   取得できなかった cue は query を言い換えて1回だけ再試行し、それでも無ければ諦める。
5. **ミックス**: `$SFX mix <video>`（既定出力 `<stem>_sfx.<ext>`、同じフォルダに `credits.txt`）。
   元動画は上書きしない。既存出力がある場合のみ `--force`。
6. **報告**: 出力パス、配置した効果音の一覧（秒・音・ライセンス）、CC BY 音源がある場合は
   「公開時に credits.txt の帰属表示を概要欄等に記載が必要」と伝える。

## ルール

- 使用可能ライセンスは CC0 / PDM（帰属不要）と CC BY（帰属必須）のみ。BY-SA・NC・ND・sampling+ は使わない（理由は reference.md）。
- Openverse 匿名枠は 20回/分・200回/日。スクリプトがキャッシュと間隔制御をするので、
  検索を手動の curl で重ねない。
- 音量は自動（元音声の平均音量 +8dB をピーク目標、音声が鳴っている区間はさらに -4dB、最後にリミッター）。
  ユーザーが「BGMに埋もれる」等と言ったら `gain_db` で調整する。
- 詳細仕様・制約・トラブルシュート: [reference.md](reference.md)
