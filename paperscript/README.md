# PaperScript

PaperScript is a small, dependency-free updater for Paper servers that uses the current PaperMC Fill v3 downloads service.

It is built for the setup where you keep the updater in its own folder, such as:

```text
/server/
/server/Paper-1.21.11-130.jar
/server/server.properties
/server/paperscript.sh
/server/paperscript/
/server/paperscript/paperscript.py
/server/paperscript/backups/
/server/paperscript/downloads/
/server/paperscript/logs.log
```

The goal is to keep the server root clean while still letting you run updates from the server root, from the `paperscript/` folder, or from an empty directory used for testing.

## Why Python instead of Bash

Python is the best fit here for a few reasons:

- It works well on modern macOS and Ubuntu without needing `jq`, Node, npm packages, or extra shell utilities.
- JSON handling, version comparison, checksums, logging, prompts, and future features are much easier to keep readable.
- The runtime stays small: one Python script, one tiny shell launcher, and a handful of generated runtime files.
- It is easier to grow later into features like config files, multi-server support, changelog output, or automation.

Bash is still useful here as a tiny wrapper, but Python is much more maintainable for the actual updater logic.

## Features

- Uses the Fill v3 Paper downloads API.
- Sends a custom PaperMC User-Agent by default: `mrfloris-PaperScript/2.0 (https://github.com/mrfdev/PaperScript)`.
- Detects the newest stable Paper version and the newest stable build for that version.
- Only auto-downloads when the stable build is newer for the same version.
- Prompts before moving from one Minecraft version to a newer one.
- Lets you force a specific version or exact build download.
- Adds a `status` command to show the current state and whether an update is available.
- Adds a `verify` command to hash the current jar and compare it with recorded and API-reported SHA-256 values.
- Adds an `experimental` command to show or download the latest experimental Paper release overall.
- Adds a `cleanup` command for downloads, backups, `__pycache__`, logs, and local JSON state/config resets.
- Adds a `--dry-run` mode so you can preview actions without changing files.
- Verifies the downloaded jar against the API-provided SHA-256 checksum before install.
- Backs up the current Paper jar before installing a new one.
- Installs jars using a consistent `Paper-<version>-<build>.jar` filename.
- Keeps only the newest 10 backups by default after successful installs.
- Detects a likely running Java server process in the target directory.
- Offers graceful stop, force stop, upgrade anyway, or exit when a server seems to be running.
- Uses `server-port` from `server.properties` to narrow process detection on multi-server machines.
- Uses tmux for graceful stop, then falls back to `SIGTERM`.
- Writes activity to `logs.log`.
- Keeps downloads and backups inside `PaperScript/`.
- Uses colored terminal output by default for prompts, warnings, success messages, and errors.
- Supports `--quiet` mode for cron and scheduled tasks.

## Requirements

- Python 3.9 or newer
- `python3` available on your path

No third-party Python packages are required.

## Setup

Clone the repo into the folder you want to keep the updater files in:

```bash
git clone <your-repo-url> PaperScript
cd PaperScript
chmod +x paperscript.sh paperscript/paperscript.py
```

PaperScript now works out of the box with a built-in User-Agent string:

```text
mrfloris-PaperScript/2.0 (https://github.com/mrfdev/PaperScript)
```

If you ever want to override that, you can still use `--user-agent` or `PAPERSCRIPT_USER_AGENT`.

PaperScript also keeps `config.json` local to each machine and server. The repo includes a tracked template in [config.example.json](./config.example.json), while your real `config.json` stays ignored by git on purpose.

## Commands

### `update`

Checks the current server directory, finds the latest stable Paper version and latest stable build, and installs it when appropriate.

Behavior:

- If no current `Paper-<version>-<build>.jar` exists, it offers the latest stable build.
- If the current version matches the latest stable version, it only downloads when the build number is newer.
- If the current version and build already match the latest stable release, `--force update` re-downloads and reinstalls that same latest stable jar.
- If the latest stable version is newer than your current version, it asks before upgrading versions.
- If `server.properties` exists and a matching Java process appears to be running, it asks how to proceed.
- If `--dry-run` is used, it reports what it would do without stopping the server or changing files.

Examples:

```bash
./paperscript.sh
./paperscript.sh update
./paperscript.sh --force update
./paperscript.sh update --dry-run
./paperscript.sh --yes --quiet update
./paperscript.sh --no-color update
./paperscript.sh --server-dir /srv/mc/live update
./paperscript.sh --yes update
```

### `status`

Shows the current local Paper state plus the latest stable release info from the API.

It reports:

- current jar version and build
- current jar SHA-256
- whether `server.properties` exists
- configured server port
- whether a likely Java server process is running
- latest stable version and build
- whether an update is available
- latest channel info for the newest stable version
- latest experimental release overall
- direct command hints for installing the latest stable or latest experimental release
- expected SHA-256 from the last PaperScript install, when available
- backup retention settings

Examples:

```bash
./paperscript.sh status
./paperscript.sh --server-dir /srv/mc/live status
```

### `verify`

Hashes the current installed jar and compares it against:

- the SHA-256 recorded in `state.json` during the last PaperScript install, when available
- the expected SHA-256 from the live Paper API for that exact version and build, when available

This is useful if you want a quick integrity check after download or later on before starting a server.

Examples:

```bash
./paperscript.sh verify
./paperscript.sh --server-dir /srv/mc/live verify
```

### `experimental`

Shows the latest experimental Paper release overall. This is different from the alpha channel shown for the latest stable version in `status`.

For example:

- `Latest channels for stable version 1.21.11` means alpha, beta, and stable builds that belong to version `1.21.11`
- `Latest experimental release overall` may instead be something newer like `26.1.2 alpha build #7`

Examples:

```bash
./paperscript.sh experimental
./paperscript.sh experimental --download
./paperscript.sh --yes experimental --download
```

If you want the exact manual install command after seeing the latest experimental version, PaperScript prints it for you.

### `cleanup`

Removes selected local runtime files and caches.

Default behavior:

- `./paperscript.sh cleanup`
  Cleans the safe/default targets: `downloads/` and Python `__pycache__/`

Selectable targets:

- `--downloads`
  Delete staged downloads and temporary files in `downloads/`
- `--backups`
  Delete all files in `backups/`
- `--pycache`
  Delete Python `__pycache__/` folders
- `--logs`
  Clear `logs.log`
- `--json` or `--config`
  Delete `config.json` and `state.json` so the next run starts fresh

Confirmation behavior:

- cleanup shows a short explanation of what will be deleted
- cleanup asks for `y/N` confirmation by default
- `--yes` skips the confirmation prompt
- `--dry-run` shows what would be deleted without removing anything

Examples:

```bash
./paperscript.sh cleanup
./paperscript.sh cleanup --downloads
./paperscript.sh cleanup --backups
./paperscript.sh cleanup --pycache
./paperscript.sh cleanup --logs
./paperscript.sh cleanup --json
./paperscript.sh cleanup --yes --downloads --pycache
./paperscript.sh cleanup --dry-run --json
```

### `list-versions`

Lists all Paper versions the API currently exposes.

Examples:

```bash
./paperscript.sh list-versions
./paperscript.sh list-versions --channels
```

With `--channels`, PaperScript also queries each version and shows the newest build it can find for channels such as `stable`, `beta`, and `alpha`.

This is the command to use when you want visibility like:

- what versions exist at all
- whether `26.2.x` exists only as alpha or beta
- whether `26.1.2` is the newest stable
- what older families like `1.20.4` or `1.19.2` still have available

### `inspect VERSION`

Shows the newest available build per channel for one specific Minecraft version, then offers to download one interactively.

If the selected build is already installed, PaperScript can now offer a direct `Download it anyway?` confirmation so you can re-download the same build without leaving the inspect flow.

Examples:

```bash
./paperscript.sh inspect 1.20.4
./paperscript.sh inspect 1.19.2
./paperscript.sh inspect 26.1.2
```

### `explore`

Interactive version picker. It lists all available versions, lets you choose one by number, shows the newest builds for that version, and can then download it.

If the build you choose is already installed, `explore` uses the same direct `Download it anyway?` confirmation flow as `inspect`.

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
./paperscript.sh --force download --version 1.21.1 --build 130
```

Notes:

- `--version` downloads the newest build for that version on the selected channel.
- `--build` downloads that exact build number for the version.
- The default channel comes from `config.json`, and defaults to `STABLE`.
- Version upgrades still prompt unless you add `--yes`.
- `--force` lets you re-download and reinstall the same build even if it is already present.
- Use `./paperscript.sh --force update` to re-download the current latest stable build.
- Use `./paperscript.sh --force download --version <version> --build <build>` to re-download one exact build.

## Global Options

- `--server-dir PATH`
  Use a specific server directory instead of the current directory.
- `--contact VALUE`
  Optional contact value used to build a legacy `PaperScript/<version> (<contact>)` User-Agent override.
- `--user-agent VALUE`
  Full custom User-Agent header. If omitted, PaperScript uses the built-in default.
- `--tmux-session NAME`
  tmux session to use for graceful stop. Defaults to config, `PAPERSCRIPT_TMUX_SESSION`, or `mcserver`.
- `--timeout SECONDS`
  HTTP timeout, default comes from `config.json` and is `30` unless changed.
- `--yes`
  Accept prompts automatically. If a running server is detected, PaperScript will try a graceful stop automatically.
- `--force`
  Reinstall even if the same build is already present. Useful with `update` or `download`.
- `--dry-run`
  Show what would happen without downloading, moving jars, pruning backups, or stopping the server.
- `--quiet`
  Suppress normal console output. Logs still go to `logs.log`, and errors still return a non-zero exit code.
- `--no-color`
  Disable ANSI colors in terminal output.

For cron or scheduled tasks, the safest pattern is usually:

```bash
./paperscript.sh --yes --quiet update
```

That way:

- the run stays non-interactive
- normal output stays quiet
- activity is still written to `logs.log`
- failures still return a non-zero exit status

## How Server Directory Detection Works

PaperScript uses this behavior:

- If you pass `--server-dir`, that path is used.
- If you run from a normal directory, the current working directory is treated as the server directory.
- If you run the script from inside a folder actually named `PaperScript`, the parent directory is treated as the server directory.

That makes this work naturally for the common layout:

```bash
cd /server/paperscript
python3 paperscript.py update
```

and also for:

```bash
cd /server
./paperscript.sh update
```

## Logging and Runtime Files

PaperScript writes and stores:

- `logs.log`
  Append-only activity log
- `config.json`
  Generated config file with defaults for tmux, backups, channel choices, and related behavior
- `config.example.json`
  Tracked config template for the repository
- `state.json`
  Last installed jar information
- `downloads/`
  Temporary and staged downloads
- `backups/`
  Previous jars moved out of the server root

These runtime files live inside the visible `paperscript/` project directory, so they stay isolated from other server files and from folders other tools may create.

These are ignored by git through `.gitignore`.

That means:

- `config.example.json` belongs in the repo
- `config.json` stays local on each server
- different servers can safely use different settings without creating git noise

## Config Defaults

PaperScript creates `config.json` automatically if it does not exist yet.

If you want a visible starting point in the repo, use `config.example.json` as the template and keep your real `config.json` local.

The current default config is:

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
  "fallback_process_detection": true
}
```

The most useful settings to change per server are usually:

- `tmux_session`
- `keep_backups`
- `running_server_action`
- `server_name`
- `default_channel`

## Running Server Detection

If `server.properties` exists, PaperScript assumes the directory may be a live server directory and checks for a likely matching Java process.

It first uses the `server-port` value from `server.properties` and looks for a Java process listening on that exact TCP port. That makes it much safer on a machine that runs several Minecraft servers at once.

If port-based detection does not find anything, it falls back to jar-name, command-line, and working-directory checks.

When it finds one, it offers:

- graceful stop
- force stop
- upgrade anyway
- exit

Graceful stop behavior:

- first tries `tmux send-keys -t <session> stop Enter`
- otherwise falls back to `SIGTERM`

Force stop uses `SIGKILL`.

By default the tmux session name is `mcserver`, but you can override it:

```bash
./paperscript.sh --tmux-session production update
PAPERSCRIPT_TMUX_SESSION=test-server ./paperscript.sh update
```

## What Counts as the Current Jar

PaperScript currently detects jars named like:

```text
Paper-<version>-<build>.jar
```

Examples:

- `Paper-1.21.11-130.jar`
- `Paper-26.1.2-41.jar`

That naming keeps version and build detection reliable.

## Example Workflows

See what a live server would do without changing anything:

```bash
./paperscript.sh status
./paperscript.sh verify
./paperscript.sh update --dry-run
```

Update a dev server in the current directory:

```bash
./paperscript.sh update
```

Check whether a new yearly-version family is out before upgrading production:

```bash
./paperscript.sh list-versions --channels
```

Inspect an older supported branch:

```bash
./paperscript.sh inspect 1.20.4
```

Download a specific version without automatically jumping to whatever is newest:

```bash
./paperscript.sh download --version 26.1.2
```

Download an exact historical build:

```bash
./paperscript.sh download --version 1.19.2 --build 88
```

Target a separate server directory:

```bash
./paperscript.sh --server-dir /Users/you/minecraft/test-server update
```

## Good Next Features

Nice follow-up additions that would fit this project well:

- changelog display before download
- JSON output mode for automation
- built-in start and restart helpers
- support for other Fill projects like Velocity or Folia

## API Notes

PaperScript is built around the current PaperMC downloads service and its required User-Agent policy:

- Docs: <https://docs.papermc.io/misc/downloads-service/>
- Swagger UI: <https://fill.papermc.io/swagger-ui/index.html#/>

## Before Publishing

If you plan to make the repo public, it is worth adding:

- a short contribution guide
- a release tag once the command names feel stable
