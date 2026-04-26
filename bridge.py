#!/usr/bin/env python3
import argparse
import json
import os
import re
import select
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

SESSION_ID_PATTERN = re.compile(
    r"session id:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
APPROVAL_MODES = {"queue", "safe", "read-only", "full-auto", "dangerous"}
APPROVAL_RETRY_MODES = {"full-auto", "dangerous"}
APPROVAL_BLOCK_PATTERNS = (
    "approval",
    "approve",
    "permission",
    "sandbox",
    "denied",
    "not permitted",
    "operation not permitted",
    "read-only file system",
    "network",
    "temporary failure in name resolution",
)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class ApprovalRequired(RuntimeError):
    def __init__(self, request_id: int, message: str):
        super().__init__(message)
        self.request_id = request_id


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
            {
                "last_update_id": 0,
                "conversations": {},
                "chat_settings": {},
                "sessions": {},
                "approval_requests": {},
                "next_approval_id": 1,
            },
        )
        self.bot_token = config["telegram_bot_token"]
        self.allowed_username = config["allowed_username"].lstrip("@").lower()
        self.provider = config.get("provider", "codex").lower()
        self.model = config.get("model", "gpt-5.4")
        self.max_history = int(config.get("max_history", 12))
        self.codex_sessions = bool(config.get("codex_sessions", True))
        self.codex_approval_mode = str(config.get("codex_approval_mode", "queue")).lower()
        if self.codex_approval_mode not in APPROVAL_MODES:
            self.codex_approval_mode = "queue"
        self.cli_timeout = int(
            config.get(
                "cli_timeout_seconds",
                config.get("codex_timeout_seconds", 180),
            )
        )
        self.processing_message = str(
            config.get("processing_message", "受け付けました。処理中です。")
        ).strip()
        self.progress_updates = bool(config.get("progress_updates", True))
        self.progress_interval = max(10, int(config.get("progress_interval_seconds", 30)))
        self.progress_output_interval = max(5, int(config.get("progress_output_interval_seconds", 15)))
        self.progress_line_max_chars = max(80, int(config.get("progress_line_max_chars", 240)))
        self.telegram_base = f"https://api.telegram.org/bot{self.bot_token}"
        self.workdir = str(Path(config.get("workdir", str(Path.home()))).expanduser())
        self.system_prompt = config.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        self.state.setdefault("chat_settings", {})
        self.state.setdefault("sessions", {})
        self.state.setdefault("approval_requests", {})
        self.state.setdefault("next_approval_id", 1)

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

    def current_approval_mode(self, chat_id: int | None = None) -> str:
        if chat_id is None:
            return self.codex_approval_mode
        mode = str(self.chat_settings(chat_id).get("approval_mode", self.codex_approval_mode)).lower()
        return mode if mode in APPROVAL_MODES else self.codex_approval_mode

    def set_approval_mode(self, chat_id: int, mode: str):
        self.chat_settings(chat_id)["approval_mode"] = mode

    def reset_approval_mode(self, chat_id: int):
        self.chat_settings(chat_id).pop("approval_mode", None)

    def session_settings(self, chat_id: int):
        return self.state["sessions"].setdefault(str(chat_id), {})

    def current_session_id(self, chat_id: int, provider: str):
        return self.session_settings(chat_id).get(provider)

    def set_session_id(self, chat_id: int, provider: str, session_id: str):
        self.session_settings(chat_id)[provider] = session_id

    def clear_session_id(self, chat_id: int, provider: str | None = None):
        if provider is None:
            self.state["sessions"].pop(str(chat_id), None)
            return
        self.session_settings(chat_id).pop(provider, None)

    def pending_approvals(self, chat_id: int):
        return self.state["approval_requests"].setdefault(str(chat_id), {})

    def create_approval_request(self, chat_id: int, provider: str, prompt: str, error: str) -> int:
        request_id = int(self.state.get("next_approval_id", 1))
        self.state["next_approval_id"] = request_id + 1
        self.pending_approvals(chat_id)[str(request_id)] = {
            "provider": provider,
            "prompt": prompt,
            "error": error[-1200:],
            "created_at": int(time.time()),
        }
        return request_id

    def pop_approval_request(self, chat_id: int, request_id: str):
        return self.pending_approvals(chat_id).pop(str(request_id), None)

    def looks_like_approval_block(self, text: str) -> bool:
        lower = text.lower()
        return any(pattern in lower for pattern in APPROVAL_BLOCK_PATTERNS)

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

    def build_initial_prompt(self, prompt: str) -> str:
        return "\n".join([self.system_prompt, "", f"User: {prompt}", "Assistant:"])

    def build_cli_command(self, provider: str, prompt: str, output_path: Path):
        command_template = self.resolve_command_template(provider)
        variables = {
            "prompt": prompt,
            "model": self.model,
            "output_path": str(output_path),
            "workdir": self.workdir,
        }
        return [part.format(**variables) for part in command_template]

    def codex_approval_args(self, mode: str):
        if mode == "read-only":
            return ["-s", "read-only"]
        if mode == "full-auto":
            return ["--full-auto"]
        if mode == "dangerous":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        return []

    def codex_binary(self):
        return self.resolve_command_template("codex")[0]

    def build_codex_new_session_command(self, prompt: str, output_path: Path, approval_mode: str):
        return [
            self.codex_binary(),
            "exec",
            "--skip-git-repo-check",
            *self.codex_approval_args(approval_mode),
            "-C",
            self.workdir,
            "-m",
            self.model,
            "-o",
            str(output_path),
            prompt,
        ]

    def build_codex_resume_command(self, session_id: str, prompt: str, output_path: Path, approval_mode: str):
        return [
            self.codex_binary(),
            "exec",
            "resume",
            "--skip-git-repo-check",
            *self.codex_approval_args(approval_mode),
            "-m",
            self.model,
            "-o",
            str(output_path),
            session_id,
            prompt,
        ]

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

    def extract_session_id(self, completed: subprocess.CompletedProcess):
        match = SESSION_ID_PATTERN.search(completed.stdout or "")
        return match.group(1) if match else None

    def progress_text_from_line(self, provider: str, line: str) -> str | None:
        text = ANSI_ESCAPE_PATTERN.sub("", line).strip()
        if not text:
            return None
        if self.cli_response_mode(provider) == "json_stdout" and text.startswith("{"):
            return None
        collapsed = " ".join(text.split())
        if len(collapsed) > self.progress_line_max_chars:
            collapsed = collapsed[: self.progress_line_max_chars - 3] + "..."
        return collapsed

    def send_progress(self, chat_id: int, text: str):
        if self.progress_updates:
            self.send_message(chat_id, text)

    def run_cli_with_progress(self, cmd, provider: str, chat_id: int, env) -> subprocess.CompletedProcess:
        started_at = time.monotonic()
        last_periodic_notice = started_at
        last_output_notice = 0.0
        stdout_parts = []
        label = self.provider_label(provider)
        self.send_progress(chat_id, f"{label} を起動しました。処理を開始しています。")
        process = subprocess.Popen(
            cmd,
            cwd=self.workdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            while True:
                now = time.monotonic()
                if now - started_at > self.cli_timeout:
                    process.kill()
                    remaining, _ = process.communicate()
                    if remaining:
                        stdout_parts.append(remaining)
                    raise subprocess.TimeoutExpired(cmd, self.cli_timeout, output="".join(stdout_parts))

                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                if ready:
                    line = process.stdout.readline()
                    if line:
                        stdout_parts.append(line)
                        progress_text = self.progress_text_from_line(provider, line)
                        if progress_text and now - last_output_notice >= self.progress_output_interval:
                            self.send_progress(chat_id, f"進捗: {progress_text}")
                            last_output_notice = now
                            last_periodic_notice = now
                    elif process.poll() is not None:
                        break
                elif process.poll() is not None:
                    break
                elif now - last_periodic_notice >= self.progress_interval:
                    elapsed = int(now - started_at)
                    self.send_progress(chat_id, f"処理継続中です。経過 {elapsed} 秒。")
                    last_periodic_notice = now

            remaining, _ = process.communicate()
            if remaining:
                stdout_parts.append(remaining)
            stdout = "".join(stdout_parts)
            completed = subprocess.CompletedProcess(cmd, process.returncode, stdout=stdout, stderr=None)
            if completed.returncode:
                raise subprocess.CalledProcessError(completed.returncode, cmd, output=stdout, stderr=None)
            return completed
        finally:
            if process.poll() is None:
                process.kill()

    def ask_cli(self, prompt: str, chat_id: int, approval_override: str | None = None) -> str:
        provider = self.current_provider(chat_id)
        run_id = f"{chat_id}-{int(time.time() * 1000)}"
        output_path = BASE_DIR / f".last_message_{run_id}.txt"
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.config.get("env", {}).items()})
        readiness, message, _ = self.cli_readiness(provider)
        if readiness != "ok":
            raise RuntimeError(f"{self.provider_label(provider)} is not ready: {message}")
        configured_approval_mode = self.current_approval_mode(chat_id)
        approval_mode = approval_override or configured_approval_mode
        execution_approval_mode = "safe" if approval_mode == "queue" else approval_mode
        session_id = self.current_session_id(chat_id, provider)
        if provider == "codex" and self.codex_sessions:
            if session_id:
                cmd = self.build_codex_resume_command(session_id, prompt, output_path, execution_approval_mode)
            else:
                cmd = self.build_codex_new_session_command(
                    self.build_initial_prompt(prompt),
                    output_path,
                    execution_approval_mode,
                )
        else:
            cmd = self.build_cli_command(provider, self.build_initial_prompt(prompt), output_path)
        try:
            completed = self.run_cli_with_progress(cmd, provider, chat_id, env)
            new_session_id = self.extract_session_id(completed)
            if provider == "codex" and self.codex_sessions and new_session_id:
                self.set_session_id(chat_id, provider, new_session_id)
            return self.extract_response(provider, completed, output_path)
        except FileNotFoundError as exc:
            missing_name = exc.filename or cmd[0]
            raise RuntimeError(
                f"{self.provider_label(provider)} is not ready: missing executable '{missing_name}'. "
                "Check the service PATH or cli_command_template."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"{self.provider_label(provider)} timed out after {int(exc.timeout)} seconds"
            ) from exc
        except subprocess.CalledProcessError as exc:
            snippet = (exc.stdout or "").strip()[-1200:]
            if provider == "codex" and approval_mode == "queue" and self.looks_like_approval_block(snippet):
                request_id = self.create_approval_request(chat_id, provider, prompt, snippet)
                raise ApprovalRequired(
                    request_id,
                    "\n".join(
                        [
                            "承認が必要そうな操作で止まりました。",
                            f"approval_id: {request_id}",
                            "再実行するには:",
                            f"/approve {request_id} full-auto",
                            f"/approve {request_id} dangerous",
                            "取り消すには:",
                            f"/deny {request_id}",
                        ]
                    ),
                ) from exc
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
            self.clear_session_id(chat_id)
            self.send_message(chat_id, "セッションをリセットしました。")
            return
        if text.startswith("/session"):
            parts = text.split()
            action = parts[1].lower() if len(parts) > 1 else "status"
            if action == "status":
                session_id = self.current_session_id(chat_id, provider)
                self.send_message(
                    chat_id,
                    "\n".join(
                        [
                            f"provider: {provider}",
                            f"codex_sessions: {self.codex_sessions}",
                            f"session_id: {session_id or '(none)'}",
                            "usage: /session status | /session new | /session reset",
                        ]
                    ),
                )
                return
            if action in {"new", "reset"}:
                self.clear_session_id(chat_id, provider)
                self.state["conversations"][str(chat_id)] = []
                self.send_message(chat_id, "現在のセッションをクリアしました。次のメッセージで新しいセッションを開始します。")
                return
            self.send_message(chat_id, "usage: /session status | /session new | /session reset")
            return
        if text.startswith("/approval"):
            parts = text.split()
            action = parts[1].lower() if len(parts) > 1 else "status"
            if action == "status":
                pending = self.pending_approvals(chat_id)
                pending_lines = [
                    f"{request_id}: {item.get('provider', 'codex')} {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(item.get('created_at', 0)))}"
                    for request_id, item in sorted(pending.items(), key=lambda pair: int(pair[0]))
                ]
                self.send_message(
                    chat_id,
                    "\n".join(
                        [
                            f"default_approval_mode: {self.codex_approval_mode}",
                            f"approval_mode: {self.current_approval_mode(chat_id)}",
                            "available: queue | safe | read-only | full-auto | dangerous | reset",
                            "pending:",
                            *(pending_lines or ["(none)"]),
                        ]
                    ),
                )
                return
            if action == "reset":
                self.reset_approval_mode(chat_id)
                self.send_message(chat_id, f"approval_mode を既定値 {self.codex_approval_mode} に戻しました。")
                return
            if action not in APPROVAL_MODES:
                self.send_message(chat_id, "usage: /approval status | /approval queue | /approval safe | /approval read-only | /approval full-auto | /approval dangerous | /approval reset")
                return
            self.set_approval_mode(chat_id, action)
            self.send_message(chat_id, f"approval_mode を {action} に変更しました。")
            return
        if text.startswith("/deny"):
            parts = text.split()
            if len(parts) < 2:
                self.send_message(chat_id, "usage: /deny <approval_id>")
                return
            request = self.pop_approval_request(chat_id, parts[1])
            if not request:
                self.send_message(chat_id, f"approval_id が見つかりません: {parts[1]}")
                return
            self.send_message(chat_id, f"approval_id {parts[1]} を取り消しました。")
            return
        if text.startswith("/approve"):
            parts = text.split()
            if len(parts) < 2:
                self.send_message(chat_id, "usage: /approve <approval_id> [full-auto|dangerous]")
                return
            request_id = parts[1]
            retry_mode = parts[2].lower() if len(parts) > 2 else "full-auto"
            if retry_mode not in APPROVAL_RETRY_MODES:
                self.send_message(chat_id, "承認再実行モードは full-auto または dangerous を指定してください。")
                return
            request = self.pop_approval_request(chat_id, request_id)
            if not request:
                self.send_message(chat_id, f"approval_id が見つかりません: {request_id}")
                return
            if request.get("provider") != "codex":
                self.send_message(chat_id, "現在、承認キューの再実行は Codex provider のみ対応です。")
                return
            if self.processing_message:
                self.send_message(chat_id, f"approval_id {request_id} を {retry_mode} で再実行します。")
            settings = self.chat_settings(chat_id)
            previous_provider = settings.get("provider")
            self.set_provider(chat_id, "codex")
            try:
                answer = self.ask_cli(request["prompt"], chat_id, approval_override=retry_mode)
                self.send_message(chat_id, answer)
                log_line(f"approved request_id={request_id} chat_id={chat_id} mode={retry_mode}")
            except Exception as exc:
                log_line(f"error while handling approval request_id={request_id}: {exc}")
                self.send_message(chat_id, f"承認再実行に失敗しました: {exc}")
            finally:
                if previous_provider is None:
                    self.reset_provider(chat_id)
                else:
                    self.set_provider(chat_id, previous_provider)
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
                f"/start, /help, /reset, /status, /provider, /session, /approval, /approve, /deny が使えます。通常メッセージは {self.provider_label(provider)} へ送ります。",
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
                        f"codex_sessions: {self.codex_sessions}",
                        f"session_id: {self.current_session_id(chat_id, provider) or '(none)'}",
                        f"approval_mode: {self.current_approval_mode(chat_id)}",
                        f"pending_approvals: {len(self.pending_approvals(chat_id))}",
                        f"progress_updates: {self.progress_updates}",
                        f"progress_interval_seconds: {self.progress_interval}",
                        f"progress_output_interval_seconds: {self.progress_output_interval}",
                        f"workdir: {self.workdir}",
                        *diagnostic_lines,
                    ]
                ),
            )
            return
        try:
            if self.processing_message:
                self.send_message(chat_id, self.processing_message)
            answer = self.ask_cli(text, chat_id)
            self.send_message(chat_id, answer)
            log_line(f"replied to chat_id={chat_id} username={username} provider={provider}")
        except ApprovalRequired as exc:
            log_line(f"approval queued chat_id={chat_id} request_id={exc.request_id}")
            self.send_message(chat_id, str(exc))
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
