"""Exact-receipt appearance recovery with disposable links and source generations."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "native/recipes/desktop"
sys.path.insert(0, str(RECIPE))
spec = importlib.util.spec_from_file_location(
    "appearance_recovery_recipe", RECIPE / "desktop_appearance.py"
)
appearance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(appearance)


class Interrupted(BaseException):
    """Model termination without allowing ordinary exception cleanup to hide it."""


class AppearanceRecovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="appearance-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.context = {
            "home": str(self.home),
            "platform": "linux-x64",
            "paths": {
                "state": str(self.root / "state"),
                "runtime": str(self.root / "runtime"),
            },
        }
        self.package = "nordic-theme"
        self.state = self.root / "state/desktop-appearance"
        self.receipt = self.state / (self.package + ".json")
        self.pending = self.state / (self.package + "-pending.json")
        self.output = self.root / "build"

    def artifact(self, version, links):
        lock = {self.package: {"version": version}}
        stage = self.output / version / "stage"
        for relative in links:
            resource = stage / relative
            resource.mkdir(parents=True)
            (resource / "content").write_text(version + ":" + relative)
        record = {
            "id": self.package,
            "version": version,
            "stage": str(stage),
            "lock_sha256": appearance.lock_digest(lock),
            "payload": appearance.payload_manifest(stage),
            "links": links,
        }
        receipt = stage.parent / "receipt.json"
        receipt.write_text(json.dumps(record))
        return lock, receipt

    def provision(self, artifact):
        lock, receipt = artifact
        with patch.object(appearance, "assemble", return_value=receipt):
            return appearance.reconcile(self.context, lock, self.package, "provision", self.output)

    def recover(self):
        # Recovery must use the journal's versions, not the caller's newer lock.
        return appearance.reconcile(self.context, {}, self.package, "recover", self.output)

    def target(self, name):
        return self.home / ".local/share/themes" / name

    def read_pending(self):
        return json.loads(self.pending.read_text())

    def interrupt_links(self, artifact, completed):
        actual = appearance.atomic_link
        calls = 0

        def interrupt(target, path):
            nonlocal calls
            transaction = self.read_pending()
            self.assertEqual(
                transaction["candidate"]["version"], artifact[0][self.package]["version"]
            )
            if completed == 0:
                raise Interrupted()
            actual(target, path)
            calls += 1
            if calls == completed:
                raise Interrupted()

        with patch.object(appearance, "atomic_link", side_effect=interrupt):
            with self.assertRaises(Interrupted):
                self.provision(artifact)

    def assert_selection(self, record):
        self.assertEqual(json.loads(self.receipt.read_text()), record)
        for relative, target in appearance.destinations(self.context, record):
            self.assertEqual(os.readlink(target), str(Path(record["stage"]) / relative))
            self.assertEqual((target / "content").read_text(), record["version"] + ":" + relative)
        appearance.verify(record)

    def upgrade(self):
        previous = self.provision(self.artifact("old", ["themes/Common", "themes/Removed"]))
        candidate = self.artifact("new", ["themes/Common", "themes/Added"])
        self.interrupt_links(candidate, 1)
        return previous, candidate

    def test_journal_precedes_first_link_and_first_receipt(self):
        self.interrupt_links(self.artifact("first", ["themes/Common", "themes/Added"]), 0)
        transaction = self.read_pending()
        self.assertIsNone(transaction["previous"])
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.target("Common").is_symlink())
        self.assertEqual(self.recover(), {"recovered": self.package, "selection": None})
        self.assertFalse(self.pending.exists())
        self.assertEqual(self.recover(), {"recovered": self.package, "selection": None})
        appearance.verify(transaction["candidate"])

    def test_partial_first_install_rolls_back_and_retry_installs(self):
        artifact = self.artifact("first", ["themes/Common", "themes/Added"])
        self.interrupt_links(artifact, 1)
        transaction = self.read_pending()
        self.assertTrue(self.target("Common").is_symlink())
        self.assertFalse(self.receipt.exists())
        self.recover()
        self.assertFalse(self.target("Common").is_symlink())
        self.assertFalse(self.target("Added").is_symlink())
        self.assertFalse(self.receipt.exists())
        appearance.verify(transaction["candidate"])
        self.assert_selection(self.provision(artifact))

    def test_mixed_generation_upgrade_restores_exact_previous_selection(self):
        previous, _ = self.upgrade()
        transaction = self.read_pending()
        self.assertNotEqual(
            os.readlink(self.target("Common")), str(Path(previous["stage"]) / "themes/Common")
        )
        self.assertEqual(
            os.readlink(self.target("Removed")), str(Path(previous["stage"]) / "themes/Removed")
        )
        self.assertEqual(self.recover(), previous)
        self.assert_selection(previous)
        self.assertFalse(self.target("Added").is_symlink())
        appearance.verify(transaction["candidate"])
        self.assertEqual(self.recover(), previous)

    def test_provision_retry_recovers_pending_upgrade_before_replacing_it(self):
        previous, candidate = self.upgrade()
        selected = self.provision(candidate)
        self.assert_selection(selected)
        self.assertFalse(self.pending.exists())
        self.assertFalse(self.target("Removed").is_symlink())
        appearance.verify(previous)
        self.assertIn(previous["stage"], {record["stage"] for record in selected["retained"]})

    def test_published_receipt_commits_candidate_despite_partial_links(self):
        previous = self.provision(self.artifact("old", ["themes/Common", "themes/Removed"]))
        candidate = self.artifact("new", ["themes/Common", "themes/Added"])
        publish = appearance.atomic_json

        def interrupted_publication(path, record):
            publish(path, record)
            if path == self.receipt:
                raise Interrupted()

        with patch.object(appearance, "atomic_json", side_effect=interrupted_publication):
            with self.assertRaises(Interrupted):
                self.provision(candidate)
        transaction = self.read_pending()
        committed = transaction["candidate"]
        self.assertEqual(json.loads(self.receipt.read_text()), committed)
        # Model link directory entries surviving interruption at different points.
        appearance.atomic_link(
            str(Path(previous["stage"]) / "themes/Common"), self.target("Common")
        )
        self.target("Added").unlink()
        appearance.atomic_link(
            str(Path(previous["stage"]) / "themes/Removed"), self.target("Removed")
        )
        self.assertEqual(self.recover(), committed)
        self.assert_selection(committed)
        self.assertFalse(self.target("Removed").is_symlink())
        self.assertEqual(self.recover(), committed)
        appearance.verify(previous)

    def test_interrupted_rollback_can_be_recovered_repeatedly(self):
        previous, _ = self.upgrade()
        transaction = self.read_pending()
        added = self.target("Added")
        appearance.atomic_link(str(Path(transaction["candidate"]["stage"]) / "themes/Added"), added)
        actual = appearance.atomic_link

        def interrupted_restore(target, path):
            actual(target, path)
            raise Interrupted()

        with patch.object(appearance, "atomic_link", side_effect=interrupted_restore):
            with self.assertRaises(Interrupted):
                self.recover()
        self.assertTrue(self.pending.exists())
        self.assertEqual(self.recover(), previous)
        self.assert_selection(previous)
        self.assertFalse(added.is_symlink())
        self.assertEqual(self.recover(), previous)

    def test_foreign_regular_file_or_symlink_blocks_all_recovery_writes(self):
        previous, _ = self.upgrade()
        replacement = self.target("Removed")
        foreign = self.root / "foreign-content"
        foreign.write_text("keep exact user content")
        for kind in ("file", "symlink"):
            with self.subTest(kind=kind):
                replacement.unlink()
                if kind == "file":
                    replacement.write_bytes(b"keep exact replacement\x00\xff")
                else:
                    # A familiar runtime prefix is not ownership evidence.
                    replacement.symlink_to(Path(previous["stage"]) / "themes/unrecorded")
                snapshot = (
                    self.pending.read_bytes(),
                    self.receipt.read_bytes(),
                    os.readlink(self.target("Common")),
                )
                with (
                    patch.object(appearance, "atomic_link") as link,
                    patch.object(appearance, "atomic_json") as publish,
                ):
                    with self.assertRaisesRegex(ValueError, "Foreign appearance replacement"):
                        self.recover()
                    link.assert_not_called()
                    publish.assert_not_called()
                self.assertEqual(
                    snapshot,
                    (
                        self.pending.read_bytes(),
                        self.receipt.read_bytes(),
                        os.readlink(self.target("Common")),
                    ),
                )
                if kind == "file":
                    self.assertEqual(replacement.read_bytes(), b"keep exact replacement\x00\xff")
                else:
                    self.assertEqual(
                        os.readlink(replacement), str(Path(previous["stage"]) / "themes/unrecorded")
                    )
                self.assertEqual(foreign.read_text(), "keep exact user content")

    def test_foreign_receipt_blocks_recovery_before_link_mutation(self):
        previous, _ = self.upgrade()
        foreign = dict(previous, version="foreign")
        self.receipt.write_text(json.dumps(foreign))
        before = os.readlink(self.target("Common"))
        with self.assertRaisesRegex(ValueError, "Foreign appearance receipt"):
            self.recover()
        self.assertEqual(os.readlink(self.target("Common")), before)
        self.assertEqual(json.loads(self.receipt.read_text()), foreign)
        self.assertTrue(self.pending.exists())

    def test_pending_transition_blocks_check_and_retirement(self):
        previous, _ = self.upgrade()
        for action in ("check", "retire"):
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(ValueError, "requires recovery"),
            ):
                appearance.reconcile(self.context, {}, self.package, action, self.output)
        appearance.verify(previous)
        self.assertTrue(self.pending.exists())


if __name__ == "__main__":
    unittest.main()
