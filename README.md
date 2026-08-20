# PaperScript

PaperScript is a dependency-free, manually invoked Paper JAR staging tool that uses the current PaperMC Fill v3 downloads service. It downloads a verified, versioned JAR beside the one already in use; it never stops, starts, restarts, or signals the server.

It is designed for the layout where your server root stays readable, while PaperScript keeps its own files in a visible `paperscript/` directory:

```text
/anydirectory/
/anydirectory/server.properties
/anydirectory/Paper-26.2-84.jar
/anydirectory/paperscript.sh
/anydirectory/paperscript/
/anydirectory/paperscript/paperscript.py
/anydirectory/paperscript/config.example.json
/anydirectory/paperscript/config.json
/anydirectory/paperscript/state.json
/anydirectory/paperscript/last-launched-jar.txt
/anydirectory/paperscript/cache/
/anydirectory/paperscript/downloads/
/anydirectory/paperscript/backups/jars/26.2/
/anydirectory/paperscript/locks/
/anydirectory/paperscript/logs.log
/anydirectory/paperscript/todo.log
```

This keeps staging-tool clutter out of the server root and makes the disposable cache, download workspace, and logs easy to clean. Preserve `config.json`, `state.json`, `last-launched-jar.txt`, and the tracked PaperScript source files unless you intentionally want to reset them.

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
- Can inspect and stage the latest preview release that is newer than the current stable line, preferring `BETA` and falling back to `ALPHA`.
- Stages a newer same-version stable build only when the command is run manually.
- Prompts before staging a newer or older Minecraft version family.
- Supports forced re-download of the same build with `--force`.
- Verifies downloads against the API-provided SHA-256.
- Stores staged-build identity and SHA-256 in `state.json` for later `verify` checks.
- Caches Paper API metadata locally so repeated release checks are faster.
- Publishes the verified download atomically as `Paper-<version>-<build>.jar` without replacing an existing JAR.
- Uses the launcher's atomic `last-launched-jar.txt` marker to protect the JAR selected for the last start.
- Keeps the last-launched JAR plus the newest staged JAR in the server root by default.
- While a launcher/JVM run is in flight, temporarily protects its one prior rollback JAR as well.
- Archives older exact same-version numeric builds under `paperscript/backups/jars/<version>/` and keeps five by default.
- Provides explicit `cleanup --server-jars --keep N --dry-run` reconciliation.
- Serializes staging and root-JAR cleanup with a per-server lock.
- Refuses overlapping launcher runs with a fail-closed lock held for the JVM lifetime.
- Detects likely running servers and tmux sessions for read-only status only.
- Supports `--dry-run`, `--quiet`, `--no-color`, and per-server config defaults.
- Supports color themes and a compact or full status view.
- Keeps each target server's runtime files isolated inside that server's `paperscript/` directory.

## Requirements

- Python `3.9+`
- `python3` available on your path
- `tmux` only if you want its session shown in read-only status

No third-party Python packages are required.

## Setup

For a central checkout that targets an existing server, clone separately and pass the server root explicitly:

```bash
git clone https://github.com/mrfdev/PaperScript.git /opt/PaperScript
cd /opt/PaperScript
chmod +x paperscript.sh paperscript/paperscript.py
./paperscript.sh --server-dir /srv/minecraft/live status
```

For the compact layout shown above, place `paperscript.sh` and the tracked `paperscript/` directory directly in the server root. Do not run from a child checkout without `--server-dir`, because the current working directory is the default target.

If you want to initialize or repair the runtime files manually:

```bash
./paperscript.sh --server-dir /srv/minecraft/live init
```

In a compact in-root installation, the shorter `./paperscript.sh init` is equivalent. App startup may create target-local config, directories, and logging scaffolding; `init` asks before creating or repairing its remaining runtime files.

## Refreshing An Existing Live Server

If you already run a live server and want to replace an older local PaperScript checkout with a fresh copy from GitHub, the normal safe path is:

```bash
./paperscript.sh --server-dir /srv/minecraft/live status
./paperscript.sh --server-dir /srv/minecraft/live update --dry-run
./paperscript.sh --server-dir /srv/minecraft/live update
```

These examples assume a central checkout. In the compact in-root layout, omit the repeated `--server-dir` option.

For servers that use the required numeric naming pattern, such as `Paper-26.2-84.jar`, PaperScript detects the newest local build and stages the next verified build beside it. The updated `1MB-minecraft.sh` selects the greatest numeric build for its configured Minecraft version on the next manual start.

Keep your local `paperscript/config.json`, `paperscript/state.json`, and `paperscript/last-launched-jar.txt` when refreshing the checkout. In particular, review changes to:

- `default_channel`
- `check_latest_channel_only`
- `tmux_session`

then deleting the whole `paperscript/` runtime directory will also delete those per-server preferences.

PaperScript always stages the canonical `Paper-<version>-<numeric-build>.jar` name. Legacy names such as `Paper-26.2.jar` remain launcher fallbacks, but PaperScript never overwrites them.

If you intentionally want a fresh runtime, start once with the updated `1MB-minecraft.sh` before root-JAR cleanup. Until a valid marker exists, staging succeeds but automatic root cleanup is deliberately deferred.

## Quick Start

Show the current state:

```bash
./paperscript.sh status
```

Stage the latest stable Paper build for the launcher-selected Minecraft family:

```bash
./paperscript.sh update
```

Force a re-download and verification of the latest stable build for that launcher family:

```bash
./paperscript.sh --force update
```

Inspect the latest preview release beyond stable:

```bash
./paperscript.sh experimental
```

Download that preview build:

```bash
./paperscript.sh experimental --download
```

## Commands

### `update`

Checks the server directory, finds the latest stable build for the launcher-selected family, and stages it beside existing JARs when appropriate.

Behavior:

- If no managed Paper JAR is detected, it offers the latest stable build.
- If the launcher-selected version has a newer stable build, it stages that build.
- If the newest staged build for the launcher family already matches stable, `--force update` re-downloads and verifies it without replacing the existing file.
- `update` stays on the launcher-selected Minecraft version even when a newer family exists; cross-version staging requires an explicit `download --version ...` command.
- The download is SHA-256 and size verified, fsynced, then atomically published under its numeric build filename.
- PaperScript never asks to stop the server and contains no stop/kill path.
- After staging, valid launcher identity enables bounded root cleanup; missing/invalid identity defers cleanup without guessing.
- If `--dry-run` is used, it reports JAR/archive actions without staging, moving, or pruning JARs; normal target-local config, logging, and metadata-cache activity may still occur.

Examples:

```bash
./paperscript.sh
./paperscript.sh update
./paperscript.sh --force update
./paperscript.sh update --dry-run
./paperscript.sh --no-color update
./paperscript.sh --server-dir /srv/mc/live update
```

### `status`

Shows PaperScript, launcher, staged-JAR, retention, and release state, including the newest preview release beyond stable when one exists.

The normal full view can include:

- PaperScript release
- server directory and runtime directory
- server label
- tmux session name and whether it currently exists
- manual/external lifecycle policy
- server properties detection
- configured server port
- running server detection
- newest managed jar, full path, version, build, staged channel, and SHA-256
- last launcher-selected jar, including a clearly labelled legacy marker when applicable
- the predicted next JAR for the family in the last launcher marker, including legacy fallback state; PaperScript does not execute or infer later edits to `_minecraftVersion`
- stored expected SHA-256 from the last PaperScript stage
- newest stable release
- update status
- newest channels for the current stable version
- newest preview release beyond stable
- server-root retention and bounded archive settings
- historical backup/archive counts and cleanup suggestions when useful

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

Shows the latest stable Paper release overall and can stage it directly.

This is useful when you want a clear stable overview without running a full update flow first.

Examples:

```bash
./paperscript.sh stable
./paperscript.sh stable --download
./paperscript.sh --force stable --download
./paperscript.sh stable --download --yes --force
```

### `experimental`

This command name is kept for compatibility, but it now behaves like a preview-channel helper.

It looks for the latest non-stable Paper release that is newer than the current stable line. It prefers `BETA` and only falls back to `ALPHA` if no beta build exists.

If the current stable release is already the newest line, PaperScript says so instead of pointing you at an older beta build from that same now-stable version.

For example:

- `Latest channels for stable version 26.2` means the channels that exist for `26.2`
- `Latest preview release` is only shown when a newer not-yet-stable line exists beyond the current stable release

Examples:

```bash
./paperscript.sh experimental
./paperscript.sh experimental --download
./paperscript.sh --yes experimental --download
./paperscript.sh experimental --download --yes
```

### `verify`

Hashes the newest managed jar and compares it against:

- the SHA-256 recorded in `state.json` during the last PaperScript stage
- the expected SHA-256 from the live Paper API for that exact version and build
- the recorded staging channel and current channel reported by the API

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
./paperscript.sh list-versions --channels --limit 10
./paperscript.sh --debug-http list-versions --channels --limit 10
```

With `--channels`, PaperScript also shows the newest build it can find per channel for each version.

Because `--channels` performs many API requests, PaperScript now:

- retries transient API and Cloudflare errors
- continues past temporary per-version failures by default
- supports `--limit` for smaller debug runs
- supports `--channel-delay-ms` to slow the request rate when needed

This is useful for questions like:

- which versions exist at all
- whether a future `26.3.x` line only exists as alpha or beta
- whether `26.2` is still the newest stable family or a newer preview line has appeared
- whether older versions such as `1.20.4` or `1.19.2` still have builds available

### `inspect VERSION`

Shows the newest available build per channel for one specific version, then offers to download one interactively.

If the selected build is already staged, PaperScript can offer a direct confirmation. An existing target is never overwritten: it is accepted only when its SHA-256 already matches PaperMC.

Examples:

```bash
./paperscript.sh inspect 26.2
./paperscript.sh inspect 1.20.4
./paperscript.sh inspect 1.19.2
```

### `explore`

Interactive version picker. It lists all available versions, lets you choose one by number, shows the newest builds for that version, and can then download it.

If the build you choose is already staged, `explore` can offer the same confirmation flow as `inspect`.

Examples:

```bash
./paperscript.sh explore
```

### `download`

Downloads, verifies, and stages a chosen version or exact build on demand.

Examples:

```bash
./paperscript.sh download --version 26.2
./paperscript.sh download --version 1.20.4
./paperscript.sh download --version 1.20.4 --build 123
./paperscript.sh download --version 26.2 --channel BETA
./paperscript.sh --force download --version 26.2
./paperscript.sh --force download --version 26.2 --build 84
./paperscript.sh download --version 26.2 --build 84 --force --yes
```

Notes:

- `--version` downloads the newest build for that version on the selected channel.
- `--build` downloads that exact build number for the version.
- The default channel comes from `config.json` and defaults to `STABLE`.
- Version upgrades still prompt unless you add `--yes`.
- `--force` allows an already-selected build through the command flow, but never overwrites an existing target path.
- PaperScript refuses same-version build downgrades because `1MB-minecraft.sh` always chooses the greatest numeric build, so an older file could not become the next launch.
- Use `./paperscript.sh --force update` to re-download the current latest stable build.
- Use `./paperscript.sh --force download --version <version> --build <build>` to re-download one exact build.

### `cleanup`

Removes selected local runtime files and caches, or explicitly reconciles versioned JARs in the server root.

Default behavior:

- `./paperscript.sh cleanup`
  Cleans the safe/default targets: `downloads/` and Python `__pycache__/`

Targets:

- `--downloads`
  Delete old download workspace files in `downloads/`
- `--backups`
  Clean legacy top-level backup items while preserving the managed `backups/jars/` archive
- `--backups --keep N`
  Keep the newest `N` backups and remove older ones
- `--server-jars`
  Protect the valid launcher-marked JAR, greatest numeric next-start JAR, and any in-flight launcher rollback JAR, then archive older exact numeric builds for that same version
- `--server-jars --keep N`
  Use `N` as the steady-state matching-root limit (minimum `2`); the default is `2`. When last-launched and newest are the same file, only one physical JAR is needed. An active launcher may temporarily add one prior rollback JAR, so the safe transient maximum is normally `3`.
- `--server-jars --version VERSION`
  Restrict reconciliation to this version; it must agree with the launch marker target
- `--all`
  Clean downloads, legacy top-level backups, metadata cache, `__pycache__`, logs, and JSON state/config together. It never implies `--server-jars` and preserves `backups/jars/`.
- `--metadata-cache`
  Delete cached Paper API metadata in `cache/`
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
- `--dry-run` lists both root-to-archive moves and exact archive-cap prunes without removing anything

`--server-jars` is fail-closed: it refuses to move anything when the launcher marker is missing, malformed, points outside the server root, identifies a symlink, or names another version. Other Minecraft versions, legacy names, partial downloads, malformed names, symlinks, plugin JARs, and unknown files are never managed.

Examples:

```bash
./paperscript.sh cleanup
./paperscript.sh cleanup --all
./paperscript.sh cleanup --downloads
./paperscript.sh cleanup --backups
./paperscript.sh cleanup --metadata-cache
./paperscript.sh cleanup --backups --keep 10
./paperscript.sh cleanup --server-jars --dry-run
./paperscript.sh cleanup --server-jars --keep 2
./paperscript.sh cleanup --server-jars --version 26.2 --keep 2
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

It asks before its listed repair actions unless you use `--yes`. PaperScript startup itself may already create target-local config, directories, and log scaffolding.

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
  tmux session to display in read-only status. Defaults to config, `PAPERSCRIPT_TMUX_SESSION`, or `mcserver`.
- `--timeout SECONDS`
  HTTP timeout in seconds. Default comes from `config.json` and is `30` unless changed.
- `--debug-http`
  Log HTTP request attempts and retries for Paper API troubleshooting.
- `--no-metadata-cache`
  Bypass the local Paper API metadata cache for this run.
- `--yes`
  Accept prompts automatically where it is safe to do so.
- `--force`
  Allow the same build to be selected again. PaperScript still refuses to overwrite an existing JAR whose checksum differs.
- `--dry-run`
  Show what would happen without downloading, moving jars, or pruning archives.
- `--quiet`
  Suppress normal console output. Logs still go to `paperscript/logs.log`.
- `--no-color`
  Disable ANSI colors in terminal output.

PaperScript also accepts these global flags after the command, so both styles work:

```bash
./paperscript.sh --yes --force stable --download
./paperscript.sh stable --download --yes --force
```

PaperScript is intended for manual invocation. This project does not recommend cron or unattended staging.

For API troubleshooting, a good pattern is:

```bash
./paperscript.sh --debug-http --timeout 5 stable
./paperscript.sh --debug-http list-versions --channels --limit 10
./paperscript.sh --no-metadata-cache --debug-http stable
```

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

If `server.properties` exists, `status` treats the directory as a possible live server and performs read-only checks for a likely matching Java process.

It first uses the `server-port` value from `server.properties` and looks for a Java process listening on that exact TCP port. That makes it safer on a machine that runs several Minecraft servers at once.

If port-based detection does not find anything, it falls back to:

- jar-name matching
- command-line matching
- working-directory matching

Detection never controls that process. Staging does not run a stop command, send tmux keys, send Unix signals, start Java, or restart a tmux session. Stop the server manually through your normal CLI/tmux workflow when you are ready, then run `1MB-minecraft.sh`; it will select the greatest numeric build for its configured version.

Examples:

```bash
./paperscript.sh --tmux-session production status
PAPERSCRIPT_TMUX_SESSION=test-server ./paperscript.sh status
```

## Logging And Runtime Files

PaperScript stores its runtime files inside the visible `paperscript/` directory:

- `paperscript/config.example.json`
  Tracked config template for the repo
- `paperscript/config.json`
  Local per-server config, intentionally ignored by git
- `paperscript/state.json`
  Last staged jar information, intentionally ignored by git
- `paperscript/last-launched-jar.txt`
  Exact basename selected by the customized `1MB-minecraft.sh`, written atomically immediately before Java starts; a nonzero JVM exit restores the previous valid marker
- `paperscript/logs.log`
  Activity log
- `paperscript/downloads/`
  Legacy/diagnostic download workspace; active staging uses a unique hidden temp file on the server filesystem
- `paperscript/cache/`
  Cached Paper API metadata used to speed up repeated checks
- `paperscript/backups/jars/<version>/`
  Bounded archive of older exact numeric Paper builds moved out of the server root
- `paperscript/locks/`
  Per-server advisory staging/cleanup lock plus the fail-closed active-launch guard
- `paperscript/todo.log`
  Deferred future ideas for the project

These runtime files are isolated on purpose so the server root stays clean and different `--server-dir` targets keep separate config, state, cache, logs, archive, marker, and lock files without git noise.

If an older central checkout kept `config.json` or `state.json` beside `paperscript.py`, the first run against a separate `--server-dir` prints a migration warning and leaves those legacy files untouched. Review and manually copy only the settings/state that belong to that target; PaperScript does not guess which server shared legacy state belongs to.

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
  "allow_same_version_build_upgrade": true,
  "keep_backups": 10,
  "keep_server_jars": 2,
  "keep_archived_jars": 5,
  "reconcile_server_jars_after_stage": true,
  "http_timeout_seconds": 30,
  "status_show_all_channels": true,
  "log_file": "logs.log",
  "backup_dir": "backups",
  "downloads_dir": "downloads",
  "metadata_cache_dir": "cache",
  "metadata_cache_enabled": true,
  "metadata_cache_ttl_seconds": 300,
  "confirm_before_force_download": true,
  "confirm_before_downgrade": true,
  "auto_detect_server_by_port": true,
  "fallback_process_detection": true,
  "quiet": false,
  "no_color": false,
  "color_theme": "default",
  "default_status_view": "full",
  "command_hint_mode": "auto",
  "release_link_mode": "auto",
  "debug_http": false,
  "http_retries": 2,
  "http_retry_backoff_seconds": 1.5,
  "list_versions_channel_delay_ms": 150,
  "list_versions_continue_on_error": true
}
```

Useful per-server settings:

- `server_name`
  Friendly label for status output
- `tmux_session`
  Session to display in read-only status
- `keep_backups`
  Retention for historical files in the legacy top-level `backups/` cleanup target
- `keep_server_jars`
  Steady-state exact same-version numeric JAR limit (minimum `2`): valid last-launched plus greatest numeric next-start JAR. One in-flight launcher rollback JAR may temporarily exceed it.
- `keep_archived_jars`
  Maximum exact numeric Paper JARs retained per version under `paperscript/backups/jars/` (minimum `1`)
- `reconcile_server_jars_after_stage`
  Archive older matching root JARs after a successful stage, but only when launcher identity validates
- `default_channel`
  Default download channel for `download --version`
- `metadata_cache_enabled`
  Enable or disable the local Paper API metadata cache
- `metadata_cache_ttl_seconds`
  How long cached metadata stays valid before PaperScript refreshes it
- `quiet`
  Suppress normal console output while retaining the activity log
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
- `debug_http`
  Log HTTP request attempts and retries
- `http_retries`
  Retry count for transient API or Cloudflare errors
- `http_retry_backoff_seconds`
  Base retry delay before exponential backoff
- `list_versions_channel_delay_ms`
  Delay between per-version channel lookups during `list-versions --channels`
- `list_versions_continue_on_error`
  Continue past temporary per-version failures instead of aborting the whole listing

## Force Re-Downloading A Build

If a build is already selected and you want PaperScript to re-check that request, use one of these:

```bash
./paperscript.sh --force update
./paperscript.sh --force stable --download
./paperscript.sh --force experimental --download
./paperscript.sh --force download --version 26.2 --build 84
./paperscript.sh stable --download --yes --force
```

Inside `inspect` and `explore`, PaperScript can also offer:

```text
Download it anyway? [y/N]
```

when the selected build is already staged. An existing target is kept byte-for-byte and is only accepted when its checksum matches the Paper API.

## Example Workflows

Check a live server without changing server JARs or controlling its lifecycle:

```bash
./paperscript.sh status
./paperscript.sh verify
./paperscript.sh update --dry-run
```

Stage a newer build for a dev server in the current directory:

```bash
./paperscript.sh update
```

See whether a newer version family exists before touching production:

```bash
./paperscript.sh list-versions --channels
./paperscript.sh stable
./paperscript.sh experimental
```

If `experimental` reports that no preview release newer than stable exists, that means the current stable line, such as `26.2`, is already the newest main target.

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

## Test-Instance 1MB Launcher

This repository includes a customized test-instance copy of `1MB-minecraft.sh` at the
server root. The copy records the SHA-256 of the original launcher it came from so its
changes can be reviewed and manually applied to the canonical 1MB source later.

For the configured `_minecraftVersion`, the customized launcher:

- prefers `Paper-<version>-<numeric-build>.jar`
- compares build numbers numerically and selects the greatest build
- ignores other Minecraft versions, partial downloads, and malformed build names
- ignores symlink JAR candidates
- falls back to the legacy `paper-<version>.jar` name when no versioned build exists
- writes the exact selected basename atomically to `paperscript/last-launched-jar.txt` before invoking Java
- resolves and enters its own directory first, so launching it from another working directory cannot select another server's JAR
- keeps a fail-closed launch lock for the JVM lifetime, refuses overlapping launches, and restores the prior valid marker when Java returns an error

The launcher only chooses a jar when the server is started. It does not download jars,
stop a running server, or modify the external canonical 1MB source.

The launcher never guesses that an abandoned launch lock is stale: the wrapper can die while Java remains alive. If `paperscript/locks/server-launch/` remains after a crash, first confirm that the Minecraft JVM is fully stopped, then remove only that lock directory before retrying the launcher.

## Testing

PaperScript does not keep a Minecraft server, world, plugin directory, or reusable server template in this repository. Those files would be large, environment-specific, and too easy to mix with production data. Tests use disposable server directories instead.

Run the dependency-free unit and drift checks:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n paperscript.sh 1MB-minecraft.sh tests/live-smoke.sh
python3 -m py_compile paperscript/paperscript.py tests/test_paperscript.py tests/test_1mb_minecraft.py
```

The unit suite verifies version ordering, stable-channel defaults, same-version build-upgrade behavior, preview selection, saved channel metadata, atomic state/config defaults, launcher marker publication and rollback, numeric latest-build selection, non-disruptive staging, per-server and active-launch locking, fail-closed marker handling, symlink/version containment, dry-run pruning previews, root retention, and archive caps. Launcher tests use a disposable fake `java` executable and never start a real server.

Run the opt-in live PaperMC smoke test:

```bash
./tests/live-smoke.sh
```

The live test creates a temporary server root, uses `STABLE` for both release checks, stages the canonical `Paper-{version}-{build}.jar`, verifies its SHA-256, confirms `status` detects its version/build/channel/path, and runs `verify`. It never starts or stops a server. The temporary jar and runtime files are removed afterward.

To retain the disposable directory for troubleshooting:

```bash
PAPERSCRIPT_SMOKE_KEEP=1 ./tests/live-smoke.sh
```

GitHub Actions runs the network-free unit and syntax checks on current macOS and Ubuntu runners with the minimum supported Python 3.9. The full live download remains opt-in so routine CI does not repeatedly download a Paper server jar or add avoidable load to PaperMC.

## Todo

PaperScript keeps a live local todo file at [paperscript/todo.log](./paperscript/todo.log) for deferred ideas that are not implemented yet.

Current completed production foundations include manual non-disruptive staging, per-server locking, atomic staged publication/state, bounded JAR retention, and latest-build launcher selection with an active marker. Remaining queued ideas include:

- metadata-cache durability and corrupt-cache diagnostics
- manual review/application of the launcher diff to the canonical `1MB-minecraft.sh`
- read-only doctor checks, actionable verification results, and machine-readable status
- broader failure-injection, CLI integration, and launcher compatibility tests

Full server, world, plugin, and BlueMap backups remain a separate operational concern so
staging a new Paper jar does not wait for very large backup jobs.

## API Notes

PaperScript is built around the current PaperMC downloads service and its User-Agent expectations:

- Docs: [https://docs.papermc.io/misc/downloads-service/](https://docs.papermc.io/misc/downloads-service/)
- Swagger UI: [https://fill.papermc.io/swagger-ui/index.html#/](https://fill.papermc.io/swagger-ui/index.html#/)
- Downloads page: [https://papermc.io/downloads/paper](https://papermc.io/downloads/paper)

## License

MIT. See [LICENSE](./LICENSE).
