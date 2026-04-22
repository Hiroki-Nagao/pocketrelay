# telecodex

Telegram Bot messages routed into a local `codex exec` session.

This project is a small bridge for people who already use Codex CLI on a machine and want to send prompts from Telegram without wiring up the OpenAI API directly. The bot polls Telegram updates, forwards allowed messages into local Codex CLI, and sends the final answer back to Telegram.

## What It Does

- Receives Telegram messages through a bot
- Restricts access to one allowed Telegram username
- Invokes local `codex exec` for each request
- Sends the final Codex reply back to Telegram
- Stores a short local conversation history per chat

## How It Works

`telecodex` does not call the OpenAI API itself. Instead, it reuses the machine's existing Codex CLI login state and shells out to `codex exec`.

Current request flow:

1. Telegram user sends a message to the bot
2. `bridge.py` fetches the update with long polling
3. The message is turned into a prompt with recent chat context
4. Local Codex CLI runs once and writes its final reply to a temp file
5. The reply is posted back to Telegram

## Requirements

- Linux machine with Python 3
- Telegram bot token from `@BotFather`
- Codex CLI already installed and logged in on the same machine
- Network access for Telegram polling

Important:

- This repository currently assumes a local Codex CLI installed under the author's NVM path inside `bridge.py`
- You will likely need to adjust the `CODEX_NODE` and `CODEX_JS` paths for your own machine
- This is a pragmatic personal bridge, not a polished portable package yet

## Files

- `bridge.py`: main bridge process
- `config.example.json`: configuration template
- `systemd/telegram-codex-bridge.service`: example user service
- `state.json`: runtime state file, created locally
- `bridge.log`: runtime log file, created locally

## Setup

1. Clone the repository.
2. Copy the config template.
3. Fill in your bot token and allowed Telegram username.
4. Adjust the hardcoded Codex CLI paths in `bridge.py` if needed.
5. Run the bridge once to verify it works.

```bash
cp config.example.json config.json
python3 bridge.py --once
```

To run continuously:

```bash
python3 bridge.py
```

## systemd User Service

An example service file is included at `systemd/telegram-codex-bridge.service`.

Before using it:

- update the repository path in `ExecStart`
- update the repository path in `WorkingDirectory`

Then install and start it:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/telegram-codex-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telegram-codex-bridge.service
```

## Configuration

Example `config.json`:

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

## Security Notes

- Do not commit `config.json`
- Do not commit your bot token
- Anyone with your bot token can control your bot
- This bridge intentionally trusts the local Codex login state on the machine

## Limitations

- The current implementation uses `codex exec --ephemeral`, so it is not a true persistent Codex session
- Context is approximated by replaying recent chat history into each prompt
- Telegram access control is username-based, which is simple but not the strongest option
- Codex CLI path discovery is not automatic yet
- CLI behavior may break if future Codex versions change `exec` behavior

## Future Improvements

- Resume real Codex sessions instead of replaying history
- Move secrets to environment files instead of `config.json`
- Support `from.id` or `chat_id` allowlists
- Auto-detect Codex CLI path
- Add command routing such as `/ask`, `/reset`, and `/status`
