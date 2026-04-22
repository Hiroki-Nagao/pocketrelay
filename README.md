# Telegram Codex Bridge

Telegram Bot で受けたメッセージを、このラズパイ上の `codex exec` に流して返答する簡易ブリッジです。

## Files

- `bridge.py`: メインスクリプト
- `config.example.json`: 設定テンプレート
- `state.json`: 最終 update_id と会話履歴
- `bridge.log`: 実行ログ
- `systemd/telegram-codex-bridge.service`: user service テンプレート

## Usage

まず設定テンプレートをコピーして `config.json` を作成します。

```bash
cp config.example.json config.json
```

一度だけ処理:

```bash
python3 bridge.py --once
```

常駐:

```bash
python3 bridge.py
```

systemd user service を使う場合は、サービスファイル内のパスを実際の配置先に合わせて調整してから `~/.config/systemd/user/` へ配置します。

## Notes

- `@cho4649` 以外は拒否します。
- OpenAI API キーは不要です。ローカルの Codex CLI ログイン状態をそのまま使います。
- 返答ごとに `codex exec --ephemeral` を起動するため、完全な同一セッション継続ではありません。会話の直近履歴だけをプロンプトへ再注入します。
- このブリッジは Telegram 上の別セッションです。現在の Codex TUI セッションそのものを直接遠隔操作するものではありません。
