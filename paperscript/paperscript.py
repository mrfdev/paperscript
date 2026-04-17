#!/usr/bin/env python3
"""PaperScript: a PaperMC updater focused on safe, interactive server upgrades."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_NAME = "PaperScript"
APP_VERSION = "5.0.1"
APP_BUILD = "049"
APP_RELEASE = f"{APP_VERSION} build {APP_BUILD}"
API_ROOT = "https://fill.papermc.io/v3/projects/paper"
PROJECT_URL = "https://github.com/mrfdev/PaperScript"
PAPER_DOWNLOADS_URL = "https://papermc.io/downloads/paper"
DEFAULT_CHANNEL = "STABLE"
DEFAULT_TIMEOUT = 30
DEFAULT_USER_AGENT = f"mrfloris-PaperScript/2.0 ({PROJECT_URL})"
CURRENT_JAR_PATTERN = re.compile(r"^paper-(.+)-(\d+)\.jar$", re.IGNORECASE)
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_CYAN = "\033[36m"
ANSI_BRIGHT_CYAN = "\033[96m"
ANSI_MAGENTA = "\033[35m"
ANSI_BRIGHT_WHITE = "\033[97m"
COLOR_THEMES: dict[str, dict[str, str]] = {
    "default": {
        "key": ANSI_BRIGHT_CYAN,
        "value": ANSI_BRIGHT_WHITE,
        "success": ANSI_GREEN,
        "warning": ANSI_YELLOW,
        "error": ANSI_RED,
        "prompt": ANSI_MAGENTA,
        "hint": ANSI_YELLOW,
    },
    "soft": {
        "key": ANSI_CYAN,
        "value": ANSI_BRIGHT_WHITE,
        "success": ANSI_GREEN,
        "warning": ANSI_YELLOW,
        "error": ANSI_RED,
        "prompt": ANSI_MAGENTA,
        "hint": ANSI_YELLOW,
    },
    "high-contrast": {
        "key": ANSI_BRIGHT_WHITE,
        "value": ANSI_BRIGHT_WHITE,
        "success": ANSI_GREEN,
        "warning": ANSI_YELLOW,
        "error": ANSI_RED,
        "prompt": ANSI_MAGENTA,
        "hint": ANSI_YELLOW,
    },
}
TODO_TEMPLATE = """PaperScript todo

- Future automation: optional update scheduling or smarter unattended workflows.
- Future cleanup polish: extra selective cleanup and repair helpers beyond the current safe set.
- Future tmux/server control: start, stop, restart, and richer session management helpers.
- Future validation mode: additional smoke-test or planner mode beyond today's dry-run behavior.
"""
DEFAULT_CONFIG: dict[str, Any] = {
    "server_name": None,
    "tmux_session": "mcserver",
    "default_channel": "STABLE",
    "check_latest_channel_only": "STABLE",
    "allow_cross_version_auto_upgrade": False,
    "allow_same_version_build_upgrade": True,
    "keep_backups": 10,
    "cleanup_backups_after_install": True,
    "running_server_action": "ask",
    "graceful_stop_command": "stop",
    "http_timeout_seconds": 30,
    "status_show_all_channels": True,
    "download_filename_pattern": "Paper-{version}-{build}.jar",
    "log_file": "logs.log",
    "backup_dir": "backups",
    "downloads_dir": "downloads",
    "confirm_before_force_download": True,
    "confirm_before_downgrade": True,
    "auto_detect_server_by_port": True,
    "fallback_process_detection": True,
    "quiet": False,
    "no_color": False,
    "color_theme": "default",
    "default_status_view": "full",
    "command_hint_mode": "auto",
    "release_link_mode": "auto",
}


class PaperScriptError(Exception):
    """Raised when the script cannot continue safely."""


@dataclass(frozen=True)
class ParsedVersion:
    raw: str
    numbers: tuple[int, ...]
    suffix_rank: int
    suffix_number: int

    def key(self) -> tuple[tuple[int, ...], int, int]:
        return (self.numbers, self.suffix_rank, self.suffix_number)


@dataclass
class JarInfo:
    path: Path
    version: str
    build: int


@dataclass
class BuildInfo:
    version: str
    build_id: int
    channel: str
    download_name: str
    download_url: str
    sha256: str | None
    size: int | None
    created_at: str | None

    @property
    def filename(self) -> str:
        return f"Paper-{self.version}-{self.build_id}.jar"


@dataclass
class DownloadVerification:
    sha256: str
    bytes_written: int


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_version(version: str) -> ParsedVersion:
    base, _, suffix = version.partition("-")
    number_parts: list[int] = []
    for part in base.split("."):
        if part.isdigit():
            number_parts.append(int(part))
        else:
            match = re.match(r"(\d+)", part)
            number_parts.append(int(match.group(1)) if match else 0)

    suffix_rank = 3
    suffix_number = 0
    if suffix:
        match = re.match(r"([A-Za-z]+)(\d*)", suffix)
        label = match.group(1).lower() if match else suffix.lower()
        suffix_number = int(match.group(2)) if match and match.group(2) else 0
        if label in {"alpha", "a"}:
            suffix_rank = 0
        elif label in {"beta", "b", "pre", "preview"}:
            suffix_rank = 1
        elif label in {"rc"}:
            suffix_rank = 2
        else:
            suffix_rank = 0

    return ParsedVersion(
        raw=version,
        numbers=tuple(number_parts),
        suffix_rank=suffix_rank,
        suffix_number=suffix_number,
    )


def compare_versions(left: str, right: str) -> int:
    a = parse_version(left).key()
    b = parse_version(right).key()
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def supports_color(stream: Any, no_color: bool = False) -> bool:
    if no_color or os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def color_text(text: str, color: str, enabled: bool, bold: bool = False) -> str:
    if not enabled:
        return text
    prefix = f"{ANSI_BOLD}{color}" if bold else color
    return f"{prefix}{text}{ANSI_RESET}"


def style_key_value(
    message: str,
    enabled: bool,
    label_color: str = "",
    value_color: str = ANSI_BRIGHT_WHITE,
) -> str:
    if not enabled or ": " not in message:
        return message
    label, value = message.split(": ", 1)
    label_prefix = label_color if label_color else ANSI_BRIGHT_CYAN
    value_prefix = ANSI_BOLD + value_color
    return f"{label_prefix}{label}:{ANSI_RESET} {value_prefix}{value}{ANSI_RESET}"


def prompt_yes_no(question: str, default: bool = False, logger: "Logger | None" = None) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        prompt = f"{question} {suffix} "
        if logger is not None:
            reply = logger.prompt_input(prompt).strip().lower()
        else:
            reply = input(prompt).strip().lower()
        if not reply:
            return default
        if reply in {"y", "yes"}:
            return True
        if reply in {"n", "no"}:
            return False
        if logger is not None:
            logger.warn("Please answer yes or no.")
        else:
            print("Please answer yes or no.")


def prompt_choice(
    question: str,
    choices: list[tuple[str, str]],
    default: str | None = None,
    logger: "Logger | None" = None,
) -> str:
    if logger is not None:
        logger.info(question)
    else:
        print(question)
    for key, label in choices:
        default_mark = " (default)" if default == key else ""
        if logger is not None:
            logger.log(f"  {key}) {label}{default_mark}")
        else:
            print(f"  {key}) {label}{default_mark}")
    valid = {key for key, _ in choices}
    while True:
        if logger is not None:
            reply = logger.prompt_input("> ").strip().lower()
        else:
            reply = input("> ").strip().lower()
        if not reply and default is not None:
            return default
        if reply in valid:
            return reply
        if logger is not None:
            logger.warn(f"Choose one of: {', '.join(sorted(valid))}")
        else:
            print(f"Choose one of: {', '.join(sorted(valid))}")


def parse_properties(path: Path) -> dict[str, str]:
    properties: dict[str, str] = {}
    if not path.exists():
        return properties
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key.strip()] = value.strip()
    return properties


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (PermissionError, FileNotFoundError) as error:
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=str(error))


def format_bytes(size: int | None) -> str:
    if size is None:
        return "unknown size"
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def format_bool(value: bool) -> str:
    return "yes" if value else "no"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class Logger:
    def __init__(
        self,
        log_path: Path,
        quiet: bool = False,
        use_color: bool = False,
        theme_name: str = "default",
    ) -> None:
        self.log_path = log_path
        self.quiet = quiet
        self.use_color = use_color
        self.theme = resolve_color_theme(theme_name)
        ensure_directory(log_path.parent)

    def _console_text(self, message: str) -> str:
        if not self.use_color:
            return message
        lower = message.lower()
        theme = self.theme
        if message.startswith("ERROR:") or "mismatch" in lower or "failed" in lower:
            return color_text(message, theme["error"], True, bold=True)
        if (
            lower.startswith("downloaded to")
            or lower.startswith("installed ")
            or lower.startswith("backed up ")
            or lower.startswith("cleanup finished:")
            or lower.startswith("server stopped")
            or lower.startswith("server force-stopped")
        ):
            return color_text(message, theme["success"], True, bold=True)
        if "checksum verification: match" in lower:
            return style_key_value(message, True, theme["success"], theme["value"])
        if lower.startswith("update status:"):
            return style_key_value(
                message,
                True,
                theme["success"] if "latest stable build" in lower else theme["warning"],
                theme["value"],
            )
        if lower.startswith("running server detected:"):
            return style_key_value(message, True, theme["warning"], theme["value"])
        if lower.startswith("tmux session available:"):
            return style_key_value(
                message,
                True,
                theme["success"] if lower.endswith("yes") else theme["warning"],
                theme["value"],
            )
        if (
            lower.startswith("use './paperscript.sh")
            or lower.startswith("exact manual command:")
            or lower.startswith("release page:")
            or lower.startswith("for a stable overview")
            or lower.startswith("for an experimental overview")
        ):
            return color_text(message, theme["hint"], True, bold=True)
        if (
            lower.startswith("dry run:")
            or "no newer stable build" in lower
            or "no download was performed" in lower
            or "cancelled" in lower
            or "force" in lower
            or "latest stable is" in lower
            or "newer version available" in lower
            or "would ask" in lower
        ):
            return color_text(message, theme["warning"], True, bold=True)
        if ": " in message:
            return style_key_value(message, True, theme["key"], theme["value"])
        return message

    def log(self, message: str) -> None:
        line = f"[{utc_now()}] {message}"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if not self.quiet:
            print(self._console_text(message))

    def error(self, message: str) -> None:
        self.log(f"ERROR: {message}")

    def warn(self, message: str) -> None:
        self.log(message)

    def info(self, message: str) -> None:
        self.log(message)

    def kv(self, label: str, value: str, width: int = 28) -> None:
        self.log(f"{label:<{width}}: {value}")

    def prompt_input(self, message: str) -> str:
        if not sys.stdin.isatty():
            raise PaperScriptError(
                "A prompt was required, but no interactive terminal is available. Re-run with --yes or adjust config."
            )
        return input(color_text(message, self.theme["prompt"], self.use_color, bold=True))


class PaperAPI:
    def __init__(self, user_agent: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.user_agent = user_agent
        self.timeout = timeout

    def _request_json(self, url: str) -> Any:
        request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="ignore") if hasattr(error, "read") else ""
            raise PaperScriptError(f"API request failed for {url}: HTTP {error.code} {detail}".strip()) from error
        except URLError as error:
            raise PaperScriptError(f"API request failed for {url}: {error.reason}") from error

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as error:
            raise PaperScriptError(f"API returned invalid JSON for {url}") from error

        if isinstance(data, dict) and data.get("ok") is False:
            raise PaperScriptError(data.get("message") or f"API returned an error for {url}")
        return data

    def get_project_versions(self) -> list[dict[str, Any]]:
        rich = self._request_json(f"{API_ROOT}/versions")
        if isinstance(rich, dict) and isinstance(rich.get("versions"), list):
            versions: list[dict[str, Any]] = []
            for item in rich["versions"]:
                version_info = item.get("version", {})
                version_id = (
                    version_info.get("id")
                    or item.get("id")
                    or item.get("key")
                )
                if not version_id:
                    continue
                normalized = dict(item)
                normalized["id"] = version_id
                normalized["group"] = version_info.get("group") or item.get("group") or guess_version_group(version_id)
                versions.append(normalized)
            if versions:
                return sorted(versions, key=lambda item: parse_version(item["id"]).key(), reverse=True)

        simple = self._request_json(API_ROOT)
        raw_versions = simple.get("versions", {})
        flattened: list[dict[str, Any]] = []
        if isinstance(raw_versions, dict):
            for group, items in raw_versions.items():
                for version_id in items:
                    flattened.append({"id": version_id, "group": group})
        return sorted(flattened, key=lambda item: parse_version(item["id"]).key(), reverse=True)

    def get_builds(self, version: str) -> list[BuildInfo]:
        raw = self._request_json(f"{API_ROOT}/versions/{version}/builds")
        builds = raw.get("builds") if isinstance(raw, dict) else raw
        if not isinstance(builds, list):
            raise PaperScriptError(f"Unexpected build payload for version {version}")

        normalized: list[BuildInfo] = []
        for item in builds:
            if not isinstance(item, dict):
                continue
            download = item.get("downloads", {}).get("server:default", {})
            build_id = item.get("id") or item.get("number") or item.get("build")
            if build_id is None or not download.get("url"):
                continue
            normalized.append(
                BuildInfo(
                    version=version,
                    build_id=int(build_id),
                    channel=str(item.get("channel", "UNKNOWN")).upper(),
                    download_name=str(download.get("name") or f"Paper-{version}-{build_id}.jar"),
                    download_url=str(download["url"]),
                    sha256=download.get("checksums", {}).get("sha256"),
                    size=download.get("size"),
                    created_at=item.get("createdAt") or item.get("time"),
                )
            )
        normalized.sort(key=lambda item: item.build_id, reverse=True)
        return normalized

    def get_latest_build(self, version: str, channel: str = DEFAULT_CHANNEL) -> BuildInfo | None:
        channel_upper = channel.upper()
        for build in self.get_builds(version):
            if build.channel == channel_upper:
                return build
        return None

    def get_build_by_id(self, version: str, build_id: int) -> BuildInfo | None:
        for build in self.get_builds(version):
            if build.build_id == build_id:
                return build
        return None

    def download_file(self, build: BuildInfo, destination: Path) -> DownloadVerification:
        ensure_directory(destination.parent)
        request = Request(build.download_url, headers={"User-Agent": self.user_agent})
        sha256 = hashlib.sha256()
        bytes_written = 0
        try:
            with urlopen(request, timeout=self.timeout) as response, destination.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    sha256.update(chunk)
                    bytes_written += len(chunk)
                    handle.write(chunk)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="ignore") if hasattr(error, "read") else ""
            raise PaperScriptError(f"Download failed: HTTP {error.code} {detail}".strip()) from error
        except URLError as error:
            raise PaperScriptError(f"Download failed: {error.reason}") from error

        if build.sha256:
            digest = sha256.hexdigest()
            if digest.lower() != build.sha256.lower():
                raise PaperScriptError(
                    f"Checksum mismatch for {build.filename}: expected {build.sha256}, got {digest}"
                )
            return DownloadVerification(sha256=digest, bytes_written=bytes_written)

        return DownloadVerification(sha256=sha256.hexdigest(), bytes_written=bytes_written)


def guess_version_group(version: str) -> str:
    pieces = version.split(".")
    if len(pieces) >= 2:
        return ".".join(pieces[:2])
    return version


def normalize_choice(value: Any, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in allowed:
        return normalized
    return default


def resolve_color_theme(name: Any) -> dict[str, str]:
    normalized = normalize_choice(name, set(COLOR_THEMES), "default")
    return COLOR_THEMES[normalized]


class PaperScriptApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.script_dir = Path(__file__).resolve().parent
        self.runtime_dir = self.script_dir
        self.server_dir = self._resolve_server_dir()
        self.config_path = self.runtime_dir / "config.json"
        self.state_path = self.runtime_dir / "state.json"
        self.todo_path = self.runtime_dir / "todo.log"
        self.config = self._load_config()
        self.quiet_mode = bool(args.quiet or self.config.get("quiet"))
        self.no_color = bool(args.no_color or self.config.get("no_color"))
        self.color_theme_name = normalize_choice(self.config.get("color_theme"), set(COLOR_THEMES), "default")
        self.backups_dir = self.runtime_dir / str(self.config["backup_dir"])
        self.downloads_dir = self.runtime_dir / str(self.config["downloads_dir"])
        self.log_path = self.runtime_dir / str(self.config["log_file"])
        self.logger = Logger(
            self.log_path,
            quiet=self.quiet_mode,
            use_color=supports_color(sys.stdout, no_color=self.no_color),
            theme_name=self.color_theme_name,
        )
        ensure_directory(self.backups_dir)
        ensure_directory(self.downloads_dir)
        self.state = self._load_json(self.state_path)
        self.server_name = self.config.get("server_name")
        self.default_channel = str(self.config["default_channel"]).upper()
        self.check_latest_channel_only = str(self.config["check_latest_channel_only"]).upper()
        self.allow_cross_version_auto_upgrade = bool(self.config["allow_cross_version_auto_upgrade"])
        self.allow_same_version_build_upgrade = bool(self.config["allow_same_version_build_upgrade"])
        self.keep_backups = int(self.config["keep_backups"])
        self.cleanup_backups_after_install = bool(self.config["cleanup_backups_after_install"])
        self.running_server_action = str(self.config["running_server_action"])
        self.graceful_stop_command = str(self.config["graceful_stop_command"])
        self.status_show_all_channels = bool(self.config["status_show_all_channels"])
        self.download_filename_pattern = str(self.config["download_filename_pattern"])
        self.confirm_before_force_download = bool(self.config["confirm_before_force_download"])
        self.confirm_before_downgrade = bool(self.config["confirm_before_downgrade"])
        self.auto_detect_server_by_port = bool(self.config["auto_detect_server_by_port"])
        self.fallback_process_detection = bool(self.config["fallback_process_detection"])
        self.default_status_view = normalize_choice(self.config.get("default_status_view"), {"full", "compact"}, "full")
        self.command_hint_mode = normalize_choice(self.config.get("command_hint_mode"), {"auto", "always", "never"}, "auto")
        self.release_link_mode = normalize_choice(self.config.get("release_link_mode"), {"auto", "always", "never"}, "auto")
        self.http_timeout = int(args.timeout) if args.timeout is not None else int(self.config["http_timeout_seconds"])
        self.user_agent = self._resolve_user_agent()
        self.api = PaperAPI(self.user_agent, timeout=self.http_timeout)
        self.tmux_session = (
            args.tmux_session
            or os.environ.get("PAPERSCRIPT_TMUX_SESSION")
            or self.config.get("tmux_session")
            or "mcserver"
        )

    def force_example_for_current(self, current: JarInfo | None) -> str | None:
        if current is None:
            return None
        return f"./paperscript.sh --force download --version {current.version} --build {current.build}"

    def tmux_session_available(self) -> bool:
        return run_command(["tmux", "has-session", "-t", self.tmux_session]).returncode == 0

    def effective_status_view(self) -> str:
        if getattr(self.args, "status_full", False):
            return "full"
        if getattr(self.args, "status_compact", False):
            return "compact"
        return self.default_status_view

    def should_show_command_hints(self, important: bool = False) -> bool:
        if self.command_hint_mode == "always":
            return True
        if self.command_hint_mode == "never":
            return False
        return important or not self.logger.quiet

    def should_show_release_link(self, relevant: bool = False) -> bool:
        if self.release_link_mode == "always":
            return True
        if self.release_link_mode == "never":
            return False
        return relevant

    def log_command_hint(self, message: str, important: bool = False) -> None:
        if self.should_show_command_hints(important):
            self.logger.log(message)

    def log_release_page(self, relevant: bool = False) -> None:
        if self.should_show_release_link(relevant):
            self.logger.log(f"Release page: {PAPER_DOWNLOADS_URL}")

    def _resolve_server_dir(self) -> Path:
        if self.args.server_dir:
            return Path(self.args.server_dir).expanduser().resolve()
        cwd = Path.cwd().resolve()
        if cwd == self.script_dir and self.script_dir.name.lower() == APP_NAME.lower():
            return self.script_dir.parent.resolve()
        return cwd

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _load_config(self) -> dict[str, Any]:
        raw = self._load_json(self.config_path)
        merged = dict(DEFAULT_CONFIG)
        merged.update(raw)
        if raw != merged:
            self._save_json(self.config_path, merged)
        return merged

    def _resolve_user_agent(self) -> str:
        configured = (
            self.args.user_agent
            or os.environ.get("PAPERSCRIPT_USER_AGENT")
            or self.config.get("user_agent")
        )
        if configured:
            return configured

        contact = (
            self.args.contact
            or os.environ.get("PAPERSCRIPT_CONTACT")
            or self.config.get("contact")
        )
        if contact and sys.stdin.isatty():
            if not self.config.get("contact"):
                self.config["contact"] = contact
                self._save_json(self.config_path, self.config)
            return f"{APP_NAME}/{APP_VERSION} ({contact})"

        return DEFAULT_USER_AGENT

    def record_state(self, build: BuildInfo, installed_path: Path, current_sha256: str) -> None:
        self.state.update(
            {
                "current_build": build.build_id,
                "current_jar": installed_path.name,
                "current_version": build.version,
                "installed_at": utc_now(),
                "server_dir": str(self.server_dir),
                "expected_sha256": build.sha256,
                "current_sha256": current_sha256,
                "download_url": build.download_url,
            }
        )
        self._save_json(self.state_path, self.state)

    def find_current_jar(self) -> JarInfo | None:
        state_name = self.state.get("current_jar")
        if state_name:
            state_path = self.server_dir / state_name
            match = CURRENT_JAR_PATTERN.match(state_path.name)
            if state_path.exists() and match:
                return JarInfo(state_path, match.group(1), int(match.group(2)))

        candidates: list[JarInfo] = []
        for path in self.server_dir.glob("*.jar"):
            match = CURRENT_JAR_PATTERN.match(path.name)
            if not match:
                continue
            candidates.append(JarInfo(path, match.group(1), int(match.group(2))))
        if not candidates:
            return None

        candidates.sort(
            key=lambda item: (parse_version(item.version).key(), item.build),
            reverse=True,
        )
        return candidates[0]

    def latest_stable_version(self) -> tuple[str, BuildInfo]:
        versions = [item["id"] for item in self.api.get_project_versions()]
        for version in versions:
            build = self.api.get_latest_build(version, channel=self.check_latest_channel_only)
            if build:
                return version, build
        raise PaperScriptError("No stable Paper builds were found.")

    def latest_version_for_channel(self, channel: str) -> tuple[str, BuildInfo]:
        channel_upper = channel.upper()
        versions = [item["id"] for item in self.api.get_project_versions()]
        for version in versions:
            build = self.api.get_latest_build(version, channel=channel_upper)
            if build:
                return version, build
        raise PaperScriptError(f"No {channel_upper} Paper builds were found.")

    def describe_server_context(self) -> None:
        has_server_properties = (self.server_dir / "server.properties").exists()
        current_jar = self.find_current_jar()
        self.logger.log(f"Script directory: {self.script_dir}")
        self.logger.log(f"Server directory: {self.server_dir}")
        self.logger.log(f"Runtime directory: {self.runtime_dir}")
        self.logger.log(f"Server properties found: {'yes' if has_server_properties else 'no'}")
        if current_jar:
            self.logger.log(
                f"Detected current jar: {current_jar.path.name} "
                f"(version {current_jar.version}, build {current_jar.build})"
            )
        else:
            self.logger.log("Detected current jar: none")
        self.log_command_hint(
            "For a stable overview, run './paperscript.sh stable'. For an experimental overview, run './paperscript.sh experimental'."
        )

    def list_versions(self, show_channels: bool = False) -> None:
        versions = self.api.get_project_versions()
        self.logger.log(f"Found {len(versions)} Paper versions from the API.")
        for item in versions:
            line = item["id"]
            extra: list[str] = []
            if item.get("group"):
                extra.append(f"group {item['group']}")
            if item.get("support"):
                extra.append(f"support {item['support']}")
            minimum_java = item.get("minimumJavaVersion") or item.get("minimum_java_version")
            if minimum_java:
                extra.append(f"java {minimum_java}+")
            if show_channels:
                summaries = self.latest_channel_summaries(item["id"])
                if summaries:
                    extra.append(", ".join(summaries))
            if extra:
                self.logger.log(f"  - {line} ({'; '.join(extra)})")
            else:
                self.logger.log(f"  - {line}")

    def latest_channel_summaries(self, version: str) -> list[str]:
        builds = self.api.get_builds(version)
        channels: dict[str, BuildInfo] = {}
        for build in builds:
            channels.setdefault(build.channel, build)
        output: list[str] = []
        for channel in ["STABLE", "BETA", "ALPHA", "RECOMMENDED"]:
            build = channels.get(channel)
            if build:
                output.append(f"{channel.lower()} #{build.build_id}")
        return output

    def latest_builds_by_channel(self, version: str) -> dict[str, BuildInfo]:
        builds = self.api.get_builds(version)
        channels: dict[str, BuildInfo] = {}
        for build in builds:
            channels.setdefault(build.channel, build)
        return channels

    def inspect_version(self, version: str, offer_download: bool = True) -> None:
        by_channel = self.latest_builds_by_channel(version)
        if not by_channel:
            raise PaperScriptError(f"No builds found for version {version}")

        self.logger.log(f"Latest known builds for version {version}:")
        for channel in ["STABLE", "BETA", "ALPHA", "RECOMMENDED"]:
            build = by_channel.get(channel)
            if build:
                created = f", created {build.created_at}" if build.created_at else ""
                self.logger.log(
                    f"  - {channel}: build #{build.build_id}, {build.filename}, "
                    f"{format_bytes(build.size)}{created}"
                )

        if offer_download and sys.stdin.isatty():
            if prompt_yes_no(f"Download a build for version {version} now?", default=False, logger=self.logger):
                choices = [(channel.lower(), channel.title()) for channel in by_channel]
                selected = prompt_choice(
                    "Which channel do you want?",
                    choices,
                    default="stable" if "STABLE" in by_channel else None,
                    logger=self.logger,
                )
                self.install_build(
                    by_channel[selected.upper()],
                    force_version_prompt=True,
                    prompt_for_force_reinstall=True,
                )

    def explore_versions(self) -> None:
        versions = [item["id"] for item in self.api.get_project_versions()]
        print("Available versions:")
        for index, version in enumerate(versions, start=1):
            print(f"  {index:>2}. {version}")
        while True:
            reply = self.logger.prompt_input("Pick a version number (or press Enter to cancel): ").strip()
            if not reply:
                self.logger.log("Cancelled version explorer.")
                return
            if reply.isdigit() and 1 <= int(reply) <= len(versions):
                selected = versions[int(reply) - 1]
                self.inspect_version(selected, offer_download=True)
                return
            print("Please enter one of the listed numbers.")

    def choose_target_for_update(self) -> BuildInfo | None:
        current = self.find_current_jar()
        latest_version, latest_build = self.latest_stable_version()

        if current is None:
            self.logger.log(
                f"No current Paper jar detected. Latest stable is version {latest_version} build #{latest_build.build_id}."
            )
            return latest_build

        version_cmp = compare_versions(current.version, latest_version)
        if version_cmp == 0:
            if latest_build.build_id > current.build:
                if not self.allow_same_version_build_upgrade:
                    self.logger.log(
                        "A newer build exists for the current version, but same-version build upgrades are disabled in config."
                    )
                    return None
                self.logger.log(
                    f"Current server is on {current.version} build #{current.build}. "
                    f"Latest stable build is #{latest_build.build_id}."
                )
                return latest_build
            if latest_build.build_id == current.build and self.args.force:
                self.logger.log(
                    f"Current server is already on {current.version} build #{current.build}, "
                    "but --force was supplied, so PaperScript will re-download and reinstall the latest stable build."
                )
                return latest_build
            self.logger.log(
                f"Current server is already on {current.version} build #{current.build}. "
                "No newer stable build is available, so no download was performed."
            )
            self.logger.log("If you want to re-download this jar anyway, run one of these:")
            self.logger.log("  ./paperscript.sh --force update")
            exact_force = self.force_example_for_current(current)
            if exact_force:
                self.logger.log(f"  {exact_force}")
            return None

        if version_cmp > 0:
            self.logger.log(
                f"Current server version {current.version} is newer than the latest stable version "
                f"this script found ({latest_version}). No download was performed automatically."
            )
            return None

        self.logger.log(
            f"Current server is on version {current.version} build #{current.build}. "
            f"Latest stable is {latest_version} build #{latest_build.build_id}."
        )
        if self.allow_cross_version_auto_upgrade:
            self.logger.log("Cross-version auto-upgrade is enabled in config, so PaperScript will continue.")
            return latest_build
        if self.args.dry_run:
            self.logger.log(
                f"Dry run: PaperScript would ask before upgrading from {current.version} to {latest_version}."
            )
            return latest_build
        if self.args.yes or prompt_yes_no(
            f"This is a version upgrade from {current.version} to {latest_version}. Download it?",
            default=False,
            logger=self.logger,
        ):
            return latest_build
        self.logger.log("Skipped version upgrade by choice.")
        self.logger.log("No download was performed.")
        return None

    def ensure_safe_to_upgrade(self) -> None:
        if not (self.server_dir / "server.properties").exists():
            return
        processes = self.detect_running_server_processes()
        if not processes:
            return

        self.logger.log("A Paper server process appears to be running in this server directory.")
        for pid, command in processes:
            self.logger.log(f"  - PID {pid}: {command}")

        if self.args.dry_run:
            action = self.planned_running_server_action()
            self.logger.log(f"Dry run: PaperScript would handle the running server with action '{action}'.")
            return

        if self.args.yes:
            self.logger.log("--yes was supplied, so PaperScript will try a graceful stop automatically.")
            self.graceful_stop(processes)
            return

        configured_action = self.running_server_action
        if configured_action == "graceful-stop":
            self.graceful_stop(processes)
            return
        if configured_action == "force-stop":
            self.force_stop(processes)
            return
        if configured_action == "upgrade-anyway":
            self.logger.log("Config is set to continue even if the server appears to still be running.")
            return

        choice = prompt_choice(
            "Choose how to continue:",
            [
                ("g", "Gracefully stop the server first"),
                ("f", "Force stop the server"),
                ("u", "Upgrade anyway without stopping"),
                ("e", "Exit without changing anything"),
            ],
            default="e",
            logger=self.logger,
        )
        if choice == "g":
            self.graceful_stop(processes)
            return
        if choice == "f":
            self.force_stop(processes)
            return
        if choice == "u":
            self.logger.log("Proceeding even though the server appears to still be running.")
            return
        raise PaperScriptError("Stopped at user request.")

    def detect_running_server_processes(self) -> list[tuple[int, str]]:
        if self.auto_detect_server_by_port:
            by_port = self.detect_processes_by_server_port()
            if by_port:
                return by_port

        if not self.fallback_process_detection:
            return []

        result = run_command(["ps", "-axo", "pid=,command="])
        if result.returncode != 0:
            return []

        current = self.find_current_jar()
        jar_name = current.path.name if current else None
        server_dir_text = str(self.server_dir)
        matches: list[tuple[int, str]] = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            pid_text, _, command = stripped.partition(" ")
            if not pid_text.isdigit():
                continue
            pid = int(pid_text)
            command = command.strip()
            if "java" not in command.lower():
                continue
            if jar_name and jar_name in command:
                matches.append((pid, command))
                continue
            if server_dir_text in command:
                matches.append((pid, command))
                continue
            cwd = self.process_cwd(pid)
            if cwd and Path(cwd).resolve() == self.server_dir:
                matches.append((pid, command))
        return matches

    def detect_processes_by_server_port(self) -> list[tuple[int, str]]:
        properties = parse_properties(self.server_dir / "server.properties")
        port_text = properties.get("server-port", "25565").strip()
        if not port_text.isdigit():
            return []

        result = run_command(["lsof", "-nP", f"-iTCP:{port_text}", "-sTCP:LISTEN", "-Fp", "-Fc", "-Fn"])
        if result.returncode != 0:
            return []

        matches: list[tuple[int, str]] = []
        current_pid: int | None = None
        current_command = ""
        for line in result.stdout.splitlines():
            if not line:
                continue
            prefix = line[0]
            value = line[1:]
            if prefix == "p":
                if current_pid is not None and "java" in current_command.lower():
                    matches.append((current_pid, current_command or "java"))
                current_pid = int(value) if value.isdigit() else None
                current_command = ""
            elif prefix == "c":
                current_command = value
        if current_pid is not None and "java" in current_command.lower():
            matches.append((current_pid, current_command or "java"))
        return matches

    def planned_running_server_action(self) -> str:
        if self.args.yes:
            return "graceful-stop"
        if self.running_server_action in {"ask", "graceful-stop", "force-stop", "upgrade-anyway"}:
            return self.running_server_action
        return "ask"

    def process_cwd(self, pid: int) -> str | None:
        result = run_command(["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"])
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
        return None

    def graceful_stop(self, processes: list[tuple[int, str]]) -> None:
        if self.try_tmux_stop():
            if self.wait_for_exit([pid for pid, _ in processes], timeout_seconds=45):
                self.logger.log("Server stopped after sending the tmux stop command.")
                return
            self.logger.log("The tmux stop command was sent, but the process is still running.")

        for pid, _ in processes:
            os.kill(pid, signal.SIGTERM)
            self.logger.log(f"Sent SIGTERM to PID {pid}.")
        if not self.wait_for_exit([pid for pid, _ in processes], timeout_seconds=20):
            raise PaperScriptError("The server did not stop after a soft shutdown attempt.")
        self.logger.log("Server stopped.")

    def force_stop(self, processes: list[tuple[int, str]]) -> None:
        for pid, _ in processes:
            os.kill(pid, signal.SIGKILL)
            self.logger.log(f"Sent SIGKILL to PID {pid}.")
        if not self.wait_for_exit([pid for pid, _ in processes], timeout_seconds=10):
            raise PaperScriptError("A server process still appears to be running after SIGKILL.")
        self.logger.log("Server force-stopped.")

    def wait_for_exit(self, pids: list[int], timeout_seconds: int) -> bool:
        deadline = time.time() + timeout_seconds
        remaining = set(pids)
        while remaining and time.time() < deadline:
            finished: list[int] = []
            for pid in remaining:
                try:
                    os.kill(pid, 0)
                except OSError:
                    finished.append(pid)
            for pid in finished:
                remaining.discard(pid)
            if remaining:
                time.sleep(1)
        return not remaining

    def try_tmux_stop(self) -> bool:
        session = self.tmux_session
        has_session = run_command(["tmux", "has-session", "-t", session])
        if has_session.returncode != 0:
            self.logger.log(
                f"tmux session '{session}' was not found, so PaperScript cannot send a graceful stop command there."
            )
            return False

        send = run_command(["tmux", "send-keys", "-t", session, self.graceful_stop_command, "Enter"])
        if send.returncode != 0:
            self.logger.log(
                f"Sending '{self.graceful_stop_command}' to tmux session '{session}' failed: "
                f"{send.stderr.strip() or 'unknown error'}"
            )
            return False

        self.logger.log(f"Sent '{self.graceful_stop_command}' to tmux session '{session}'.")
        return True

    def backup_existing_jar(self, current: JarInfo | None, incoming_name: str) -> None:
        if current and current.path.exists():
            destination = self.backups_dir / f"{timestamp_for_filename()}__{current.path.name}"
            shutil.move(str(current.path), str(destination))
            self.logger.log(f"Backed up current jar to {destination}")

        incoming_path = self.server_dir / incoming_name
        if incoming_path.exists():
            destination = self.backups_dir / f"{timestamp_for_filename()}__{incoming_path.name}"
            shutil.move(str(incoming_path), str(destination))
            self.logger.log(f"Backed up existing target jar to {destination}")

    def prune_old_backups(self) -> None:
        if not self.cleanup_backups_after_install or self.keep_backups < 0:
            return

        backups = [path for path in self.backups_dir.iterdir() if path.is_file()]
        backups.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for old_path in backups[self.keep_backups:]:
            old_path.unlink()
            self.logger.log(f"Removed old backup {old_path}")

    def format_download_filename(self, build: BuildInfo) -> str:
        try:
            return self.download_filename_pattern.format(version=build.version, build=build.build_id)
        except (KeyError, IndexError, ValueError):
            return f"Paper-{build.version}-{build.build_id}.jar"

    def print_dry_run_summary(self, target_path: Path, current: JarInfo | None, build: BuildInfo) -> None:
        self.logger.log("Dry run: no files were changed.")
        self.logger.log(f"Dry run: would download {build.download_url}")
        if build.sha256:
            self.logger.log(f"Dry run: expected SHA-256 from API is {build.sha256}")
        self.logger.log(f"Dry run: would stage the jar in {self.downloads_dir}")
        if current:
            self.logger.log(f"Dry run: would back up {current.path.name} into {self.backups_dir}")
        if target_path.exists():
            self.logger.log(f"Dry run: would also back up existing target jar {target_path.name}")
        self.logger.log(f"Dry run: would install {target_path}")
        if self.cleanup_backups_after_install:
            self.logger.log(f"Dry run: would keep the newest {self.keep_backups} backups after install.")

    def install_build(
        self,
        build: BuildInfo,
        force_version_prompt: bool = False,
        prompt_for_force_reinstall: bool = False,
    ) -> None:
        current = self.find_current_jar()
        target_name = self.format_download_filename(build)
        manual_force_reinstall = False
        if current and current.version == build.version and current.build >= build.build_id and not self.args.force:
            if (
                current.build == build.build_id
                and prompt_for_force_reinstall
                and not self.args.dry_run
                and sys.stdin.isatty()
            ):
                if prompt_yes_no(
                    f"Current jar {current.path.name} is already build #{current.build}. Download it anyway?",
                    default=False,
                    logger=self.logger,
                ):
                    self.logger.log("Proceeding with a forced re-download of the same build.")
                    manual_force_reinstall = True
                else:
                    self.logger.log("Cancelled re-download of the same build.")
                    return
            if not manual_force_reinstall:
                self.logger.log(
                    f"Current jar {current.path.name} is already build #{current.build} for version {current.version}. "
                    "Nothing newer needs to be downloaded."
                )
                return

        if current and current.version == build.version and current.build < build.build_id and not self.allow_same_version_build_upgrade:
            self.logger.log("Same-version build upgrades are disabled in config, so the newer build will not be installed.")
            return

        force_requested = self.args.force or manual_force_reinstall
        if current and force_requested and self.confirm_before_force_download and not self.args.yes and not self.args.dry_run and not manual_force_reinstall:
            if not prompt_yes_no(
                "Force download is enabled. Continue with the requested install?",
                default=False,
                logger=self.logger,
            ):
                self.logger.log("Cancelled forced download.")
                return
        elif current and force_requested and self.confirm_before_force_download and self.args.dry_run:
            self.logger.log("Dry run: PaperScript would ask for confirmation before a forced download.")

        if current and compare_versions(current.version, build.version) > 0 and self.confirm_before_downgrade and not self.args.yes and not self.args.dry_run:
            if not prompt_yes_no(
                f"This appears to be a downgrade from version {current.version} to {build.version}. Continue?",
                default=False,
                logger=self.logger,
            ):
                self.logger.log("Cancelled downgrade.")
                return
        elif current and compare_versions(current.version, build.version) > 0 and self.confirm_before_downgrade and self.args.dry_run:
            self.logger.log(
                f"Dry run: PaperScript would ask before downgrading from {current.version} to {build.version}."
            )

        if current and compare_versions(current.version, build.version) < 0 and force_version_prompt and not self.args.yes:
            if self.args.dry_run:
                self.logger.log(
                    f"Dry run: PaperScript would ask before upgrading from {current.version} to {build.version}."
                )
            elif not prompt_yes_no(
                f"This will upgrade from version {current.version} to {build.version}. Continue?",
                default=False,
                logger=self.logger,
            ):
                self.logger.log("Cancelled version upgrade.")
                return

        self.ensure_safe_to_upgrade()

        target_path = self.server_dir / target_name
        temp_path = self.downloads_dir / f"{target_name}.part"
        final_temp = self.downloads_dir / target_name

        if self.args.dry_run:
            self.print_dry_run_summary(target_path, current, build)
            return

        if temp_path.exists():
            temp_path.unlink()
        if final_temp.exists():
            final_temp.unlink()

        self.logger.log(
            f"Downloading Paper {build.version} build #{build.build_id} "
            f"({build.channel}, {format_bytes(build.size)})..."
        )
        try:
            verification = self.api.download_file(build, temp_path)
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise
        temp_path.rename(final_temp)
        self.logger.log(f"Downloaded to {final_temp}")
        if build.sha256:
            self.logger.log(f"Expected SHA-256: {build.sha256}")
        self.logger.log(f"Downloaded SHA-256: {verification.sha256}")
        self.logger.log(
            f"Checksum verification: {'match' if not build.sha256 or verification.sha256.lower() == build.sha256.lower() else 'mismatch'}"
        )

        self.backup_existing_jar(current, target_name)
        shutil.move(str(final_temp), str(target_path))
        self.logger.log(f"Installed {target_path}")
        self.record_state(build, target_path, verification.sha256)
        self.prune_old_backups()

    def run_update(self) -> None:
        self.describe_server_context()
        target = self.choose_target_for_update()
        if target is None:
            self.logger.log("Update finished with no download or install changes.")
            return
        self.install_build(target, force_version_prompt=False)

    def run_download(self, version: str, build_id: int | None, channel: str) -> None:
        if build_id is not None:
            builds = self.api.get_builds(version)
            selected = next((build for build in builds if build.build_id == build_id), None)
            if not selected:
                raise PaperScriptError(f"Build #{build_id} was not found for version {version}")
            self.install_build(selected, force_version_prompt=True, prompt_for_force_reinstall=True)
            return

        selected = self.api.get_latest_build(version, channel=channel)
        if not selected:
            raise PaperScriptError(f"No {channel.upper()} build was found for version {version}")
        self.install_build(selected, force_version_prompt=True, prompt_for_force_reinstall=True)

    def run_status(self) -> None:
        compact = self.effective_status_view() == "compact"
        properties = parse_properties(self.server_dir / "server.properties")
        current = self.find_current_jar()
        running = self.detect_running_server_processes()
        latest_version, latest_build = self.latest_stable_version()
        latest_experimental_version, latest_experimental_build = self.latest_version_for_channel("ALPHA")
        tmux_available = self.tmux_session_available()
        update_relevant = current is None

        self.logger.kv("PaperScript version", APP_RELEASE)
        self.logger.kv("Server directory", str(self.server_dir))
        if not compact:
            self.logger.kv("Runtime directory", str(self.runtime_dir))
            self.logger.kv("Server label", str(self.server_name or "none"))
            self.logger.kv("tmux session", self.tmux_session)
            self.logger.kv("tmux session available", format_bool(tmux_available))
            self.logger.kv("Graceful stop command", self.graceful_stop_command)
            self.logger.kv("Server properties found", format_bool((self.server_dir / "server.properties").exists()))
            self.logger.kv("Configured server port", properties.get("server-port", "25565"))
            self.logger.kv("Running server detected", format_bool(bool(running)))
            if running:
                for pid, command in running:
                    self.logger.kv(f"  PID {pid}", command)
        if current:
            self.logger.kv(
                "Current jar",
                f"{current.path.name} (version {current.version}, build #{current.build})",
            )
            current_sha = sha256_file(current.path)
            if not compact:
                self.logger.kv("Current jar SHA-256", current_sha)
            expected_sha = self.state.get("expected_sha256")
            state_jar = self.state.get("current_jar")
            if expected_sha and state_jar == current.path.name and not compact:
                self.logger.kv("Expected SHA-256", str(expected_sha))
                self.logger.kv(
                    "Current SHA matches expected",
                    format_bool(current_sha.lower() == str(expected_sha).lower()),
                )
        else:
            self.logger.kv("Current jar", "none")

        self.logger.kv(
            f"Latest {self.check_latest_channel_only.lower()} release",
            f"{latest_version} build #{latest_build.build_id}",
        )
        if current is None:
            self.logger.kv(
                "Update status",
                "no installed jar detected, so PaperScript would offer the latest release.",
            )
            update_relevant = True
        else:
            version_cmp = compare_versions(current.version, latest_version)
            if version_cmp == 0:
                if latest_build.build_id > current.build:
                    self.logger.kv(
                        "Update status",
                        f"newer build available for the same version ({current.build} -> {latest_build.build_id}).",
                    )
                    update_relevant = True
                elif latest_build.build_id == current.build:
                    self.logger.kv("Update status", "already on the latest stable build.")
                else:
                    self.logger.kv(
                        "Update status",
                        "installed build is newer than the latest stable build this script found.",
                    )
            elif version_cmp < 0:
                self.logger.kv(
                    "Update status",
                    f"newer version available ({current.version} -> {latest_version}).",
                )
                update_relevant = True
            else:
                self.logger.kv(
                    "Update status",
                    "installed version is newer than the latest stable version this script found.",
                )

        self.log_command_hint(
            "Use './paperscript.sh stable' to inspect the latest stable release, './paperscript.sh update' to install it, "
            "or './paperscript.sh --force update' to re-download it even if it is already installed."
        )
        self.log_release_page(update_relevant)

        if self.status_show_all_channels and not compact:
            self.logger.log(f"Latest channels for stable version {latest_version}:")
            channels = self.latest_builds_by_channel(latest_version)
            for channel in ["STABLE", "BETA", "ALPHA", "RECOMMENDED"]:
                build = channels.get(channel)
                if build:
                    self.logger.kv(
                        f"  {channel}",
                        f"build #{build.build_id}, {format_bytes(build.size)}",
                        width=14,
                    )
        self.logger.kv(
            "Latest experimental release",
            f"{latest_experimental_version} build #{latest_experimental_build.build_id}",
        )
        self.log_command_hint(
            "Use './paperscript.sh experimental' to inspect it, or './paperscript.sh experimental --download' to install it."
        )
        self.logger.kv(
            "Backup retention",
            f"keep {self.keep_backups} backups, cleanup after install {format_bool(self.cleanup_backups_after_install)}",
        )

    def run_stable(self, download: bool = False) -> None:
        version, build = self.latest_stable_version()
        self.logger.log(f"Latest stable release overall: {version} build #{build.build_id} ({format_bytes(build.size)})")
        self.logger.log(f"Download URL: {build.download_url}")
        if build.sha256:
            self.logger.log(f"Expected SHA-256: {build.sha256}")
        self.log_command_hint(
            f"Exact manual command: ./paperscript.sh download --version {version} --channel {self.check_latest_channel_only}"
        )
        self.log_release_page(relevant=True)
        if download:
            self.install_build(build, force_version_prompt=False, prompt_for_force_reinstall=True)

    def run_experimental(self, download: bool = False) -> None:
        version, build = self.latest_version_for_channel("ALPHA")
        self.logger.log(
            f"Latest experimental release overall: {version} build #{build.build_id} ({format_bytes(build.size)})"
        )
        self.logger.log(f"Download URL: {build.download_url}")
        if build.sha256:
            self.logger.log(f"Expected SHA-256: {build.sha256}")
        self.log_command_hint(f"Exact manual command: ./paperscript.sh download --version {version} --channel ALPHA", important=True)
        self.log_release_page(relevant=True)
        if download:
            self.install_build(build, force_version_prompt=True, prompt_for_force_reinstall=True)

    def run_verify(self) -> None:
        current = self.find_current_jar()
        if not current:
            raise PaperScriptError("No current Paper jar was detected to verify.")

        current_sha = sha256_file(current.path)
        self.logger.log(
            f"Verify target: {current.path.name} (version {current.version}, build #{current.build})"
        )
        self.logger.log(f"Current SHA-256: {current_sha}")

        state_jar = self.state.get("current_jar")
        state_expected = self.state.get("expected_sha256")
        state_current = self.state.get("current_sha256")
        if state_jar == current.path.name:
            if not state_current and not state_expected:
                self.logger.log("Recorded install state exists for this jar, but it does not contain stored SHA-256 values yet.")
            if state_current:
                self.logger.log(f"Recorded SHA-256 from install time: {state_current}")
                self.logger.log(
                    f"Current SHA-256 matches recorded install SHA: "
                    f"{format_bool(current_sha.lower() == str(state_current).lower())}"
                )
            if state_expected:
                self.logger.log(f"Recorded expected SHA-256: {state_expected}")
                self.logger.log(
                    f"Current SHA-256 matches recorded expected SHA: "
                    f"{format_bool(current_sha.lower() == str(state_expected).lower())}"
                )
        else:
            self.logger.log("Recorded install state does not match the currently detected jar, so local state comparison is unavailable.")

        api_build: BuildInfo | None = None
        try:
            api_build = self.api.get_build_by_id(current.version, current.build)
        except PaperScriptError as error:
            self.logger.log(f"API checksum lookup unavailable: {error}")

        if api_build is None:
            self.logger.log("API checksum verification: unavailable for this jar or the API could not be reached.")
            return

        self.logger.log(f"API download URL: {api_build.download_url}")
        if api_build.sha256:
            self.logger.log(f"API expected SHA-256: {api_build.sha256}")
            self.logger.log(
                f"Current SHA-256 matches API expected SHA: "
                f"{format_bool(current_sha.lower() == api_build.sha256.lower())}"
            )
        else:
            self.logger.log("API checksum verification: this build did not include a SHA-256 in the API response.")

    def cleanup_selection(self) -> dict[str, bool]:
        selected = {
            "downloads": bool(getattr(self.args, "cleanup_downloads", False)),
            "backups": bool(getattr(self.args, "cleanup_backups", False)),
            "pycache": bool(getattr(self.args, "cleanup_pycache", False)),
            "logs": bool(getattr(self.args, "cleanup_logs", False)),
            "json": bool(getattr(self.args, "cleanup_json", False)),
        }
        if getattr(self.args, "cleanup_keep", None) is not None:
            selected["backups"] = True
        if not any(selected.values()):
            selected["downloads"] = True
            selected["pycache"] = True
        return selected

    def cleanup_descriptions(self, selection: dict[str, bool]) -> list[str]:
        descriptions: list[str] = []
        if selection["downloads"]:
            descriptions.append(f"Delete staged downloads and temp files in {self.downloads_dir}")
        if selection["backups"]:
            keep = getattr(self.args, "cleanup_keep", None)
            if keep is None:
                descriptions.append(f"Delete all backup jars in {self.backups_dir}")
            else:
                descriptions.append(f"Trim backup jars in {self.backups_dir} so only the newest {keep} remain")
        if selection["pycache"]:
            descriptions.append(f"Delete Python __pycache__ folders under {self.runtime_dir}")
        if selection["logs"]:
            descriptions.append(f"Clear the log file at {self.log_path}")
        if selection["json"]:
            descriptions.append(
                f"Delete {self.config_path.name} and {self.state_path.name} so the next run starts fresh"
            )
        return descriptions

    def remove_directory_contents(self, path: Path) -> int:
        if not path.exists():
            return 0
        removed = 0
        for child in path.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        return removed

    def find_pycache_dirs(self) -> list[Path]:
        return [path for path in self.runtime_dir.rglob("__pycache__") if path.is_dir()]

    def trim_backups_to_keep(self, keep: int) -> int:
        if keep < 0:
            raise PaperScriptError("Cleanup backup retention cannot be negative.")
        backups = [path for path in self.backups_dir.iterdir() if path.is_file()]
        backups.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        removed = 0
        for old_path in backups[keep:]:
            old_path.unlink()
            removed += 1
        return removed

    def console_only(self, message: str) -> None:
        if not self.logger.quiet:
            print(message)

    def run_init(self) -> None:
        actions: list[str] = []
        if not self.backups_dir.exists():
            actions.append(f"Create backup directory {self.backups_dir}")
        if not self.downloads_dir.exists():
            actions.append(f"Create downloads directory {self.downloads_dir}")
        if not self.config_path.exists():
            actions.append(f"Create default config file {self.config_path}")
        if not self.state_path.exists():
            actions.append(f"Create empty state file {self.state_path}")
        if not self.log_path.exists():
            actions.append(f"Create log file {self.log_path}")
        if not self.todo_path.exists():
            actions.append(f"Create todo log {self.todo_path}")

        if not actions:
            self.logger.log("PaperScript runtime is already initialized.")
            return

        self.logger.log("PaperScript init will do the following:")
        for action in actions:
            self.logger.log(f"  - {action}")

        if self.args.dry_run:
            self.logger.log("Dry run: no files were created.")
            return

        if not self.args.yes and not prompt_yes_no("Proceed with init?", default=False, logger=self.logger):
            self.logger.log("Cancelled init.")
            return

        ensure_directory(self.backups_dir)
        ensure_directory(self.downloads_dir)
        if not self.config_path.exists():
            self._save_json(self.config_path, self.config)
        if not self.state_path.exists():
            self._save_json(self.state_path, {})
        if not self.log_path.exists():
            self.log_path.write_text("", encoding="utf-8")
        if not self.todo_path.exists():
            self.todo_path.write_text(TODO_TEMPLATE, encoding="utf-8")
        self.logger.log("Init finished.")

    def run_cleanup(self) -> None:
        selection = self.cleanup_selection()
        descriptions = self.cleanup_descriptions(selection)

        self.logger.log("Cleanup targets selected:")
        for description in descriptions:
            self.logger.log(f"  - {description}")

        if self.args.dry_run:
            self.logger.log("Dry run: no files were deleted.")
            return

        if not self.args.yes:
            if not prompt_yes_no("Proceed with cleanup?", default=False, logger=self.logger):
                self.logger.log("Cancelled cleanup.")
                return

        removed_downloads = 0
        removed_backups = 0
        removed_pycache = 0
        cleared_logs = False
        removed_json = 0

        if selection["downloads"]:
            removed_downloads = self.remove_directory_contents(self.downloads_dir)
            self.logger.log(f"Removed {removed_downloads} item(s) from {self.downloads_dir}")

        if selection["backups"]:
            if getattr(self.args, "cleanup_keep", None) is None:
                removed_backups = self.remove_directory_contents(self.backups_dir)
                self.logger.log(f"Removed {removed_backups} item(s) from {self.backups_dir}")
            else:
                removed_backups = self.trim_backups_to_keep(int(self.args.cleanup_keep))
                self.logger.log(
                    f"Removed {removed_backups} old backup item(s) from {self.backups_dir} and kept the newest {self.args.cleanup_keep}"
                )

        if selection["pycache"]:
            for pycache_dir in self.find_pycache_dirs():
                shutil.rmtree(pycache_dir)
                removed_pycache += 1
            self.logger.log(f"Removed {removed_pycache} __pycache__ folder(s)")

        if selection["json"]:
            for json_path in [self.config_path, self.state_path]:
                if json_path.exists():
                    json_path.unlink()
                    removed_json += 1
            self.logger.log(f"Removed {removed_json} JSON file(s)")

        if selection["logs"]:
            self.log_path.write_text("", encoding="utf-8")
            cleared_logs = True

        summary = (
            "Cleanup finished: "
            f"downloads={removed_downloads}, "
            f"backups={removed_backups}, "
            f"pycache={removed_pycache}, "
            f"json={removed_json}, "
            f"logs={'cleared' if cleared_logs else 'unchanged'}"
        )
        if cleared_logs:
            self.console_only(summary)
        else:
            self.logger.log(summary)


def timestamp_for_filename() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperscript.sh",
        description="Download and upgrade Paper server jars through the Fill v3 API.",
        epilog=(
            f"Project and examples: {PROJECT_URL}\n"
            "Use 'update' for the latest stable release, or 'download --version ... --build ...' for an exact jar."
        ),
    )
    parser.add_argument("--server-dir", help="Target server directory. Defaults to the current directory.")
    parser.add_argument(
        "--user-agent",
        help="Custom User-Agent header to send to the PaperMC API. Defaults to the built-in PaperScript identity.",
    )
    parser.add_argument(
        "--tmux-session",
        help="tmux session name to use for graceful stop. Defaults to config, PAPERSCRIPT_TMUX_SESSION, or mcserver.",
    )
    parser.add_argument(
        "--contact",
        help="Optional legacy contact value used to build a PaperScript/<version> (<contact>) User-Agent override.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=f"HTTP timeout in seconds. Default: config value or {DEFAULT_TIMEOUT}.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Accept prompts automatically where it is safe to do so.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force reinstall even if the same build is already present. Useful with update or download.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what PaperScript would do without changing files or stopping servers.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress normal console output. Useful for cron or scheduled tasks; logs still go to logs.log.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors in terminal output.",
    )

    subparsers = parser.add_subparsers(dest="command")

    update_parser = subparsers.add_parser("update", help="Download the latest stable Paper build when appropriate.")
    update_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what PaperScript would do without changing files or stopping servers.",
    )
    status_parser = subparsers.add_parser("status", help="Show current server state and whether an update is available.")
    status_parser.add_argument(
        "--compact",
        dest="status_compact",
        action="store_true",
        help="Show a shorter status view.",
    )
    status_parser.add_argument(
        "--full",
        dest="status_full",
        action="store_true",
        help="Force the full status view even if config defaults to compact.",
    )
    subparsers.add_parser("verify", help="Verify the current jar SHA-256 against recorded state and the live API when available.")
    stable_parser = subparsers.add_parser(
        "stable",
        help="Show or download the latest stable Paper release overall.",
    )
    stable_parser.add_argument(
        "--download",
        action="store_true",
        help="Download and install the latest stable release overall.",
    )
    experimental_parser = subparsers.add_parser(
        "experimental",
        help="Show or download the latest experimental Paper release overall.",
    )
    experimental_parser.add_argument(
        "--download",
        action="store_true",
        help="Download and install the latest experimental release overall.",
    )
    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Remove selected runtime files such as downloads, backups, __pycache__, logs, or JSON state/config.",
    )
    cleanup_parser.add_argument(
        "--downloads",
        dest="cleanup_downloads",
        action="store_true",
        help="Delete staged downloads and temporary files in downloads/.",
    )
    cleanup_parser.add_argument(
        "--backups",
        dest="cleanup_backups",
        action="store_true",
        help="Delete all files in backups/, or trim them when used with --keep.",
    )
    cleanup_parser.add_argument(
        "--keep",
        dest="cleanup_keep",
        type=int,
        default=None,
        help="When cleaning backups, keep the newest N backup files instead of deleting them all.",
    )
    cleanup_parser.add_argument(
        "--pycache",
        dest="cleanup_pycache",
        action="store_true",
        help="Delete Python __pycache__ folders under the PaperScript runtime directory.",
    )
    cleanup_parser.add_argument(
        "--logs",
        dest="cleanup_logs",
        action="store_true",
        help="Clear logs.log.",
    )
    cleanup_parser.add_argument(
        "--json",
        "--config",
        dest="cleanup_json",
        action="store_true",
        help="Delete config.json and state.json so the next run starts fresh.",
    )
    cleanup_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the cleanup confirmation prompt.",
    )
    cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what cleanup would delete without removing anything.",
    )

    list_parser = subparsers.add_parser("list-versions", help="List versions available from the API.")
    list_parser.add_argument(
        "--channels",
        action="store_true",
        help="Also show the newest build per channel for each version.",
    )

    inspect_parser = subparsers.add_parser("inspect", help="Show the latest builds for one version.")
    inspect_parser.add_argument("version", help="Minecraft version to inspect, for example 1.20.4 or 26.1.2")

    subparsers.add_parser("explore", help="Interactively pick a version, inspect it, and optionally download it.")
    subparsers.add_parser("init", help="Create or repair the PaperScript runtime files in paperscript/ with confirmation.")

    download_parser = subparsers.add_parser("download", help="Download a chosen version or exact build.")
    download_parser.add_argument("--version", required=True, help="Minecraft version to download.")
    download_parser.add_argument("--build", type=int, help="Exact build number to download.")
    download_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what PaperScript would do without changing files or stopping servers.",
    )
    download_parser.add_argument(
        "--channel",
        default=None,
        choices=["ALPHA", "BETA", "STABLE", "RECOMMENDED", "alpha", "beta", "stable", "recommended"],
        help="Build channel to use when --build is omitted. Default: config value or STABLE.",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        args.command = "update"

    try:
        app = PaperScriptApp(args)
        if args.command == "update":
            app.run_update()
        elif args.command == "status":
            app.run_status()
        elif args.command == "verify":
            app.run_verify()
        elif args.command == "stable":
            app.run_stable(download=args.download)
        elif args.command == "experimental":
            app.run_experimental(download=args.download)
        elif args.command == "cleanup":
            app.run_cleanup()
        elif args.command == "list-versions":
            app.list_versions(show_channels=args.channels)
        elif args.command == "inspect":
            app.inspect_version(args.version, offer_download=True)
        elif args.command == "explore":
            app.explore_versions()
        elif args.command == "init":
            app.run_init()
        elif args.command == "download":
            selected_channel = args.channel.upper() if args.channel else app.default_channel
            app.run_download(args.version, args.build, selected_channel)
        else:
            parser.print_help()
            return 1
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except PaperScriptError as error:
        print(
            color_text(
                f"Error: {error}",
                ANSI_RED,
                supports_color(sys.stderr, no_color=bool(getattr(args, "no_color", False))),
                bold=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
