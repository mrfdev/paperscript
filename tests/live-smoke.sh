#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SMOKE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/paperscript-live-smoke.XXXXXX")"

cleanup() {
  if [[ "${PAPERSCRIPT_SMOKE_KEEP:-0}" == "1" ]]; then
    printf 'Preserved smoke-test directory: %s\n' "$SMOKE_ROOT"
  else
    rm -rf -- "$SMOKE_ROOT"
  fi
}
trap cleanup EXIT

mkdir -p "$SMOKE_ROOT/paperscript"
cp "$PROJECT_ROOT/paperscript.sh" "$SMOKE_ROOT/paperscript.sh"
cp "$PROJECT_ROOT/paperscript/paperscript.py" "$SMOKE_ROOT/paperscript/paperscript.py"
cp "$PROJECT_ROOT/paperscript/config.example.json" "$SMOKE_ROOT/paperscript/config.example.json"
cp "$PROJECT_ROOT/paperscript/LICENSE" "$SMOKE_ROOT/paperscript/LICENSE"
cp "$PROJECT_ROOT/paperscript/todo.log" "$SMOKE_ROOT/paperscript/todo.log"
chmod +x "$SMOKE_ROOT/paperscript.sh" "$SMOKE_ROOT/paperscript/paperscript.py"

python3 - "$SMOKE_ROOT/paperscript/config.example.json" "$SMOKE_ROOT/paperscript/config.json" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
config = json.loads(source.read_text(encoding="utf-8"))
config.update(
    {
        "server_name": "PaperScript disposable live smoke test",
        "tmux_session": "paperscript-live-smoke",
        "default_channel": "STABLE",
        "check_latest_channel_only": "STABLE",
        "allow_cross_version_auto_upgrade": False,
        "allow_same_version_build_upgrade": True,
        "download_filename_pattern": "Paper-{version}.jar",
        "running_server_action": "ask",
        "no_color": True,
    }
)
destination.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

printf 'server-port=65534\n' > "$SMOKE_ROOT/server.properties"

printf 'Disposable server directory: %s\n' "$SMOKE_ROOT"
(
  cd "$SMOKE_ROOT"
  ./paperscript.sh --yes --no-color update
  ./paperscript.sh --no-color status --full > status.txt
  ./paperscript.sh --no-color verify > verify.txt
)

python3 - "$SMOKE_ROOT" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
runtime = root / "paperscript"
config = json.loads((runtime / "config.json").read_text(encoding="utf-8"))
state = json.loads((runtime / "state.json").read_text(encoding="utf-8"))
jar_path = root / state["current_jar"]
version_numbers = tuple(int(part) for part in state["current_version"].split("."))

assert config["default_channel"] == "STABLE"
assert config["check_latest_channel_only"] == "STABLE"
assert config["allow_same_version_build_upgrade"] is True
assert config["download_filename_pattern"] == "Paper-{version}.jar"
assert state["current_channel"] == "STABLE"
assert version_numbers >= (26, 2)
assert state["current_jar"] == f"Paper-{state['current_version']}.jar"
assert jar_path.is_file()

digest = hashlib.sha256(jar_path.read_bytes()).hexdigest()
assert digest == state["current_sha256"]
assert digest == state["expected_sha256"]

status = (root / "status.txt").read_text(encoding="utf-8")
verify = (root / "verify.txt").read_text(encoding="utf-8")
assert f"version {state['current_version']}, build #{state['current_build']}" in status
assert "channel STABLE" in status
assert str(jar_path) in status
assert "already on the latest stable build" in status
assert "Recorded install channel: STABLE" in verify
assert "API channel: STABLE" in verify
assert "Current SHA-256 matches API expected SHA: yes" in verify

print(
    "Live smoke passed: "
    f"Paper {state['current_version']} build #{state['current_build']} "
    f"({state['current_channel']}), {jar_path}, SHA-256 {digest}"
)
PY
