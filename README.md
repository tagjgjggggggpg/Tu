# Tu

## Claude Code スキル: `/openverse-sfx`

動画を渡すと、カットや動きを解析し、[Openverse](https://openverse.org) から商用利用可能な効果音
（CC0 優先、必要時のみ CC BY）を検索・取得して FFmpeg で配置し、別ファイルの完成動画と `credits.txt` を出力します。

```
/openverse-sfx path/to/video.mp4              # Claude が内容を見て選音（推奨）
/openverse-sfx path/to/video.mp4 --auto       # 無人モード（カット直前に whoosh）
/openverse-sfx path/to/video.mp4 ポップで控えめに
```

スクリプト単体でも使えます（パイプライン組み込み用）:

```
python3 .claude/skills/openverse-sfx/scripts/sfx.py check
python3 .claude/skills/openverse-sfx/scripts/sfx.py run video.mp4      # analyze→plan→fetch→mix
```

- 依存: Python 3.8+（標準ライブラリのみ）、ffmpeg/ffprobe
- 詳細・制約・ライセンス方針: [.claude/skills/openverse-sfx/reference.md](.claude/skills/openverse-sfx/reference.md)

Sounds are found via Openverse; this project is not endorsed or certified by Openverse.
