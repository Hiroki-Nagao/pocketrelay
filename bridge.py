#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "bridge.log"

DEFAULT_SYSTEM_PROMPT = """You are a pragmatic coding assistant running through Pocketrelay on a user-managed machine.
Keep answers concise and actionable. Assume the user may ask about the local machine, software setup, shell commands,
GitHub workflows, and coding tasks. You are replying inside Telegram, so avoid long answers and keep them scannable.
If you are unsure, state uncertainty directly."""

CLI_PRESETS = {
    "codex": {
        "label": "Codex CLI",
        "command": [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "-C",
            "{workdir}",
            "-m",
            "{model}",
            "-o",
            "{output_path}",
            "{prompt}",
        ],
        "response_mode": "output_file",
    },
    "claude": {
        "label": "Claude Code",
        "command": [
            "claude",
            "-p",
            "--output-format",
            "text",
            "--model",
            "{model}",
            "{prompt}",
        ],
        "response_mode": "stdout",
    },
    "gemini": {
        "label": "Gemini CLI",
        "command": [
            "gemini",
            "-p",
            "{prompt}",
            "--model",
            "{model}",
            "--output-format",
            "json",
        ],
        "response_mode": "json_stdout",
        "response_key": "response",
    },
}


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
    request_headers = {"User-Agent": "pocketrelay/1.0"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_command_template(value):
    if value is None:
        return None
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ValueError("cli_command_template must be a string or a list of strings")


def resolve_binary(binary: str):
    if os.path.isabs(binary):
        path = Path(binary)
        return path if path.exists() else None
    resolved = shutil.which(binary)
    return Path(resolved) if resolved else None


class PocketRelayBridge:
    def __init__(self, config):
        self.config = config
        self.state = load_json(
            STATE_PATH,
            {"last_update_id": 0, "conversations": {}, "chat_settings": {}},
        )
        self.bot_token = config["telegram_bot_token"]
        self.allowed_username = config["allowed_username"].lstrip("@").lower()
        self.provider = config.get("provider", "codex").lower()
        self.model = config.get("model", "gpt-5.4")
        self.max_history = int(config.get("max_history", 12))
        self.cli_timeout = int(
            config.get(
                "cli_timeout_seconds",
                config.get("codex_timeout_seconds", 180),
            )
        )
        self.telegram_base = f"https://api.telegram.org/bot{self.bot_token}"
        self.workdir = str(Path(config.get("workdir", str(Path.home()))).expanduser())
        self.system_prompt = config.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        self.state.setdefault("chat_settings", {})

    def chat_settings(self, chat_id: int):
        return self.state["chat_settings"].setdefault(str(chat_id), {})

    def current_provider(self, chat_id: int | None = None) -> str:
        if chat_id is None:
            return self.provider
        return str(self.chat_settings(chat_id).get("provider", self.provider)).lower()

    def set_provider(self, chat_id: int, provider: str):
        self.chat_settings(chat_id)["provider"] = provider.lower()

    def reset_provider(self, chat_id: int):
        self.chat_settings(chat_id).pop("provider", None)

    def available_providers(self):
        providers = []
        for provider in CLI_PRESETS:
            readiness, _, _ = self.cli_readiness(provider)
            providers.append((provider, readiness))
        return providers

    def resolve_command_template(self, provider: str):
        custom_template = None
        if provider == self.provider:
            custom_template = normalize_command_template(self.config.get("cli_command_template"))
        if custom_template:
            return custom_template
        preset = CLI_PRESETS.get(provider)
        if preset:
            return list(preset["command"])
        raise ValueError(f"Unsupported provider: {provider}")

    def provider_label(self, provider: str) -> str:
        if provider == self.provider and self.config.get("cli_label"):
            return str(self.config["cli_label"])
        preset = CLI_PRESETS.get(provider)
        if preset:
            return preset["label"]
        return provider

    def cli_response_mode(self, provider: str) -> str:
        if provider == self.provider and self.config.get("cli_response_mode"):
            return str(self.config["cli_response_mode"])
        preset = CLI_PRESETS.get(provider)
        if preset:
            return preset["response_mode"]
        return "stdout"

    def cli_response_key(self, provider: str) -> str:
        if provider == self.provider and self.config.get("cli_response_key"):
            return str(self.config["cli_response_key"])
        preset = CLI_PRESETS.get(provider)
        if preset:
            return preset.get("response_key", "response")
        return "response"

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
        lines = [self.system_prompt, "", "Conversation so far:"]
        for item in history:
            role = "User" if item["role"] == "user" else "Assistant"
            lines.append(f"{role}: {item['content']}")
        lines.append("")
        lines.append(f"User: {prompt}")
        lines.append("Assistant:")
        return "\n".join(lines)

    def build_cli_command(self, provider: str, prompt: str, output_path: Path):
        command_template = self.resolve_command_template(provider)
        variables = {
            "prompt": prompt,
            "model": self.model,
            "output_path": str(output_path),
            "workdir": self.workdir,
        }
        return [part.format(**variables) for part in command_template]

    def command_binary_status(self, provider: str):
        command = self.resolve_command_template(provider)
        if not command:
            return ("missing", "no command configured")
        binary = command[0]
        resolved = resolve_binary(binary)
        return ("ok" if resolved else "missing", str(resolved or binary))

    def command_runtime_diagnostics(self, provider: str):
        command = self.resolve_command_template(provider)
        if not command:
            return [("missing", "cli_command", "no command configured")]
        issues = []
        binary = command[0]
        resolved = resolve_binary(binary)
        if not resolved:
            return [("missing", "cli_binary", binary)]
        issues.append(("ok", "cli_binary", str(resolved)))

        try:
            with resolved.open("r", encoding="utf-8", errors="replace") as f:
                shebang = f.readline().strip()
        except OSError:
            return issues

        if not shebang.startswith("#!"):
            return issues

        shebang_parts = shlex.split(shebang[2:].strip())
        if len(shebang_parts) >= 2 and Path(shebang_parts[0]).name == "env":
            interpreter = shebang_parts[1]
            interpreter_resolved = resolve_binary(interpreter)
            issues.append(
                (
                    "ok" if interpreter_resolved else "missing",
                    "shebang_dependency",
                    str(interpreter_resolved or interpreter),
                )
            )
        elif shebang_parts:
            interpreter = shebang_parts[0]
            interpreter_path = Path(interpreter)
            issues.append(
                (
                    "ok" if interpreter_path.exists() else "missing",
                    "shebang_dependency",
                    interpreter,
                )
            )
        return issues

    def cli_readiness(self, provider: str):
        diagnostics = self.command_runtime_diagnostics(provider)
        missing = [item for item in diagnostics if item[0] != "ok"]
        if missing:
            parts = [f"{kind}={value}" for _, kind, value in missing]
            return ("error", "missing dependencies: " + ", ".join(parts), diagnostics)
        return ("ok", "ready", diagnostics)

    def extract_response(self, provider: str, completed: subprocess.CompletedProcess, output_path: Path) -> str:
        mode = self.cli_response_mode(provider)
        stdout = (completed.stdout or "").strip()
        if mode == "output_file":
            if output_path.exists():
                text = output_path.read_text(encoding="utf-8").strip()
                if text:
                    return text
            raise RuntimeError(f"{self.provider_label(provider)} did not produce a final message")
        if mode == "stdout":
            if stdout:
                return stdout
            raise RuntimeError(f"{self.provider_label(provider)} produced empty output")
        if mode == "json_stdout":
            if not stdout:
                raise RuntimeError(f"{self.provider_label(provider)} produced empty output")
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{self.provider_label(provider)} returned non-JSON output") from exc
            text = str(payload.get(self.cli_response_key(provider), "")).strip()
            if text:
                return text
            error = payload.get("error")
            if error:
                raise RuntimeError(f"{self.provider_label(provider)} error: {error}")
            raise RuntimeError(
                f"{self.provider_label(provider)} JSON response did not include '{self.cli_response_key(provider)}'"
            )
        raise RuntimeError(f"Unsupported cli_response_mode: {mode}")

    def ask_cli(self, prompt: str, chat_id: int) -> str:
        provider = self.current_provider(chat_id)
        run_id = f"{chat_id}-{int(time.time() * 1000)}"
        output_path = BASE_DIR / f".last_message_{run_id}.txt"
        full_prompt = self.build_prompt(prompt, chat_id)
        cmd = self.build_cli_command(provider, full_prompt, output_path)
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.config.get("env", {}).items()})
        readiness, message, _ = self.cli_readiness(provider)
        if readiness != "ok":
            raise RuntimeError(f"{self.provider_label(provider)} is not ready: {message}")
        try:
            completed = subprocess.run(
                cmd,
                cwd=self.workdir,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.cli_timeout,
            )
            return self.extract_response(provider, completed, output_path)
        except FileNotFoundError as exc:
            missing_name = exc.filename or cmd[0]
            raise RuntimeError(
                f"{self.provider_label(provider)} is not ready: missing executable '{missing_name}'. "
                "Check the service PATH or cli_command_template."
            ) from exc
        except subprocess.CalledProcessError as exc:
            snippet = (exc.stdout or "").strip()[-1200:]
            raise RuntimeError(f"{self.provider_label(provider)} execution failed. {snippet}") from exc
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
        provider = self.current_provider(chat_id)
        if text == "/start":
            self.send_message(chat_id, f"接続済みです。メッセージを送ると {self.provider_label(provider)} 経由で返答します。")
            return
        if text == "/reset":
            self.state["conversations"][str(chat_id)] = []
            self.send_message(chat_id, "会話履歴をリセットしました。")
            return
        if text.startswith("/provider"):
            parts = text.split()
            if len(parts) == 1:
                available = ", ".join(
                    f"{name}({status})" for name, status in self.available_providers()
                )
                self.send_message(
                    chat_id,
                    "\n".join(
                        [
                            f"current_provider: {provider}",
                            f"default_provider: {self.provider}",
                            f"available_providers: {available}",
                            "usage: /provider codex | /provider claude | /provider gemini | /provider reset",
                        ]
                    ),
                )
                return
            requested = parts[1].lower()
            if requested == "reset":
                self.reset_provider(chat_id)
                self.send_message(chat_id, f"provider を既定値 {self.provider} に戻しました。")
                return
            if requested not in CLI_PRESETS:
                self.send_message(chat_id, f"未対応の provider です: {requested}")
                return
            self.set_provider(chat_id, requested)
            readiness, readiness_message, _ = self.cli_readiness(requested)
            self.send_message(
                chat_id,
                f"provider を {requested} に変更しました。readiness={readiness} ({readiness_message})",
            )
            return
        if text == "/help":
            self.send_message(
                chat_id,
                f"/start, /help, /reset, /status, /provider が使えます。通常メッセージは {self.provider_label(provider)} へ送ります。",
            )
            return
        if text == "/status":
            cli_status, cli_path = self.command_binary_status(provider)
            readiness, readiness_message, diagnostics = self.cli_readiness(provider)
            diagnostic_lines = [f"{kind}: {name}={value}" for kind, name, value in diagnostics]
            self.send_message(
                chat_id,
                "\n".join(
                    [
                        "bridge: running",
                        f"allowed_username: @{self.allowed_username}",
                        f"default_provider: {self.provider}",
                        f"provider: {provider}",
                        f"cli_label: {self.provider_label(provider)}",
                        f"cli_command: {self.resolve_command_template(provider)[0]}",
                        f"cli_binary: {cli_status}",
                        f"cli_binary_path: {cli_path}",
                        f"cli_readiness: {readiness}",
                        f"cli_readiness_message: {readiness_message}",
                        f"workdir: {self.workdir}",
                        *diagnostic_lines,
                    ]
                ),
            )
            return
        try:
            self.append_history(chat_id, "user", text)
            answer = self.ask_cli(text, chat_id)
            self.append_history(chat_id, "assistant", answer)
            self.send_message(chat_id, answer)
            log_line(f"replied to chat_id={chat_id} username={username} provider={provider}")
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
    bridge = PocketRelayBridge(config)
    readiness, readiness_message, diagnostics = bridge.cli_readiness(bridge.provider)
    if readiness != "ok":
        summary = ", ".join(f"{kind}={value}" for _, kind, value in diagnostics)
        log_line(f"cli readiness warning: {readiness_message} ({summary})")
    if args.once:
        bridge.run_once()
    else:
        bridge.run_forever()


if __name__ == "__main__":
    main()
