# telecodex

English comes first in this README, and each section is followed by Japanese.  
この README は英語が先にあり、各セクションのあとに日本語が続きます。

Telegram Bot messages routed into a local `codex exec` session.  
Telegram Bot のメッセージを、ローカルの `codex exec` セッションに渡すためのブリッジです。

This project is a small bridge for people who already use Codex CLI on a machine and want to send prompts from Telegram without wiring up the OpenAI API directly. The bot polls Telegram updates, forwards allowed messages into local Codex CLI, and sends the final answer back to Telegram.  
このプロジェクトは、すでにマシン上で Codex CLI を使っていて、OpenAI API を直接つながずに Telegram からプロンプトを送りたい人向けの小さなブリッジです。Bot は Telegram の更新をポーリングし、許可されたメッセージをローカルの Codex CLI に転送し、最終回答を Telegram に返します。

## What It Does / 何をするものか

- Receives Telegram messages through a bot  
  Telegram のメッセージを Bot 経由で受け取ります
- Restricts access to one allowed Telegram username  
  許可した 1 つの Telegram ユーザー名だけにアクセスを制限します
- Invokes local `codex exec` for each request  
  各リクエストごとにローカルの `codex exec` を実行します
- Sends the final Codex reply back to Telegram  
  Codex の最終返信を Telegram に返します
- Stores a short local conversation history per chat  
  チャットごとに短い会話履歴をローカル保存します

## How It Works / 仕組み

`telecodex` does not call the OpenAI API itself. Instead, it reuses the machine's existing Codex CLI login state and shells out to `codex exec`.  
`telecodex` 自体は OpenAI API を直接呼びません。代わりに、そのマシンにある既存の Codex CLI のログイン状態を再利用し、`codex exec` を外部実行します。

Current request flow:  
現在のリクエスト処理の流れ:

1. Telegram user sends a message to the bot  
   Telegram ユーザーが Bot にメッセージを送る
2. `bridge.py` fetches the update with long polling  
   `bridge.py` がロングポーリングで更新を取得する
3. The message is turned into a prompt with recent chat context  
   メッセージが最近のチャット文脈つきのプロンプトに変換される
4. Local Codex CLI runs once and writes its final reply to a temp file  
   ローカルの Codex CLI が 1 回実行され、最終返信を一時ファイルに書き出す
5. The reply is posted back to Telegram  
   返信が Telegram に投稿される

## Requirements / 必要なもの

- Linux machine with Python 3  
  Python 3 が入った Linux マシン
- Telegram bot token from `@BotFather`  
  `@BotFather` で取得した Telegram Bot トークン
- Codex CLI already installed and logged in on the same machine  
  同じマシン上で、すでにインストールとログインが済んでいる Codex CLI
- Network access for Telegram polling  
  Telegram のポーリングに必要なネットワーク接続

Important:  
重要:

- This repository currently assumes a local Codex CLI installed under the author's NVM path inside `bridge.py`  
  現在のリポジトリは、`bridge.py` の中で作者の NVM 配下にある Codex CLI を前提にしています
- You will likely need to adjust the `CODEX_NODE` and `CODEX_JS` paths for your own machine  
  多くの場合、自分の環境に合わせて `CODEX_NODE` と `CODEX_JS` のパスを調整する必要があります
- This is a pragmatic personal bridge, not a polished portable package yet  
  これは実用優先の個人用ブリッジであり、まだ洗練されたポータブルなパッケージではありません

## Files / ファイル構成

- `bridge.py`: main bridge process  
  メインのブリッジ処理
- `config.example.json`: configuration template  
  設定ファイルのテンプレート
- `systemd/telegram-codex-bridge.service`: example user service  
  `systemd` のユーザーサービス例
- `state.json`: runtime state file, created locally  
  実行時にローカル作成される状態ファイル
- `bridge.log`: runtime log file, created locally  
  実行時にローカル作成されるログファイル

## Setup / セットアップ

1. Clone the repository.  
   リポジトリをクローンします。
2. Copy the config template.  
   設定テンプレートをコピーします。
3. Fill in your bot token and allowed Telegram username.  
   Bot トークンと許可する Telegram ユーザー名を入力します。
4. Adjust the hardcoded Codex CLI paths in `bridge.py` if needed.  
   必要なら `bridge.py` 内のハードコードされた Codex CLI のパスを調整します。
5. Run the bridge once to verify it works.  
   まず 1 回実行して動作確認します。

```bash
cp config.example.json config.json
python3 bridge.py --once
```

To run continuously:  
継続実行する場合:

```bash
python3 bridge.py
```

## systemd User Service / systemd ユーザーサービス

An example service file is included at `systemd/telegram-codex-bridge.service`.  
`systemd/telegram-codex-bridge.service` にサービスファイルの例があります。

Before using it:  
使う前に次を更新してください:

- update the repository path in `ExecStart`  
  `ExecStart` 内のリポジトリパス
- update the repository path in `WorkingDirectory`  
  `WorkingDirectory` 内のリポジトリパス

Then install and start it:  
その後、インストールして起動します:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/telegram-codex-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telegram-codex-bridge.service
```

## Configuration / 設定

Example `config.json`:  
`config.json` の例:

```json
{
  "telegram_bot_token": "REPLACE_WITH_BOT_TOKEN",
  "allowed_username": "@your_username",
  "model": "gpt-5.4",
  "max_history": 12,
  "telegram_timeout_seconds": 25,
  "codex_timeout_seconds": 180
}
```

## Security Notes / セキュリティ注意点

- Do not commit `config.json`  
  `config.json` はコミットしないでください
- Do not commit your bot token  
  Bot トークンはコミットしないでください
- Anyone with your bot token can control your bot  
  Bot トークンを持つ人は誰でもその Bot を操作できます
- This bridge intentionally trusts the local Codex login state on the machine  
  このブリッジは、そのマシン上のローカル Codex ログイン状態を信頼する前提です

## Limitations / 制限事項

- The current implementation uses `codex exec --ephemeral`, so it is not a true persistent Codex session  
  現在の実装は `codex exec --ephemeral` を使っているため、本当の永続セッションではありません
- Context is approximated by replaying recent chat history into each prompt  
  文脈は、最近のチャット履歴を各プロンプトに再投入することで近似しています
- Telegram access control is username-based, which is simple but not the strongest option  
  Telegram のアクセス制御はユーザー名ベースで、単純ですが最も強固な方法ではありません
- Codex CLI path discovery is not automatic yet  
  Codex CLI のパス検出はまだ自動化されていません
- CLI behavior may break if future Codex versions change `exec` behavior  
  将来の Codex バージョンで `exec` の挙動が変わると動かなくなる可能性があります

## Future Improvements / 今後の改善案

- Resume real Codex sessions instead of replaying history  
  履歴の再投入ではなく、本物の Codex セッション再開に対応する
- Move secrets to environment files instead of `config.json`  
  秘密情報を `config.json` ではなく環境ファイルに移す
- Support `from.id` or `chat_id` allowlists  
  `from.id` や `chat_id` ベースの許可リストに対応する
- Auto-detect Codex CLI path  
  Codex CLI のパスを自動検出する
- Add command routing such as `/ask`, `/reset`, and `/status`  
  `/ask`、`/reset`、`/status` のようなコマンドルーティングを追加する
