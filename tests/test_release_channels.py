from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "paperscript" / "paperscript.py"
SPEC = importlib.util.spec_from_file_location("paperscript_release_channels", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load PaperScript from {MODULE_PATH}")
paperscript = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = paperscript
SPEC.loader.exec_module(paperscript)


def fill_build(version: str, build: int, channel: str) -> dict[str, object]:
    return {
        "id": build,
        "channel": channel,
        "createdAt": "2026-10-05T00:00:00Z",
        "downloads": {
            "server:default": {
                "name": f"paper-{version}-{build}.jar",
                "url": f"https://example.invalid/paper-{version}-{build}.jar",
                "checksums": {"sha256": "a" * 64},
                "size": 123,
            }
        },
    }


class ReleaseChannelRegressionTests(unittest.TestCase):
    """Exercise release selection through the CLI with Fill v3 response fixtures."""

    NOW = 1_791_158_400.0

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.server_dir = Path(self.directory.name).resolve()
        self.runtime_dir = self.server_dir / "paperscript"
        self.runtime_dir.mkdir()
        (self.runtime_dir / "state.json").write_text("{}\n", encoding="utf-8")
        self.current_jar = self.server_dir / "Paper-26.3-142.jar"
        self.current_jar.write_bytes(b"existing server jar must remain unchanged")
        (self.runtime_dir / paperscript.LAST_LAUNCHED_JAR_MARKER).write_text(
            self.current_jar.name + "\n", encoding="utf-8"
        )
        # Deliberately unordered: the newest build must be chosen numerically.
        self.responses = {
            f"{paperscript.API_ROOT}/versions": {
                "versions": [
                    {"version": {"id": "26.2", "group": "26"}},
                    {"version": {"id": "26.3", "group": "26"}},
                ]
            },
            f"{paperscript.API_ROOT}/versions/26.3/builds": [
                fill_build("26.3", 142, "BETA"),
                fill_build("26.3", 86, "ALPHA"),
                fill_build("26.3", 152, "BETA"),
                fill_build("26.3", 133, "ALPHA"),
                fill_build("26.3", 149, "BETA"),
                fill_build("26.3", 134, "BETA"),
                fill_build("26.3", 143, "BETA"),
            ],
            f"{paperscript.API_ROOT}/versions/26.2/builds": [
                fill_build("26.2", 128, "STABLE"),
                fill_build("26.2", 129, "STABLE"),
            ],
        }
        self.requested_urls: list[str] = []
        self.write_config("BETA")

    def write_config(self, update_channel: str | None, *, default_channel: str | None = None) -> None:
        config = {
            "auto_detect_server_by_port": False,
            "fallback_process_detection": False,
            "command_hint_mode": "always",
            "release_link_mode": "never",
            "http_retries": 0,
        }
        if update_channel is not None:
            config["check_latest_channel_only"] = update_channel
        if default_channel is not None:
            config["default_channel"] = default_channel
        (self.runtime_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def request_json(self, url: str) -> object:
        self.requested_urls.append(url)
        self.assertIn(url, self.responses, f"Unexpected API endpoint: {url}")
        return self.responses[url]

    def run_cli(self, *command: str) -> str:
        stdout, stderr = io.StringIO(), io.StringIO()
        original_jars = {path: path.read_bytes() for path in self.server_dir.glob("*.jar")}
        argv = [
            "paperscript.py", "--server-dir", str(self.server_dir),
            "--no-color", "--dry-run", *command,
        ]
        guards = [
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(paperscript.time, "time", return_value=self.NOW),
            mock.patch.object(paperscript.PaperAPI, "_request_json", side_effect=self.request_json),
            mock.patch.object(paperscript, "urlopen", side_effect=AssertionError("No real network allowed")),
            mock.patch.object(paperscript.PaperAPI, "download_file", side_effect=AssertionError("No downloads allowed")),
            mock.patch.object(paperscript, "run_command", return_value=paperscript.subprocess.CompletedProcess([], 1, "", "")),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ]
        with ExitStack() as stack:
            for guard in guards:
                stack.enter_context(guard)
            exit_code = paperscript.main()
        self.assertEqual(exit_code, 0, stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            {path: path.read_bytes() for path in self.server_dir.glob("*.jar")},
            original_jars,
        )
        return stdout.getvalue()

    def test_default_command_beta_update_bypasses_valid_build_142_cache(self) -> None:
        cache_dir = self.runtime_dir / "cache"
        cache_dir.mkdir(mode=0o700)
        cache_path = cache_dir / "builds-26.3.json"
        cache_path.write_text(
            json.dumps({
                "cached_at": "2026-10-05T00:00:00Z",
                "cached_at_epoch": self.NOW - 1,
                "data": [fill_build("26.3", 142, "BETA")],
            }),
            encoding="utf-8",
        )
        cache_path.chmod(0o600)

        output = self.run_cli()

        self.assertIn("would atomically publish Paper-26.3-152.jar", output)
        self.assertIn(f"{paperscript.API_ROOT}/versions/26.3/builds", self.requested_urls)

    def test_default_stable_channel_refuses_beta_only_launcher_family(self) -> None:
        self.write_config(None)

        output = self.run_cli()

        self.assertIn("No STABLE build was found", output)
        self.assertIn("26.3", output)
        self.assertNotIn("would atomically publish", output)

    def test_default_beta_update_without_managed_jar_uses_configured_channel(self) -> None:
        self.current_jar.unlink()
        (self.runtime_dir / paperscript.LAST_LAUNCHED_JAR_MARKER).unlink()

        output = self.run_cli()

        self.assertIn("No managed Paper jar detected. Latest beta is version 26.3 build #152", output)
        self.assertIn("would atomically publish Paper-26.3-152.jar", output)
        self.assertNotIn("would atomically publish Paper-26.2-129.jar", output)

    def test_default_download_channel_beta_does_not_opt_update_into_beta(self) -> None:
        self.write_config(None, default_channel="BETA")

        update_output = self.run_cli()
        download_output = self.run_cli("download", "--version", "26.3")

        self.assertIn("No STABLE build was found", update_output)
        self.assertNotIn("would atomically publish", update_output)
        self.assertIn("would atomically publish Paper-26.3-152.jar", download_output)

    def test_experimental_with_beta_update_config_finds_preview_newer_than_actual_stable(self) -> None:
        output = self.run_cli("experimental")

        self.assertIn("26.3 build #152 (BETA", output)
        self.assertNotIn("No preview release newer", output)
        self.assertIn(f"{paperscript.API_ROOT}/versions/26.2/builds", self.requested_urls)

    def test_experimental_download_with_beta_update_config_revalidates_actual_preview(self) -> None:
        output = self.run_cli("experimental", "--download")

        self.assertIn("would atomically publish Paper-26.3-152.jar", output)
        self.assertNotIn("No preview release newer", output)

    def test_stable_with_beta_update_config_returns_actual_stable_release(self) -> None:
        output = self.run_cli("stable")

        self.assertIn("Latest stable release overall: 26.2 build #129", output)
        self.assertIn("download --version 26.2 --channel STABLE", output)
        self.assertNotIn("Latest stable release overall: 26.3", output)

    def test_stable_download_with_beta_update_config_revalidates_stable_channel(self) -> None:
        output = self.run_cli("stable", "--download")

        self.assertIn("would atomically publish Paper-26.2-129.jar", output)
        self.assertNotIn("would atomically publish Paper-26.3-152.jar", output)

    def test_beta_update_describes_the_selected_channel_accurately(self) -> None:
        output = self.run_cli("update")

        self.assertIn("would atomically publish Paper-26.3-152.jar", output)
        self.assertRegex(output.lower(), r"latest beta build[^\n]*#152")
        self.assertNotRegex(output.lower(), r"latest stable build[^\n]*#152")

    def test_status_with_beta_update_config_keeps_actual_stable_and_preview_distinct(self) -> None:
        output = self.run_cli("status")

        self.assertRegex(output, r"Latest stable release\s*:\s*26\.2 build #129")
        self.assertRegex(output, r"Latest preview release\s*:\s*26\.3 build #152 \(BETA\)")
        self.assertRegex(output, r"Latest beta release\s*:\s*26\.3 build #152")
        self.assertNotIn("newer stable build available for launcher family 26.3", output)

    def test_beta_update_config_does_not_make_older_beta_a_preview_after_stable_catches_up(self) -> None:
        self.responses[f"{paperscript.API_ROOT}/versions/26.3/builds"].append(
            fill_build("26.3", 153, "STABLE")
        )

        experimental_output = self.run_cli("experimental", "--download")
        status_output = self.run_cli("status")

        self.assertIn("No preview release newer than the current stable release was found", experimental_output)
        self.assertIn("Stable is 26.3 build #153", experimental_output)
        self.assertNotIn("would atomically publish", experimental_output)
        self.assertRegex(status_output, r"Latest stable release\s*:\s*26\.3 build #153")
        self.assertRegex(status_output, r"Latest beta release\s*:\s*26\.3 build #152")
        self.assertRegex(
            status_output,
            r"Latest preview release\s*:\s*none newer than stable \(26\.3 build #153\)",
        )


if __name__ == "__main__":
    unittest.main()
