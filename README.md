# PaperScript

PaperScript is a dependency-free updater for Paper servers that uses the current PaperMC Fill v3 downloads service.

It is designed for the layout where your server root stays readable, while PaperScript keeps its own files in a visible `paperscript/` directory:

```text
/anydirectory/
/anydirectory/server.properties
/anydirectory/Paper-1.21.11-130.jar
/anydirectory/paperscript.sh
/anydirectory/paperscript/
/anydirectory/paperscript/paperscript.py
/anydirectory/paperscript/config.example.json
/anydirectory/paperscript/config.json
/anydirectory/paperscript/state.json
/anydirectory/paperscript/downloads/
/anydirectory/paperscript/backups/
/anydirectory/paperscript/logs.log
/anydirectory/paperscript/todo.log
```

This makes it easy to `git pull`, wipe PaperScript runtime files, or move between macOS and Ubuntu without flooding the server root with updater clutter.

## Why Python Instead Of Bash

Python is the best fit here for this project:

- It works well on modern macOS and Ubuntu without needing `jq`, npm packages, or extra shell tooling.
- JSON, version sorting, checksums, prompts, logging, and future expansion are much easier to keep readable.
- The runtime stays small: one Python script plus one tiny shell launcher.
- It is much easier to maintain than a large Bash script once features like status views, integrity checks, cleanup, and per-server config exist.

Bash still has a place here, which is why the launcher remains a simple `paperscript.sh`.

## Features

- Uses the PaperMC Fill v3 API.
- Sends a custom User-Agent by default:
  `mrfloris-PaperScript/2.0 (https://github.com/mrfdev/PaperScript)`
- Finds the latest stable Paper version and latest stable build automatically.
- Can inspect and install the latest experimental release overall.
- Only auto-updates when the stable build is newer for the same version.
- Prompts before cross-version upgrades.
- Prompts before downgrades.
- Supports forced re-download of the same build with `--force`.
- Verifies downloads against the API-provided SHA-256.
- Stores the installed SHA-256 in `state.json` for later `verify` checks.
- Backs up the current jar before replacing it.
- Keeps only the newest 10 backups by default.
- Can trim backups to a chosen count with `cleanup --backups --keep N`.
- Detects likely running servers using `server-port`, process checks, and tmux-aware graceful stop attempts.
- Uses tmux by preference for graceful stop and falls back to `SIGTERM`.
- Supports `--dry-run`, `--quiet`, `--no-color`, and per-server config defaults.
- Supports color themes and a compact or full status view.
- Keeps updater runtime files isolated inside `paperscript/`.

## Requirements

- Python `3.9+`
- `python3` available on your path
- `tmux` if you want graceful stop support

No third-party Python packages are required.

## Setup

Clone the repo into the server directory or a test directory:

```bash
git clone https://github.com/mrfdev/PaperScript.git
cd PaperScript
chmod +x paperscript.sh paperscript/paperscript.py
```

If you want to initialize or repair the runtime files manually:

```bash
./paperscript.sh init
```

That creates or repairs the local runtime pieces in `paperscript/` and asks for confirmation before it changes anything.

## Quick Start

Show the current state:

```bash
./paperscript.sh status
```

Install the latest stable Paper release when appropriate:

```bash
./paperscript.sh update
```

Force a re-download of the current latest stable build:

```bash
./paperscript.sh --force update
```

Inspect the latest experimental release:

```bash
./paperscript.sh experimental
```

Download that experimental build:

```bash
./paperscript.sh experimental --download
```

## Commands

### `update`

Checks the server directory, detects the latest stable Paper release, and installs it when appropriate.

Behavior:

- If no current `Paper-<version>-<build>.jar` exists, it offers the latest stable build.
- If your current version matches the newest stable version, it only downloads when the build number is newer.
- If your current version and build already match the newest stable release, `--force update` re-downloads it.
- If the newest stable version is a newer Minecraft version, PaperScript asks before upgrading.
- If `server.properties` exists and a likely matching Java process is running, PaperScript asks how to proceed.
- If `--dry-run` is used, it reports what it would do without changing files or stopping anything.

Examples:

```bash
./paperscript.sh
./paperscript.sh update
./paperscript.sh --force update
./paperscript.sh update --dry-run
./paperscript.sh --yes --quiet update
./paperscript.sh --no-color update
./paperscript.sh --server-dir /srv/mc/live update
```

### `status`

Shows the current PaperScript and Paper server state, the newest stable release, and the newest experimental release overall.

The normal full view can include:

- PaperScript release
- server directory and runtime directory
- server label
- tmux session name and whether it currently exists
- graceful stop command
- server properties detection
- configured server port
- running server detection
- current jar, version, build, and SHA-256
- stored expected SHA-256 from the last PaperScript install
- newest stable release
- update status
- newest channels for the current stable version
- newest experimental release overall
- backup retention settings

Status views:

- `./paperscript.sh status`
  Full status view
- `./paperscript.sh status --compact`
  Shorter overview
- `./paperscript.sh status --full`
  Force full mode even if config defaults to compact

Examples:

```bash
./paperscript.sh status
./paperscript.sh status --compact
./paperscript.sh --server-dir /srv/mc/live status
```

### `stable`

Shows the latest stable Paper release overall and can install it directly.

This is useful when you want a clear stable overview without running a full update flow first.

Examples:

```bash
./paperscript.sh stable
./paperscript.sh stable --download
./paperscript.sh --force stable --download
```

### `experimental`

Shows the latest experimental Paper release overall and can install it directly.

This is different from the `ALPHA` line shown under the latest stable version in `status`.

For example:

- `Latest channels for stable version 1.21.11` means the channels that exist for `1.21.11`
- `Latest experimental release overall` may instead be something newer like `26.1.2 build #7`

Examples:

```bash
./paperscript.sh experimental
./paperscript.sh experimental --download
./paperscript.sh --yes experimental --download
```

### `verify`

Hashes the current installed jar and compares it against:

- the SHA-256 recorded in `state.json` during the last PaperScript install
- the expected SHA-256 from the live Paper API for that exact version and build

Examples:

```bash
./paperscript.sh verify
./paperscript.sh --server-dir /srv/mc/live verify
```

### `list-versions`

Lists every Paper version the API currently exposes.

Examples:

```bash
./paperscript.sh list-versions
./paperscript.sh list-versions --channels
```

With `--channels`, PaperScript also shows the newest build it can find per channel for each version.

This is useful for questions like:

- which versions exist at all
- whether `26.2.x` only exists as alpha or beta
- whether `26.1.2` is the newest stable or experimental family
- whether older versions such as `1.20.4` or `1.19.2` still have builds available

### `inspect VERSION`

Shows the newest available build per channel for one specific version, then offers to download one interactively.

If the selected build is already installed, PaperScript can offer a direct `Download it anyway?` confirmation so you can re-download the same jar without leaving the inspect flow.

Examples:

```bash
./paperscript.sh inspect 1.20.4
./paperscript.sh inspect 1.19.2
./paperscript.sh inspect 26.1.2
```

### `explore`

Interactive version picker. It lists all available versions, lets you choose one by number, shows the newest builds for that version, and can then download it.

If the build you choose is already installed, `explore` can offer the same `Download it anyway?` flow as `inspect`.

Examples:

```bash
./paperscript.sh explore
```

### `download`

Downloads a chosen version or exact build on demand.

Examples:

```bash
./paperscript.sh download --version 26.1.2
./paperscript.sh download --version 1.20.4
./paperscript.sh download --version 1.20.4 --build 123
./paperscript.sh download --version 26.2.1 --channel BETA
./paperscript.sh --force download --version 26.1.2
./paperscript.sh --force download --version 1.21.11 --build 130
```

Notes:

- `--version` downloads the newest build for that version on the selected channel.
- `--build` downloads that exact build number for the version.
- The default channel comes from `config.json` and defaults to `STABLE`.
- Version upgrades still prompt unless you add `--yes`.
- `--force` lets you re-download and reinstall the same build even if it is already present.
- Use `./paperscript.sh --force update` to re-download the current latest stable build.
- Use `./paperscript.sh --force download --version <version> --build <build>` to re-download one exact build.

### `cleanup`

Removes selected local runtime files and caches from `paperscript/`.

Default behavior:

- `./paperscript.sh cleanup`
  Cleans the safe/default targets: `downloads/` and Python `__pycache__/`

Targets:

- `--downloads`
  Delete staged downloads and temp files in `downloads/`
- `--backups`
  Clean backup jars
- `--backups --keep N`
  Keep the newest `N` backups and remove older ones
- `--pycache`
  Delete Python `__pycache__/` folders
- `--logs`
  Clear `logs.log`
- `--json` or `--config`
  Delete `config.json` and `state.json` so the next run starts fresh

Confirmation behavior:

- cleanup explains what will be removed
- cleanup asks for `y/N` confirmation by default
- `--yes` skips the prompt
- `--dry-run` shows what would be deleted without removing anything

Examples:

```bash
./paperscript.sh cleanup
./paperscript.sh cleanup --downloads
./paperscript.sh cleanup --backups
./paperscript.sh cleanup --backups --keep 10
./paperscript.sh cleanup --pycache
./paperscript.sh cleanup --logs
./paperscript.sh cleanup --json
./paperscript.sh cleanup --yes --downloads --pycache
./paperscript.sh cleanup --dry-run --json
```

### `init`

Creates or repairs local runtime files inside `paperscript/`.

It can create:

- `config.json`
- `state.json`
- `logs.log`
- `todo.log`
- `downloads/`
- `backups/`

It always asks for confirmation unless you use `--yes`.

Examples:

```bash
./paperscript.sh init
./paperscript.sh --yes init
./paperscript.sh init --dry-run
```

## Global Options

- `--server-dir PATH`
  Use a specific server directory instead of the current directory.
- `--contact VALUE`
  Optional legacy contact value used to build a `PaperScript/<version> (<contact>)` User-Agent override.
- `--user-agent VALUE`
  Full custom User-Agent header. If omitted, PaperScript uses the built-in default.
- `--tmux-session NAME`
  tmux session to use for graceful stop. Defaults to config, `PAPERSCRIPT_TMUX_SESSION`, or `mcserver`.
- `--timeout SECONDS`
  HTTP timeout in seconds. Default comes from `config.json` and is `30` unless changed.
- `--yes`
  Accept prompts automatically where it is safe to do so.
- `--force`
  Reinstall even if the same build is already present. Most useful with `update`, `stable --download`, `experimental --download`, or `download`.
- `--dry-run`
  Show what would happen without downloading, moving jars, pruning backups, or stopping servers.
- `--quiet`
  Suppress normal console output. Logs still go to `paperscript/logs.log`.
- `--no-color`
  Disable ANSI colors in terminal output.

For cron or scheduled tasks, the safest pattern is usually:

```bash
./paperscript.sh --yes --quiet update
```

That keeps the run non-interactive, quiet on stdout, and still logged to `paperscript/logs.log`.

## How Server Directory Detection Works

PaperScript uses this behavior:

- If you pass `--server-dir`, that path is used.
- If you run from a normal directory, the current working directory is treated as the server directory.
- If you run the Python file from inside a directory actually named `PaperScript`, the parent directory is treated as the server directory.

That makes this work naturally:

```bash
cd /server
./paperscript.sh update
```

and also:

```bash
cd /server/paperscript
python3 paperscript.py update
```

## Running Server Detection

If `server.properties` exists, PaperScript assumes the directory may be a live server directory and checks for a likely matching Java process.

It first uses the `server-port` value from `server.properties` and looks for a Java process listening on that exact TCP port. That makes it safer on a machine that runs several Minecraft servers at once.

If port-based detection does not find anything, it falls back to:

- jar-name matching
- command-line matching
- working-directory matching

When it finds one, it offers:

- graceful stop
- force stop
- upgrade anyway
- exit

Graceful stop behavior:

- first tries `tmux send-keys -t <session> stop Enter`
- then waits for exit
- if that fails, falls back to `SIGTERM`

Force stop uses `SIGKILL`.

Examples:

```bash
./paperscript.sh --tmux-session production update
PAPERSCRIPT_TMUX_SESSION=test-server ./paperscript.sh update
```

## Logging And Runtime Files

PaperScript stores its runtime files inside the visible `paperscript/` directory:

- `paperscript/config.example.json`
  Tracked config template for the repo
- `paperscript/config.json`
  Local per-server config, intentionally ignored by git
- `paperscript/state.json`
  Last installed jar information, intentionally ignored by git
- `paperscript/logs.log`
  Activity log
- `paperscript/downloads/`
  Temporary and staged downloads
- `paperscript/backups/`
  Previous jars moved out of the server root
- `paperscript/todo.log`
  Deferred future ideas for the project

These runtime files are isolated on purpose so the server root stays clean and different servers can keep different local settings without git noise.

## Config Defaults

PaperScript creates `paperscript/config.json` automatically if it does not exist yet.

The repo includes a tracked template at [paperscript/config.example.json](./paperscript/config.example.json).

Current default config:

```json
{
  "server_name": null,
  "tmux_session": "mcserver",
  "default_channel": "STABLE",
  "check_latest_channel_only": "STABLE",
  "allow_cross_version_auto_upgrade": false,
  "allow_same_version_build_upgrade": true,
  "keep_backups": 10,
  "cleanup_backups_after_install": true,
  "running_server_action": "ask",
  "graceful_stop_command": "stop",
  "http_timeout_seconds": 30,
  "status_show_all_channels": true,
  "download_filename_pattern": "Paper-{version}-{build}.jar",
  "log_file": "logs.log",
  "backup_dir": "backups",
  "downloads_dir": "downloads",
  "confirm_before_force_download": true,
  "confirm_before_downgrade": true,
  "auto_detect_server_by_port": true,
  "fallback_process_detection": true,
  "quiet": false,
  "no_color": false,
  "color_theme": "default",
  "default_status_view": "full",
  "command_hint_mode": "auto",
  "release_link_mode": "auto"
}
```

Useful per-server settings:

- `server_name`
  Friendly label for status output
- `tmux_session`
  Session to use for graceful stop
- `keep_backups`
  How many backup jars to keep after install
- `running_server_action`
  Default behavior when a running server is detected
- `default_channel`
  Default download channel for `download --version`
- `quiet`
  Make unattended runs silent by default
- `no_color`
  Disable ANSI colors by default
- `color_theme`
  Theme name. Current options: `default`, `soft`, `high-contrast`
- `default_status_view`
  `full` or `compact`
- `command_hint_mode`
  `auto`, `always`, or `never`
- `release_link_mode`
  `auto`, `always`, or `never`

## Force Re-Downloading A Build

If you already have a jar installed but want the same file again anyway, use one of these:

```bash
./paperscript.sh --force update
./paperscript.sh --force stable --download
./paperscript.sh --force experimental --download
./paperscript.sh --force download --version 1.21.11 --build 130
```

Inside `inspect` and `explore`, PaperScript can also offer:

```text
Download it anyway? [y/N]
```

when the selected build is already installed.

## Example Workflows

Check a live server without changing anything:

```bash
./paperscript.sh status
./paperscript.sh verify
./paperscript.sh update --dry-run
```

Update a dev server in the current directory:

```bash
./paperscript.sh update
```

See whether a newer version family exists before touching production:

```bash
./paperscript.sh list-versions --channels
./paperscript.sh stable
./paperscript.sh experimental
```

Inspect an older branch:

```bash
./paperscript.sh inspect 1.20.4
```

Download an exact historical build:

```bash
./paperscript.sh download --version 1.19.2 --build 88
```

Target a separate server directory:

```bash
./paperscript.sh --server-dir /Users/you/minecraft/test-server update
```

## API Notes

PaperScript is built around the current PaperMC downloads service and its User-Agent expectations:

- Docs: [https://docs.papermc.io/misc/downloads-service/](https://docs.papermc.io/misc/downloads-service/)
- Swagger UI: [https://fill.papermc.io/swagger-ui/index.html#/](https://fill.papermc.io/swagger-ui/index.html#/)
- Downloads page: [https://papermc.io/downloads/paper](https://papermc.io/downloads/paper)

## License

MIT. See [LICENSE](./LICENSE).
