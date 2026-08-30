#!/usr/bin/env python3
"""PaperScript: manually stage verified PaperMC jars without controlling the server."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


APP_NAME = "PaperScript"
APP_VERSION = "5.2.0"
APP_BUILD = "052"
APP_RELEASE = f"{APP_VERSION} build {APP_BUILD}"
API_ROOT = "https://fill.papermc.io/v3/projects/paper"
PROJECT_URL = "https://github.com/mrfdev/PaperScript"
PAPER_DOWNLOADS_URL = "https://papermc.io/downloads/paper"
DEFAULT_CHANNEL = "STABLE"
DEFAULT_TIMEOUT = 30
DEFAULT_USER_AGENT = f"mrfloris-PaperScript/2.0 ({PROJECT_URL})"
STAGE_SELECTION_EXACT = "exact"
STAGE_SELECTION_LATEST_CHANNEL = "latest-channel"
STAGE_SELECTION_LATEST_OVERALL = "latest-overall"
STAGE_SELECTION_LATEST_PREVIEW = "latest-preview"
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
STAGING_FREE_SPACE_RESERVE_BYTES = 64 * 1024 * 1024
MAX_JAR_MANIFEST_BYTES = 1024 * 1024
JAR_MANIFEST_PATH = "META-INF/MANIFEST.MF"
COMMAND_NAMES = {
    "update",
    "status",
    "verify",
    "stable",
    "experimental",
    "cleanup",
    "list-versions",
    "inspect",
    "explore",
    "init",
    "download",
}
CURRENT_JAR_PATTERN = re.compile(r"^paper-(.+)-(\d+)\.jar$", re.IGNORECASE)
LEGACY_JAR_PATTERN = re.compile(r"^paper-(.+)\.jar$", re.IGNORECASE)
SAFE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LAST_LAUNCHED_JAR_MARKER = "last-launched-jar.txt"
LAUNCHER_MARKER_ROLLBACK = ".launcher-marker-rollback"
DEPRECATED_CONFIG_KEYS = {
    "allow_cross_version_auto_upgrade",
    "cleanup_backups_after_install",
    "running_server_action",
    "graceful_stop_command",
    "download_filename_pattern",
}
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
ANSI_SGR_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
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
TODO_TEMPLATE = """PaperScript production todo

Product direction and boundaries

- [x] Keep PaperScript manually invoked. Remove cron, scheduling, and unattended-update recommendations.
- [x] Make update a non-disruptive staging operation: download and verify a new
      Paper-<version>-<build>.jar beside the active jar while the server keeps running.
      PaperScript must not stop, kill, start, or restart the server, and must not replace
      the jar currently in use.
- [x] Keep tmux lifecycle control manual and external to PaperScript. PaperScript may use
      tmux information for read-only status and doctor checks, but update must not send
      stop or start commands.
- [x] Keep full server, world, plugin, and BlueMap backup orchestration separate. A quick
      Paper jar download must not wait for hundreds of gigabytes of backup work. Limit
      PaperScript itself to safe jar retention and cleanup.

Safe jar staging

- [x] Add a per-server exclusive lock so overlapping manual runs cannot share, remove,
      or overwrite each other's staged downloads or state.
- [x] Make staging transactional: require Paper API size and SHA-256, preflight server-root
      write access, durable directory sync, and free-space reserve; stream into one private
      same-filesystem inode without exceeding the declared size; verify on-disk size,
      SHA-256, executable JAR structure, and every ZIP CRC; fsync; then atomically publish
      Paper-<version>-<build>.jar without overwriting an existing path.
- [x] Write config and state atomically with temp files, fsync, and os.replace; preserve
      and report corrupt JSON instead of silently replacing it.
- [x] Write metadata cache atomically through validated directory descriptors and reject
      unsafe cache directories and leaves.
- [ ] Preserve a corrupt cache for diagnosis before refreshing it.
- [x] Validate and contain every configured path. Reject filesystem roots, traversal,
      unsafe symlinks, and jar filename patterns that are not plain .jar basenames.
- [x] Namespace runtime state and locks by canonical server directory so one PaperScript
      checkout can safely target more than one server.

1MB-minecraft.sh integration

- [x] Copy the latest canonical 1MB-minecraft.sh into the disposable PaperScript test
      instance and record its original SHA-256 for later manual comparison.
- [x] Customize the test-instance copy so, for its configured Minecraft version, it
      recognizes Paper-<version>-<numeric-build>.jar and selects the greatest numeric build
      on the next manual start. Never select a jar from another Minecraft version.
- [x] Preserve compatibility with the existing paper-<version>.jar naming fallback, ignore
      .part and malformed files, handle case consistently, and use numeric rather than
      lexicographic build ordering.
- [x] Add launcher tests for multiple builds, multiple Minecraft versions, legacy fallback,
      malformed names, and build-number boundaries such as 9 versus 10.
- [x] Record the exact launcher-selected basename atomically before Java starts, resolve
      the launcher's own directory, ignore symlink jar candidates, hold a fail-closed
      launch lock for the JVM lifetime, and restore the prior marker on JVM failure.
- [ ] Manually review and apply the test-instance launcher diff at its canonical source,
      then propagate it through the normal 1MB-minecraft.sh release/update flow rather than
      editing every generated server copy.

Bounded Paper jar retention

- [x] Keep the valid last-launched jar plus the newest staged same-version jar in the
      server root by default; temporarily protect one in-flight launcher rollback jar;
      never infer or move an active jar without a valid marker.
- [x] Archive only exact regular non-symlink Paper-<version>-<numeric-build>.jar files for
      the marker version under paperscript/backups/jars/<version>/ and cap that archive.
- [x] Add explicit cleanup --server-jars with confirmation, --keep, --version, and dry-run;
      keep it out of cleanup --all and preserve the old cleanup --keep backup behavior.
- [x] Add adversarial tests for numeric ordering, versions, malformed names, symlinks,
      invalid markers, archive caps, lock contention, and active-jar byte/inode stability.

Validation and operator feedback

- [ ] Add a read-only doctor command covering supported Java version, disk space,
      permissions, API access, config/state validity, active and staged jars, and tmux
      identity without controlling the tmux session.
- [x] Make verify fail closed: require a valid SHA-256 for production downloads and return
      nonzero status for mismatch or unverifiable state.
- [ ] Add machine-readable JSON output and documented exit codes for current, update
      available, staged, no-op, mismatch, degraded/API unavailable, and invalid config.
- [ ] Review the Codex Security report.md Security Objectives and Assumptions sections,
      then reconcile accepted guarantees with repository documentation, tests, and deployment guidance.
- [ ] Add offline/degraded status with stale-cache fallback, log rotation and severity/run
      IDs, a --version option, supported-Python CI, and reproducible tagged releases with
      checksummed artifacts.

Tests

- [ ] Add hermetic CLI integration tests using a local HTTP fixture and disposable server
      roots for download retries, corrupt responses/cache, checksum mismatch, and exits.
- [ ] Add failure-injection tests for concurrent runs, signals at every staging boundary,
      full disks, path traversal and symlinks, corrupt JSON, and cross-filesystem paths.
- [ ] Add manual or scheduled project CI smoke coverage for Java/Paper compatibility without
      turning server updates themselves into scheduled production automation.
"""
DEFAULT_CONFIG: dict[str, Any] = {
    "server_name": None,
    "tmux_session": "mcserver",
    "default_channel": "STABLE",
    "check_latest_channel_only": "STABLE",
    "allow_same_version_build_upgrade": True,
    "keep_backups": 10,
    "keep_server_jars": 2,
    "keep_archived_jars": 5,
    "reconcile_server_jars_after_stage": True,
    "http_timeout_seconds": 30,
    "status_show_all_channels": True,
    "log_file": "logs.log",
    "backup_dir": "backups",
    "downloads_dir": "downloads",
    "metadata_cache_dir": "cache",
    "metadata_cache_enabled": True,
    "metadata_cache_ttl_seconds": 300,
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
    "debug_http": False,
    "http_retries": 2,
    "http_retry_backoff_seconds": 1.5,
    "list_versions_channel_delay_ms": 150,
    "list_versions_continue_on_error": True,
}
BOOLEAN_CONFIG_KEYS = {
    "allow_same_version_build_upgrade",
    "reconcile_server_jars_after_stage",
    "status_show_all_channels",
    "metadata_cache_enabled",
    "confirm_before_force_download",
    "confirm_before_downgrade",
    "auto_detect_server_by_port",
    "fallback_process_detection",
    "quiet",
    "no_color",
    "debug_http",
    "list_versions_continue_on_error",
}
INTEGER_CONFIG_MINIMUMS = {
    "keep_backups": 0,
    "keep_server_jars": 2,
    "keep_archived_jars": 1,
    "http_timeout_seconds": 1,
    "metadata_cache_ttl_seconds": 0,
    "http_retries": 0,
    "list_versions_channel_delay_ms": 0,
}
NUMBER_CONFIG_MINIMUMS = {
    "http_retry_backoff_seconds": 0.0,
}
STRING_CONFIG_KEYS = {
    "tmux_session",
    "log_file",
    "backup_dir",
    "downloads_dir",
    "metadata_cache_dir",
}
OPTIONAL_STRING_CONFIG_KEYS = {"server_name", "contact", "user_agent"}
CONFIG_CHOICES = {
    "default_channel": {"ALPHA", "BETA", "STABLE", "RECOMMENDED"},
    "check_latest_channel_only": {"ALPHA", "BETA", "STABLE", "RECOMMENDED"},
    "color_theme": set(COLOR_THEMES),
    "default_status_view": {"full", "compact"},
    "command_hint_mode": {"auto", "always", "never"},
    "release_link_mode": {"auto", "always", "never"},
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


@dataclass(frozen=True)
class UpdateSelection:
    build: BuildInfo
    staging_policy: str


@dataclass
class DownloadVerification:
    sha256: str
    bytes_written: int
    elapsed_seconds: float


@dataclass(frozen=True)
class ArtifactVerification:
    sha256: str
    size: int
    entry_count: int
    main_class: str


@dataclass(frozen=True)
class JarRetentionPlan:
    version: str
    last_launched: Path
    kept: tuple[Path, ...]
    to_archive: tuple[Path, ...]
    to_prune: tuple[Path, ...] = ()
    launch_rollback: Path | None = None


@dataclass(frozen=True)
class LauncherJarSelection:
    path: Path
    version: str
    build: int | None

    @property
    def is_numeric_build(self) -> bool:
        return self.build is not None


class StyledConsoleText(str):
    """Console text containing only PaperScript-owned styling around sanitized text."""


class ServerMutationLock:
    """Non-blocking advisory lock for one server root."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: TextIO | None = None

    def __enter__(self) -> "ServerMutationLock":
        ensure_directory(self.path.parent)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.handle.close()
            self.handle = None
            raise PaperScriptError(
                f"Another PaperScript staging, verification, or server-jar cleanup is already running "
                f"for this server ({self.path})."
            ) from error
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


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


def terminal_safe_text(text: str) -> str:
    """Render terminal control characters as visible text without reinterpreting them."""
    escaped: list[str] = []
    named = {"\n": r"\n", "\r": r"\r", "\t": r"\t"}
    for character in str(text):
        codepoint = ord(character)
        if character in named:
            escaped.append(named[character])
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            escaped.append(f"\\x{codepoint:02x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def plain_console_text(message: str) -> str:
    """Remove PaperScript-owned SGR styling before writing a durable text log."""
    return ANSI_SGR_PATTERN.sub("", str(message))


def prompt_yes_no(question: str, default: bool = False, logger: "Logger | None" = None) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        prompt = f"{question} {suffix} "
        if logger is not None:
            reply = logger.prompt_input(prompt).strip().lower()
        else:
            reply = input(terminal_safe_text(prompt)).strip().lower()
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
        print(terminal_safe_text(question))
    for key, label in choices:
        default_mark = " (default)" if default == key else ""
        if logger is not None:
            logger.log(f"  {key}) {label}{default_mark}")
        else:
            print(terminal_safe_text(f"  {key}) {label}{default_mark}"))
    valid = {key for key, _ in choices}
    while True:
        if logger is not None:
            reply = logger.prompt_input("> ").strip().lower()
        else:
            reply = input(terminal_safe_text("> ")).strip().lower()
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


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def paths_overlap(left: Path, right: Path) -> bool:
    return path_is_within(left, right) or path_is_within(right, left)


def fsync_directory(path: Path, *, strict: bool = False) -> None:
    """Persist a directory entry update where the platform supports it."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as error:
        if strict:
            raise PaperScriptError(f"Could not open directory for durable sync {path}: {error}") from error
        return
    try:
        os.fsync(descriptor)
    except OSError as error:
        if strict:
            raise PaperScriptError(f"Could not durably sync directory {path}: {error}") from error
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Write a small runtime file without exposing a partial/truncated value."""
    ensure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()


def open_safe_cache_directory(path: Path) -> int | None:
    """Open one owner-controlled cache directory for descriptor-relative operations."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        before = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISDIR(before.st_mode)
        or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
        or stat.S_IMODE(before.st_mode) & 0o022
    ):
        return None

    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        after = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        return None
    if (
        not stat.S_ISDIR(after.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or (hasattr(os, "geteuid") and after.st_uid != os.geteuid())
        or stat.S_IMODE(after.st_mode) & 0o022
    ):
        os.close(descriptor)
        return None
    return descriptor


def read_safe_cache_text(directory_descriptor: int, leaf_name: str) -> str | None:
    """Read one regular cache inode relative to an already-validated directory."""
    try:
        before = os.stat(
            leaf_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
        or stat.S_IMODE(before.st_mode) & 0o022
    ):
        return None

    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(
            leaf_name,
            flags,
            dir_fd=directory_descriptor,
        )
    except OSError:
        return None
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or (hasattr(os, "geteuid") and after.st_uid != os.geteuid())
            or stat.S_IMODE(after.st_mode) & 0o022
        ):
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def atomic_write_cache_text(
    directory_descriptor: int,
    leaf_name: str,
    content: str,
    mode: int = 0o600,
) -> None:
    """Atomically replace a cache leaf without resolving the directory pathname again."""
    temporary_name = ""
    descriptor = -1
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        for _ in range(16):
            temporary_name = f".{leaf_name}.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    mode,
                    dir_fd=directory_descriptor,
                )
                break
            except FileExistsError:
                continue
        else:
            raise PaperScriptError(f"Could not allocate a private cache file for {leaf_name}.")

        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(
            temporary_name,
            leaf_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporary_name = ""
        os.fsync(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass


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


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = int(seconds // 60)
    remainder = seconds - (minutes * 60)
    return f"{minutes}m {remainder:.1f}s"


def format_rate(bytes_written: int, seconds: float) -> str:
    if seconds <= 0:
        return "instant"
    return f"{format_bytes(int(bytes_written / seconds))}/s"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def required_artifact_size(build: BuildInfo) -> int:
    """Return trustworthy Fill size metadata or refuse to stage the artifact."""
    if isinstance(build.size, bool) or not isinstance(build.size, int) or build.size <= 0:
        raise PaperScriptError(
            f"Paper API did not provide a valid positive size for {build.filename}; staging was refused."
        )
    return build.size


def normalized_sha256(value: object) -> str | None:
    """Return one canonical SHA-256 digest or reject malformed metadata/state."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return None
    return value.lower()


def require_artifact_metadata(build: BuildInfo) -> int:
    """Fail closed when Fill omits either field needed for safe staging."""
    if not isinstance(build.download_url, str) or terminal_safe_text(build.download_url) != build.download_url:
        parsed_url = None
    else:
        try:
            parsed_url = urlsplit(build.download_url)
        except ValueError:
            parsed_url = None
    if (
        parsed_url is None
        or parsed_url.scheme.casefold() != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise PaperScriptError(
            f"Paper API did not provide a safe HTTPS download URL for {build.filename}; staging was refused."
        )
    if normalized_sha256(build.sha256) is None:
        raise PaperScriptError(
            f"Paper API did not provide a valid SHA-256 for {build.filename}; staging was refused."
        )
    return required_artifact_size(build)


def sha256_descriptor(descriptor: int) -> str:
    """Hash the already-open inode so verification cannot follow a replaced path."""
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as handle:
        handle.seek(0)
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def manifest_main_class(payload: bytes) -> str:
    if len(payload) > MAX_JAR_MANIFEST_BYTES:
        raise PaperScriptError(
            f"JAR manifest exceeds the {format_bytes(MAX_JAR_MANIFEST_BYTES)} safety limit."
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PaperScriptError("JAR manifest is not valid UTF-8.") from error

    physical_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    main_section: list[str] = []
    for line in physical_lines:
        if line.startswith(" "):
            if not main_section:
                raise PaperScriptError("JAR manifest starts with an invalid continuation line.")
            main_section[-1] += line[1:]
            continue
        if not line:
            break
        main_section.append(line)

    values: list[str] = []
    for line in main_section:
        key, separator, value = line.partition(":")
        if separator and key.casefold() == "main-class":
            values.append(value.strip())
    if len(values) != 1 or not values[0]:
        raise PaperScriptError("JAR manifest must contain exactly one non-empty Main-Class entry.")

    main_class = values[0]
    if (
        any(character.isspace() for character in main_class)
        or "/" in main_class
        or "\\" in main_class
        or main_class.startswith(".")
        or main_class.endswith(".")
        or ".." in main_class
    ):
        raise PaperScriptError(f"JAR manifest has an unsafe Main-Class value: {main_class!r}.")
    return main_class


def verify_paper_artifact_descriptor(descriptor: int, build: BuildInfo) -> ArtifactVerification:
    """Verify exact downloaded bytes as an intact executable JAR before publication."""
    expected_size = required_artifact_size(build)
    descriptor_stat = os.fstat(descriptor)
    if not stat.S_ISREG(descriptor_stat.st_mode):
        raise PaperScriptError(f"Staging file for {build.filename} is no longer a regular file.")
    if descriptor_stat.st_size != expected_size:
        raise PaperScriptError(
            f"Downloaded size mismatch for {build.filename}: expected {expected_size}, "
            f"got {descriptor_stat.st_size} bytes on disk."
        )

    digest = sha256_descriptor(descriptor)
    expected_sha256 = str(build.sha256 or "")
    if digest.lower() != expected_sha256.lower():
        raise PaperScriptError(
            f"Checksum mismatch for {build.filename}: expected {expected_sha256}, got {digest}"
        )

    try:
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            handle.seek(0)
            with zipfile.ZipFile(handle, "r") as archive:
                entries = archive.infolist()
                regular_entries = [entry for entry in entries if not entry.is_dir()]
                if not regular_entries:
                    raise PaperScriptError("JAR archive contains no files.")

                manifest_entries = [
                    entry for entry in entries if entry.filename == JAR_MANIFEST_PATH
                ]
                if len(manifest_entries) != 1 or manifest_entries[0].is_dir():
                    raise PaperScriptError(
                        f"JAR archive must contain exactly one regular {JAR_MANIFEST_PATH}."
                    )
                manifest_info = manifest_entries[0]
                if manifest_info.file_size > MAX_JAR_MANIFEST_BYTES:
                    raise PaperScriptError(
                        f"JAR manifest exceeds the {format_bytes(MAX_JAR_MANIFEST_BYTES)} safety limit."
                    )
                main_class = manifest_main_class(archive.read(manifest_info))

                main_class_path = main_class.replace(".", "/") + ".class"
                main_class_entries = [
                    entry for entry in entries if entry.filename == main_class_path
                ]
                if len(main_class_entries) != 1 or main_class_entries[0].is_dir():
                    raise PaperScriptError(
                        f"JAR Main-Class {main_class!r} does not have exactly one {main_class_path} entry."
                    )
                with archive.open(main_class_entries[0], "r") as class_handle:
                    if class_handle.read(4) != b"\xca\xfe\xba\xbe":
                        raise PaperScriptError(
                            f"JAR Main-Class entry {main_class_path} is not a Java class file."
                        )

                corrupt_entry = archive.testzip()
                if corrupt_entry is not None:
                    raise PaperScriptError(
                        f"JAR CRC/decompression verification failed for entry {corrupt_entry!r}."
                    )
    except PaperScriptError:
        raise
    except (
        OSError,
        EOFError,
        RuntimeError,
        ValueError,
        NotImplementedError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise PaperScriptError(f"Artifact is not an intact executable JAR: {error}") from error

    return ArtifactVerification(
        sha256=digest,
        size=descriptor_stat.st_size,
        entry_count=len(regular_entries),
        main_class=main_class,
    )


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
            or lower.startswith("staged:")
            or lower.startswith("archived old server-root jar:")
            or lower.startswith("backed up ")
            or lower.startswith("cleanup finished:")
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
        if isinstance(message, StyledConsoleText):
            log_message = terminal_safe_text(plain_console_text(message))
            console_message = str(message)
        else:
            log_message = terminal_safe_text(str(message))
            console_message = self._console_text(log_message)
        line = f"[{utc_now()}] {log_message}"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if not self.quiet:
            print(console_message)

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
        try:
            safe_message = terminal_safe_text(message)
            return input(color_text(safe_message, self.theme["prompt"], self.use_color, bold=True))
        except EOFError as error:
            raise PaperScriptError("Input stream closed while waiting for a reply. PaperScript cancelled the prompt.") from error


class PaperAPI:
    def __init__(
        self,
        user_agent: str,
        timeout: int = DEFAULT_TIMEOUT,
        logger: Logger | None = None,
        debug_http: bool = False,
        retries: int = 0,
        retry_backoff_seconds: float = 1.5,
        cache_dir: Path | None = None,
        cache_ttl_seconds: int = 300,
        cache_enabled: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.logger = logger
        self.debug_http = debug_http
        self.retries = max(0, int(retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.cache_dir = cache_dir
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self.cache_enabled = cache_enabled

    def _log_http(self, message: str) -> None:
        if self.logger is not None and not self.logger.quiet:
            self.logger.log(message)

    def _cache_leaf_name(self, label: str) -> str:
        safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label.strip())
        return f"{safe_label}.json"

    def _open_cache_directory(self) -> int | None:
        if not self.cache_enabled or self.cache_dir is None:
            return None
        return open_safe_cache_directory(self.cache_dir)

    def _cache_path(self, label: str) -> Path | None:
        directory_descriptor = self._open_cache_directory()
        if directory_descriptor is None or self.cache_dir is None:
            return None
        os.close(directory_descriptor)
        return self.cache_dir / self._cache_leaf_name(label)

    def _load_cache(self, label: str) -> Any | None:
        directory_descriptor = self._open_cache_directory()
        if directory_descriptor is None:
            return None
        try:
            cache_text = read_safe_cache_text(
                directory_descriptor,
                self._cache_leaf_name(label),
            )
            if cache_text is None:
                return None
            payload = json.loads(cache_text)
        except json.JSONDecodeError:
            return None
        finally:
            os.close(directory_descriptor)
        if not isinstance(payload, dict):
            return None
        cached_at = payload.get("cached_at_epoch")
        data = payload.get("data")
        if isinstance(cached_at, bool) or not isinstance(cached_at, (int, float)):
            return None
        try:
            cached_at_float = float(cached_at)
        except (OverflowError, ValueError):
            return None
        now = time.time()
        if not math.isfinite(cached_at_float) or cached_at_float > now:
            return None
        age_seconds = now - cached_at_float
        if age_seconds > self.cache_ttl_seconds:
            return None
        if self.debug_http:
            self._log_http(f"Using cached metadata ({format_duration(age_seconds)}) for {label}")
        return data

    def _save_cache(self, label: str, data: Any) -> None:
        directory_descriptor = self._open_cache_directory()
        if directory_descriptor is None:
            return
        payload = {
            "cached_at": utc_now(),
            "cached_at_epoch": time.time(),
            "data": data,
        }
        try:
            atomic_write_cache_text(
                directory_descriptor,
                self._cache_leaf_name(label),
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                mode=0o600,
            )
        finally:
            os.close(directory_descriptor)

    def _request_json(self, url: str) -> Any:
        request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        for attempt in range(self.retries + 1):
            started_at = time.monotonic()
            if self.debug_http:
                self._log_http(f"HTTP GET {attempt + 1}/{self.retries + 1}: {url}")
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read().decode("utf-8")
                if self.debug_http:
                    self._log_http(
                        f"HTTP GET completed in {format_duration(time.monotonic() - started_at)}: {url}"
                    )
                break
            except HTTPError as error:
                detail_raw = error.read().decode("utf-8", errors="ignore") if hasattr(error, "read") else ""
                if error.code in TRANSIENT_HTTP_CODES and attempt < self.retries:
                    wait_seconds = self.retry_backoff_seconds * (2 ** attempt)
                    self._log_http(
                        f"Transient API error HTTP {error.code} for {url}. Retrying in {wait_seconds:.1f}s..."
                    )
                    time.sleep(wait_seconds)
                    continue
                detail = summarize_http_detail(detail_raw)
                message = f"API request failed for {url}: HTTP {error.code}"
                if detail:
                    message += f" {detail}"
                if error.code in TRANSIENT_HTTP_CODES:
                    message += " The Paper API or Cloudflare may be having a temporary issue; please retry."
                raise PaperScriptError(message) from error
            except URLError as error:
                if attempt < self.retries:
                    wait_seconds = self.retry_backoff_seconds * (2 ** attempt)
                    self._log_http(
                        f"Transient network error for {url}: {error.reason}. Retrying in {wait_seconds:.1f}s..."
                    )
                    time.sleep(wait_seconds)
                    continue
                raise PaperScriptError(f"API request failed for {url}: {error.reason}") from error

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as error:
            raise PaperScriptError(f"API returned invalid JSON for {url}") from error

        if isinstance(data, dict) and data.get("ok") is False:
            raise PaperScriptError(data.get("message") or f"API returned an error for {url}")
        return data

    def get_project_versions(self, *, use_cache: bool = True) -> list[dict[str, Any]]:
        cached = self._load_cache("versions") if use_cache else None
        rich = cached if cached is not None else self._request_json(f"{API_ROOT}/versions")
        if cached is None:
            self._save_cache("versions", rich)
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

        cached_simple = self._load_cache("project-root") if use_cache else None
        simple = cached_simple if cached_simple is not None else self._request_json(API_ROOT)
        if cached_simple is None:
            self._save_cache("project-root", simple)
        raw_versions = simple.get("versions", {})
        flattened: list[dict[str, Any]] = []
        if isinstance(raw_versions, dict):
            for group, items in raw_versions.items():
                for version_id in items:
                    flattened.append({"id": version_id, "group": group})
        return sorted(flattened, key=lambda item: parse_version(item["id"]).key(), reverse=True)

    def get_builds(self, version: str, *, use_cache: bool = True) -> list[BuildInfo]:
        if not SAFE_VERSION_PATTERN.fullmatch(version):
            raise PaperScriptError(f"Unsafe Paper version identifier: {version!r}")
        try:
            cache_label = f"builds-{version}"
            cached = self._load_cache(cache_label) if use_cache else None
            raw = cached if cached is not None else self._request_json(f"{API_ROOT}/versions/{version}/builds")
            if cached is None:
                self._save_cache(cache_label, raw)
        except PaperScriptError as error:
            detail = str(error).lower()
            if "version_not_found" in detail or "no version was found with the given identifier" in detail:
                raise PaperScriptError(
                    f"Paper version '{version}' was not found by the API. "
                    "Try './paperscript.sh list-versions --channels' to browse available versions, "
                    "or './paperscript.sh experimental' for the latest experimental release."
                ) from error
            raise
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

    def get_latest_build(
        self,
        version: str,
        channel: str = DEFAULT_CHANNEL,
        *,
        use_cache: bool = True,
    ) -> BuildInfo | None:
        channel_upper = channel.upper()
        for build in self.get_builds(version, use_cache=use_cache):
            if build.channel == channel_upper:
                return build
        return None

    def get_build_by_id(
        self,
        version: str,
        build_id: int,
        *,
        use_cache: bool = True,
    ) -> BuildInfo | None:
        for build in self.get_builds(version, use_cache=use_cache):
            if build.build_id == build_id:
                return build
        return None

    def download_file(
        self,
        build: BuildInfo,
        destination: Path,
        destination_descriptor: int | None = None,
    ) -> DownloadVerification:
        ensure_directory(destination.parent)
        request = Request(build.download_url, headers={"User-Agent": self.user_agent})
        expected_size = build.size if isinstance(build.size, int) and not isinstance(build.size, bool) else None
        for attempt in range(self.retries + 1):
            sha256 = hashlib.sha256()
            bytes_written = 0
            started_at = time.monotonic()
            try:
                if self.debug_http:
                    self._log_http(f"HTTP DOWNLOAD {attempt + 1}/{self.retries + 1}: {build.download_url}")
                if destination_descriptor is None:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    output_descriptor = os.open(destination, flags, 0o600)
                else:
                    output_descriptor = os.dup(destination_descriptor)
                try:
                    output_stat = os.fstat(output_descriptor)
                    if not stat.S_ISREG(output_stat.st_mode):
                        raise PaperScriptError(
                            f"Download destination for {build.filename} is not a regular file."
                        )
                    with os.fdopen(output_descriptor, "wb") as handle:
                        output_descriptor = -1
                        handle.seek(0)
                        handle.truncate(0)
                        with urlopen(request, timeout=self.timeout) as response:
                            while True:
                                chunk = response.read(1024 * 1024)
                                if not chunk:
                                    break
                                if expected_size is not None and bytes_written + len(chunk) > expected_size:
                                    raise PaperScriptError(
                                        f"Download exceeded the Paper API size for {build.filename}: "
                                        f"expected {expected_size} bytes."
                                    )
                                sha256.update(chunk)
                                bytes_written += len(chunk)
                                handle.write(chunk)
                        handle.flush()
                finally:
                    if output_descriptor >= 0:
                        os.close(output_descriptor)
                elapsed_seconds = time.monotonic() - started_at
                if self.debug_http:
                    self._log_http(
                        f"HTTP DOWNLOAD completed in {format_duration(elapsed_seconds)} "
                        f"at {format_rate(bytes_written, elapsed_seconds)}: {build.download_url}"
                    )
                break
            except HTTPError as error:
                detail_raw = error.read().decode("utf-8", errors="ignore") if hasattr(error, "read") else ""
                if error.code in TRANSIENT_HTTP_CODES and attempt < self.retries:
                    wait_seconds = self.retry_backoff_seconds * (2 ** attempt)
                    self._log_http(
                        f"Transient download error HTTP {error.code} for {build.download_url}. Retrying in {wait_seconds:.1f}s..."
                    )
                    time.sleep(wait_seconds)
                    continue
                detail = summarize_http_detail(detail_raw)
                message = f"Download failed: HTTP {error.code}"
                if detail:
                    message += f" {detail}"
                if error.code in TRANSIENT_HTTP_CODES:
                    message += " The Paper API or Cloudflare may be having a temporary issue; please retry."
                raise PaperScriptError(message) from error
            except URLError as error:
                if attempt < self.retries:
                    wait_seconds = self.retry_backoff_seconds * (2 ** attempt)
                    self._log_http(
                        f"Transient download network error for {build.download_url}: {error.reason}. Retrying in {wait_seconds:.1f}s..."
                    )
                    time.sleep(wait_seconds)
                    continue
                raise PaperScriptError(f"Download failed: {error.reason}") from error
            except (OSError, http.client.IncompleteRead) as error:
                transient = isinstance(
                    error,
                    (TimeoutError, ConnectionError, http.client.IncompleteRead),
                )
                if transient and attempt < self.retries:
                    wait_seconds = self.retry_backoff_seconds * (2 ** attempt)
                    self._log_http(
                        f"Transient download I/O error for {build.download_url}: {error}. "
                        f"Retrying in {wait_seconds:.1f}s..."
                    )
                    time.sleep(wait_seconds)
                    continue
                raise PaperScriptError(f"Download I/O failed for {build.filename}: {error}") from error

        if build.sha256:
            digest = sha256.hexdigest()
            if digest.lower() != build.sha256.lower():
                raise PaperScriptError(
                    f"Checksum mismatch for {build.filename}: expected {build.sha256}, got {digest}"
                )
            return DownloadVerification(
                sha256=digest,
                bytes_written=bytes_written,
                elapsed_seconds=elapsed_seconds,
            )

        return DownloadVerification(
            sha256=sha256.hexdigest(),
            bytes_written=bytes_written,
            elapsed_seconds=elapsed_seconds,
        )


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


def summarize_http_detail(detail: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", detail)
    cleaned = " ".join(cleaned.split())
    cleaned = terminal_safe_text(cleaned)
    if not cleaned:
        return ""
    return f"{cleaned[:220]}..." if len(cleaned) > 220 else cleaned


def normalize_global_options(argv: list[str]) -> list[str]:
    global_flags = {"--yes", "--force", "--dry-run", "--quiet", "--no-color"}
    global_value_options = {"--server-dir", "--user-agent", "--tmux-session", "--contact", "--timeout"}
    command_index: int | None = None
    for index, token in enumerate(argv):
        if token in COMMAND_NAMES:
            command_index = index
            break
    if command_index is None:
        return argv

    before = list(argv[:command_index])
    command = argv[command_index]
    after = argv[command_index + 1 :]
    moved: list[str] = []
    kept: list[str] = []
    index = 0
    while index < len(after):
        token = after[index]
        if token in global_flags:
            moved.append(token)
            index += 1
            continue
        if token in global_value_options:
            moved.append(token)
            if index + 1 < len(after):
                moved.append(after[index + 1])
                index += 2
            else:
                index += 1
            continue
        matched_inline = False
        for option in global_value_options:
            if token.startswith(f"{option}="):
                moved.append(token)
                matched_inline = True
                break
        if matched_inline:
            index += 1
            continue
        kept.append(token)
        index += 1
    return before + moved + [command] + kept


class PaperScriptApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.script_dir = Path(__file__).resolve().parent
        self.server_dir = self._resolve_server_dir()
        self.server_runtime_dir = self.server_dir / "paperscript"
        self.runtime_dir = self.server_runtime_dir
        self.last_launched_jar_marker_path = (
            self.server_runtime_dir / LAST_LAUNCHED_JAR_MARKER
        )
        self.jar_archive_dir = self.server_runtime_dir / "backups" / "jars"
        self.server_lock_path = self.server_runtime_dir / "locks" / "paper-jars.lock"
        self.config_path = self.runtime_dir / "config.json"
        self.state_path = self.runtime_dir / "state.json"
        self.todo_path = self.runtime_dir / "todo.log"
        legacy_runtime_names = ("config.json", "state.json")
        self.legacy_runtime_files = [
            self.script_dir / name
            for name in legacy_runtime_names
            if self.runtime_dir != self.script_dir
            and (self.script_dir / name).is_file()
            and not (self.runtime_dir / name).exists()
        ]
        self.validate_server_runtime_path(self.runtime_dir)
        if self.runtime_dir.exists() and not self.runtime_dir.is_dir():
            raise PaperScriptError(
                f"PaperScript runtime path must be a directory, not {self.runtime_dir}."
            )
        self.config = self._load_config()
        self.quiet_mode = bool(args.quiet or self.config.get("quiet"))
        self.no_color = bool(args.no_color or self.config.get("no_color"))
        self.color_theme_name = normalize_choice(self.config.get("color_theme"), set(COLOR_THEMES), "default")
        self.backups_dir = self.configured_runtime_path("backup_dir")
        self.downloads_dir = self.configured_runtime_path("downloads_dir")
        self.metadata_cache_dir = self.configured_runtime_path("metadata_cache_dir")
        self.log_path = self.configured_runtime_path("log_file")
        self.validate_runtime_layout()
        self.logger = Logger(
            self.log_path,
            quiet=self.quiet_mode,
            use_color=supports_color(sys.stdout, no_color=self.no_color),
            theme_name=self.color_theme_name,
        )
        if self.legacy_runtime_files:
            legacy_paths = ", ".join(str(path) for path in self.legacy_runtime_files)
            self.logger.warn(
                f"Legacy central-checkout runtime file(s) were not reused for target {self.server_dir}: "
                f"{legacy_paths}. Review them and manually copy any target-specific settings or state into "
                f"{self.runtime_dir}; PaperScript created safe target-local defaults."
            )
        ensure_directory(self.backups_dir)
        ensure_directory(self.downloads_dir)
        ensure_directory(self.metadata_cache_dir)
        self.state = self._load_json(self.state_path)
        self.server_name = self.config.get("server_name")
        self.default_channel = str(self.config["default_channel"]).upper()
        self.check_latest_channel_only = str(self.config["check_latest_channel_only"]).upper()
        self.allow_same_version_build_upgrade = self.config["allow_same_version_build_upgrade"]
        self.keep_backups = self.config["keep_backups"]
        self.keep_server_jars = self.config["keep_server_jars"]
        self.keep_archived_jars = self.config["keep_archived_jars"]
        self.reconcile_server_jars_after_stage = self.config["reconcile_server_jars_after_stage"]
        self.status_show_all_channels = self.config["status_show_all_channels"]
        self.confirm_before_force_download = self.config["confirm_before_force_download"]
        self.confirm_before_downgrade = self.config["confirm_before_downgrade"]
        self.auto_detect_server_by_port = self.config["auto_detect_server_by_port"]
        self.fallback_process_detection = self.config["fallback_process_detection"]
        self.default_status_view = normalize_choice(self.config.get("default_status_view"), {"full", "compact"}, "full")
        self.command_hint_mode = normalize_choice(self.config.get("command_hint_mode"), {"auto", "always", "never"}, "auto")
        self.release_link_mode = normalize_choice(self.config.get("release_link_mode"), {"auto", "always", "never"}, "auto")
        self.debug_http = bool(args.debug_http or self.config.get("debug_http"))
        self.metadata_cache_enabled = self.config["metadata_cache_enabled"] and not bool(args.no_metadata_cache)
        self.metadata_cache_ttl_seconds = self.config["metadata_cache_ttl_seconds"]
        self.http_retries = self.config["http_retries"]
        self.http_retry_backoff_seconds = float(self.config["http_retry_backoff_seconds"])
        self.list_versions_channel_delay_ms = self.config["list_versions_channel_delay_ms"]
        self.list_versions_continue_on_error = self.config["list_versions_continue_on_error"]
        self.http_timeout = int(args.timeout) if args.timeout is not None else self.config["http_timeout_seconds"]
        if self.http_timeout < 1:
            raise PaperScriptError("--timeout must be at least 1 second.")
        self.user_agent = self._resolve_user_agent()
        self.api = PaperAPI(
            self.user_agent,
            timeout=self.http_timeout,
            logger=self.logger,
            debug_http=self.debug_http,
            retries=self.http_retries,
            retry_backoff_seconds=self.http_retry_backoff_seconds,
            cache_dir=self.metadata_cache_dir,
            cache_ttl_seconds=self.metadata_cache_ttl_seconds,
            cache_enabled=self.metadata_cache_enabled,
        )
        self.tmux_session = (
            args.tmux_session
            or os.environ.get("PAPERSCRIPT_TMUX_SESSION")
            or self.config.get("tmux_session")
            or "mcserver"
        )

    def log_api_activity(self, message: str) -> None:
        if not self.logger.quiet:
            self.logger.log(message)

    def metadata_cache_file_count(self) -> int:
        if not self.metadata_cache_dir.exists():
            return 0
        return sum(1 for path in self.metadata_cache_dir.iterdir() if path.is_file())

    def force_example_for_current(self, current: JarInfo | None) -> str | None:
        if current is None or current.build < 0:
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

    def format_browser_entry(self, prefix: str, value: str, suffix: str = "") -> str:
        prefix = terminal_safe_text(prefix)
        value = terminal_safe_text(value)
        suffix = terminal_safe_text(suffix)
        if not self.logger.use_color:
            return f"{prefix}{value}{suffix}"
        theme = self.logger.theme
        rendered_prefix = color_text(prefix, theme["key"], True)
        rendered_value = color_text(value, theme["value"], True, bold=True)
        rendered_suffix = color_text(suffix, theme["key"], True) if suffix else ""
        return StyledConsoleText(f"{rendered_prefix}{rendered_value}{rendered_suffix}")

    def _resolve_server_dir(self) -> Path:
        if self.args.server_dir:
            resolved = Path(self.args.server_dir).expanduser().resolve()
        else:
            cwd = Path.cwd().resolve()
            resolved = (
                self.script_dir.parent.resolve()
                if cwd == self.script_dir and self.script_dir.name.lower() == APP_NAME.lower()
                else cwd
            )
        if resolved == Path(resolved.anchor):
            raise PaperScriptError("Refusing to use a filesystem root as the server directory.")
        return resolved

    def configured_runtime_path(self, key: str) -> Path:
        raw_path = Path(str(self.config[key]))
        if raw_path.is_absolute():
            raise PaperScriptError(
                f"Config key {key!r} must be relative to {self.runtime_dir}; absolute paths are refused."
            )
        if ".." in raw_path.parts:
            raise PaperScriptError(
                f"Config key {key!r} contains parent traversal; PaperScript runtime paths must stay local."
            )
        candidate = self.runtime_dir / raw_path
        self.validate_server_runtime_path(candidate)
        if candidate.resolve(strict=False) == self.runtime_dir.resolve(strict=False):
            raise PaperScriptError(
                f"Config key {key!r} cannot target the PaperScript runtime directory itself."
            )
        parent = candidate.parent
        while parent != self.runtime_dir:
            if parent.exists() and not parent.is_dir():
                raise PaperScriptError(
                    f"Config key {key!r} has a non-directory path component: {parent}."
                )
            parent = parent.parent
        return candidate

    def validate_runtime_layout(self) -> None:
        """Keep cleanup, archive, lock, state, and log roles from aliasing each other."""
        self.validate_server_runtime_path(self.jar_archive_dir)
        self.validate_server_runtime_path(self.server_lock_path)
        self.validate_server_runtime_path(self.last_launched_jar_marker_path)

        directory_roles = {
            "backup_dir": self.backups_dir,
            "downloads_dir": self.downloads_dir,
            "metadata_cache_dir": self.metadata_cache_dir,
        }
        directory_items = list(directory_roles.items())
        for index, (left_name, left_path) in enumerate(directory_items):
            if left_path.exists() and not left_path.is_dir():
                raise PaperScriptError(
                    f"Config key {left_name!r} must identify a directory, not {left_path}."
                )
            for right_name, right_path in directory_items[index + 1 :]:
                if paths_overlap(left_path, right_path):
                    raise PaperScriptError(
                        f"Config paths {left_name!r} and {right_name!r} overlap; cleanup roles must be disjoint."
                    )

        if paths_overlap(self.backups_dir, self.jar_archive_dir):
            if self.backups_dir.resolve(strict=False) != self.jar_archive_dir.parent.resolve(strict=False):
                raise PaperScriptError(
                    "backup_dir may contain the managed JAR archive only as its direct backups/jars child."
                )
        for key in ("downloads_dir", "metadata_cache_dir"):
            if paths_overlap(directory_roles[key], self.jar_archive_dir):
                raise PaperScriptError(
                    f"Config key {key!r} cannot overlap the managed JAR archive {self.jar_archive_dir}."
                )

        lock_dir = self.server_lock_path.parent
        for key, directory in directory_items:
            if paths_overlap(directory, lock_dir):
                raise PaperScriptError(
                    f"Config key {key!r} cannot overlap the reserved lock directory {lock_dir}."
                )

        reserved_files = {
            self.config_path,
            self.state_path,
            self.todo_path,
            self.last_launched_jar_marker_path,
            self.server_runtime_dir / LAUNCHER_MARKER_ROLLBACK,
            self.server_lock_path,
        }
        for key, directory in directory_items:
            if any(path_is_within(reserved, directory) for reserved in reserved_files):
                raise PaperScriptError(
                    f"Config key {key!r} contains a reserved PaperScript runtime file."
                )

        if self.log_path.exists() and not self.log_path.is_file():
            raise PaperScriptError(f"Config key 'log_file' must identify a regular file, not {self.log_path}.")
        if self.log_path in reserved_files:
            raise PaperScriptError("Config key 'log_file' aliases a reserved PaperScript runtime file.")
        reserved_directories = [
            self.jar_archive_dir,
            lock_dir,
            *directory_roles.values(),
        ]
        if any(paths_overlap(self.log_path, directory) for directory in reserved_directories):
            raise PaperScriptError(
                "Config key 'log_file' must not be inside a cleanup, archive, or lock directory."
            )

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise PaperScriptError(f"Could not read JSON file {path}: {error}") from error
        except json.JSONDecodeError as error:
            raise PaperScriptError(
                f"JSON file {path} is malformed at line {error.lineno}, column {error.colno}; "
                "PaperScript left it unchanged."
            ) from error
        if not isinstance(payload, dict):
            raise PaperScriptError(f"JSON file {path} must contain an object; PaperScript left it unchanged.")
        return payload

    def _save_json(self, path: Path, payload: dict[str, Any]) -> None:
        atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _validate_config(self, config: dict[str, Any]) -> None:
        for key in sorted(BOOLEAN_CONFIG_KEYS):
            if type(config.get(key)) is not bool:
                raise PaperScriptError(
                    f"Config key {key!r} must be true or false; PaperScript left config.json unchanged."
                )
        for key, minimum in INTEGER_CONFIG_MINIMUMS.items():
            value = config.get(key)
            if type(value) is not int or value < minimum:
                raise PaperScriptError(
                    f"Config key {key!r} must be an integer of at least {minimum}; "
                    "PaperScript left config.json unchanged."
                )
        for key, minimum in NUMBER_CONFIG_MINIMUMS.items():
            value = config.get(key)
            if (
                type(value) not in {int, float}
                or not math.isfinite(float(value))
                or value < minimum
            ):
                raise PaperScriptError(
                    f"Config key {key!r} must be a number of at least {minimum:g}; "
                    "PaperScript left config.json unchanged."
                )
        for key in sorted(STRING_CONFIG_KEYS):
            value = config.get(key)
            if not isinstance(value, str) or not value.strip():
                raise PaperScriptError(
                    f"Config key {key!r} must be a non-empty string; PaperScript left config.json unchanged."
                )
        for key in {"log_file", "backup_dir", "downloads_dir", "metadata_cache_dir"}:
            configured_path = Path(str(config[key]))
            if configured_path.is_absolute() or ".." in configured_path.parts:
                raise PaperScriptError(
                    f"Config key {key!r} must be a relative path without parent traversal; "
                    "PaperScript left config.json unchanged."
                )
        validation_root = Path("/__paperscript_runtime_layout__")
        validation_archive = validation_root / "backups" / "jars"
        validation_lock_dir = validation_root / "locks"
        validation_directories = {
            "backup_dir": validation_root / str(config["backup_dir"]),
            "downloads_dir": validation_root / str(config["downloads_dir"]),
            "metadata_cache_dir": validation_root / str(config["metadata_cache_dir"]),
        }
        validation_items = list(validation_directories.items())
        for index, (left_name, left_path) in enumerate(validation_items):
            for right_name, right_path in validation_items[index + 1 :]:
                if paths_overlap(left_path, right_path):
                    raise PaperScriptError(
                        f"Config paths {left_name!r} and {right_name!r} overlap; "
                        "PaperScript left config.json unchanged."
                    )
        validation_backup = validation_directories["backup_dir"]
        if paths_overlap(validation_backup, validation_archive):
            if validation_backup != validation_archive.parent:
                raise PaperScriptError(
                    "Config key 'backup_dir' conflicts with the managed JAR archive; "
                    "PaperScript left config.json unchanged."
                )
        for key in ("downloads_dir", "metadata_cache_dir"):
            if paths_overlap(validation_directories[key], validation_archive):
                raise PaperScriptError(
                    f"Config key {key!r} conflicts with the managed JAR archive; "
                    "PaperScript left config.json unchanged."
                )
        for key, directory in validation_items:
            if paths_overlap(directory, validation_lock_dir):
                raise PaperScriptError(
                    f"Config key {key!r} conflicts with the reserved lock directory; "
                    "PaperScript left config.json unchanged."
                )
        validation_log = validation_root / str(config["log_file"])
        validation_reserved_files = {
            validation_root / "config.json",
            validation_root / "state.json",
            validation_root / "todo.log",
            validation_root / LAST_LAUNCHED_JAR_MARKER,
            validation_root / LAUNCHER_MARKER_ROLLBACK,
            validation_lock_dir / "paper-jars.lock",
        }
        for key, directory in validation_items:
            if any(
                path_is_within(reserved, directory)
                for reserved in validation_reserved_files
            ):
                raise PaperScriptError(
                    f"Config key {key!r} contains a reserved runtime file; "
                    "PaperScript left config.json unchanged."
                )
        if validation_log in validation_reserved_files or any(
            paths_overlap(validation_log, directory)
            for directory in [validation_archive, validation_lock_dir, *validation_directories.values()]
        ):
            raise PaperScriptError(
                "Config key 'log_file' conflicts with a cleanup, archive, lock, or reserved-file role; "
                "PaperScript left config.json unchanged."
            )
        for key in sorted(OPTIONAL_STRING_CONFIG_KEYS):
            value = config.get(key)
            if value is not None and not isinstance(value, str):
                raise PaperScriptError(
                    f"Config key {key!r} must be a string or null; PaperScript left config.json unchanged."
                )
        for key, choices in CONFIG_CHOICES.items():
            value = config.get(key)
            if not isinstance(value, str):
                expected = ", ".join(sorted(choices))
                raise PaperScriptError(
                    f"Config key {key!r} must be one of {expected}; PaperScript left config.json unchanged."
                )
            normalized = value.upper() if key in {"default_channel", "check_latest_channel_only"} else value.lower()
            normalized_choices = {
                choice.upper() if key in {"default_channel", "check_latest_channel_only"} else choice.lower()
                for choice in choices
            }
            if normalized not in normalized_choices:
                expected = ", ".join(sorted(choices))
                raise PaperScriptError(
                    f"Config key {key!r} must be one of {expected}; PaperScript left config.json unchanged."
                )

    def _load_config(self) -> dict[str, Any]:
        raw = self._load_json(self.config_path)
        migrated = {key: value for key, value in raw.items() if key not in DEPRECATED_CONFIG_KEYS}
        allowed_keys = set(DEFAULT_CONFIG) | OPTIONAL_STRING_CONFIG_KEYS
        unknown_keys = sorted(set(migrated) - allowed_keys)
        if unknown_keys:
            rendered = ", ".join(repr(key) for key in unknown_keys)
            raise PaperScriptError(
                f"Unknown config key(s): {rendered}. PaperScript left config.json unchanged."
            )
        merged = dict(DEFAULT_CONFIG)
        merged.update(migrated)
        self._validate_config(merged)
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

    def record_state(self, build: BuildInfo, staged_path: Path, current_sha256: str) -> None:
        self.state.update(
            {
                "staged_build": build.build_id,
                "staged_channel": build.channel,
                "staged_jar": staged_path.name,
                "staged_version": build.version,
                "staged_at": utc_now(),
                "staged_sha256": current_sha256,
                "server_dir": str(self.server_dir),
                "expected_sha256": build.sha256,
                "download_url": build.download_url,
            }
        )
        self._save_json(self.state_path, self.state)

    def recorded_staged_jar(self, version: str) -> Path | None:
        state_name = self.state.get("staged_jar")
        state_version = self.state.get("staged_version")
        state_server_dir = self.state.get("server_dir")
        if not state_name or not state_version:
            return None
        if str(state_version).casefold() != version.casefold():
            return None
        if state_server_dir:
            try:
                if Path(str(state_server_dir)).resolve() != self.server_dir.resolve():
                    return None
            except OSError:
                return None
        path = self.server_dir / str(state_name)
        if self.strict_managed_jar_info(path, version) is None:
            return None
        expected = self.state.get("staged_sha256") or self.state.get("expected_sha256")
        if expected and re.fullmatch(r"[0-9a-fA-F]{64}", str(expected)):
            if sha256_file(path).lower() != str(expected).lower():
                raise PaperScriptError(
                    f"Recorded staged jar {path.name} no longer matches its recorded SHA-256; cleanup was refused."
                )
        return path

    def jar_info_from_state(self, path: Path) -> JarInfo | None:
        state_name = self.state.get("staged_jar") or self.state.get("current_jar")
        if not state_name or path.name != str(state_name):
            return None

        version = self.state.get("staged_version") or self.state.get("current_version")
        build = self.state.get("staged_build")
        if build is None:
            build = self.state.get("current_build")
        if not version or build is None:
            return None

        try:
            build_number = int(build)
        except (TypeError, ValueError):
            return None

        return JarInfo(path, str(version), build_number)

    def jar_info_from_filename(self, path: Path) -> JarInfo | None:
        match = CURRENT_JAR_PATTERN.match(path.name)
        if match:
            return JarInfo(path, match.group(1), int(match.group(2)))
        return self.jar_info_from_state(path)

    def find_current_jar(self) -> JarInfo | None:
        candidates: list[JarInfo] = []
        for path in self.server_dir.glob("*.jar"):
            if path.is_symlink() or not path.is_file():
                continue
            jar = self.jar_info_from_filename(path)
            if jar and CURRENT_JAR_PATTERN.fullmatch(path.name):
                candidates.append(jar)
        if candidates:
            candidates.sort(
                key=lambda item: (parse_version(item.version).key(), item.build),
                reverse=True,
            )
            return candidates[0]

        # Compatibility fallback for a legacy/custom non-numeric jar explicitly recorded in state.
        state_name = self.state.get("staged_jar") or self.state.get("current_jar")
        if state_name:
            state_path = self.server_dir / str(state_name)
            if state_path.parent == self.server_dir and not state_path.is_symlink() and state_path.is_file():
                return self.jar_info_from_state(state_path)
        return None

    @contextmanager
    def server_mutation_lock(self) -> Iterator[None]:
        self.validate_server_runtime_path(self.server_lock_path)
        with ServerMutationLock(self.server_lock_path):
            yield

    def validate_server_runtime_path(self, path: Path) -> None:
        """Reject retention paths that escape the target server through traversal or symlinks."""
        server_root = self.server_dir.resolve()
        candidate = path.resolve(strict=False)
        try:
            candidate.relative_to(server_root)
        except ValueError as error:
            raise PaperScriptError(
                f"Refusing to use a PaperScript server-runtime path outside {server_root}: {path}"
            ) from error

        current = path
        while current != self.server_dir:
            if current.is_symlink():
                raise PaperScriptError(
                    f"Refusing to use symlinked server-runtime path component: {current}"
                )
            parent = current.parent
            if parent == current:
                raise PaperScriptError(f"Could not contain server-runtime path {path} inside {server_root}.")
            current = parent

    def strict_managed_jar_info(self, path: Path, version: str | None = None) -> JarInfo | None:
        """Return canonical numeric Paper metadata without following symlinks."""
        if path.parent != self.server_dir or path.is_symlink():
            return None
        try:
            metadata = path.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            return None
        match = CURRENT_JAR_PATTERN.fullmatch(path.name)
        if not match:
            return None
        parsed_version = match.group(1)
        if not SAFE_VERSION_PATTERN.fullmatch(parsed_version):
            return None
        if version is not None and parsed_version.casefold() != version.casefold():
            return None
        return JarInfo(path=path, version=parsed_version, build=int(match.group(2)))

    def managed_server_jars(self, version: str) -> list[JarInfo]:
        if not SAFE_VERSION_PATTERN.fullmatch(version):
            raise PaperScriptError(f"Unsafe Minecraft version for jar retention: {version!r}")
        candidates: list[JarInfo] = []
        try:
            children = list(self.server_dir.iterdir())
        except OSError as error:
            raise PaperScriptError(f"Could not inspect server directory {self.server_dir}: {error}") from error
        for path in children:
            jar = self.strict_managed_jar_info(path, version)
            if jar is not None:
                candidates.append(jar)
        candidates.sort(key=lambda item: (item.build, item.path.name.casefold(), item.path.name), reverse=True)
        return candidates

    def launcher_jar_selection(self) -> LauncherJarSelection:
        """Read the launcher's exact selection without authorizing legacy jars for retention."""
        marker = self.last_launched_jar_marker_path
        self.validate_server_runtime_path(marker)
        if marker.is_symlink() or not marker.is_file():
            raise PaperScriptError(
                f"No valid launcher marker exists at {marker}. "
                "Start the server once with the updated 1MB-minecraft.sh, then retry."
            )
        try:
            if marker.stat().st_size > 512:
                raise PaperScriptError(f"Launcher marker is unexpectedly large: {marker}")
            raw_name = marker.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PaperScriptError(f"Could not read launcher marker {marker}: {error}") from error
        name = raw_name.strip()
        if (
            not name
            or name != Path(name).name
            or "/" in name
            or "\\" in name
            or len(raw_name.splitlines()) != 1
        ):
            raise PaperScriptError(f"Launcher marker does not contain one safe jar basename: {marker}")
        path = self.server_dir / name
        if path.is_symlink():
            raise PaperScriptError(f"Launcher marker identifies a symlink, which is unsafe: {path}")
        try:
            metadata = path.lstat()
        except OSError as error:
            raise PaperScriptError(f"Launcher marker identifies a missing or unreadable jar: {path}") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise PaperScriptError(f"Launcher marker does not identify a regular file: {path}")

        numeric = self.strict_managed_jar_info(path)
        if numeric is not None:
            return LauncherJarSelection(numeric.path, numeric.version, numeric.build)

        legacy_match = LEGACY_JAR_PATTERN.fullmatch(name)
        if legacy_match and SAFE_VERSION_PATTERN.fullmatch(legacy_match.group(1)):
            return LauncherJarSelection(path, legacy_match.group(1), None)
        raise PaperScriptError(
            f"Launcher marker {marker} does not identify an existing regular Paper jar with a safe version name."
        )

    def last_launched_jar(self, version: str | None = None) -> JarInfo:
        selection = self.launcher_jar_selection()
        if version is not None and selection.version.casefold() != version.casefold():
            expected = f"Paper-{version}-<build>.jar"
            raise PaperScriptError(
                f"Launcher marker {self.last_launched_jar_marker_path} does not identify {expected}. "
                "Root jar retention was skipped."
            )
        if selection.build is None:
            raise PaperScriptError(
                f"Launcher marker identifies legacy jar {selection.path.name}. Root jar retention requires "
                "a numeric Paper-<version>-<build>.jar marker and was skipped. Stage a numeric build, "
                "then start it once with the updated 1MB-minecraft.sh."
            )
        return JarInfo(selection.path, selection.version, selection.build)

    def inflight_launcher_rollback_jar(self, version: str) -> JarInfo | None:
        """Protect the prior numeric JAR while the launcher may need to roll back its marker."""
        launch_lock = self.server_runtime_dir / "locks" / "server-launch"
        rollback_marker = self.server_runtime_dir / LAUNCHER_MARKER_ROLLBACK
        self.validate_server_runtime_path(launch_lock)
        self.validate_server_runtime_path(rollback_marker)
        if not launch_lock.exists():
            return None
        if launch_lock.is_symlink() or not launch_lock.is_dir():
            raise PaperScriptError(
                f"Active launcher lock is unsafe; root JAR retention was refused: {launch_lock}"
            )
        if not rollback_marker.exists():
            return None
        if rollback_marker.is_symlink() or not rollback_marker.is_file():
            raise PaperScriptError(
                f"Active launcher rollback marker is unsafe; root JAR retention was refused: {rollback_marker}"
            )
        try:
            if rollback_marker.stat().st_size > 512:
                raise PaperScriptError(
                    f"Active launcher rollback marker is unexpectedly large: {rollback_marker}"
                )
            raw_name = rollback_marker.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PaperScriptError(
                f"Could not read active launcher rollback marker {rollback_marker}: {error}"
            ) from error
        name = raw_name.strip()
        if not name:
            return None
        if (
            name != Path(name).name
            or "/" in name
            or "\\" in name
            or len(raw_name.splitlines()) != 1
        ):
            raise PaperScriptError(
                f"Active launcher rollback marker does not contain one safe JAR basename: {rollback_marker}"
            )
        path = self.server_dir / name
        managed = self.strict_managed_jar_info(path, version)
        if managed is not None:
            return managed

        numeric = CURRENT_JAR_PATTERN.fullmatch(name)
        if numeric and numeric.group(1).casefold() == version.casefold():
            raise PaperScriptError(
                f"Active launcher rollback JAR is missing or unsafe: {path}. Root JAR retention was refused."
            )
        return None

    def plan_server_jar_retention(
        self,
        version: str,
        keep: int | None = None,
        staged_path: Path | None = None,
    ) -> JarRetentionPlan:
        keep_count = self.keep_server_jars if keep is None else int(keep)
        if keep_count < 2:
            raise PaperScriptError(
                "Server-root jar retention must keep at least two slots: last launched plus newest staged."
            )
        last_launched = self.last_launched_jar(version)
        candidates = self.managed_server_jars(version)
        protected: list[Path] = [last_launched.path]
        launch_rollback = self.inflight_launcher_rollback_jar(version)
        if launch_rollback is not None and launch_rollback.path not in protected:
            protected.append(launch_rollback.path)
        staged: JarInfo | None = None
        if staged_path is not None:
            staged = self.strict_managed_jar_info(staged_path, version)
            if staged is None:
                raise PaperScriptError(f"Just-staged jar is no longer a safe regular file: {staged_path}")
        if candidates and candidates[0].path not in protected:
            protected.append(candidates[0].path)
        if staged is not None and candidates and staged.path != candidates[0].path:
            self.logger.log(
                f"Recorded staged jar {staged.path.name} is not the greatest numeric build; "
                f"the launcher will choose {candidates[0].path.name}, so only the actual next selection is protected."
            )
        for jar in candidates:
            if (
                keep_count > 2
                and jar.path not in protected
                and len(protected) < keep_count
            ):
                protected.append(jar.path)
        protected_set = set(protected)
        to_archive = tuple(jar.path for jar in candidates if jar.path not in protected_set)
        to_prune = self.predict_managed_archive_prune(version, to_archive)
        return JarRetentionPlan(
            version=version,
            last_launched=last_launched.path,
            kept=tuple(protected),
            to_archive=to_archive,
            to_prune=to_prune,
            launch_rollback=launch_rollback.path if launch_rollback is not None else None,
        )

    def archive_directory_for_version(self, version: str) -> Path:
        if not SAFE_VERSION_PATTERN.fullmatch(version):
            raise PaperScriptError(f"Unsafe Minecraft version for jar archive: {version!r}")
        path = self.jar_archive_dir / version
        self.validate_server_runtime_path(path)
        return path

    def fsync_archive_publish_chain(self, archive_dir: Path) -> None:
        """Require every directory entry from the version archive through the server root to be durable."""
        current = archive_dir
        while True:
            fsync_directory(current, strict=True)
            if current == self.server_dir:
                return
            if current == current.parent:
                raise PaperScriptError(
                    f"Could not contain archive directory {archive_dir} under {self.server_dir}."
                )
            current = current.parent

    def archive_server_jar(self, source: Path, version: str) -> Path:
        jar = self.strict_managed_jar_info(source, version)
        if jar is None:
            raise PaperScriptError(f"Refusing to archive a changed or unmanaged jar: {source}")
        archive_dir = self.archive_directory_for_version(version)
        ensure_directory(archive_dir)
        self.validate_server_runtime_path(archive_dir)
        destination = archive_dir / source.name

        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise PaperScriptError(f"Refusing to overwrite non-file jar archive destination: {destination}")
        if destination.exists():
            before = source.lstat()
            source_sha = sha256_file(source)
            after = source.lstat()
            if (
                source.is_symlink()
                or (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
            ):
                raise PaperScriptError(
                    f"Jar changed while an archive collision was checked; left both paths unchanged: {source}"
                )
            if sha256_file(destination) != source_sha:
                raise PaperScriptError(
                    f"Jar archive collision has different content; left both paths unchanged: {destination}"
                )
            self.fsync_archive_publish_chain(archive_dir)
            source.unlink()
            fsync_directory(self.server_dir)
            self.logger.log(f"Removed duplicate root jar already present in the archive: {source.name}")
            return destination

        before = source.lstat()
        descriptor, temporary_name = tempfile.mkstemp(
            dir=archive_dir,
            prefix=f".{source.name}.",
            suffix=".part",
        )
        temporary = Path(temporary_name)
        try:
            digest = hashlib.sha256()
            with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
                descriptor = -1
                while True:
                    chunk = input_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    output_handle.write(chunk)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            after = source.lstat()
            if (
                source.is_symlink()
                or (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
                or digest.hexdigest() != sha256_file(temporary)
            ):
                raise PaperScriptError(f"Jar changed while it was being archived; left the root copy in place: {source}")
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                raise PaperScriptError(f"Jar archive destination appeared during cleanup: {destination}") from error
            temporary.unlink()
            self.fsync_archive_publish_chain(archive_dir)
            source.unlink()
            fsync_directory(self.server_dir)
            self.logger.log(f"Archived old server-root jar: {source.name} -> {destination}")
            return destination
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()

    def predict_managed_archive_prune(
        self,
        version: str,
        incoming: tuple[Path, ...] = (),
    ) -> tuple[Path, ...]:
        archive_dir = self.archive_directory_for_version(version)
        archived: list[JarInfo] = []
        if archive_dir.exists():
            for path in archive_dir.iterdir():
                if path.is_symlink():
                    continue
                try:
                    metadata = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                match = CURRENT_JAR_PATTERN.fullmatch(path.name)
                if not match or match.group(1).casefold() != version.casefold():
                    continue
                archived.append(JarInfo(path, match.group(1), int(match.group(2))))
        known_paths = {item.path for item in archived}
        for source in incoming:
            match = CURRENT_JAR_PATTERN.fullmatch(source.name)
            if not match or match.group(1).casefold() != version.casefold():
                raise PaperScriptError(f"Cannot predict archive retention for unmanaged jar: {source}")
            destination = archive_dir / source.name
            if destination in known_paths or destination.exists():
                continue
            archived.append(JarInfo(destination, match.group(1), int(match.group(2))))
            known_paths.add(destination)
        archived.sort(key=lambda item: (item.build, item.path.name.casefold(), item.path.name), reverse=True)
        return tuple(jar.path for jar in archived[self.keep_archived_jars :])

    def prune_managed_jar_archive(
        self,
        version: str,
        expected: tuple[Path, ...] | None = None,
    ) -> int:
        archive_dir = self.archive_directory_for_version(version)
        to_prune = self.predict_managed_archive_prune(version)
        if expected is not None and to_prune != expected:
            raise PaperScriptError(
                "Managed JAR archive contents changed after retention was planned; no archive files were pruned."
            )
        removed = 0
        for path in to_prune:
            if path.is_symlink() or not path.is_file():
                raise PaperScriptError(f"Refusing to prune a changed managed JAR archive path: {path}")
            match = CURRENT_JAR_PATTERN.fullmatch(path.name)
            if not match or match.group(1).casefold() != version.casefold():
                raise PaperScriptError(f"Refusing to prune an unmanaged JAR archive path: {path}")
            path.unlink()
            removed += 1
            self.logger.log(f"Pruned archived Paper jar: {path}")
        if removed:
            fsync_directory(archive_dir)
        return removed

    def managed_jar_archive_count(self, version: str) -> int:
        archive_dir = self.archive_directory_for_version(version)
        if not archive_dir.exists():
            return 0
        count = 0
        for path in archive_dir.iterdir():
            match = CURRENT_JAR_PATTERN.fullmatch(path.name)
            if (
                not path.is_symlink()
                and path.is_file()
                and match
                and match.group(1).casefold() == version.casefold()
            ):
                count += 1
        return count

    def apply_server_jar_retention(self, plan: JarRetentionPlan, dry_run: bool = False) -> int:
        current_marker = self.last_launched_jar(plan.version)
        if current_marker.path != plan.last_launched:
            raise PaperScriptError("The last-launched jar changed while retention was being planned; retry cleanup.")
        if dry_run:
            for source in plan.to_archive:
                destination = self.archive_directory_for_version(plan.version) / source.name
                self.logger.log(f"Dry run: would archive {source.name} to {destination}")
            for path in plan.to_prune:
                self.logger.log(
                    f"Dry run: would permanently prune archived Paper jar beyond the configured cap: {path}"
                )
            return 0

        archived = 0
        for source in plan.to_archive:
            self.archive_server_jar(source, plan.version)
            archived += 1
        self.prune_managed_jar_archive(plan.version, expected=plan.to_prune)
        return archived

    def latest_stable_version(self, *, use_cache: bool = True) -> tuple[str, BuildInfo]:
        return self.latest_version_for_channel(
            self.check_latest_channel_only,
            use_cache=use_cache,
        )

    def latest_version_for_channel(
        self,
        channel: str,
        *,
        use_cache: bool = True,
    ) -> tuple[str, BuildInfo]:
        channel_upper = channel.upper()
        versions = [
            item["id"]
            for item in self.api.get_project_versions(use_cache=use_cache)
        ]
        for version in versions:
            build = self.api.get_latest_build(
                version,
                channel=channel_upper,
                use_cache=use_cache,
            )
            if build:
                return version, build
        raise PaperScriptError(f"No {channel_upper} Paper builds were found.")

    def latest_preview_version(
        self,
        stable_version: str,
        *,
        use_cache: bool = True,
    ) -> tuple[str, BuildInfo] | None:
        versions = [
            item["id"]
            for item in self.api.get_project_versions(use_cache=use_cache)
        ]
        for version in versions:
            if compare_versions(version, stable_version) <= 0:
                continue
            for channel in ["BETA", "ALPHA"]:
                build = self.api.get_latest_build(
                    version,
                    channel=channel,
                    use_cache=use_cache,
                )
                if build:
                    return version, build
        return None

    def describe_server_context(self) -> None:
        has_server_properties = (self.server_dir / "server.properties").exists()
        current_jar = self.find_current_jar()
        self.logger.log(f"Script directory: {self.script_dir}")
        self.logger.log(f"Server directory: {self.server_dir}")
        self.logger.log(f"Runtime directory: {self.runtime_dir}")
        self.logger.log(f"Server properties found: {'yes' if has_server_properties else 'no'}")
        if current_jar:
            self.logger.log(
                f"Detected newest managed jar: {current_jar.path.name} "
                f"(version {current_jar.version}, build {current_jar.build})"
            )
        else:
            self.logger.log("Detected newest managed jar: none")
        self.log_command_hint(
            "For a stable overview, run './paperscript.sh stable'. For a preview overview, run './paperscript.sh experimental'."
        )

    def list_versions(
        self,
        show_channels: bool = False,
        limit: int | None = None,
        channel_delay_ms: int | None = None,
    ) -> None:
        self.log_api_activity("Contacting Paper API for version data...")
        versions = self.api.get_project_versions()
        if limit is not None and limit > 0:
            versions = versions[:limit]
        self.logger.log(f"Found {len(versions)} Paper versions from the API.")
        failed_versions: list[tuple[str, str]] = []
        effective_delay_ms = self.list_versions_channel_delay_ms if channel_delay_ms is None else channel_delay_ms
        if show_channels:
            self.log_api_activity(
                f"Fetching channel summaries for {len(versions)} version(s). This may take a while if the API is slow."
            )
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
                try:
                    summaries = self.latest_channel_summaries(item["id"])
                    if summaries:
                        extra.append(", ".join(summaries))
                except PaperScriptError as error:
                    if not self.list_versions_continue_on_error:
                        raise
                    failed_versions.append((item["id"], str(error)))
                    extra.append("channel lookup failed")
                if effective_delay_ms > 0:
                    time.sleep(effective_delay_ms / 1000)
            if extra:
                self.logger.log(self.format_browser_entry("  - ", line, f" ({'; '.join(extra)})"))
            else:
                self.logger.log(self.format_browser_entry("  - ", line))
        if failed_versions:
            self.logger.log(
                f"Channel lookup failed for {len(failed_versions)} version(s). "
                "The API may be throttling or returning temporary gateway errors."
            )
            sample_version, sample_error = failed_versions[0]
            self.logger.log(f"First failed version: {sample_version}")
            self.logger.log(f"First failure detail: {sample_error}")
            self.logger.log(
                "Try './paperscript.sh --debug-http list-versions --channels --limit 10' for a smaller verbose retry."
            )

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
                self.stage_build(
                    by_channel[selected.upper()],
                    force_version_prompt=True,
                    prompt_for_forced_recheck=True,
                    selection_policy=STAGE_SELECTION_LATEST_CHANNEL,
                    required_channel=selected.upper(),
                )

    def explore_versions(self) -> None:
        versions = [item["id"] for item in self.api.get_project_versions()]
        self.console_only("Available versions:")
        for index, version in enumerate(versions, start=1):
            self.console_only(self.format_browser_entry(f"  {index:>2}. ", version))
        while True:
            reply = self.logger.prompt_input("Pick a version number (or press Enter to cancel): ").strip()
            if not reply:
                self.logger.log("Cancelled version explorer.")
                return
            if reply.isdigit() and 1 <= int(reply) <= len(versions):
                selected = versions[int(reply) - 1]
                self.inspect_version(selected, offer_download=True)
                return
            self.console_only("Please enter one of the listed numbers.")

    def choose_target_for_update(self, *, use_cache: bool = True) -> UpdateSelection | None:
        newest_managed = self.find_current_jar()
        try:
            current = self.last_launched_jar()
            update_basis = "launcher-marked"
        except PaperScriptError:
            try:
                launcher_selection = self.launcher_jar_selection()
            except PaperScriptError:
                current = newest_managed
                update_basis = "newest managed (launcher marker unavailable)"
            else:
                same_family = self.managed_server_jars(launcher_selection.version)
                state_jar = self.jar_info_from_state(launcher_selection.path)
                current = (
                    same_family[0]
                    if same_family
                    else state_jar
                    if state_jar is not None
                    else JarInfo(launcher_selection.path, launcher_selection.version, -1)
                )
                update_basis = "launcher-marked legacy family"
        latest_overall_version, latest_overall_build = self.latest_stable_version(
            use_cache=use_cache,
        )

        if current is None:
            self.logger.log(
                    f"No managed Paper jar detected. Latest stable is version {latest_overall_version} "
                f"build #{latest_overall_build.build_id}."
            )
            return UpdateSelection(
                latest_overall_build,
                STAGE_SELECTION_LATEST_OVERALL,
            )

        if current.version.casefold() == latest_overall_version.casefold():
            latest_build = latest_overall_build
        else:
            latest_build = self.api.get_latest_build(
                current.version,
                channel=self.check_latest_channel_only,
                use_cache=use_cache,
            )
            if latest_build is None:
                self.logger.log(
                    f"No {self.check_latest_channel_only} build was found for the selected Minecraft version "
                    f"{current.version}; no download was performed."
                )
                return None
            if compare_versions(latest_overall_version, current.version) > 0:
                self.logger.log(
                    f"A newer Minecraft family ({latest_overall_version}) exists, but update only stages builds "
                    f"for the {update_basis} version {current.version}. Use an explicit download --version "
                    "command after reviewing launcher/plugin compatibility."
                )

        if latest_build.build_id > current.build:
            if not self.allow_same_version_build_upgrade:
                self.logger.log(
                    "A newer build exists for the selected version, but same-version build upgrades are disabled in config."
                )
                return None
            current_description = (
                f"{current.version} build #{current.build}"
                if current.build >= 0
                else f"{current.version} with an unknown legacy build"
            )
            self.logger.log(
                f"The {update_basis} server family is {current_description}. "
                f"Latest stable build for that version is #{latest_build.build_id}."
            )
            return UpdateSelection(
                latest_build,
                STAGE_SELECTION_LATEST_CHANNEL,
            )
        if latest_build.build_id == current.build and self.args.force:
            self.logger.log(
                f"The {update_basis} server family already has {current.version} build #{current.build}, "
                "but --force was supplied, so PaperScript will re-check that build."
            )
            return UpdateSelection(
                latest_build,
                STAGE_SELECTION_LATEST_CHANNEL,
            )
        self.logger.log(
            f"The {update_basis} server family already has {current.version} build #{current.build}. "
            "No newer stable build is available, so no download was performed."
        )
        self.logger.log("If you want to re-check this jar anyway, run one of these:")
        self.logger.log("  ./paperscript.sh --force update")
        exact_force = self.force_example_for_current(current)
        if exact_force:
            self.logger.log(f"  {exact_force}")
        return None

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

    def process_cwd(self, pid: int) -> str | None:
        result = run_command(["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"])
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
        return None

    def stage_target_path(self, build: BuildInfo) -> Path:
        if not SAFE_VERSION_PATTERN.fullmatch(build.version):
            raise PaperScriptError(f"Unsafe Minecraft version for staging: {build.version!r}")
        target_name = build.filename
        match = CURRENT_JAR_PATTERN.fullmatch(target_name)
        if (
            not match
            or match.group(1).casefold() != build.version.casefold()
            or int(match.group(2)) != build.build_id
        ):
            raise PaperScriptError(
                f"Safe staging target must be Paper-{build.version}-{build.build_id}.jar."
            )
        return self.server_dir / target_name

    @contextmanager
    def private_staging_file(
        self,
        target_path: Path,
        expected_size: int,
    ) -> Iterator[tuple[int, Path]]:
        """Create one private same-filesystem inode and only clean the same identity."""
        descriptor = -1
        temp_path: Path | None = None
        staging_identity: tuple[int, int] | None = None
        try:
            try:
                descriptor, temporary_name = tempfile.mkstemp(
                    dir=self.server_dir,
                    prefix=f".{target_path.name}.",
                    suffix=".part",
                )
                temp_path = Path(temporary_name)
                descriptor_stat = os.fstat(descriptor)
                staging_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
                os.fchmod(descriptor, 0o600)
            except OSError as error:
                raise PaperScriptError(
                    f"Staging preflight could not create a private temporary file in "
                    f"{self.server_dir}: {error}"
                ) from error

            if not stat.S_ISREG(descriptor_stat.st_mode):
                raise PaperScriptError(
                    f"Staging preflight did not create a regular temporary file in {self.server_dir}."
                )

            try:
                free_bytes = shutil.disk_usage(self.server_dir).free
            except OSError as error:
                raise PaperScriptError(
                    f"Staging preflight could not determine free disk space for {self.server_dir}: {error}"
                ) from error
            reserve_bytes = max(
                STAGING_FREE_SPACE_RESERVE_BYTES,
                math.ceil(expected_size * 0.10),
            )
            required_free_bytes = expected_size + reserve_bytes
            if free_bytes < required_free_bytes:
                raise PaperScriptError(
                    f"Staging preflight found only {format_bytes(free_bytes)} free in {self.server_dir}; "
                    f"{format_bytes(expected_size)} is needed for {target_path.name} and "
                    f"{format_bytes(reserve_bytes)} must remain free. No download was started."
                )

            fsync_directory(self.server_dir, strict=True)
            self.logger.log(
                f"Staging preflight: write access and durable directory sync confirmed; "
                f"{format_bytes(required_free_bytes)} required including reserve, "
                f"{format_bytes(free_bytes)} available."
            )
            yield descriptor, temp_path
        finally:
            active_error = sys.exc_info()[1]
            cleanup_errors: list[str] = []
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError as error:
                    cleanup_errors.append(f"could not close the temporary file: {error}")
            if temp_path is not None and os.path.lexists(temp_path):
                try:
                    cleanup_stat = temp_path.lstat()
                    if staging_identity is None or (
                        cleanup_stat.st_dev,
                        cleanup_stat.st_ino,
                    ) != staging_identity:
                        cleanup_errors.append(
                            f"refused to remove changed temporary path {temp_path}"
                        )
                    else:
                        temp_path.unlink()
                except OSError as error:
                    cleanup_errors.append(f"could not remove {temp_path}: {error}")
            if cleanup_errors:
                message = "Staging temporary-file cleanup was incomplete: " + "; ".join(cleanup_errors)
                if active_error is None:
                    raise PaperScriptError(message)
                self.logger.log(message)

    def assert_staging_path_identity(self, path: Path, descriptor: int) -> os.stat_result:
        """Require the private path to still name the inode opened during preflight."""
        try:
            path_stat = path.lstat()
            descriptor_stat = os.fstat(descriptor)
        except OSError as error:
            raise PaperScriptError(f"Staging temporary file changed or disappeared: {error}") from error
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino)
            != (descriptor_stat.st_dev, descriptor_stat.st_ino)
        ):
            raise PaperScriptError(
                "Staging temporary path no longer identifies the private regular file created during preflight."
            )
        return descriptor_stat

    def verify_existing_staged_target(
        self,
        target_path: Path,
        build: BuildInfo,
    ) -> tuple[ArtifactVerification, os.stat_result]:
        """Verify an existing target without following a symlink or trusting its name."""
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target_path, flags)
        except OSError as error:
            raise PaperScriptError(
                f"Could not safely open existing staging target {target_path}: {error}"
            ) from error
        try:
            descriptor_stat = os.fstat(descriptor)
            path_stat = target_path.lstat()
            if (
                not stat.S_ISREG(descriptor_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or (path_stat.st_dev, path_stat.st_ino)
                != (descriptor_stat.st_dev, descriptor_stat.st_ino)
            ):
                raise PaperScriptError(
                    f"Existing staging target {target_path} changed identity or is not a regular file."
                )
            verification = verify_paper_artifact_descriptor(descriptor, build)
            return verification, descriptor_stat
        finally:
            os.close(descriptor)

    def print_dry_run_summary(self, target_path: Path, current: JarInfo | None, build: BuildInfo) -> None:
        self.logger.log("Dry run: no server JAR or managed archive files were changed.")
        self.logger.log(f"Dry run: would download {build.download_url}")
        self.logger.log(f"Dry run: expected size from API is {format_bytes(build.size)}")
        self.logger.log(f"Dry run: expected SHA-256 from API is {build.sha256}")
        self.logger.log(
            "Dry run: would preflight server-root write access, durable directory sync, and free disk reserve."
        )
        self.logger.log(f"Dry run: would download to a unique temporary file in {self.server_dir}")
        self.logger.log(
            "Dry run: would verify the on-disk size, SHA-256, executable JAR manifest/class, "
            "and every ZIP entry CRC before publication."
        )
        self.logger.log(
            f"Dry run: would atomically publish {target_path.name} without overwriting an existing path."
        )
        if target_path.exists():
            self.logger.log(
                f"Dry run: {target_path.name} already exists and would only be accepted if its SHA-256 matches the API."
            )
        if current:
            self.logger.log(f"Dry run: existing jar {current.path.name} would remain unchanged.")
        if self.reconcile_server_jars_after_stage:
            self.logger.log(
                f"Dry run: after staging, PaperScript would keep up to {self.keep_server_jars} steady-state "
                "launcher/newest root jar roles plus any in-flight launcher rollback jar, and cap the "
                f"per-version jar archive at {self.keep_archived_jars}."
            )
        self.logger.log("Dry run: PaperScript would not stop, start, restart, or signal the server process.")

    def stage_build(
        self,
        build: BuildInfo,
        force_version_prompt: bool = False,
        prompt_for_forced_recheck: bool = False,
        selection_policy: str = STAGE_SELECTION_LATEST_CHANNEL,
        required_channel: str | None = None,
    ) -> None:
        if self.args.dry_run:
            build = self.revalidate_build_for_staging(
                build,
                selection_policy=selection_policy,
                required_channel=required_channel,
            )
            self._stage_build(
                build,
                force_version_prompt=force_version_prompt,
                prompt_for_forced_recheck=prompt_for_forced_recheck,
            )
            return
        with self.server_mutation_lock():
            build = self.revalidate_build_for_staging(
                build,
                selection_policy=selection_policy,
                required_channel=required_channel,
            )
            self._stage_build(
                build,
                force_version_prompt=force_version_prompt,
                prompt_for_forced_recheck=prompt_for_forced_recheck,
            )

    def revalidate_build_for_staging(
        self,
        selected: BuildInfo,
        *,
        selection_policy: str = STAGE_SELECTION_LATEST_CHANNEL,
        required_channel: str | None = None,
    ) -> BuildInfo:
        """Recompute selection policy and artifact metadata from fresh Paper API data."""
        self.stage_target_path(selected)
        channel = str(required_channel or selected.channel).upper()
        if selection_policy == STAGE_SELECTION_EXACT:
            fresh = self.api.get_build_by_id(
                selected.version,
                selected.build_id,
                use_cache=False,
            )
        elif selection_policy == STAGE_SELECTION_LATEST_CHANNEL:
            fresh = self.api.get_latest_build(
                selected.version,
                channel=channel,
                use_cache=False,
            )
        elif selection_policy == STAGE_SELECTION_LATEST_OVERALL:
            _, fresh = self.latest_version_for_channel(
                channel,
                use_cache=False,
            )
        elif selection_policy == STAGE_SELECTION_LATEST_PREVIEW:
            stable_version, _ = self.latest_stable_version(use_cache=False)
            preview = self.latest_preview_version(
                stable_version,
                use_cache=False,
            )
            fresh = preview[1] if preview is not None else None
        else:
            raise PaperScriptError(
                f"Unknown staging selection policy {selection_policy!r}; staging was refused."
            )
        if fresh is None:
            raise PaperScriptError(
                f"Paper staging selection {selected.version} build #{selected.build_id} "
                f"({selection_policy}) could not be authenticated against a fresh Paper API response; "
                "staging was refused."
            )
        if (
            selection_policy
            in {STAGE_SELECTION_LATEST_CHANNEL, STAGE_SELECTION_LATEST_OVERALL}
            and fresh.channel.casefold() != channel.casefold()
        ):
            raise PaperScriptError(
                f"Fresh Paper API selection returned channel {fresh.channel} while {channel} was required; "
                "staging was refused."
            )
        self.stage_target_path(fresh)
        require_artifact_metadata(fresh)
        if (
            fresh.version.casefold(),
            fresh.build_id,
            fresh.channel.casefold(),
        ) != (
            selected.version.casefold(),
            selected.build_id,
            selected.channel.casefold(),
        ):
            self.logger.log(
                f"Fresh Paper API selection superseded cached metadata: "
                f"{selected.version} #{selected.build_id} {selected.channel} -> "
                f"{fresh.version} #{fresh.build_id} {fresh.channel}."
            )
        return fresh

    def _stage_build(
        self,
        build: BuildInfo,
        force_version_prompt: bool = False,
        prompt_for_forced_recheck: bool = False,
    ) -> None:
        target_path = self.stage_target_path(build)
        expected_size = require_artifact_metadata(build)
        current = self.find_current_jar()
        same_family = self.managed_server_jars(build.version)
        known_same_family_builds = [jar.build for jar in same_family]
        if current is not None and current.version.casefold() == build.version.casefold():
            known_same_family_builds.append(current.build)
        newest_same_family_build = max(known_same_family_builds, default=-1)
        if build.build_id < newest_same_family_build:
            raise PaperScriptError(
                f"Refusing to stage Paper {build.version} build #{build.build_id} while newer same-family "
                f"build #{newest_same_family_build} exists. 1MB-minecraft.sh always selects the greatest "
                "numeric build, so the requested downgrade could not become the next launch."
            )
        manual_forced_recheck = False
        if current and current.version == build.version and current.build >= build.build_id and not self.args.force:
            if (
                current.build == build.build_id
                and prompt_for_forced_recheck
                and not self.args.dry_run
                and sys.stdin.isatty()
            ):
                if prompt_yes_no(
                    f"Build {current.path.name} is already staged. Re-check it against the Paper API?",
                    default=False,
                    logger=self.logger,
                ):
                    self.logger.log("Proceeding with a forced re-download of the same build.")
                    manual_forced_recheck = True
                else:
                    self.logger.log("Cancelled re-download of the same build.")
                    return
            if not manual_forced_recheck:
                self.logger.log(
                    f"Newest managed jar {current.path.name} is already build #{current.build} for version {current.version}. "
                    "Nothing newer needs to be staged."
                )
                return

        if current and current.version == build.version and current.build < build.build_id and not self.allow_same_version_build_upgrade:
            self.logger.log("Same-version build upgrades are disabled in config, so the newer build will not be staged.")
            return

        force_requested = self.args.force or manual_forced_recheck
        if current and force_requested and self.confirm_before_force_download and not self.args.yes and not self.args.dry_run and not manual_forced_recheck:
            if not prompt_yes_no(
                "Force download is enabled. Continue with the requested stage?",
                default=False,
                logger=self.logger,
            ):
                self.logger.log("Cancelled forced download.")
                return
        elif current and force_requested and self.confirm_before_force_download and self.args.dry_run and not self.args.yes:
            self.logger.log("Dry run: PaperScript would ask for confirmation before a forced download.")

        try:
            launcher_intent_version = self.launcher_jar_selection().version
        except PaperScriptError:
            launcher_intent_version = current.version if current is not None else None

        if (
            launcher_intent_version
            and compare_versions(launcher_intent_version, build.version) > 0
            and self.confirm_before_downgrade
            and not self.args.yes
            and not self.args.dry_run
        ):
            if not prompt_yes_no(
                f"Stage older Minecraft family {build.version} beside the launcher family "
                f"{launcher_intent_version}? This does not change launcher selection. Continue?",
                default=False,
                logger=self.logger,
            ):
                self.logger.log("Cancelled older-family staging.")
                return
        elif (
            launcher_intent_version
            and compare_versions(launcher_intent_version, build.version) > 0
            and self.confirm_before_downgrade
            and self.args.dry_run
            and not self.args.yes
        ):
            self.logger.log(
                f"Dry run: PaperScript would ask before staging older family {build.version} beside "
                f"launcher family {launcher_intent_version}; launcher selection would not change."
            )

        if (
            launcher_intent_version
            and compare_versions(launcher_intent_version, build.version) < 0
            and force_version_prompt
            and not self.args.yes
        ):
            if self.args.dry_run:
                self.logger.log(
                    f"Dry run: PaperScript would ask before staging newer family {build.version} beside "
                    f"launcher family {launcher_intent_version}."
                )
            elif not prompt_yes_no(
                f"Stage newer Minecraft family {build.version} beside launcher family "
                f"{launcher_intent_version}? This does not change launcher selection. Continue?",
                default=False,
                logger=self.logger,
            ):
                self.logger.log("Cancelled newer-family staging.")
                return

        if self.args.dry_run:
            self.print_dry_run_summary(target_path, current, build)
            return

        if target_path.is_symlink() or (target_path.exists() and not target_path.is_file()):
            raise PaperScriptError(f"Refusing to replace a symlink or non-file staging target: {target_path}")
        existing_identity: tuple[int, int, int, int, str] | None = None
        if target_path.exists():
            try:
                existing_verification, existing_stat = self.verify_existing_staged_target(
                    target_path,
                    build,
                )
            except PaperScriptError as error:
                raise PaperScriptError(
                    f"Target {target_path.name} already exists but failed exact Paper artifact "
                    f"validation ({error}). PaperScript will not overwrite a possibly active or "
                    "locally modified jar."
                ) from error
            if not force_requested:
                self.logger.log(
                    f"Staged target {target_path.name} already matches the Paper API size and SHA-256 "
                    "and is an intact executable JAR; left it unchanged."
                )
                self.record_state(build, target_path, existing_verification.sha256)
                self.reconcile_after_stage(build.version, target_path)
                return
            existing_identity = (
                existing_stat.st_dev,
                existing_stat.st_ino,
                existing_stat.st_size,
                existing_stat.st_mtime_ns,
                existing_verification.sha256,
            )
            self.logger.log(
                f"Force re-download will verify fresh bytes for {target_path.name} without replacing the existing file."
            )

        stage_started_at = time.monotonic()
        with self.private_staging_file(target_path, expected_size) as (descriptor, temp_path):
            self.logger.log(
                f"Downloading Paper {build.version} build #{build.build_id} "
                f"({build.channel}, {format_bytes(expected_size)})..."
            )
            try:
                download_verification = self.api.download_file(build, temp_path, descriptor)
            except PaperScriptError:
                raise
            except OSError as error:
                raise PaperScriptError(
                    f"Download I/O failed for {build.filename}: {error}"
                ) from error

            self.assert_staging_path_identity(temp_path, descriptor)
            if download_verification.bytes_written != expected_size:
                raise PaperScriptError(
                    f"Downloaded size mismatch for {build.filename}: expected {expected_size}, "
                    f"transport reported {download_verification.bytes_written} bytes."
                )
            artifact_verification = verify_paper_artifact_descriptor(descriptor, build)
            if str(download_verification.sha256).lower() != artifact_verification.sha256.lower():
                raise PaperScriptError(
                    f"Download stream SHA-256 for {build.filename} disagrees with the verified "
                    "on-disk staging inode."
                )

            try:
                os.fchmod(descriptor, 0o644)
                os.fsync(descriptor)
            except OSError as error:
                raise PaperScriptError(
                    f"Could not durably sync the verified JAR before publication: {error}"
                ) from error
            self.assert_staging_path_identity(temp_path, descriptor)
            self.logger.log(
                f"JAR validation: {artifact_verification.entry_count} files passed ZIP CRC checks; "
                f"executable Main-Class is {artifact_verification.main_class}."
            )

            if existing_identity is not None:
                try:
                    after_verification, after = self.verify_existing_staged_target(
                        target_path,
                        build,
                    )
                except PaperScriptError as error:
                    raise PaperScriptError(
                        f"Existing target {target_path.name} changed during force re-download; "
                        "left it unchanged."
                    ) from error
                after_identity = (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after_verification.sha256,
                )
                if after_identity != existing_identity:
                    raise PaperScriptError(
                        f"Existing target {target_path.name} changed during force re-download; no file was replaced."
                    )
            else:
                try:
                    os.link(temp_path, target_path, follow_symlinks=False)
                except FileExistsError as error:
                    raise PaperScriptError(
                        f"Target {target_path.name} appeared while the download was in progress; no file was overwritten."
                    ) from error
                except OSError as error:
                    raise PaperScriptError(
                        f"Could not atomically publish {target_path.name} without overwriting an existing "
                        f"path: {error}"
                    ) from error

                try:
                    published_stat = target_path.lstat()
                    staged_stat = os.fstat(descriptor)
                except OSError as error:
                    raise PaperScriptError(
                        f"Could not confirm the published identity for {target_path.name}: {error}"
                    ) from error
                if (
                    not stat.S_ISREG(published_stat.st_mode)
                    or (published_stat.st_dev, published_stat.st_ino)
                    != (staged_stat.st_dev, staged_stat.st_ino)
                ):
                    raise PaperScriptError(
                        f"Published target {target_path.name} did not retain the verified staging-file identity."
                    )
                try:
                    temp_path.unlink()
                    fsync_directory(self.server_dir, strict=True)
                except (OSError, PaperScriptError) as error:
                    raise PaperScriptError(
                        f"Verified target {target_path.name} was published, but final temporary-link "
                        f"cleanup or directory synchronization failed: {error}"
                    ) from error

        if existing_identity is None:
            self.logger.log(
                f"Staged: {target_path.name} ({build.channel}, size, SHA-256, and executable JAR verified)"
            )
        else:
            self.logger.log(
                f"Re-downloaded and verified: {target_path.name} ({build.channel}); existing staged file unchanged"
            )
        self.logger.log(f"Expected SHA-256: {build.sha256}")
        self.logger.log(f"Downloaded SHA-256: {artifact_verification.sha256}")
        self.logger.log(
            f"Download timing: {format_duration(download_verification.elapsed_seconds)} at "
            f"{format_rate(download_verification.bytes_written, download_verification.elapsed_seconds)}"
        )
        self.record_state(build, target_path, artifact_verification.sha256)
        self.reconcile_after_stage(build.version, target_path)
        self.logger.log(f"Total staging timing: {format_duration(time.monotonic() - stage_started_at)}")
        self.logger.log("PaperScript did not stop, start, restart, or signal the server process.")
        self.logger.log("Full server, world, plugin, and BlueMap backups are separate and were not run.")

    def reconcile_after_stage(self, version: str, staged_path: Path) -> None:
        if not self.reconcile_server_jars_after_stage:
            self.logger.log("Automatic server-root jar retention is disabled in config.")
            return
        try:
            plan = self.plan_server_jar_retention(
                version,
                keep=self.keep_server_jars,
                staged_path=staged_path,
            )
            archived = self.apply_server_jar_retention(plan)
        except PaperScriptError as error:
            self.logger.log(f"Automatic server-root jar retention deferred: {error}")
            return
        rollback_note = "; includes one in-flight launcher rollback jar" if plan.launch_rollback else ""
        self.logger.log(
            f"Server-root managed jars: {len(plan.kept)} protected "
            f"(steady-state limit {self.keep_server_jars}{rollback_note}); "
            f"archived {archived} older matching jar(s)."
        )

    def run_update(self) -> None:
        self.describe_server_context()
        self.log_api_activity("Contacting Paper API for the latest stable release...")
        selection = self.choose_target_for_update(use_cache=False)
        if selection is None:
            self.logger.log("Update finished with no staging changes.")
            return
        self.stage_build(
            selection.build,
            force_version_prompt=False,
            selection_policy=selection.staging_policy,
            required_channel=self.check_latest_channel_only,
        )

    def run_download(self, version: str, build_id: int | None, channel: str) -> None:
        if build_id is not None:
            selected = self.api.get_build_by_id(
                version,
                build_id,
                use_cache=False,
            )
            if not selected:
                raise PaperScriptError(
                    f"Build #{build_id} was not found for version {version}. "
                    f"Try './paperscript.sh inspect {version}' to see the available builds first."
                )
            self.stage_build(
                selected,
                force_version_prompt=True,
                prompt_for_forced_recheck=True,
                selection_policy=STAGE_SELECTION_EXACT,
            )
            return

        selected = self.api.get_latest_build(
            version,
            channel=channel,
            use_cache=False,
        )
        if not selected:
            raise PaperScriptError(
                f"No {channel.upper()} build was found for version {version}. "
                f"Try './paperscript.sh inspect {version}' to see which channels exist."
            )
        self.stage_build(
            selected,
            force_version_prompt=True,
            prompt_for_forced_recheck=True,
            selection_policy=STAGE_SELECTION_LATEST_CHANNEL,
            required_channel=channel,
        )

    def run_status(self) -> None:
        compact = self.effective_status_view() == "compact"
        properties = parse_properties(self.server_dir / "server.properties")
        current = self.find_current_jar()
        try:
            launcher_selection = self.launcher_jar_selection()
            launcher_marker_error = None
        except PaperScriptError as error:
            launcher_selection = None
            launcher_marker_error = str(error)

        if launcher_selection is not None and launcher_selection.build is not None:
            last_launched = JarInfo(
                launcher_selection.path,
                launcher_selection.version,
                launcher_selection.build,
            )
        else:
            last_launched = None

        launcher_family_jars = (
            self.managed_server_jars(launcher_selection.version)
            if launcher_selection is not None
            else []
        )
        if launcher_selection is None:
            next_launcher_status = "unknown; launcher identity unavailable"
        elif launcher_family_jars:
            next_launcher = launcher_family_jars[0]
            if next_launcher.path == launcher_selection.path:
                next_launcher_status = f"{next_launcher.path.name} (same as last selection)"
            elif launcher_selection.build is None:
                next_launcher_status = (
                    f"{next_launcher.path.name} (numeric build supersedes legacy fallback)"
                )
            else:
                next_launcher_status = f"{next_launcher.path.name} (newer than last selection)"
        else:
            next_launcher_status = f"{launcher_selection.path.name} (legacy fallback)"
        status_current = current
        if launcher_selection is not None:
            marker_state = self.jar_info_from_state(launcher_selection.path)
            status_current = (
                launcher_family_jars[0]
                if launcher_family_jars
                else marker_state
                if marker_state is not None
                else JarInfo(
                    launcher_selection.path,
                    launcher_selection.version,
                    launcher_selection.build if launcher_selection.build is not None else -1,
                )
            )
        running = self.detect_running_server_processes()
        self.log_api_activity("Contacting Paper API for current release data...")
        latest_version, latest_build = self.latest_stable_version()
        latest_preview = self.latest_preview_version(latest_version)
        tmux_available = self.tmux_session_available()
        backup_count = self.backup_file_count()
        metadata_cache_count = self.metadata_cache_file_count()
        update_relevant = status_current is None

        self.logger.kv("PaperScript version", APP_RELEASE)
        self.logger.kv("Server directory", str(self.server_dir))
        if not compact:
            self.logger.kv("Runtime directory", str(self.runtime_dir))
            self.logger.kv("Server label", str(self.server_name or "none"))
            self.logger.kv("tmux session", self.tmux_session)
            self.logger.kv("tmux session available", format_bool(tmux_available))
            self.logger.kv("Lifecycle control", "manual/external; PaperScript never stops or starts the server")
            self.logger.kv("Server properties found", format_bool((self.server_dir / "server.properties").exists()))
            self.logger.kv("Configured server port", properties.get("server-port", "25565"))
            self.logger.kv("Running server detected", format_bool(bool(running)))
            if running:
                for pid, command in running:
                    self.logger.kv(f"  PID {pid}", command)
        if current:
            state_jar = self.state.get("staged_jar") or self.state.get("current_jar")
            state_channel = (
                self.state.get("staged_channel") or self.state.get("current_channel")
            ) if state_jar == current.path.name else None
            current_details = f"{current.path.name} (version {current.version}, build #{current.build}"
            if state_channel:
                current_details += f", channel {str(state_channel).upper()}"
            current_details += ")"
            self.logger.kv(
                "Newest managed jar",
                current_details,
            )
            current_sha = sha256_file(current.path)
            if not compact:
                self.logger.kv("Newest managed jar path", str(current.path))
                self.logger.kv("Newest managed SHA-256", current_sha)
            expected_sha = self.state.get("expected_sha256")
            if expected_sha and state_jar == current.path.name and not compact:
                self.logger.kv("Expected SHA-256", str(expected_sha))
                self.logger.kv(
                    "Newest managed SHA matches expected",
                    format_bool(current_sha.lower() == str(expected_sha).lower()),
                )
        else:
            self.logger.kv("Newest managed jar", "none")
        if launcher_selection is None:
            launcher_status = "unknown; run the updated 1MB-minecraft.sh once"
            if launcher_marker_error and not compact:
                launcher_status += f" ({launcher_marker_error})"
        elif launcher_selection.build is None:
            launcher_status = (
                f"{launcher_selection.path.name} (legacy name; numeric-build retention is deferred)"
            )
        else:
            launcher_status = launcher_selection.path.name
        self.logger.kv("Last launcher-selected jar", launcher_status)
        self.logger.kv(
            "Predicted next selection (last marker family)",
            next_launcher_status,
        )

        self.logger.kv(
            f"Latest {self.check_latest_channel_only.lower()} release",
            f"{latest_version} build #{latest_build.build_id}",
        )
        family_latest_build = latest_build
        if status_current is not None and status_current.version.casefold() != latest_version.casefold():
            family_latest_build = self.api.get_latest_build(
                status_current.version,
                channel=self.check_latest_channel_only,
            )

        if status_current is None:
            self.logger.kv(
                "Update status",
                "no managed jar detected, so PaperScript would offer the latest release.",
            )
            update_relevant = True
        elif family_latest_build is None:
            self.logger.kv(
                "Update status",
                f"no {self.check_latest_channel_only.lower()} build was found for launcher family {status_current.version}.",
            )
        else:
            if status_current.build < 0:
                self.logger.kv(
                    "Update status",
                    f"launcher family {status_current.version} uses a legacy jar with an unknown build; "
                    f"update can stage stable build #{family_latest_build.build_id}.",
                )
                update_relevant = True
            elif family_latest_build.build_id > status_current.build:
                self.logger.kv(
                    "Update status",
                    f"newer stable build available for launcher family {status_current.version} "
                    f"({status_current.build} -> {family_latest_build.build_id}).",
                )
                update_relevant = True
            elif family_latest_build.build_id == status_current.build:
                self.logger.kv("Update status", "latest stable build for the launcher family is already staged.")
            else:
                self.logger.kv(
                    "Update status",
                    "newest managed build for the launcher family is newer than the latest stable build this script found.",
                )

        if status_current is not None and compare_versions(latest_version, status_current.version) > 0:
            self.logger.kv(
                "Newer Minecraft family",
                f"{latest_version} build #{latest_build.build_id}; update remains on {status_current.version}.",
            )
            self.log_command_hint(
                f"After reviewing launcher/plugin compatibility, stage it explicitly with "
                f"'./paperscript.sh download --version {latest_version} --channel STABLE'."
            )
            update_relevant = True
        self.log_command_hint(
            "Use './paperscript.sh stable' to inspect the latest stable release, './paperscript.sh update' to stage "
            "the newest stable build for the launcher family, or './paperscript.sh --force update' to re-check it."
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
        if latest_preview is None:
            self.logger.kv(
                "Latest preview release",
                f"none newer than stable ({latest_version} build #{latest_build.build_id})",
            )
            self.log_command_hint(
                f"Use './paperscript.sh inspect {latest_version}' to browse older beta or alpha channels for the current stable line."
            )
        else:
            latest_preview_version, latest_preview_build = latest_preview
            self.logger.kv(
                "Latest preview release",
                f"{latest_preview_version} build #{latest_preview_build.build_id} ({latest_preview_build.channel})",
            )
            self.log_command_hint(
                "Use './paperscript.sh experimental' to inspect it, or './paperscript.sh experimental --download' to stage it."
            )
        self.logger.kv(
            "Server-root jar retention",
            f"steady-state limit {self.keep_server_jars} for last-launched/newest roles; "
            "one in-flight launcher rollback may be added; reconcile after stage "
            f"{format_bool(self.reconcile_server_jars_after_stage)}",
        )
        if last_launched:
            self.logger.kv(
                f"Jar archive ({last_launched.version})",
                f"{self.managed_jar_archive_count(last_launched.version)}/{self.keep_archived_jars} file(s)",
            )
        self.logger.kv("Legacy backups found", f"{backup_count} file(s)")
        self.logger.kv(
            "Metadata cache",
            f"{'enabled' if self.metadata_cache_enabled else 'disabled'}, "
            f"ttl {self.metadata_cache_ttl_seconds}s, files {metadata_cache_count}",
        )
        if backup_count > 0:
            if self.keep_backups >= 0 and backup_count > self.keep_backups:
                self.log_command_hint(
                    f"Run './paperscript.sh cleanup --backups --keep {self.keep_backups}' to trim older legacy backups, "
                    "or './paperscript.sh cleanup --backups' to delete those legacy items.",
                    important=True,
                )
            elif not compact:
                self.log_command_hint(
                    "Run './paperscript.sh cleanup --backups' to delete legacy backup items; the managed JAR archive is preserved."
                )
        if metadata_cache_count > 0 and not compact:
            self.log_command_hint(
                "Run './paperscript.sh cleanup --metadata-cache' to clear cached Paper API metadata, "
                "or './paperscript.sh --no-metadata-cache ...' to bypass it for one run."
            )

    def run_stable(self, download: bool = False) -> None:
        self.log_api_activity("Contacting Paper API for the latest stable release...")
        version, build = self.latest_stable_version(use_cache=not download)
        self.logger.log(f"Latest stable release overall: {version} build #{build.build_id} ({format_bytes(build.size)})")
        self.logger.log(f"Download URL: {build.download_url}")
        if build.sha256:
            self.logger.log(f"Expected SHA-256: {build.sha256}")
        self.log_command_hint(
            f"Exact manual command: ./paperscript.sh download --version {version} --channel {self.check_latest_channel_only}"
        )
        self.log_release_page(relevant=True)
        if download:
            self.stage_build(
                build,
                force_version_prompt=True,
                prompt_for_forced_recheck=True,
                selection_policy=STAGE_SELECTION_LATEST_OVERALL,
                required_channel=self.check_latest_channel_only,
            )

    def run_experimental(self, download: bool = False) -> None:
        self.log_api_activity("Contacting Paper API for the latest preview release newer than stable...")
        stable_version, stable_build = self.latest_stable_version(use_cache=not download)
        preview = self.latest_preview_version(
            stable_version,
            use_cache=not download,
        )
        if preview is None:
            self.logger.log(
                f"No preview release newer than the current stable release was found. Stable is {stable_version} build #{stable_build.build_id}."
            )
            self.log_command_hint(
                f"Use './paperscript.sh stable' for the current mainline release, or './paperscript.sh inspect {stable_version}' to browse older beta or alpha builds on that stable line."
            )
            return

        version, build = preview
        self.logger.log(
            f"Latest preview release newer than stable: {version} build #{build.build_id} ({build.channel}, {format_bytes(build.size)})"
        )
        self.logger.log(f"Download URL: {build.download_url}")
        if build.sha256:
            self.logger.log(f"Expected SHA-256: {build.sha256}")
        self.log_command_hint(
            f"Exact manual command: ./paperscript.sh download --version {version} --channel {build.channel}",
            important=True,
        )
        self.log_release_page(relevant=True)
        if download:
            self.stage_build(
                build,
                force_version_prompt=True,
                prompt_for_forced_recheck=True,
                selection_policy=STAGE_SELECTION_LATEST_PREVIEW,
            )

    def open_verify_target(self, path: Path) -> tuple[int, os.stat_result]:
        """Open the verify target without following a symlink and bind it to its path."""
        flags = os.O_RDONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise PaperScriptError(
                f"Could not safely open verify target {path}: {error}"
            ) from error
        try:
            descriptor_stat = os.fstat(descriptor)
            path_stat = path.lstat()
            if (
                not stat.S_ISREG(descriptor_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or (path_stat.st_dev, path_stat.st_ino)
                != (descriptor_stat.st_dev, descriptor_stat.st_ino)
            ):
                raise PaperScriptError(
                    f"Verify target {path} changed identity or is not a regular non-symlink file."
                )
            return descriptor, descriptor_stat
        except BaseException:
            os.close(descriptor)
            raise

    def assert_verify_target_unchanged(
        self,
        path: Path,
        descriptor: int,
        expected: os.stat_result,
    ) -> None:
        """Require the path and opened verify inode to remain stable through the check."""
        try:
            descriptor_stat = os.fstat(descriptor)
            path_stat = path.lstat()
        except OSError as error:
            raise PaperScriptError(
                f"Verify target {path} changed identity or disappeared during verification: {error}"
            ) from error
        expected_identity = (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_mtime_ns,
            expected.st_ctime_ns,
        )
        descriptor_identity = (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
            descriptor_stat.st_size,
            descriptor_stat.st_mtime_ns,
            descriptor_stat.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino)
            != (descriptor_stat.st_dev, descriptor_stat.st_ino)
            or descriptor_identity != expected_identity
        ):
            raise PaperScriptError(
                f"Verify target {path} changed identity or contents during verification."
            )

    def run_verify(self) -> None:
        with self.server_mutation_lock():
            self._run_verify_locked()

    def _run_verify_locked(self) -> None:
        current = self.find_current_jar()
        if not current:
            raise PaperScriptError("No managed Paper jar was detected to verify.")

        descriptor, target_identity = self.open_verify_target(current.path)
        try:
            current_sha = sha256_descriptor(descriptor)
            self.assert_verify_target_unchanged(
                current.path,
                descriptor,
                target_identity,
            )
            self.logger.log(
                f"Verify target: {current.path.name} (version {current.version}, build #{current.build})"
            )
            self.logger.log(f"Newest managed SHA-256: {current_sha}")

            state_jar = self.state.get("staged_jar") or self.state.get("current_jar")
            state_channel = self.state.get("staged_channel") or self.state.get("current_channel")
            state_expected = self.state.get("expected_sha256")
            state_current = self.state.get("staged_sha256")
            if state_current is None or state_current == "":
                state_current = self.state.get("current_sha256")
            if state_jar == current.path.name:
                if state_channel:
                    self.logger.log(f"Recorded staging channel: {str(state_channel).upper()}")
                if not state_current and not state_expected:
                    self.logger.log(
                        "Recorded staging state exists for this jar, but it does not contain stored SHA-256 values yet."
                    )
                state_checks = (
                    (
                        "staging",
                        "Recorded SHA-256 from staging time",
                        "Newest managed SHA-256 matches recorded staging SHA",
                        state_current,
                    ),
                    (
                        "expected",
                        "Recorded expected SHA-256",
                        "Newest managed SHA-256 matches recorded expected SHA",
                        state_expected,
                    ),
                )
                for state_label, value_label, match_label, recorded_value in state_checks:
                    if recorded_value is None or recorded_value == "":
                        continue
                    recorded_sha = normalized_sha256(recorded_value)
                    if recorded_sha is None:
                        raise PaperScriptError(
                            f"Verification failed: recorded {state_label} SHA-256 is invalid for {current.path.name}."
                        )
                    self.logger.log(f"{value_label}: {recorded_sha}")
                    state_matches = secrets.compare_digest(current_sha, recorded_sha)
                    self.logger.log(f"{match_label}: {format_bool(state_matches)}")
                    if not state_matches:
                        raise PaperScriptError(
                            f"Verification failed: {current.path.name} does not match the recorded "
                            f"{state_label} SHA-256."
                        )
            else:
                self.logger.log(
                    "Recorded staging state does not match the currently detected jar, so local state comparison is unavailable."
                )

            try:
                api_build = self.api.get_build_by_id(
                    current.version,
                    current.build,
                    use_cache=False,
                )
            except PaperScriptError as error:
                raise PaperScriptError(
                    f"Fresh API checksum lookup failed for {current.path.name}: {error}"
                ) from error

            if api_build is None:
                raise PaperScriptError(
                    f"Verification failed: exact build {current.version} #{current.build} "
                    "was not found by the fresh Paper API lookup."
                )

            api_sha = normalized_sha256(api_build.sha256)
            if api_sha is None:
                raise PaperScriptError(
                    f"Verification failed: the fresh Paper API did not provide a valid SHA-256 "
                    f"for {current.path.name}."
                )

            self.logger.log(f"API channel: {api_build.channel}")
            self.logger.log(f"API download URL: {api_build.download_url}")
            self.logger.log(f"API expected SHA-256: {api_sha}")
            api_matches = secrets.compare_digest(current_sha, api_sha)
            self.logger.log(
                f"Newest managed SHA-256 matches API expected SHA: {format_bool(api_matches)}"
            )
            if not api_matches:
                raise PaperScriptError(
                    f"Verification failed: {current.path.name} does not match the fresh Paper API SHA-256."
                )

            self.assert_verify_target_unchanged(
                current.path,
                descriptor,
                target_identity,
            )
            final_current = self.find_current_jar()
            if (
                final_current is None
                or final_current.path != current.path
                or final_current.version != current.version
                or final_current.build != current.build
            ):
                final_name = final_current.path.name if final_current is not None else "none"
                raise PaperScriptError(
                    f"Verification failed: {current.path.name} is no longer the newest managed jar "
                    f"(current selection: {final_name})."
                )
            self.assert_verify_target_unchanged(
                current.path,
                descriptor,
                target_identity,
            )
            self.logger.log(
                "Verification succeeded: newest managed jar matches the fresh Paper API SHA-256."
            )
        finally:
            os.close(descriptor)

    def cleanup_selection(self) -> dict[str, bool]:
        selected = {
            "all": bool(getattr(self.args, "cleanup_all", False)),
            "downloads": bool(getattr(self.args, "cleanup_downloads", False)),
            "backups": bool(getattr(self.args, "cleanup_backups", False)),
            "server_jars": bool(getattr(self.args, "cleanup_server_jars", False)),
            "metadata_cache": bool(getattr(self.args, "cleanup_metadata_cache", False)),
            "pycache": bool(getattr(self.args, "cleanup_pycache", False)),
            "logs": bool(getattr(self.args, "cleanup_logs", False)),
            "json": bool(getattr(self.args, "cleanup_json", False)),
        }
        if selected["all"]:
            for key in ["downloads", "backups", "metadata_cache", "pycache", "logs", "json"]:
                selected[key] = True
        if selected["backups"] and selected["server_jars"]:
            raise PaperScriptError(
                "--backups/--all cannot be combined with --server-jars; clean them in separate commands."
            )
        if getattr(self.args, "cleanup_version", None) is not None and not selected["server_jars"]:
            raise PaperScriptError("cleanup --version requires the explicit --server-jars target.")
        if getattr(self.args, "cleanup_keep", None) is not None:
            if not selected["server_jars"]:
                selected["backups"] = True
        if not any(selected.values()):
            selected["downloads"] = True
            selected["pycache"] = True
        return selected

    def cleanup_descriptions(self, selection: dict[str, bool]) -> list[str]:
        descriptions: list[str] = []
        if selection["downloads"]:
            descriptions.append(f"Delete old download-workspace items in {self.downloads_dir}")
        if selection["backups"]:
            keep = getattr(self.args, "cleanup_keep", None)
            if keep is None:
                descriptions.append(
                    f"Delete legacy top-level backup items in {self.backups_dir} while preserving {self.jar_archive_dir}"
                )
            else:
                descriptions.append(
                    f"Trim legacy top-level backup items in {self.backups_dir} so only the newest {keep} remain"
                )
        if selection["server_jars"]:
            keep = getattr(self.args, "cleanup_keep", None)
            keep_count = self.keep_server_jars if keep is None else int(keep)
            requested_version = getattr(self.args, "cleanup_version", None)
            version_label = requested_version or "the version in the launcher marker"
            descriptions.append(
                f"Keep the last-launched jar plus the greatest numeric next-start jar (steady-state limit "
                f"{keep_count}) and any in-flight launcher rollback jar for {version_label}; archive older "
                f"exact numeric builds under {self.jar_archive_dir}"
            )
        if selection["metadata_cache"]:
            descriptions.append(f"Delete cached Paper API metadata in {self.metadata_cache_dir}")
        if selection["pycache"]:
            descriptions.append(f"Delete Python __pycache__ folders under {self.runtime_dir}")
        if selection["logs"]:
            descriptions.append(f"Clear the log file at {self.log_path}")
        if selection["json"]:
            descriptions.append(
                f"Delete {self.config_path.name} and {self.state_path.name} so the next run starts fresh"
            )
        return descriptions

    def remove_directory_contents(
        self,
        path: Path,
        excluded_paths: tuple[Path, ...] = (),
    ) -> int:
        if not path.exists():
            return 0
        excluded = set(excluded_paths)
        removed = 0
        for child in path.iterdir():
            if child in excluded:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        return removed

    def find_pycache_dirs(self) -> list[Path]:
        return [path for path in self.runtime_dir.rglob("__pycache__") if path.is_dir()]

    def backup_file_count(self) -> int:
        if not self.backups_dir.exists():
            return 0
        return sum(1 for path in self.backups_dir.iterdir() if path.is_file())

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
            if isinstance(message, StyledConsoleText):
                print(str(message))
            else:
                print(terminal_safe_text(message))

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

        server_jar_plan: JarRetentionPlan | None = None
        if selection["server_jars"]:
            marker_jar = self.last_launched_jar()
            requested_version = getattr(self.args, "cleanup_version", None)
            version = str(requested_version or marker_jar.version)
            keep = getattr(self.args, "cleanup_keep", None)
            keep_count = self.keep_server_jars if keep is None else int(keep)
            staged_path = self.recorded_staged_jar(version)
            server_jar_plan = self.plan_server_jar_retention(
                version,
                keep=keep_count,
                staged_path=staged_path,
            )
            self.logger.log(
                f"Protected last-launched jar: {server_jar_plan.last_launched.name}"
            )
            if server_jar_plan.launch_rollback is not None:
                self.logger.log(
                    f"Protected in-flight launcher rollback jar: {server_jar_plan.launch_rollback.name}"
                )
            for path in server_jar_plan.kept:
                if path not in {
                    server_jar_plan.last_launched,
                    server_jar_plan.launch_rollback,
                }:
                    self.logger.log(f"Protected staged/newest jar: {path.name}")
            if not server_jar_plan.to_archive:
                self.logger.log("No older matching server-root jars need to be archived.")
            for path in server_jar_plan.to_archive:
                self.logger.log(f"Will archive older server-root jar: {path.name}")
            for path in server_jar_plan.to_prune:
                self.logger.log(
                    f"Will permanently prune archived Paper jar beyond the configured cap: {path}"
                )

        if self.args.dry_run:
            if server_jar_plan is not None:
                self.apply_server_jar_retention(server_jar_plan, dry_run=True)
            self.logger.log("Dry run: no files were deleted.")
            return

        if not self.args.yes:
            if not prompt_yes_no("Proceed with cleanup?", default=False, logger=self.logger):
                self.logger.log("Cancelled cleanup.")
                return

        removed_downloads = 0
        removed_backups = 0
        archived_server_jars = 0
        removed_metadata_cache = 0
        removed_pycache = 0
        cleared_logs = False
        removed_json = 0

        if selection["server_jars"]:
            marker_jar = self.last_launched_jar()
            requested_version = getattr(self.args, "cleanup_version", None)
            version = str(requested_version or marker_jar.version)
            keep = getattr(self.args, "cleanup_keep", None)
            keep_count = self.keep_server_jars if keep is None else int(keep)
            with self.server_mutation_lock():
                self.state = self._load_json(self.state_path)
                staged_path = self.recorded_staged_jar(version)
                locked_plan = self.plan_server_jar_retention(
                    version,
                    keep=keep_count,
                    staged_path=staged_path,
                )
                if locked_plan != server_jar_plan:
                    raise PaperScriptError(
                        "Server-root jars or launcher/staging identity changed after the cleanup plan was shown. "
                        "Nothing was archived; re-run cleanup to review the new plan."
                    )
                archived_server_jars = self.apply_server_jar_retention(locked_plan)
            self.logger.log(
                f"Archived {archived_server_jars} older matching server-root jar(s); "
                f"kept {len(locked_plan.kept)} protected jar(s) in {self.server_dir}"
            )

        if selection["downloads"]:
            removed_downloads = self.remove_directory_contents(self.downloads_dir)
            self.logger.log(f"Removed {removed_downloads} item(s) from {self.downloads_dir}")

        if selection["backups"]:
            if getattr(self.args, "cleanup_keep", None) is None:
                removed_backups = self.remove_directory_contents(
                    self.backups_dir,
                    excluded_paths=(self.jar_archive_dir,),
                )
                self.logger.log(
                    f"Removed {removed_backups} legacy backup item(s) from {self.backups_dir}; "
                    f"managed jar archive {self.jar_archive_dir} was preserved"
                )
            else:
                removed_backups = self.trim_backups_to_keep(int(self.args.cleanup_keep))
                self.logger.log(
                    f"Removed {removed_backups} old backup item(s) from {self.backups_dir} and kept the newest {self.args.cleanup_keep}"
                )

        if selection["metadata_cache"]:
            removed_metadata_cache = self.remove_directory_contents(self.metadata_cache_dir)
            self.logger.log(f"Removed {removed_metadata_cache} item(s) from {self.metadata_cache_dir}")

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
            f"server_jars_archived={archived_server_jars}, "
            f"metadata_cache={removed_metadata_cache}, "
            f"pycache={removed_pycache}, "
            f"json={removed_json}, "
            f"logs={'cleared' if cleared_logs else 'unchanged'}"
        )
        if cleared_logs:
            self.console_only(summary)
        else:
            self.logger.log(summary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperscript.sh",
        description="Manually stage verified, versioned Paper server jars through the Fill v3 API.",
        epilog=(
            f"Project and examples: {PROJECT_URL}\n"
            "Use 'update' for the latest stable build in the launcher-selected family, or "
            "'download --version ... --build ...' for an exact jar."
        ),
    )
    parser.add_argument("--server-dir", help="Target server directory. Defaults to the current directory.")
    parser.add_argument(
        "--user-agent",
        help="Custom User-Agent header to send to the PaperMC API. Defaults to the built-in PaperScript identity.",
    )
    parser.add_argument(
        "--tmux-session",
        help="tmux session name to show in read-only status. Defaults to config, PAPERSCRIPT_TMUX_SESSION, or mcserver.",
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
        help="Allow the same build through selection again; existing jar targets are never overwritten.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what PaperScript would do without downloading, moving, or pruning files.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress normal console output; logs still go to logs.log.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors in terminal output.",
    )
    parser.add_argument(
        "--debug-http",
        action="store_true",
        help="Log HTTP request attempts and retries for Paper API troubleshooting.",
    )
    parser.add_argument(
        "--no-metadata-cache",
        action="store_true",
        help="Bypass the local Paper API metadata cache for this run.",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("update", help="Stage the latest stable build for the launcher-selected Paper family.")
    status_parser = subparsers.add_parser(
        "status",
        help="Show launcher, staged-JAR, retention, and release state.",
    )
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
    subparsers.add_parser(
        "verify",
        help=(
            "Fail closed unless the newest managed jar matches its recorded digests and "
            "a fresh exact-build Paper API SHA-256."
        ),
    )
    stable_parser = subparsers.add_parser(
        "stable",
        help="Show and optionally stage the latest stable Paper release overall.",
    )
    stable_parser.add_argument(
        "--download",
        action="store_true",
        help="Download, verify, and stage the latest stable release overall.",
    )
    experimental_parser = subparsers.add_parser(
        "experimental",
        help="Show and optionally stage the latest preview release newer than stable.",
    )
    experimental_parser.add_argument(
        "--download",
        action="store_true",
        help="Download, verify, and stage the latest preview release that is newer than the current stable release.",
    )
    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Clean selected runtime files or explicitly archive older server-root Paper JARs.",
    )
    cleanup_parser.add_argument(
        "--all",
        dest="cleanup_all",
        action="store_true",
        help=(
            "Clean downloads, legacy backups, metadata cache, __pycache__, logs, and JSON state/config. "
            "The managed JAR archive and server-root JARs are preserved."
        ),
    )
    cleanup_parser.add_argument(
        "--downloads",
        dest="cleanup_downloads",
        action="store_true",
        help="Delete old workspace and temporary files in downloads/.",
    )
    cleanup_parser.add_argument(
        "--backups",
        dest="cleanup_backups",
        action="store_true",
        help="Delete or trim legacy top-level backups while preserving the managed backups/jars/ archive.",
    )
    cleanup_parser.add_argument(
        "--server-jars",
        dest="cleanup_server_jars",
        action="store_true",
        help=(
            "Archive older exact Paper-<version>-<build>.jar files from the server root while "
            "protecting the launcher-marked jar, greatest numeric next-start jar, and any in-flight "
            "launcher rollback jar. Never implied by --all."
        ),
    )
    cleanup_parser.add_argument(
        "--metadata-cache",
        dest="cleanup_metadata_cache",
        action="store_true",
        help="Delete cached Paper API metadata in cache/.",
    )
    cleanup_parser.add_argument(
        "--keep",
        dest="cleanup_keep",
        type=int,
        default=None,
        help=(
            "With --server-jars, keep a steady-state limit of N matching root jars (minimum 2); "
            "an in-flight launcher rollback may temporarily add one. "
            "Otherwise keep the newest N backup files."
        ),
    )
    cleanup_parser.add_argument(
        "--version",
        dest="cleanup_version",
        default=None,
        help="With --server-jars, manage only this Minecraft version; defaults to the launcher marker version.",
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

    list_parser = subparsers.add_parser("list-versions", help="List versions available from the API.")
    list_parser.add_argument(
        "--channels",
        action="store_true",
        help="Also show the newest build per channel for each version.",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit how many versions are listed. Useful with --channels when debugging API throttling.",
    )
    list_parser.add_argument(
        "--channel-delay-ms",
        type=int,
        default=None,
        help="Pause this many milliseconds between per-version channel lookups. Useful with --channels.",
    )

    inspect_parser = subparsers.add_parser("inspect", help="Show the latest builds for one version.")
    inspect_parser.add_argument("version", help="Minecraft version to inspect, for example 26.2 or 1.20.4")

    subparsers.add_parser("explore", help="Interactively pick a version, inspect it, and optionally stage it.")
    init_parser = subparsers.add_parser(
        "init",
        help="Create or repair the PaperScript runtime files in paperscript/ with confirmation.",
    )

    download_parser = subparsers.add_parser(
        "download",
        help="Download, verify, and stage a chosen version or exact build in the server root.",
    )
    download_parser.add_argument("--version", required=True, help="Minecraft version to stage.")
    download_parser.add_argument("--build", type=int, help="Exact build number to stage.")
    download_parser.add_argument(
        "--channel",
        default=None,
        choices=["ALPHA", "BETA", "STABLE", "RECOMMENDED", "alpha", "beta", "stable", "recommended"],
        help="Build channel to use when --build is omitted. Default: config value or STABLE.",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args(normalize_global_options(sys.argv[1:]))

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
            app.list_versions(
                show_channels=args.channels,
                limit=args.limit,
                channel_delay_ms=args.channel_delay_ms,
            )
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
                terminal_safe_text(f"Error: {error}"),
                ANSI_RED,
                supports_color(sys.stderr, no_color=bool(getattr(args, "no_color", False))),
                bold=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
