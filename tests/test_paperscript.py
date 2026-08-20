from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
        self.assertEqual(
            (PROJECT_ROOT / "paperscript" / "todo.log").read_text(encoding="utf-8"),
            paperscript.TODO_TEMPLATE,
        )

    def test_stable_update_defaults_remain_safe(self) -> None:
        self.assertEqual(paperscript.DEFAULT_CONFIG["default_channel"], "STABLE")
        self.assertEqual(paperscript.DEFAULT_CONFIG["check_latest_channel_only"], "STABLE")
        self.assertTrue(paperscript.DEFAULT_CONFIG["allow_same_version_build_upgrade"])
        self.assertNotIn("allow_cross_version_auto_upgrade", paperscript.DEFAULT_CONFIG)
        self.assertNotIn("download_filename_pattern", paperscript.DEFAULT_CONFIG)

    def test_malformed_config_is_preserved_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            malformed = '{"keep_server_jars": '
            config_path.write_text(malformed, encoding="utf-8")
            app = object.__new__(paperscript.PaperScriptApp)
            app.config_path = config_path

            with self.assertRaises(paperscript.PaperScriptError):
                app._load_config()

            self.assertEqual(config_path.read_text(encoding="utf-8"), malformed)

    def test_deprecated_destructive_config_keys_are_removed_during_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "server_name": "production",
                        "running_server_action": "force-stop",
                        "download_filename_pattern": "Paper-{version}.jar",
                    }
                ),
                encoding="utf-8",
            )
            app = object.__new__(paperscript.PaperScriptApp)
            app.config_path = config_path

            migrated = app._load_config()

            self.assertEqual(migrated["server_name"], "production")
            self.assertNotIn("running_server_action", migrated)
            self.assertNotIn("download_filename_pattern", migrated)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved, migrated)

    def test_invalid_config_value_types_are_preserved_and_rejected(self) -> None:
        invalid_values = {
            "quiet": "false",
            "keep_server_jars": "two",
            "keep_archived_jars": 0,
            "http_retry_backoff_seconds": -1,
        }
        for key, value in invalid_values.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "config.json"
                raw = json.dumps({key: value}) + "\n"
                config_path.write_text(raw, encoding="utf-8")
                app = object.__new__(paperscript.PaperScriptApp)
                app.config_path = config_path

                with self.assertRaisesRegex(paperscript.PaperScriptError, key):
                    app._load_config()

                self.assertEqual(config_path.read_text(encoding="utf-8"), raw)

    def test_nonfinite_and_unknown_config_values_are_preserved_and_rejected(self) -> None:
        invalid_configs = (
            {"http_retry_backoff_seconds": float("nan")},
            {"http_retry_backoff_seconds": float("inf")},
            {"http_retires": 5},
        )
        for config in invalid_configs:
            with self.subTest(config=config), tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "config.json"
                raw = json.dumps(config) + "\n"
                config_path.write_text(raw, encoding="utf-8")
                app = object.__new__(paperscript.PaperScriptApp)
                app.config_path = config_path

                with self.assertRaises(paperscript.PaperScriptError):
                    app._load_config()

                self.assertEqual(config_path.read_text(encoding="utf-8"), raw)

    def test_runtime_roles_cannot_alias_archive_state_log_or_each_other(self) -> None:
        invalid_configs = (
            {"downloads_dir": "backups/jars"},
            {"metadata_cache_dir": "backups"},
            {"log_file": "config.json"},
            {"log_file": "var", "downloads_dir": "var/downloads"},
            {"downloads_dir": "locks"},
        )
        for config in invalid_configs:
            with self.subTest(config=config), tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "config.json"
                raw = json.dumps(config) + "\n"
                config_path.write_text(raw, encoding="utf-8")
                app = object.__new__(paperscript.PaperScriptApp)
                app.config_path = config_path

                with self.assertRaises(paperscript.PaperScriptError):
                    app._load_config()

                self.assertEqual(config_path.read_text(encoding="utf-8"), raw)

    def test_server_dir_namespaces_runtime_config_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_server = Path(directory) / "first"
            second_server = Path(directory) / "second"
            parser = paperscript.build_parser()
            first_args = parser.parse_args(
                ["--server-dir", str(first_server), "status"]
            )
            second_args = parser.parse_args(
                ["--server-dir", str(second_server), "status"]
            )

            first = paperscript.PaperScriptApp(first_args)
            first.state["sentinel"] = "first"
            first._save_json(first.state_path, first.state)
            second = paperscript.PaperScriptApp(second_args)

            self.assertEqual(first.runtime_dir, first_server.resolve() / "paperscript")
            self.assertEqual(second.runtime_dir, second_server.resolve() / "paperscript")
            self.assertNotEqual(first.config_path, second.config_path)
            self.assertNotEqual(first.state_path, second.state_path)
            self.assertEqual(second.state, {})

    def test_central_checkout_runtime_files_trigger_target_migration_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_script_dir = root / "central-checkout" / "paperscript"
            target = root / "server"
            fake_script_dir.mkdir(parents=True)
            (fake_script_dir / "paperscript.py").write_text("# fixture\n", encoding="utf-8")
            legacy_config = fake_script_dir / "config.json"
            legacy_state = fake_script_dir / "state.json"
            legacy_config.write_text(
                json.dumps({"server_name": "legacy central config"}) + "\n",
                encoding="utf-8",
            )
            legacy_state.write_text(
                json.dumps({"server_dir": str(target)}) + "\n",
                encoding="utf-8",
            )
            args = paperscript.build_parser().parse_args(
                ["--server-dir", str(target), "status"]
            )

            with mock.patch.object(
                paperscript,
                "__file__",
                str(fake_script_dir / "paperscript.py"),
            ):
                app = paperscript.PaperScriptApp(args)

            self.assertEqual(app.runtime_dir, target.resolve() / "paperscript")
            self.assertEqual(app.config["server_name"], None)
            self.assertEqual(
                json.loads(legacy_config.read_text(encoding="utf-8"))["server_name"],
                "legacy central config",
            )
            self.assertIn(
                "Legacy central-checkout runtime file(s) were not reused",
                app.log_path.read_text(encoding="utf-8"),
            )

    def test_runtime_paths_reject_absolute_parent_and_symlink_escapes(self) -> None:
        parser = paperscript.build_parser()
        for configured_path in ("/tmp/paperscript-outside", "../outside"):
            with self.subTest(path=configured_path), tempfile.TemporaryDirectory() as directory:
                server_dir = Path(directory) / "server"
                runtime_dir = server_dir / "paperscript"
                runtime_dir.mkdir(parents=True)
                config_path = runtime_dir / "config.json"
                raw = json.dumps({"backup_dir": configured_path}) + "\n"
                config_path.write_text(raw, encoding="utf-8")
                args = parser.parse_args(
                    ["--server-dir", str(server_dir), "status"]
                )

                with self.assertRaises(paperscript.PaperScriptError):
                    paperscript.PaperScriptApp(args)

                self.assertEqual(config_path.read_text(encoding="utf-8"), raw)

        with tempfile.TemporaryDirectory() as directory:
            server_dir = Path(directory) / "server"
            runtime_dir = server_dir / "paperscript"
            outside = Path(directory) / "outside"
            runtime_dir.mkdir(parents=True)
            outside.mkdir()
            (runtime_dir / "downloads").symlink_to(outside, target_is_directory=True)
            args = parser.parse_args(["--server-dir", str(server_dir), "status"])

            with self.assertRaises(paperscript.PaperScriptError):
                paperscript.PaperScriptApp(args)

    def test_filesystem_root_cannot_be_a_server_directory(self) -> None:
        parser = paperscript.build_parser()
        args = parser.parse_args(["--server-dir", "/", "status"])

        with self.assertRaisesRegex(paperscript.PaperScriptError, "filesystem root"):
            paperscript.PaperScriptApp(args)


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
            self.assertEqual(saved["staged_channel"], "STABLE")
            self.assertEqual(saved["staged_jar"], "Paper-26.2.jar")
            self.assertEqual(saved["staged_version"], "26.2")
            self.assertEqual(saved["staged_build"], 84)
            self.assertNotIn("current_jar", saved)


class ServerJarRetentionTests(unittest.TestCase):
    def make_app(self, directory: str) -> paperscript.PaperScriptApp:
        app = object.__new__(paperscript.PaperScriptApp)
        app.server_dir = Path(directory)
        app.server_runtime_dir = app.server_dir / "paperscript"
        app.last_launched_jar_marker_path = (
            app.server_runtime_dir / "last-launched-jar.txt"
        )
        app.jar_archive_dir = app.server_runtime_dir / "backups" / "jars"
        app.state_path = app.server_runtime_dir / "state.json"
        app.state = {}
        app.keep_server_jars = 2
        app.keep_archived_jars = 5
        app.logger = RecordingLogger()
        return app

    def write_marker(self, app: paperscript.PaperScriptApp, name: str) -> None:
        app.last_launched_jar_marker_path.parent.mkdir(parents=True, exist_ok=True)
        app.last_launched_jar_marker_path.write_text(name + "\n", encoding="utf-8")

    def test_keeps_last_launched_and_newest_build_and_archives_the_middle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            for build in (9, 10, 11):
                (app.server_dir / f"Paper-26.2-{build}.jar").write_bytes(
                    f"build {build}".encode()
                )
            self.write_marker(app, "Paper-26.2-9.jar")

            plan = app.plan_server_jar_retention("26.2", keep=2)

            self.assertEqual(
                {path.name for path in plan.kept},
                {"Paper-26.2-9.jar", "Paper-26.2-11.jar"},
            )
            self.assertEqual(
                [path.name for path in plan.to_archive],
                ["Paper-26.2-10.jar"],
            )

            app.apply_server_jar_retention(plan)

            self.assertFalse((app.server_dir / "Paper-26.2-10.jar").exists())
            self.assertTrue(
                (app.jar_archive_dir / "26.2" / "Paper-26.2-10.jar").is_file()
            )

    def test_only_exact_same_version_regular_jars_are_managed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            active = app.server_dir / "Paper-26.2-9.jar"
            newest = app.server_dir / "Paper-26.2-11.jar"
            middle = app.server_dir / "paper-26.2-10.JAR"
            for path in (active, newest, middle):
                path.write_bytes(path.name.encode())
            untouched_names = [
                "Paper-26.3-999.jar",
                "Paper-26.2.jar",
                "Paper-26.2-latest.jar",
                "Paper-26.2-12.jar.part",
                "Spigot-26.2-1000.jar",
                "unrelated.jar",
            ]
            for name in untouched_names:
                (app.server_dir / name).write_bytes(name.encode())
            outside = app.server_dir / "outside.jar"
            outside.write_bytes(b"outside")
            symlink = app.server_dir / "Paper-26.2-1000.jar"
            symlink.symlink_to(outside)
            self.write_marker(app, active.name)

            plan = app.plan_server_jar_retention("26.2", keep=2)
            app.apply_server_jar_retention(plan)

            self.assertEqual([path.name for path in plan.to_archive], [middle.name])
            for name in untouched_names:
                self.assertTrue((app.server_dir / name).is_file(), name)
            self.assertTrue(symlink.is_symlink())
            self.assertEqual(outside.read_bytes(), b"outside")

    def test_plan_uses_actual_next_launcher_choice_when_recorded_stage_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            for build in (8, 9, 10):
                (app.server_dir / f"Paper-26.2-{build}.jar").write_bytes(b"paper")
            self.write_marker(app, "Paper-26.2-9.jar")
            staged = app.server_dir / "Paper-26.2-8.jar"

            plan = app.plan_server_jar_retention(
                "26.2",
                keep=2,
                staged_path=staged,
            )

            self.assertEqual(
                {path.name for path in plan.kept},
                {
                    "Paper-26.2-9.jar",
                    "Paper-26.2-10.jar",
                },
            )
            self.assertEqual(
                [path.name for path in plan.to_archive],
                ["Paper-26.2-8.jar"],
            )

    def test_active_launcher_rollback_claim_is_protected_during_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            for build in (84, 85, 86, 87):
                (app.server_dir / f"Paper-26.2-{build}.jar").write_bytes(
                    f"build {build}".encode()
                )
            self.write_marker(app, "Paper-26.2-85.jar")
            launch_lock = app.server_runtime_dir / "locks" / "server-launch"
            launch_lock.mkdir(parents=True)
            (launch_lock / "owner.pid").write_text("12345\n", encoding="utf-8")
            (app.server_runtime_dir / paperscript.LAUNCHER_MARKER_ROLLBACK).write_text(
                "Paper-26.2-84.jar\n",
                encoding="utf-8",
            )

            plan = app.plan_server_jar_retention("26.2", keep=2)

            self.assertEqual(
                {path.name for path in plan.kept},
                {
                    "Paper-26.2-84.jar",
                    "Paper-26.2-85.jar",
                    "Paper-26.2-87.jar",
                },
            )
            self.assertEqual(plan.launch_rollback.name, "Paper-26.2-84.jar")
            self.assertEqual(
                [path.name for path in plan.to_archive],
                ["Paper-26.2-86.jar"],
            )

            app.apply_server_jar_retention(plan)

            self.assertTrue((app.server_dir / "Paper-26.2-84.jar").is_file())
            self.assertTrue((app.server_dir / "Paper-26.2-85.jar").is_file())
            self.assertTrue((app.server_dir / "Paper-26.2-87.jar").is_file())
            self.assertFalse((app.server_dir / "Paper-26.2-86.jar").exists())

    def test_invalid_or_missing_marker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            (app.server_dir / "Paper-26.2-84.jar").write_bytes(b"paper")

            for marker in (None, "../Paper-26.2-84.jar", "Paper-26.3-84.jar"):
                if marker is None:
                    app.last_launched_jar_marker_path.unlink(missing_ok=True)
                else:
                    self.write_marker(app, marker)
                with self.subTest(marker=marker):
                    with self.assertRaises(paperscript.PaperScriptError):
                        app.plan_server_jar_retention("26.2", keep=2)

            self.assertTrue((app.server_dir / "Paper-26.2-84.jar").is_file())

    def test_retention_rejects_fewer_than_two_protection_slots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            (app.server_dir / "Paper-26.2-84.jar").write_bytes(b"paper")
            self.write_marker(app, "Paper-26.2-84.jar")

            with self.assertRaises(paperscript.PaperScriptError):
                app.plan_server_jar_retention("26.2", keep=1)

    def test_dry_run_does_not_move_or_create_any_jar_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            for build in (9, 10, 11):
                (app.server_dir / f"Paper-26.2-{build}.jar").write_bytes(b"paper")
            self.write_marker(app, "Paper-26.2-9.jar")
            plan = app.plan_server_jar_retention("26.2", keep=2)

            app.apply_server_jar_retention(plan, dry_run=True)

            self.assertEqual(
                sorted(path.name for path in app.server_dir.glob("Paper-*.jar")),
                ["Paper-26.2-10.jar", "Paper-26.2-11.jar", "Paper-26.2-9.jar"],
            )
            self.assertFalse(app.jar_archive_dir.exists())

    def test_archive_cap_keeps_the_highest_numeric_builds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            app.keep_archived_jars = 3
            archive = app.jar_archive_dir / "26.2"
            archive.mkdir(parents=True)
            for build in (1, 2, 3):
                (archive / f"Paper-26.2-{build}.jar").write_bytes(b"old")
            for build in (7, 8, 9, 10):
                (app.server_dir / f"Paper-26.2-{build}.jar").write_bytes(b"root")
            self.write_marker(app, "Paper-26.2-10.jar")

            plan = app.plan_server_jar_retention("26.2", keep=2)
            app.apply_server_jar_retention(plan)

            self.assertEqual(
                sorted(path.name for path in archive.iterdir()),
                ["Paper-26.2-7.jar", "Paper-26.2-8.jar", "Paper-26.2-9.jar"],
            )

    def test_dry_run_lists_the_exact_archive_files_that_would_be_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            app.keep_archived_jars = 2
            archive = app.jar_archive_dir / "26.2"
            archive.mkdir(parents=True)
            for build in (1, 2):
                (archive / f"Paper-26.2-{build}.jar").write_bytes(b"old")
            for build in (7, 8, 9):
                (app.server_dir / f"Paper-26.2-{build}.jar").write_bytes(b"root")
            self.write_marker(app, "Paper-26.2-9.jar")

            plan = app.plan_server_jar_retention("26.2", keep=2)
            app.apply_server_jar_retention(plan, dry_run=True)

            self.assertEqual(
                [path.name for path in plan.to_prune],
                ["Paper-26.2-2.jar", "Paper-26.2-1.jar"],
            )
            self.assertTrue(
                all(
                    any(path.name in message and "permanently prune" in message for message in app.logger.messages)
                    for path in plan.to_prune
                )
            )
            self.assertEqual(
                sorted(path.name for path in archive.iterdir()),
                ["Paper-26.2-1.jar", "Paper-26.2-2.jar"],
            )

    def test_archive_directory_sync_failure_keeps_the_root_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            source = app.server_dir / "Paper-26.2-8.jar"
            source.write_bytes(b"root")

            with mock.patch.object(
                paperscript,
                "fsync_directory",
                side_effect=paperscript.PaperScriptError("sync failed"),
            ):
                with self.assertRaisesRegex(paperscript.PaperScriptError, "sync failed"):
                    app.archive_server_jar(source, "26.2")

            self.assertTrue(source.is_file())

    def test_archive_publish_sync_chain_reaches_the_server_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            archive = app.jar_archive_dir / "26.2"

            with mock.patch.object(paperscript, "fsync_directory") as sync_directory:
                app.fsync_archive_publish_chain(archive)

            sync_directory.assert_any_call(app.server_dir, strict=True)

    def test_server_mutations_are_locked_per_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            app.server_lock_path = app.server_runtime_dir / "locks" / "paper-jars.lock"

            with app.server_mutation_lock():
                with self.assertRaises(paperscript.PaperScriptError):
                    with app.server_mutation_lock():
                        self.fail("A second mutation lock should not be acquired")

    def test_legacy_backup_cleanup_preserves_the_managed_jar_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            legacy_backup = app.server_runtime_dir / "backups" / "old.jar"
            archived = app.jar_archive_dir / "26.2" / "Paper-26.2-8.jar"
            archived.parent.mkdir(parents=True)
            legacy_backup.write_bytes(b"legacy")
            archived.write_bytes(b"managed archive")

            removed = app.remove_directory_contents(
                app.server_runtime_dir / "backups",
                excluded_paths=(app.jar_archive_dir,),
            )

            self.assertEqual(removed, 1)
            self.assertFalse(legacy_backup.exists())
            self.assertEqual(archived.read_bytes(), b"managed archive")

    def test_cleanup_aborts_if_the_confirmed_jar_plan_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            app.server_lock_path = app.server_runtime_dir / "locks" / "paper-jars.lock"
            app.state = {}
            app.args = argparse.Namespace(
                cleanup_all=False,
                cleanup_downloads=False,
                cleanup_backups=False,
                cleanup_server_jars=True,
                cleanup_metadata_cache=False,
                cleanup_pycache=False,
                cleanup_logs=False,
                cleanup_json=False,
                cleanup_keep=2,
                cleanup_version="26.2",
                dry_run=False,
                yes=True,
            )
            for build in (9, 10, 11):
                (app.server_dir / f"Paper-26.2-{build}.jar").write_bytes(
                    f"build {build}".encode()
                )
            self.write_marker(app, "Paper-26.2-9.jar")
            approved = app.plan_server_jar_retention("26.2", keep=2)
            changed = paperscript.JarRetentionPlan(
                version="26.2",
                last_launched=approved.last_launched,
                kept=(
                    app.server_dir / "Paper-26.2-9.jar",
                    app.server_dir / "Paper-26.2-10.jar",
                ),
                to_archive=(app.server_dir / "Paper-26.2-11.jar",),
            )

            with mock.patch.object(
                app,
                "plan_server_jar_retention",
                side_effect=(approved, changed),
            ), mock.patch.object(app, "apply_server_jar_retention") as apply_retention:
                with self.assertRaisesRegex(paperscript.PaperScriptError, "changed"):
                    app.run_cleanup()

            apply_retention.assert_not_called()
            for build in (9, 10, 11):
                self.assertTrue((app.server_dir / f"Paper-26.2-{build}.jar").is_file())

    def test_cleanup_reloads_staged_state_under_the_server_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self.make_app(directory)
            app.server_lock_path = app.server_runtime_dir / "locks" / "paper-jars.lock"
            app.state_path = app.server_runtime_dir / "state.json"
            app.args = argparse.Namespace(
                cleanup_all=False,
                cleanup_downloads=False,
                cleanup_backups=False,
                cleanup_server_jars=True,
                cleanup_metadata_cache=False,
                cleanup_pycache=False,
                cleanup_logs=False,
                cleanup_json=False,
                cleanup_keep=2,
                cleanup_version="26.2",
                dry_run=False,
                yes=False,
            )
            for build in (9, 10, 11):
                (app.server_dir / f"Paper-26.2-{build}.jar").write_bytes(
                    f"build {build}".encode()
                )
            self.write_marker(app, "Paper-26.2-9.jar")
            app.state = {
                "staged_jar": "Paper-26.2-11.jar",
                "staged_version": "26.2",
                "server_dir": str(app.server_dir),
            }
            app._save_json(app.state_path, app.state)

            def confirm_after_concurrent_state_change(*args, **kwargs):
                app._save_json(
                    app.state_path,
                    {
                        "staged_jar": "Paper-26.2-11.jar",
                        "staged_version": "26.2",
                        "staged_sha256": "0" * 64,
                        "server_dir": str(app.server_dir),
                    },
                )
                return True

            with mock.patch.object(
                paperscript,
                "prompt_yes_no",
                side_effect=confirm_after_concurrent_state_change,
            ), mock.patch.object(app, "apply_server_jar_retention") as apply_retention:
                with self.assertRaisesRegex(paperscript.PaperScriptError, "no longer matches"):
                    app.run_cleanup()

            apply_retention.assert_not_called()
            self.assertEqual(app.state["staged_jar"], "Paper-26.2-11.jar")
            self.assertEqual(app.state["staged_sha256"], "0" * 64)
            for build in (9, 10, 11):
                self.assertTrue((app.server_dir / f"Paper-26.2-{build}.jar").is_file())


class NonDisruptiveStagingTests(unittest.TestCase):
    def make_app(
        self,
        directory: str,
        content: bytes,
        marker: str | None,
    ) -> tuple[paperscript.PaperScriptApp, paperscript.BuildInfo]:
        app = object.__new__(paperscript.PaperScriptApp)
        app.server_dir = Path(directory)
        app.server_runtime_dir = app.server_dir / "paperscript"
        app.last_launched_jar_marker_path = app.server_runtime_dir / "last-launched-jar.txt"
        app.jar_archive_dir = app.server_runtime_dir / "backups" / "jars"
        app.server_lock_path = app.server_runtime_dir / "locks" / "paper-jars.lock"
        app.keep_server_jars = 2
        app.keep_archived_jars = 5
        app.reconcile_server_jars_after_stage = True
        app.allow_same_version_build_upgrade = True
        app.confirm_before_force_download = True
        app.confirm_before_downgrade = True
        app.args = argparse.Namespace(dry_run=False, force=False, yes=True)
        app.state = {}
        app.state_path = app.server_runtime_dir / "state.json"
        app.logger = RecordingLogger()
        if marker is not None:
            app.server_runtime_dir.mkdir(parents=True)
            app.last_launched_jar_marker_path.write_text(marker + "\n", encoding="utf-8")

        digest = paperscript.hashlib.sha256(content).hexdigest()
        build = paperscript.BuildInfo(
            version="26.2",
            build_id=11,
            channel="STABLE",
            download_name="paper-26.2-11.jar",
            download_url="https://example.invalid/paper-26.2-11.jar",
            sha256=digest,
            size=len(content),
            created_at="2026-08-20T00:00:00Z",
        )

        class FakeAPI:
            def download_file(
                self,
                selected: paperscript.BuildInfo,
                destination: Path,
            ) -> paperscript.DownloadVerification:
                destination.write_bytes(content)
                return paperscript.DownloadVerification(
                    sha256=digest,
                    bytes_written=len(content),
                    elapsed_seconds=0.01,
                )

        app.api = FakeAPI()
        return app, build

    def test_stages_beside_active_jar_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app, build = self.make_app(directory, b"verified new paper jar", "Paper-26.2-9.jar")
            active = app.server_dir / "Paper-26.2-9.jar"
            old_staged = app.server_dir / "Paper-26.2-10.jar"
            active.write_bytes(b"active paper jar")
            old_staged.write_bytes(b"older staged jar")
            active_before = active.stat()
            active_hash = paperscript.sha256_file(active)

            app.stage_build(build)

            self.assertEqual(paperscript.sha256_file(active), active_hash)
            active_after = active.stat()
            self.assertEqual(active_before.st_ino, active_after.st_ino)
            self.assertEqual(active_before.st_mtime_ns, active_after.st_mtime_ns)
            self.assertTrue((app.server_dir / "Paper-26.2-11.jar").is_file())
            self.assertFalse(old_staged.exists())
            self.assertTrue(
                (app.jar_archive_dir / "26.2" / old_staged.name).is_file()
            )
            self.assertEqual(app.state["staged_jar"], "Paper-26.2-11.jar")
            self.assertTrue(
                any("did not stop, start, restart, or signal" in message for message in app.logger.messages)
            )

    def test_staging_never_invokes_process_or_tmux_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app, build = self.make_app(
                directory,
                b"verified new paper jar",
                "Paper-26.2-9.jar",
            )
            (app.server_dir / "Paper-26.2-9.jar").write_bytes(b"active")

            with mock.patch.object(
                paperscript,
                "run_command",
                side_effect=AssertionError("staging attempted an external process command"),
            ):
                app.stage_build(build)

            self.assertTrue((app.server_dir / "Paper-26.2-11.jar").is_file())

    def test_missing_launcher_marker_stages_but_defers_root_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app, build = self.make_app(directory, b"verified new paper jar", marker=None)
            old_names = ["Paper-26.2-9.jar", "Paper-26.2-10.jar"]
            for name in old_names:
                (app.server_dir / name).write_bytes(name.encode())

            app.stage_build(build)

            for name in old_names + ["Paper-26.2-11.jar"]:
                self.assertTrue((app.server_dir / name).is_file())
            self.assertFalse(app.jar_archive_dir.exists())
            self.assertTrue(
                any("retention deferred" in message for message in app.logger.messages)
            )

    def test_target_appearing_during_download_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            content = b"verified new paper jar"
            app, build = self.make_app(directory, content, marker=None)
            target = app.server_dir / "Paper-26.2-11.jar"

            class RacingAPI:
                def download_file(
                    self,
                    selected: paperscript.BuildInfo,
                    destination: Path,
                ) -> paperscript.DownloadVerification:
                    destination.write_bytes(content)
                    target.write_bytes(b"concurrent sentinel")
                    return paperscript.DownloadVerification(
                        sha256=selected.sha256 or "",
                        bytes_written=len(content),
                        elapsed_seconds=0.01,
                    )

            app.api = RacingAPI()

            with self.assertRaises(paperscript.PaperScriptError):
                app.stage_build(build)

            self.assertEqual(target.read_bytes(), b"concurrent sentinel")

    def test_failed_verification_removes_temporary_downloads(self) -> None:
        for failure in ("missing_sha", "size", "checksum"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                content = b"downloaded paper bytes"
                app, build = self.make_app(
                    directory,
                    content,
                    "Paper-26.2-9.jar",
                )
                active = app.server_dir / "Paper-26.2-9.jar"
                active.write_bytes(b"active")
                if failure == "missing_sha":
                    build.sha256 = None
                else:
                    expected_sha = build.sha256 or ""

                    class InvalidVerificationAPI:
                        def download_file(
                            self,
                            selected: paperscript.BuildInfo,
                            destination: Path,
                        ) -> paperscript.DownloadVerification:
                            destination.write_bytes(content)
                            return paperscript.DownloadVerification(
                                sha256=("f" * 64 if failure == "checksum" else expected_sha),
                                bytes_written=(len(content) + 1 if failure == "size" else len(content)),
                                elapsed_seconds=0.01,
                            )

                    app.api = InvalidVerificationAPI()

                with self.assertRaises(paperscript.PaperScriptError):
                    app.stage_build(build)

                self.assertEqual(active.read_bytes(), b"active")
                self.assertFalse((app.server_dir / "Paper-26.2-11.jar").exists())
                self.assertEqual(list(app.server_dir.glob(".*.part")), [])

    def test_partial_download_exception_is_normalized_and_cleans_temp_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app, build = self.make_app(
                directory,
                b"unused",
                "Paper-26.2-9.jar",
            )
            active = app.server_dir / "Paper-26.2-9.jar"
            active.write_bytes(b"active")

            class PartialWriteAPI:
                def download_file(
                    self,
                    selected: paperscript.BuildInfo,
                    destination: Path,
                ) -> paperscript.DownloadVerification:
                    destination.write_bytes(b"partial")
                    raise OSError("simulated disk or stream failure")

            app.api = PartialWriteAPI()

            with self.assertRaisesRegex(paperscript.PaperScriptError, "Download I/O failed"):
                app.stage_build(build)

            self.assertEqual(active.read_bytes(), b"active")
            self.assertFalse((app.server_dir / "Paper-26.2-11.jar").exists())
            self.assertEqual(list(app.server_dir.glob(".*.part")), [])
            self.assertEqual(app.state, {})

    def test_same_family_build_downgrade_is_rejected_even_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app, build = self.make_app(
                directory,
                b"older build",
                "Paper-26.2-9.jar",
            )
            (app.server_dir / "Paper-26.2-9.jar").write_bytes(b"active")
            build.build_id = 8
            build.download_name = "paper-26.2-8.jar"
            build.download_url = "https://example.invalid/paper-26.2-8.jar"
            app.args.force = True

            with self.assertRaisesRegex(paperscript.PaperScriptError, "could not become the next launch"):
                app.stage_build(build)

            self.assertFalse((app.server_dir / "Paper-26.2-8.jar").exists())
            self.assertEqual(
                app.last_launched_jar().path.name,
                "Paper-26.2-9.jar",
            )

    def test_force_redownload_verifies_without_replacing_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            content = b"verified existing paper jar"
            app, build = self.make_app(directory, content, "Paper-26.2-11.jar")
            target = app.server_dir / "Paper-26.2-11.jar"
            target.write_bytes(content)
            before = target.stat()
            app.args.force = True

            app.stage_build(build)

            after = target.stat()
            self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
            self.assertEqual(target.read_bytes(), content)
            self.assertTrue(
                any("Re-downloaded and verified" in message for message in app.logger.messages)
            )


class CleanupSelectionCompatibilityTests(unittest.TestCase):
    def make_app(self, **overrides: object) -> paperscript.PaperScriptApp:
        values = {
            "cleanup_all": False,
            "cleanup_downloads": False,
            "cleanup_backups": False,
            "cleanup_server_jars": False,
            "cleanup_metadata_cache": False,
            "cleanup_keep": None,
            "cleanup_version": None,
            "cleanup_pycache": False,
            "cleanup_logs": False,
            "cleanup_json": False,
        }
        values.update(overrides)
        app = object.__new__(paperscript.PaperScriptApp)
        app.args = argparse.Namespace(**values)
        return app

    def test_existing_cleanup_keep_still_selects_backups(self) -> None:
        selection = self.make_app(cleanup_keep=2).cleanup_selection()
        self.assertTrue(selection["backups"])
        self.assertFalse(selection["server_jars"])

    def test_server_jars_is_explicit_and_not_part_of_all(self) -> None:
        server_selection = self.make_app(
            cleanup_server_jars=True,
            cleanup_keep=2,
        ).cleanup_selection()
        self.assertTrue(server_selection["server_jars"])
        self.assertFalse(server_selection["backups"])

        all_selection = self.make_app(cleanup_all=True).cleanup_selection()
        self.assertFalse(all_selection["server_jars"])

    def test_backup_and_server_jar_targets_cannot_be_combined(self) -> None:
        app = self.make_app(
            cleanup_backups=True,
            cleanup_server_jars=True,
        )
        with self.assertRaises(paperscript.PaperScriptError):
            app.cleanup_selection()

    def test_cleanup_version_requires_server_jars(self) -> None:
        app = self.make_app(cleanup_version="26.2")
        with self.assertRaises(paperscript.PaperScriptError):
            app.cleanup_selection()


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
        app.last_launched_jar = lambda: (_ for _ in ()).throw(
            paperscript.PaperScriptError("marker unavailable")
        )
        app.launcher_jar_selection = lambda: (_ for _ in ()).throw(
            paperscript.PaperScriptError("marker unavailable")
        )
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

    def test_update_follows_launcher_marker_instead_of_newer_staged_family(self) -> None:
        app = object.__new__(paperscript.PaperScriptApp)
        app.find_current_jar = lambda: paperscript.JarInfo(
            Path("Paper-26.3-2.jar"),
            "26.3",
            2,
        )
        app.last_launched_jar = lambda: paperscript.JarInfo(
            Path("Paper-26.2-84.jar"),
            "26.2",
            84,
        )
        overall = build_info(version="26.3", build_id=2)
        expected = build_info(version="26.2", build_id=85)
        app.latest_stable_version = lambda: ("26.3", overall)

        class FakeAPI:
            def get_latest_build(self, version: str, channel: str):
                self.request = (version, channel)
                return expected

        app.api = FakeAPI()
        app.check_latest_channel_only = "STABLE"
        app.allow_same_version_build_upgrade = True
        app.args = argparse.Namespace(force=False, dry_run=False)
        app.logger = RecordingLogger()

        selected = app.choose_target_for_update()

        self.assertIs(selected, expected)
        self.assertEqual(app.api.request, ("26.2", "STABLE"))

    def test_legacy_launcher_marker_keeps_update_on_its_version_family(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = object.__new__(paperscript.PaperScriptApp)
            app.server_dir = Path(directory)
            app.server_runtime_dir = app.server_dir / "paperscript"
            app.last_launched_jar_marker_path = (
                app.server_runtime_dir / paperscript.LAST_LAUNCHED_JAR_MARKER
            )
            app.server_runtime_dir.mkdir()
            legacy = app.server_dir / "paper-26.2.jar"
            legacy.write_bytes(b"legacy")
            app.last_launched_jar_marker_path.write_text(
                legacy.name + "\n", encoding="utf-8"
            )
            app.state = {}
            app.find_current_jar = lambda: paperscript.JarInfo(
                Path("Paper-26.3-2.jar"), "26.3", 2
            )
            overall = build_info(version="26.3", build_id=2)
            expected = build_info(version="26.2", build_id=85)
            app.latest_stable_version = lambda: ("26.3", overall)

            class FakeAPI:
                def get_latest_build(self, version: str, channel: str):
                    self.request = (version, channel)
                    return expected

            app.api = FakeAPI()
            app.check_latest_channel_only = "STABLE"
            app.allow_same_version_build_upgrade = True
            app.args = argparse.Namespace(force=False, dry_run=False)
            app.logger = RecordingLogger()

            selected = app.choose_target_for_update()

            self.assertIs(selected, expected)
            self.assertEqual(app.api.request, ("26.2", "STABLE"))


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
