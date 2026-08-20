from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = PROJECT_ROOT / "1MB-minecraft.sh"


@dataclass(frozen=True)
class LauncherRun:
    completed: subprocess.CompletedProcess[str]
    java_arguments: list[str]


class OneMbMinecraftJarSelectionTests(unittest.TestCase):
    def run_launcher(self, jar_names: list[str]) -> LauncherRun:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        fixture_root = Path(temporary.name)
        server_dir = fixture_root / "server"
        fake_bin = fixture_root / "fake-bin"
        server_dir.mkdir()
        fake_bin.mkdir()

        launcher = server_dir / LAUNCHER_PATH.name
        shutil.copy2(LAUNCHER_PATH, launcher)
        launcher.chmod(0o755)
        for jar_name in jar_names:
            (server_dir / jar_name).write_bytes(b"test jar placeholder")

        java_arguments = fixture_root / "java-arguments.txt"
        fake_java = fake_bin / "java"
        fake_java.write_text(
            """#!/usr/bin/env bash
if [ "${1:-}" = "-version" ]; then
    printf '%s\n' 'openjdk version "26.0.2"' >&2
    exit 0
fi
printf '%s\n' "$@" >"${FAKE_JAVA_ARGS:?}"
""",
            encoding="utf-8",
        )
        fake_java.chmod(0o755)

        environment = dict(os.environ)
        environment["FAKE_JAVA_ARGS"] = str(java_arguments)
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        result = subprocess.run(
            ["/bin/bash", str(launcher)],
            cwd=server_dir,
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
        return LauncherRun(completed=result, java_arguments=arguments)

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

    def test_uses_legacy_current_version_name_when_no_build_jar_exists(self) -> None:
        result = self.run_launcher(["paper-26.2.jar", "Paper-26.3-999.jar"])

        self.assertEqual(self.selected_jar(result), "paper-26.2.jar")

    def test_never_selects_a_build_from_another_minecraft_version(self) -> None:
        result = self.run_launcher(["Paper-26.3-999.jar"])

        self.assertNotEqual(result.completed.returncode, 0)
        self.assertEqual(result.java_arguments, [])
        self.assertIn("Paper-26.2.jar", result.completed.stderr)


if __name__ == "__main__":
    unittest.main()
