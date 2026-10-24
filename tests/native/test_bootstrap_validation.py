"""Shell preflight is acquisition-free; valid invocations retain real dispatch."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

PUBLIC = Path(__file__).resolve().parents[2]
NATIVE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"


class BootstrapValidation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bootstrap-validation-")
        self.addCleanup(temporary.cleanup)
        self.top = Path(temporary.name).resolve()
        self.home = self.top / "absent-home"
        self.selected = self.top / "disposable-home"
        self.bin = self.top / "bin"
        self.bin.mkdir()
        self.marker = self.top / "acquisition"
        for name in ("curl", "git", "mkdir", "mktemp", "tar", "mv"):
            self.executable(
                name,
                f"printf '%s\\n' {shlex.quote(name)} >> {shlex.quote(str(self.marker))}\n"
                + (
                    "exit 79\n"
                    if name in ("curl", "git")
                    else f'exec {shlex.quote(shutil.which(name, path=NATIVE_PATH))} "$@"\n'
                ),
            )
        self.executable("uname", 'case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac\n')
        self.entry = self.top / "bootstrap"
        self.install_entry()

    def executable(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\nset -eu\n" + body)
        path.chmod(0o755)

    def install_entry(self, *, engine=False):
        text = (PUBLIC / "bootstrap").read_text()
        text = text.replace(f"PATH={NATIVE_PATH}", f"PATH={self.bin}:{NATIVE_PATH}")
        candidates = shlex.quote(sys.executable) if engine else str(self.top / "no-python")
        text = text.replace("/usr/bin/python3 /opt/homebrew/bin/python3", candidates)
        if engine:
            shutil.copytree(
                PUBLIC / "native",
                self.top / "native",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        self.entry.write_text(text)

    def invoke(self, *arguments):
        return subprocess.run(
            ["/bin/sh", "-p", str(self.entry), *map(str, arguments)],
            env={"HOME": str(self.home), "PATH": NATIVE_PATH, "LC_ALL": "C"},
            text=True,
            capture_output=True,
            timeout=30,
        )

    def assert_pure_failure(self, *arguments):
        result = self.invoke(*arguments)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.marker.exists(), result.stderr)
        self.assertFalse(self.home.exists(), result.stderr)
        self.assertFalse(self.selected.exists(), result.stderr)
        return result

    def test_invalid_cli_never_acquires_prerequisites(self):
        invalid = [
            ("bootstrap", "--yes", "--unknown"),
            ("not-an-action",),
            ("bootstrap", "start", "--yes"),
            ("bootstrap", "--yes", "--platform", "invalid"),
            ("bootstrap", "--yes", "--platform=invalid"),
            ("bootstrap", "--yes", "--platform"),
            ("bootstrap", "--yes", "--set", "dev.enable=perhaps"),
            ("bootstrap", "--yes", "--set==true"),
            ("bootstrap", "--yes", "--set", "dev..enable=false"),
            ("bootstrap", "--yes", "--set", "dotfiles.dev.enable=true"),
            ("bootstrap", "--yes", "--fixture-context", "/absent/context.json"),
            ("bootstrap", "--yes", "--fixture-context="),
            ("bootstrap", "--yes", "--bootstrap-source="),
            ("bootstrap", "--yes", "--bootstrap-source", "/absent/source"),
            ("bootstrap", "--yes", "--base", "/absent/base"),
            ("bootstrap", "--yes", "--output", "/absent/stage"),
            ("bootstrap", "--yes", "--state-dir", "/absent/state"),
            ("plan", "--verification-tools"),
            ("services", "invalid"),
            ("source", "prepare", "--family=invalid", "--yes"),
            ("source", "prepare", "--family=mise", "--qt-version=6.11", "--yes"),
            ("source", "status", "--branch=branch"),
            ("source", "publish", "--yes", "--message", "-not-a-value"),
            ("work", "docker-access", "--uid=abc", "--yes"),
            ("verify", "--files-from=/absent/files.json"),
            ("verify", "--checks", "--id=package"),
            ("update", "--yes"),
            ("update", "--candidate=bundle", "--promote=id", "--yes"),
            ("desktop", "candidate", "--yes"),
            ("recover", "--packages", "--candidate=bundle", "--yes"),
            ("bootstrap",),
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assert_pure_failure(*arguments)

    def test_help_without_python_or_home_is_acquisition_free(self):
        for arguments in ((), ("--help",), ("bootstrap", "--help")):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("bootstrap", result.stdout)
                self.assertFalse(self.marker.exists())
                self.assertFalse(self.home.exists())

    def test_unsafe_homes_are_rejected_before_acquisition(self):
        link = self.top / "home-link"
        link.symlink_to(self.home)
        non_directory = self.top / "file"
        non_directory.write_text("foreign\n")
        for home in (
            "/",
            "//",
            "relative",
            str(self.top / ".." / "escape"),
            str(link),
            str(non_directory / "child"),
        ):
            with self.subTest(home=home):
                self.assert_pure_failure("bootstrap", "--yes", "--home", home)
        for home in (self.home, self.top):
            with self.subTest(verification_home=home):
                self.assert_pure_failure(
                    "bootstrap", "--yes", "--verification-only", "--home", home
                )

    def test_valid_bootstrap_reaches_acquisition_and_propagates_its_failure(self):
        result = self.invoke(
            "bootstrap",
            "--yes",
            "--verification-only",
            "--home",
            self.selected,
            "--platform=linux-x64",
            "--verification-tools",
            "--set=apps.yazi.enableZshIntegration=false",
        )
        self.assertEqual(result.returncode, 79, result.stderr)
        self.assertIn("curl", self.marker.read_text().splitlines())
        self.assertFalse(
            (self.selected / ".local/share/dotfiles/runtime/bootstrap-python/3.13.17").exists()
        )
        self.assertFalse(self.home.exists())

    def test_valid_non_enable_boolean_overrides_reach_real_engine(self):
        self.install_entry(engine=True)
        result = self.invoke(
            "plan",
            "--home",
            self.selected,
            "--platform=linux-x64",
            "--set=apps.yazi.enableZshIntegration=false",
            "--set=shell.git.lazygitZshIntegration=true",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["context"]
        self.assertFalse(context["dotfiles"]["apps"]["yazi"]["enableZshIntegration"])
        self.assertTrue(context["dotfiles"]["shell"]["git"]["lazygitZshIntegration"])
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.home.exists())
        self.assertFalse(self.selected.exists())


if __name__ == "__main__":
    unittest.main()
