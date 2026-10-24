"""Interrupted desktop recovery fixtures; disposable files, no APT or services."""

from __future__ import annotations

from contextlib import ExitStack, nullcontext
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "native/recipes/desktop"
sys.path.insert(0, str(RECIPE))
spec = importlib.util.spec_from_file_location("desktop_recovery_recipe", RECIPE / "desktop.py")
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)
lifecycle = desktop.lifecycle
builder = desktop.builder


class ServicePolicyRecovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="desktop-policy-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy = self.root / "policy-rc.d"
        self.journal = self.root / ".dotfiles-policy-transaction.json"

    def crash(self, phase="installed"):
        # _exit deliberately skips context-manager cleanup, like SIGKILL. The
        # child only touches this test's temporary policy directory.
        source = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
import desktop_lifecycle as lifecycle
policy = Path(sys.argv[2])
phase = sys.argv[3]
replace = os.replace
def interrupted_replace(source, destination):
    replace(source, destination)
    if phase == 'backup' and Path(source) == policy:
        os._exit(23)
lifecycle.os.replace = interrupted_replace
with lifecycle.inhibit_services(policy):
    os._exit(23)
"""
        result = subprocess.run(
            [sys.executable, "-c", source, str(RECIPE), str(self.policy), phase],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        return json.loads(self.journal.read_text())

    def test_crash_restores_exact_regular_policy_content_mode_and_inode(self):
        self.policy.write_text("#!/bin/sh\n# administrator policy\nexit 0\n")
        self.policy.chmod(0o640)
        original = lifecycle.policy_identity(self.policy)
        journal = self.crash()
        self.assertEqual(journal["original"], original)
        self.assertEqual(self.policy.read_text(), "#!/bin/sh\nexit 101\n")
        lifecycle.reconcile_service_policy(self.policy)
        self.assertEqual(lifecycle.policy_identity(self.policy), original)
        self.assertFalse(self.journal.exists())
        self.assertEqual(list(self.root.iterdir()), [self.policy])

    def test_crash_restores_relative_dangling_administrator_symlink(self):
        self.policy.symlink_to("administrator-policy-not-yet-present")
        original = lifecycle.policy_identity(self.policy)
        self.crash()
        lifecycle.reconcile_service_policy(self.policy)
        self.assertEqual(lifecycle.policy_identity(self.policy), original)
        self.assertFalse(self.journal.exists())

    def test_crash_without_original_policy_removes_only_recorded_inhibitor(self):
        self.crash()
        lifecycle.reconcile_service_policy(self.policy)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_crash_between_backup_and_replacement_restores_original(self):
        self.policy.write_text("administrator policy")
        original = lifecycle.policy_identity(self.policy)
        self.crash("backup")
        self.assertFalse(self.policy.exists())
        lifecycle.reconcile_service_policy(self.policy)
        self.assertEqual(lifecycle.policy_identity(self.policy), original)
        self.assertFalse(self.journal.exists())

    def test_foreign_policy_replacement_preserves_replacement_backup_and_journal(self):
        for kind in ("regular", "symlink", "same-script-new-inode"):
            with self.subTest(kind=kind):
                directory = self.root / kind
                directory.mkdir()
                self.policy = directory / "policy-rc.d"
                self.journal = directory / ".dotfiles-policy-transaction.json"
                self.policy.write_text("original policy")
                journal = self.crash()
                backup = directory / (".dotfiles-policy-" + journal["transaction"] + ".backup")
                replacement = directory / "replacement"
                if kind == "symlink":
                    replacement.symlink_to("new-administrator-policy")
                else:
                    replacement.write_text(
                        "#!/bin/sh\nexit 101\n" if kind == "same-script-new-inode" else "new policy"
                    )
                    replacement.chmod(0o755)
                os.replace(replacement, self.policy)
                foreign = lifecycle.policy_identity(self.policy)
                before = self.journal.read_bytes()
                with self.assertRaisesRegex(ValueError, "Foreign service policy replacement"):
                    lifecycle.reconcile_service_policy(self.policy)
                self.assertEqual(lifecycle.policy_identity(self.policy), foreign)
                self.assertEqual(lifecycle.policy_identity(backup), journal["original"])
                self.assertEqual(self.journal.read_bytes(), before)

    def test_next_apt_reconciles_crashed_policy_before_simulation(self):
        self.policy.write_text("administrator policy")
        original = lifecycle.policy_identity(self.policy)
        self.crash()
        reconcile = lifecycle.reconcile_service_policy
        inhibit = lifecycle.inhibit_services
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if "--simulate" in argv:
                self.assertEqual(lifecycle.policy_identity(self.policy), original)
            else:
                self.assertEqual(self.policy.read_text(), "#!/bin/sh\nexit 101\n")
            return ""

        with (
            patch.object(
                lifecycle,
                "reconcile_service_policy",
                side_effect=lambda *args: reconcile(self.policy),
            ),
            patch.object(lifecycle, "inhibit_services", side_effect=lambda: inhibit(self.policy)),
            patch.object(lifecycle, "run", side_effect=fake_run),
        ):
            lifecycle.apt(["install", "fixture-package"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(lifecycle.policy_identity(self.policy), original)
        self.assertFalse(self.journal.exists())


class PpaInterruptedUpgrade(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="desktop-ppa-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lock = desktop.load_lock()
        self.selected = {
            name: self.lock["ppa"]["scope"][name] for name in self.lock["ppa"]["install"]
        }
        self.old = {name: version + ".previous" for name, version in self.selected.items()}
        self.installed = dict(self.old)
        self.previous = {"versions": self.old, "files": {}, "payload": {}}
        self.pending = self.root / "ppa-pending.json"
        self.metadata_failure = False
        self.partial_failure = False
        self.apt_calls = []
        self.files = {name: self.root / (name + ".apt") for name in ("key", "source", "pin")}
        stack = self.enterContext(ExitStack())
        replacements = {
            "STATE": self.root,
            "PPA_FILES": self.files,
            "mutation": lambda: nullcontext(),
            "noble": lambda context: None,
            "read_record": lambda package_id: self.previous,
            "record_path": lambda package_id: self.root / (package_id + ".json"),
            "installed_version": lambda name: self.installed.get(name),
            "hypridle_global_links": lambda: [],
            "suppress_new_hypridle_enablement": lambda: None,
            "require_ppa_versions": lambda lock: None,
            "installed_package_payload": lambda names: {},
            "inspect_elf": lambda *args: None,
            "owner": lambda path: path.name,
            "apt": self.fake_apt,
            "run": self.fake_run,
        }
        for name, value in replacements.items():
            stack.enter_context(patch.object(lifecycle, name, value))
        real_digest = lifecycle.digest
        stack.enter_context(
            patch.object(
                lifecycle,
                "digest",
                side_effect=lambda path: (
                    "0" * 64 if str(path).startswith("/usr/bin/") else real_digest(path)
                ),
            )
        )
        stack.enter_context(
            patch(
                "urllib.request.urlopen",
                side_effect=lambda *args, **kwargs: io.BytesIO(b"fixture key"),
            )
        )

    def fake_run(self, argv, **kwargs):
        if argv[:2] == ["apt-get", "update"] and self.metadata_failure:
            raise RuntimeError("metadata refresh failed")
        if argv[0] == "gpg":
            if "--show-keys" in argv:
                return "pub::::::::::\nfpr:::::::::" + self.lock["ppa"]["fingerprint"] + ":\n"
            Path(argv[argv.index("--output") + 1]).write_bytes(b"fixture binary key")
        if str(argv[0]).startswith("/usr/bin/"):
            return self.selected[Path(argv[0]).name].split("-", 1)[0]
        return ""

    def fake_apt(self, argv):
        self.apt_calls.append(argv)
        if self.partial_failure:
            name = next(iter(self.selected))
            self.installed[name] = self.selected[name]
            raise RuntimeError("partial installation")
        self.installed.update(self.selected)

    def test_failed_metadata_refresh_preserves_previous_versions_for_recovery(self):
        self.metadata_failure = True
        with self.assertRaisesRegex(RuntimeError, "metadata refresh failed"):
            lifecycle.provision_ppa({}, self.lock)
        pending = json.loads(self.pending.read_text())
        self.assertEqual(pending["previous_versions"], self.old)
        self.assertEqual(pending["versions"], self.selected)
        self.assertEqual(self.installed, self.old)
        self.metadata_failure = False
        recovered = lifecycle.provision_ppa({}, self.lock, resume=True)
        self.assertEqual(recovered["versions"], self.selected)
        self.assertFalse(self.pending.exists())
        self.assertIn("--reinstall", self.apt_calls[-1])

    def test_mixed_previous_candidate_versions_resume_without_adopting_other_versions(self):
        self.partial_failure = True
        with self.assertRaisesRegex(RuntimeError, "partial installation"):
            lifecycle.provision_ppa({}, self.lock)
        self.assertTrue(any(self.installed[n] == self.old[n] for n in self.old))
        self.assertTrue(any(self.installed[n] == self.selected[n] for n in self.old))
        pending = json.loads(self.pending.read_text())
        self.assertEqual(pending["previous_versions"], self.old)
        self.partial_failure = False
        recovered = lifecycle.provision_ppa({}, self.lock, resume=True)
        self.assertEqual(recovered["versions"], self.selected)
        self.assertFalse(self.pending.exists())

    def test_foreign_version_refuses_recovery_without_rewriting_journal_or_apt_files(self):
        self.metadata_failure = True
        with self.assertRaises(RuntimeError):
            lifecycle.provision_ppa({}, self.lock)
        self.installed[next(iter(self.selected))] = "99.0-foreign"
        before = self.pending.read_bytes()
        files = {path: path.read_bytes() for path in self.files.values()}
        with self.assertRaisesRegex(ValueError, "ownership handoff"):
            lifecycle.provision_ppa({}, self.lock, resume=True)
        self.assertEqual(self.pending.read_bytes(), before)
        self.assertEqual({path: path.read_bytes() for path in self.files.values()}, files)
        self.assertEqual(self.apt_calls, [])


class RuntimeSelectionRecovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="desktop-runtime-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.launchers = self.root / "launchers"
        self.launchers.mkdir()
        deb = self.root / "previous.deb"
        deb.write_bytes(b"previous native artifact")
        self.previous = {
            "id": "dux-runtime",
            "package": "fixture-previous",
            "version": "1",
            "deb": str(deb),
            "deb_sha256": builder.digest(deb),
            "payload": {},
            "selection": "previous",
        }
        self.candidate = {
            "id": "dux-runtime",
            "package": "fixture-candidate",
            "version": "2",
            "payload": {"opt/dotfiles/dux/candidate/bin/dms": {"sha256": "0" * 64, "mode": 0o755}},
        }
        self.journal = self.state / "journal.json"
        self.journal.write_text(
            json.dumps({"candidate": self.candidate, "previous": self.previous})
        )
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(builder, "noble"))
        stack.enter_context(patch.object(lifecycle, "mutation", return_value=nullcontext()))
        stack.enter_context(patch.object(lifecycle, "STATE", self.state))
        stack.enter_context(patch.object(lifecycle, "RUNTIME", self.runtime))
        stack.enter_context(
            patch.object(
                lifecycle,
                "record_path",
                side_effect=lambda package_id: self.state / (package_id + ".json"),
            )
        )
        self.apt = stack.enter_context(patch.object(lifecycle, "apt"))
        self.verify = stack.enter_context(patch.object(lifecycle, "verify_payload"))
        stack.enter_context(patch.object(builder, "run", return_value=self.candidate["version"]))

        def fixture_path(value):
            if str(value) == "/usr/local/bin":
                return self.launchers
            if str(value) == "/":
                return self.root / "payload"
            return Path(value)

        stack.enter_context(patch.object(lifecycle, "Path", side_effect=fixture_path))
        stack.enter_context(patch.object(desktop, "Path", side_effect=fixture_path))

    def test_foreign_current_regular_or_symlink_refuses_before_any_recovery_mutation(self):
        for kind in ("regular", "symlink"):
            with self.subTest(kind=kind):
                current = self.runtime / "current"
                if kind == "regular":
                    current.write_text("administrator replacement")
                else:
                    current.symlink_to("administrator-selection")
                before = lifecycle.policy_identity(current)
                journal = self.journal.read_bytes()
                with self.assertRaisesRegex(ValueError, "Foreign runtime selection"):
                    desktop.recover({}, "dux-runtime")
                self.assertEqual(lifecycle.policy_identity(current), before)
                self.assertEqual(self.journal.read_bytes(), journal)
                self.assertFalse((self.state / "dux-runtime.json").exists())
                self.apt.assert_not_called()
                self.verify.assert_not_called()
                self.assertEqual(list(self.launchers.iterdir()), [])
                current.unlink()

    def test_foreign_launcher_refuses_before_apt_or_pointer_change(self):
        current = self.runtime / "current"
        current.symlink_to("candidate")
        launcher = self.launchers / "dms"
        launcher.write_text("administrator launcher")
        with self.assertRaisesRegex(ValueError, "Foreign launcher"):
            desktop.recover({}, "dux-runtime")
        self.apt.assert_not_called()
        self.assertEqual(os.readlink(current), "candidate")
        self.assertEqual(launcher.read_text(), "administrator launcher")
        self.assertTrue(self.journal.exists())

    def test_first_install_foreign_selection_refuses_before_candidate_cleanup(self):
        self.journal.write_text(json.dumps({"candidate": self.candidate, "previous": None}))
        current = self.runtime / "current"
        current.symlink_to("administrator-selection")
        before = self.journal.read_bytes()
        with self.assertRaisesRegex(ValueError, "Foreign runtime selection"):
            desktop.recover({}, "dux-runtime")
        self.apt.assert_not_called()
        self.verify.assert_not_called()
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(os.readlink(current), "administrator-selection")

    def test_absent_previous_and_candidate_selections_recover_narrowly(self):
        for selection in (None, "previous", "candidate"):
            with self.subTest(selection=selection):
                current = self.runtime / "current"
                if selection is not None:
                    current.symlink_to(selection)
                original_journal = {
                    "candidate": copy.deepcopy(self.candidate),
                    "previous": self.previous,
                }
                self.journal.write_text(json.dumps(original_journal))
                sentinel = self.root / "application-data"
                sentinel.write_text("preserve mutable state")
                result = desktop.recover({}, "dux-runtime")
                self.assertEqual(result["version"], "1")
                self.assertEqual(result["services"], "not restarted")
                self.assertEqual(os.readlink(current), "previous")
                for name, target in lifecycle.LAUNCHERS.items():
                    self.assertEqual(os.readlink(self.launchers / name), target)
                self.assertFalse(self.journal.exists())
                self.assertEqual(
                    json.loads((self.state / "dux-runtime.json").read_text()), self.previous
                )
                self.assertEqual(sentinel.read_text(), "preserve mutable state")
                self.assertEqual(
                    self.apt.call_args_list[-1].args[0], ["remove", "fixture-candidate"]
                )
                current.unlink()


if __name__ == "__main__":
    unittest.main()
