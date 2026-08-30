from __future__ import annotations

import os
import signal
import shutil
import subprocess
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = PROJECT_ROOT / "1MB-minecraft.sh"
PAPERSCRIPT_PATH = PROJECT_ROOT / "paperscript.sh"


@dataclass(frozen=True)
class LauncherRun:
    completed: subprocess.CompletedProcess[str]
    java_arguments: list[str]
    marker_content: str | None


class OneMbMinecraftJarSelectionTests(unittest.TestCase):
    def run_launcher(
        self,
        jar_names: list[str],
        existing_marker: str | None = None,
        invocation_cwd: Path | None = None,
        jar_symlinks: dict[str, str] | None = None,
        active_launcher_pid: int | None = None,
        ownerless_launcher_lock: bool = False,
        java_exit_code: int = 0,
    ) -> LauncherRun:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        fixture_root = Path(temporary.name)
        server_dir = fixture_root / "server"
        fake_bin = fixture_root / "fake-bin"
        server_dir.mkdir()
        fake_bin.mkdir()
        paperscript_dir = server_dir / "paperscript"
        paperscript_dir.mkdir()
        marker_path = paperscript_dir / "last-launched-jar.txt"
        if existing_marker is not None:
            marker_path.write_text(existing_marker, encoding="utf-8")
        if active_launcher_pid is not None or ownerless_launcher_lock:
            launch_lock = paperscript_dir / "locks" / "server-launch"
            launch_lock.mkdir(parents=True)
            if active_launcher_pid is not None:
                (launch_lock / "owner.pid").write_text(
                    f"{active_launcher_pid}\n", encoding="utf-8"
                )

        launcher = server_dir / LAUNCHER_PATH.name
        shutil.copy2(LAUNCHER_PATH, launcher)
        launcher.chmod(0o755)
        for jar_name in jar_names:
            (server_dir / jar_name).write_bytes(b"test jar placeholder")
        for link_name, target_name in (jar_symlinks or {}).items():
            (server_dir / link_name).symlink_to(target_name)

        java_arguments = fixture_root / "java-arguments.txt"
        fake_java = fake_bin / "java"
        fake_java.write_text(
            """#!/usr/bin/env bash
if [ "${1:-}" = "-version" ]; then
    printf '%s\n' 'openjdk version "26.0.2"' >&2
    exit 0
fi
printf '%s\n' "$@" >"${FAKE_JAVA_ARGS:?}"
exit "${FAKE_JAVA_EXIT:-0}"
""",
            encoding="utf-8",
        )
        fake_java.chmod(0o755)

        environment = dict(os.environ)
        environment["FAKE_JAVA_ARGS"] = str(java_arguments)
        environment["FAKE_JAVA_EXIT"] = str(java_exit_code)
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        result = subprocess.run(
            ["/bin/bash", str(launcher)],
            cwd=invocation_cwd or server_dir,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        arguments = (
            java_arguments.read_text(encoding="utf-8").splitlines()
            if java_arguments.exists()
            else []
        )
        marker_content = (
            marker_path.read_text(encoding="utf-8").strip()
            if marker_path.exists()
            else None
        )
        return LauncherRun(
            completed=result,
            java_arguments=arguments,
            marker_content=marker_content,
        )

    def selected_jar(self, result: LauncherRun) -> str:
        completed = result.completed
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        arguments = result.java_arguments
        self.assertIn("-jar", arguments)
        return arguments[arguments.index("-jar") + 1]

    def test_selects_greatest_numeric_build_for_configured_version(self) -> None:
        result = self.run_launcher(
            [
                "paper-26.2-9.jar",
                "PAPER-26.2-10.JAR",
                "Paper-26.2-latest.jar",
                "Paper-26.2-11.jar.part",
                "Paper-26.3-999.jar",
                "Paper-26.2.jar",
            ]
        )

        self.assertEqual(self.selected_jar(result), "PAPER-26.2-10.JAR")
        self.assertEqual(result.marker_content, "PAPER-26.2-10.JAR")

    def test_uses_legacy_current_version_name_when_no_build_jar_exists(self) -> None:
        result = self.run_launcher(["paper-26.2.jar", "Paper-26.3-999.jar"])

        self.assertEqual(self.selected_jar(result), "paper-26.2.jar")
        self.assertEqual(result.marker_content, "paper-26.2.jar")

    def test_never_selects_a_build_from_another_minecraft_version(self) -> None:
        result = self.run_launcher(["Paper-26.3-999.jar"])

        self.assertNotEqual(result.completed.returncode, 0)
        self.assertEqual(result.java_arguments, [])
        self.assertIsNone(result.marker_content)
        self.assertIn("Paper-26.2.jar", result.completed.stderr)

    def test_replaces_an_old_marker_before_java_starts(self) -> None:
        result = self.run_launcher(
            ["Paper-26.2-84.jar", "Paper-26.2-85.jar"],
            existing_marker="Paper-26.2-84.jar\n",
        )

        self.assertEqual(self.selected_jar(result), "Paper-26.2-85.jar")
        self.assertEqual(result.marker_content, "Paper-26.2-85.jar")

    def test_uses_the_launcher_directory_instead_of_the_callers_directory(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            result = self.run_launcher(
                ["Paper-26.2-84.jar"],
                invocation_cwd=Path(outside),
            )

        self.assertEqual(self.selected_jar(result), "Paper-26.2-84.jar")
        self.assertEqual(result.marker_content, "Paper-26.2-84.jar")

    def test_ignores_a_higher_build_symlink(self) -> None:
        result = self.run_launcher(
            ["Paper-26.2-84.jar"],
            jar_symlinks={"Paper-26.2-999.jar": "Paper-26.2-84.jar"},
        )

        self.assertEqual(self.selected_jar(result), "Paper-26.2-84.jar")
        self.assertEqual(result.marker_content, "Paper-26.2-84.jar")

    def test_concurrent_launcher_is_rejected_before_marker_changes(self) -> None:
        result = self.run_launcher(
            ["Paper-26.2-84.jar", "Paper-26.2-85.jar"],
            existing_marker="Paper-26.2-84.jar\n",
            active_launcher_pid=os.getpid(),
        )

        self.assertNotEqual(result.completed.returncode, 0)
        self.assertEqual(result.java_arguments, [])
        self.assertEqual(result.marker_content, "Paper-26.2-84.jar")
        self.assertIn("launch lock already exists", result.completed.stderr)

    def test_ownerless_launcher_lock_is_never_reclaimed_automatically(self) -> None:
        result = self.run_launcher(
            ["Paper-26.2-84.jar", "Paper-26.2-85.jar"],
            existing_marker="Paper-26.2-84.jar\n",
            ownerless_launcher_lock=True,
        )

        self.assertNotEqual(result.completed.returncode, 0)
        self.assertEqual(result.java_arguments, [])
        self.assertEqual(result.marker_content, "Paper-26.2-84.jar")
        self.assertIn("Refusing automatic recovery", result.completed.stderr)

    def test_dead_wrapper_pid_lock_is_never_reclaimed_automatically(self) -> None:
        result = self.run_launcher(
            ["Paper-26.2-84.jar", "Paper-26.2-85.jar"],
            existing_marker="Paper-26.2-84.jar\n",
            active_launcher_pid=99_999_999,
        )

        self.assertNotEqual(result.completed.returncode, 0)
        self.assertEqual(result.java_arguments, [])
        self.assertEqual(result.marker_content, "Paper-26.2-84.jar")
        self.assertIn("Refusing automatic recovery", result.completed.stderr)

    def test_failed_jvm_start_restores_the_previous_launcher_marker(self) -> None:
        result = self.run_launcher(
            ["Paper-26.2-84.jar", "Paper-26.2-85.jar"],
            existing_marker="Paper-26.2-84.jar\n",
            java_exit_code=1,
        )

        self.assertNotEqual(result.completed.returncode, 0)
        self.assertEqual(result.marker_content, "Paper-26.2-84.jar")
        self.assertIn("Failed to start the jvm", result.completed.stderr)

    def test_first_failed_jvm_start_removes_its_unproven_marker(self) -> None:
        result = self.run_launcher(
            ["Paper-26.2-85.jar"],
            java_exit_code=1,
        )

        self.assertNotEqual(result.completed.returncode, 0)
        self.assertIsNone(result.marker_content)

    def test_live_retention_preserves_marker_rollback_until_failed_java_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            server_dir = fixture_root / "server"
            fake_bin = fixture_root / "fake-bin"
            runtime = server_dir / "paperscript"
            server_dir.mkdir()
            fake_bin.mkdir()
            runtime.mkdir()
            marker = runtime / "last-launched-jar.txt"
            marker.write_text("Paper-26.2-84.jar\n", encoding="utf-8")
            for build in (84, 85):
                (server_dir / f"Paper-26.2-{build}.jar").write_bytes(
                    f"build {build}".encode()
                )

            launcher = server_dir / LAUNCHER_PATH.name
            shutil.copy2(LAUNCHER_PATH, launcher)
            launcher.chmod(0o755)
            release_java = fixture_root / "release-java"
            child_pid_path = fixture_root / "java-child.pid"
            fake_java = fake_bin / "java"
            fake_java.write_text(
                """#!/usr/bin/env bash
if [ "${1:-}" = "-version" ]; then
    printf '%s\n' 'openjdk version "26.0.2"' >&2
    exit 0
fi
printf '%s\n' "$$" >"${FAKE_JAVA_PID:?}"
while [ ! -e "${FAKE_JAVA_RELEASE:?}" ]; do
    sleep 0.02
done
exit 1
""",
                encoding="utf-8",
            )
            fake_java.chmod(0o755)

            environment = dict(os.environ)
            environment["FAKE_JAVA_PID"] = str(child_pid_path)
            environment["FAKE_JAVA_RELEASE"] = str(release_java)
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            launcher_error_path = fixture_root / "launcher.err"
            process: subprocess.Popen[str] | None = None
            try:
                with launcher_error_path.open("w", encoding="utf-8") as launcher_error:
                    process = subprocess.Popen(
                        ["/bin/bash", str(launcher)],
                        cwd=server_dir,
                        env=environment,
                        stdout=subprocess.DEVNULL,
                        stderr=launcher_error,
                        text=True,
                        start_new_session=True,
                    )
                    deadline = time.monotonic() + 5
                    rollback_marker = runtime / ".launcher-marker-rollback"
                    while time.monotonic() < deadline:
                        if (
                            child_pid_path.exists()
                            and marker.read_text(encoding="utf-8").strip()
                            == "Paper-26.2-85.jar"
                            and rollback_marker.exists()
                        ):
                            break
                        time.sleep(0.02)
                    else:
                        self.fail("launcher did not publish its active and rollback marker claims")

                    cleanup = subprocess.run(
                        [
                            str(PAPERSCRIPT_PATH),
                            "--server-dir",
                            str(server_dir),
                            "--yes",
                            "cleanup",
                            "--server-jars",
                        ],
                        cwd=PROJECT_ROOT,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                        timeout=10,
                    )
                    self.assertEqual(
                        cleanup.returncode,
                        0,
                        cleanup.stdout + cleanup.stderr,
                    )
                    self.assertTrue((server_dir / "Paper-26.2-84.jar").is_file())

                    release_java.touch()
                    process.wait(timeout=5)

                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(
                    marker.read_text(encoding="utf-8").strip(),
                    "Paper-26.2-84.jar",
                )
                self.assertTrue((server_dir / "Paper-26.2-84.jar").is_file())
                self.assertFalse(rollback_marker.exists())
            finally:
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_wrapper_signal_preserves_lock_while_java_child_survives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            server_dir = fixture_root / "server"
            fake_bin = fixture_root / "fake-bin"
            server_dir.mkdir()
            fake_bin.mkdir()
            (server_dir / "paperscript").mkdir()
            (server_dir / "Paper-26.2-85.jar").write_bytes(b"test jar placeholder")

            launcher = server_dir / LAUNCHER_PATH.name
            shutil.copy2(LAUNCHER_PATH, launcher)
            launcher.chmod(0o755)

            child_pid_path = fixture_root / "java-child.pid"
            fake_java = fake_bin / "java"
            fake_java.write_text(
                """#!/usr/bin/env bash
if [ "${1:-}" = "-version" ]; then
    printf '%s\n' 'openjdk version "26.0.2"' >&2
    exit 0
fi
printf '%s\n' "$$" >"${FAKE_JAVA_PID:?}"
exec sleep 300
""",
                encoding="utf-8",
            )
            fake_java.chmod(0o755)

            environment = dict(os.environ)
            environment["FAKE_JAVA_PID"] = str(child_pid_path)
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            first_error_path = fixture_root / "first-launch.err"
            process: subprocess.Popen[str] | None = None
            try:
                with first_error_path.open("w", encoding="utf-8") as first_error:
                    process = subprocess.Popen(
                        ["/bin/bash", str(launcher)],
                        cwd=server_dir,
                        env=environment,
                        stdout=subprocess.DEVNULL,
                        stderr=first_error,
                        text=True,
                        start_new_session=True,
                    )
                    deadline = time.monotonic() + 5
                    launch_lock = server_dir / "paperscript" / "locks" / "server-launch"
                    while time.monotonic() < deadline:
                        if child_pid_path.exists() and launch_lock.is_dir():
                            break
                        time.sleep(0.02)
                    else:
                        self.fail("blocking fake Java did not start and acquire the launch lock")

                    os.kill(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)

                child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
                os.kill(child_pid, 0)
                self.assertTrue(launch_lock.is_dir())
                self.assertIn(
                    "lock was preserved",
                    first_error_path.read_text(encoding="utf-8"),
                )

                second = subprocess.run(
                    ["/bin/bash", str(launcher)],
                    cwd=server_dir,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=5,
                )
                self.assertNotEqual(second.returncode, 0)
                self.assertIn("launch lock already exists", second.stderr)
            finally:
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
