from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "paperscript" / "paperscript.py"
SPEC = importlib.util.spec_from_file_location("paperscript_module", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load PaperScript from {MODULE_PATH}")
paperscript = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = paperscript
SPEC.loader.exec_module(paperscript)


def build_info(
    version: str = "26.2",
    build_id: int = 84,
    channel: str = "STABLE",
) -> paperscript.BuildInfo:
    return paperscript.BuildInfo(
        version=version,
        build_id=build_id,
        channel=channel,
        download_name=f"paper-{version}-{build_id}.jar",
        download_url=f"https://example.invalid/paper-{version}-{build_id}.jar",
        sha256="a" * 64,
        size=123,
        created_at="2026-07-27T00:00:00Z",
    )


class RecordingLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def log(self, message: str) -> None:
        self.messages.append(message)


class ReleaseAndConfigTests(unittest.TestCase):
    def test_release_metadata_format(self) -> None:
        self.assertRegex(paperscript.APP_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertRegex(paperscript.APP_BUILD, r"^\d{3}$")
        self.assertEqual(
            paperscript.APP_RELEASE,
            f"{paperscript.APP_VERSION} build {paperscript.APP_BUILD}",
        )
        self.assertEqual(
            paperscript.DEFAULT_USER_AGENT,
            "mrfloris-PaperScript/2.0 (https://github.com/mrfdev/PaperScript)",
        )

    def test_tracked_and_documented_config_match_runtime_defaults(self) -> None:
        config_path = PROJECT_ROOT / "paperscript" / "config.example.json"
        tracked_config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(tracked_config, paperscript.DEFAULT_CONFIG)

        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        marker = "Current default config:\n\n```json\n"
        documented_json = readme.split(marker, 1)[1].split("\n```", 1)[0]
        self.assertEqual(json.loads(documented_json), paperscript.DEFAULT_CONFIG)

    def test_stable_update_defaults_remain_safe(self) -> None:
        self.assertEqual(paperscript.DEFAULT_CONFIG["default_channel"], "STABLE")
        self.assertEqual(paperscript.DEFAULT_CONFIG["check_latest_channel_only"], "STABLE")
        self.assertTrue(paperscript.DEFAULT_CONFIG["allow_same_version_build_upgrade"])
        self.assertFalse(paperscript.DEFAULT_CONFIG["allow_cross_version_auto_upgrade"])
        self.assertEqual(
            paperscript.DEFAULT_CONFIG["download_filename_pattern"],
            "Paper-{version}-{build}.jar",
        )


class VersionAndArgumentTests(unittest.TestCase):
    def test_mojang_and_legacy_versions_sort_correctly(self) -> None:
        self.assertGreater(paperscript.compare_versions("26.2", "26.1.2"), 0)
        self.assertGreater(paperscript.compare_versions("26.2", "1.21.11"), 0)
        self.assertGreater(paperscript.compare_versions("26.2", "26.2-rc2"), 0)
        self.assertGreater(paperscript.compare_versions("26.2-rc2", "26.2-beta1"), 0)

    def test_global_flags_work_after_a_subcommand(self) -> None:
        normalized = paperscript.normalize_global_options(
            ["stable", "--download", "--yes", "--force"]
        )
        self.assertEqual(
            normalized,
            ["--yes", "--force", "stable", "--download"],
        )


class DetectionAndStateTests(unittest.TestCase):
    def test_build_number_filename_is_detected_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar_path = Path(directory) / "Paper-26.2-84.jar"
            jar_path.write_bytes(b"paper")
            app = object.__new__(paperscript.PaperScriptApp)
            app.server_dir = Path(directory)
            app.state = {}

            current = app.find_current_jar()

            self.assertIsNotNone(current)
            self.assertEqual(current.version, "26.2")
            self.assertEqual(current.build, 84)
            self.assertEqual(current.path, jar_path)

    def test_custom_filename_is_detected_from_saved_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar_path = Path(directory) / "Paper-26.2.jar"
            jar_path.write_bytes(b"paper")
            app = object.__new__(paperscript.PaperScriptApp)
            app.server_dir = Path(directory)
            app.state = {
                "current_jar": jar_path.name,
                "current_version": "26.2",
                "current_build": 84,
                "current_channel": "STABLE",
            }

            current = app.find_current_jar()

            self.assertIsNotNone(current)
            self.assertEqual(current.version, "26.2")
            self.assertEqual(current.build, 84)
            self.assertEqual(current.path, jar_path)

    def test_recorded_install_state_includes_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = object.__new__(paperscript.PaperScriptApp)
            app.state = {}
            app.state_path = Path(directory) / "state.json"
            app.server_dir = Path(directory)
            jar_path = app.server_dir / "Paper-26.2.jar"

            app.record_state(build_info(), jar_path, "b" * 64)

            saved = json.loads(app.state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["current_channel"], "STABLE")
            self.assertEqual(saved["current_jar"], "Paper-26.2.jar")
            self.assertEqual(saved["current_version"], "26.2")
            self.assertEqual(saved["current_build"], 84)


class UpdateSelectionTests(unittest.TestCase):
    def make_app(self, current_build: int, latest_build: int, allow_upgrade: bool):
        app = object.__new__(paperscript.PaperScriptApp)
        app.find_current_jar = lambda: paperscript.JarInfo(
            Path(f"Paper-26.2-{current_build}.jar"),
            "26.2",
            current_build,
        )
        target = build_info(build_id=latest_build)
        app.latest_stable_version = lambda: ("26.2", target)
        app.allow_same_version_build_upgrade = allow_upgrade
        app.args = argparse.Namespace(force=False)
        app.logger = RecordingLogger()
        return app, target

    def test_same_version_newer_stable_build_is_selected(self) -> None:
        app, expected = self.make_app(83, 84, allow_upgrade=True)
        self.assertIs(app.choose_target_for_update(), expected)

    def test_same_version_upgrade_respects_disabled_config(self) -> None:
        app, _ = self.make_app(83, 84, allow_upgrade=False)
        self.assertIsNone(app.choose_target_for_update())


class PreviewSelectionTests(unittest.TestCase):
    def test_preview_prefers_beta_then_alpha_on_newer_versions(self) -> None:
        class FakeAPI:
            def get_project_versions(self):
                return [{"id": "26.3"}, {"id": "26.2"}]

            def get_latest_build(self, version: str, channel: str):
                if version == "26.3" and channel == "BETA":
                    return build_info(version="26.3", build_id=7, channel="BETA")
                if version == "26.3" and channel == "ALPHA":
                    return build_info(version="26.3", build_id=9, channel="ALPHA")
                return None

        app = object.__new__(paperscript.PaperScriptApp)
        app.api = FakeAPI()

        version, selected = app.latest_preview_version("26.2")

        self.assertEqual(version, "26.3")
        self.assertEqual(selected.channel, "BETA")
        self.assertEqual(selected.build_id, 7)


if __name__ == "__main__":
    unittest.main()
