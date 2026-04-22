#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "bridge.log"
AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_NODE = Path.home() / ".nvm" / "versions" / "node" / "v24.15.0" / "bin" / "node"
CODEX_JS = Path.home() / ".nvm" / "versions" / "node" / "v24.15.0" / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"


SYSTEM_PROMPT = """You are Codex, a pragmatic coding assistant running through a Telegram bridge on a Raspberry Pi.
Keep answers concise and actionable. Assume the user may ask about the local machine, software setup, shell commands,
GitHub workflows, and coding tasks. You are replying inside Telegram, so avoid long answers and keep them scannable.
If you are unsure, state uncertainty directly."""


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def log_line(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def http_json(url: str, payload=None, headers=None, timeout=60):
    body = None
    request_headers = {"User-Agent": "telegram-codex-bridge/1.0"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class TelegramCodexBridge:
    def __init__(self, config):
        self.config = config
        self.state = load_json(STATE_PATH, {"last_update_id": 0, "conversations": {}})
        self.bot_token = config["telegram_bot_token"]
        self.allowed_username = config["allowed_username"].lstrip("@").lower()
        self.model = config.get("model", "gpt-5.4")
        self.max_history = int(config.get("max_history", 12))
        self.codex_timeout = int(config.get("codex_timeout_seconds", 180))
        self.telegram_base = f"https://api.telegram.org/bot{self.bot_token}"

    def save_state(self):
        save_json(STATE_PATH, self.state)

    def get_updates(self):
        params = {
            "timeout": self.config.get("telegram_timeout_seconds", 25),
            "allowed_updates": json.dumps(["message"]),
        }
        if self.state["last_update_id"]:
            params["offset"] = self.state["last_update_id"] + 1
        url = f"{self.telegram_base}/getUpdates?{urllib.parse.urlencode(params)}"
        return http_json(url, timeout=params["timeout"] + 10)

    def send_message(self, chat_id: int, text: str):
        payload = {"chat_id": chat_id, "text": text[:4000]}
        return http_json(f"{self.telegram_base}/sendMessage", payload=payload)

    def chat_history(self, chat_id: int):
        return self.state["conversations"].setdefault(str(chat_id), [])

    def append_history(self, chat_id: int, role: str, text: str):
        history = self.chat_history(chat_id)
        history.append({"role": role, "content": text})
        if len(history) > self.max_history:
            del history[:-self.max_history]

    def build_prompt(self, prompt: str, chat_id: int) -> str:
        history = self.chat_history(chat_id)
        lines = [SYSTEM_PROMPT, "", "Conversation so far:"]
        for item in history:
            role = "User" if item["role"] == "user" else "Assistant"
            lines.append(f"{role}: {item['content']}")
        lines.append("")
        lines.append(f"User: {prompt}")
        lines.append("Assistant:")
        return "\n".join(lines)

    def ask_codex_exec(self, prompt: str, chat_id: int) -> str:
        run_id = f"{chat_id}-{int(time.time() * 1000)}"
        output_path = BASE_DIR / f".last_message_{run_id}.txt"
        full_prompt = self.build_prompt(prompt, chat_id)
        cmd = [
            str(CODEX_NODE),
            str(CODEX_JS),
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "-C",
            str(Path.home()),
            "-m",
            self.model,
            "-o",
            str(output_path),
            full_prompt,
        ]
        env = dict(os.environ)
        env["PATH"] = f"{CODEX_NODE.parent}:{env.get('PATH', '')}"
        try:
            subprocess.run(
                cmd,
                cwd=str(Path.home()),
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.codex_timeout,
            )
            if output_path.exists():
                text = output_path.read_text(encoding="utf-8").strip()
                if text:
                    return text
            raise RuntimeError("Codex did not produce a final message")
        except subprocess.CalledProcessError as exc:
            snippet = (exc.stdout or "").strip()[-1200:]
            raise RuntimeError(f"Codex exec failed. {snippet}") from exc
        finally:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass

    def handle_message(self, update):
        self.state["last_update_id"] = update["update_id"]
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        from_user = message.get("from") or {}
        chat = message.get("chat") or {}
        username = (from_user.get("username") or "").lower()
        chat_id = chat.get("id")
        if not text or not chat_id:
            return
        if username != self.allowed_username:
            log_line(f"ignored message from username={username!r}")
            self.send_message(chat_id, "このBotは許可されたユーザー専用です。")
            return
        if text == "/start":
            self.send_message(chat_id, "接続済みです。メッセージを送ると Codex ブリッジ経由で返答します。")
            return
        if text == "/reset":
            self.state["conversations"][str(chat_id)] = []
            self.send_message(chat_id, "会話履歴をリセットしました。")
            return
        if text == "/help":
            self.send_message(chat_id, "/start, /help, /reset が使えます。通常メッセージは Codex ブリッジへ送ります。")
            return
        if text == "/status":
            node_ok = CODEX_NODE.exists()
            js_ok = CODEX_JS.exists()
            self.send_message(chat_id, f"bridge: running\nallowed_username: @{self.allowed_username}\ncodex_node: {'ok' if node_ok else 'missing'}\ncodex_cli: {'ok' if js_ok else 'missing'}")
            return
        try:
            self.append_history(chat_id, "user", text)
            answer = self.ask_codex_exec(text, chat_id)
            self.append_history(chat_id, "assistant", answer)
            self.send_message(chat_id, answer)
            log_line(f"replied to chat_id={chat_id} username={username}")
        except Exception as exc:
            log_line(f"error while handling message: {exc}")
            self.send_message(chat_id, f"処理に失敗しました: {exc}")

    def run_once(self):
        updates = self.get_updates()
        for update in updates.get("result", []):
            self.handle_message(update)
        self.save_state()

    def run_forever(self):
        while True:
            try:
                self.run_once()
            except urllib.error.URLError as exc:
                log_line(f"network error: {exc}")
                time.sleep(5)
            except Exception as exc:
                log_line(f"fatal loop error: {exc}")
                time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = load_json(CONFIG_PATH, None)
    if not config:
        raise SystemExit(f"Missing config file: {CONFIG_PATH}")
    bridge = TelegramCodexBridge(config)
    if args.once:
        bridge.run_once()
    else:
        bridge.run_forever()


if __name__ == "__main__":
    main()
