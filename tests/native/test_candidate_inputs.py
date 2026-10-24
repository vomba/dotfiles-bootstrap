"""Private application dependency candidates retain coherent verified source inputs."""

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

PUBLIC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PUBLIC / "native/engine"))
import manifest
import safety
import updates


def sha(content):
    return hashlib.sha256(content).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())


class ApplicationCandidateInputs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="application-candidate-")
        self.addCleanup(temporary.cleanup)
        self.top = Path(temporary.name).resolve()
        self.base, self.root, self.home = (self.top / p for p in ("public", "private", "home"))
        save(
            self.base / "native/manifests/base.json",
            {"schema_version": 1, "defaults": {"dotfiles": {}}},
        )
        (self.base / "native/engine").mkdir()
        shutil.copyfile(PUBLIC / "native/engine/pins.json", self.base / "native/engine/pins.json")
        save(self.root / "native/fixture.py", b"# An unexecuted recipe source fixture.\n")
        self.original = {
            "package.json": b'{"name":"fixture","version":"1"}\n',
            "pnpm-lock.yaml": b"lockfileVersion: '9.0'\n",
        }
        for name, content in self.original.items():
            save(self.root / "apps/fixture" / name, content)
        self.fragment = self.description(self.original)
        save(self.root / "native/manifests/app.json", self.fragment)
        self.context = manifest.compose(self.base, self.root, self.home, "linux-x64")

    def description(self, inputs):
        return {
            "schema_version": 1,
            "packages": [
                {
                    "id": "fixture-app",
                    "provider": "recipe",
                    "path": "native/fixture.py",
                    "version": "1",
                    "lock_inputs": [
                        {"path": "apps/fixture/" + name, "sha256": sha(content)}
                        for name, content in inputs.items()
                    ],
                }
            ],
        }

    def bundle(self, inputs, *, coherent=True):
        directory = self.top / "bundle"
        files = []
        values = {"apps/fixture/" + name: content for name, content in inputs.items()}
        if coherent:
            values["native/manifests/app.json"] = json.dumps(self.description(inputs)).encode()
        for number, (target, content) in enumerate(values.items()):
            source = f"input-{number}"
            save(directory / source, content)
            files.append(
                {"repository": "private", "path": target, "source": source, "sha256": sha(content)}
            )
        path = directory / "candidate.json"
        save(path, {"schema_version": 1, "files": files})
        return path

    def test_coherent_candidate_preserves_source_until_promotion_and_recovers_exact_bytes(self):
        newer = {
            "package.json": b'{"name":"fixture","version":"2"}\n',
            "pnpm-lock.yaml": b"lockfileVersion: '9.0'\nupdated: true\n",
        }
        staged = updates.stage_candidate(self.context, self.bundle(newer))
        for name, content in self.original.items():
            self.assertEqual((self.root / "apps/fixture" / name).read_bytes(), content)
        updates.promote(self.context, staged["id"])
        for name, content in newer.items():
            self.assertEqual((self.root / "apps/fixture" / name).read_bytes(), content)
        updates.recover_candidate(self.context, staged["id"])
        for name, content in self.original.items():
            self.assertEqual((self.root / "apps/fixture" / name).read_bytes(), content)

    def test_unbound_lock_change_is_rejected_without_mutating_selected_inputs(self):
        newer = {**self.original, "pnpm-lock.yaml": b"unexpected graph\n"}
        with self.assertRaisesRegex(safety.Error, "application lock digest mismatch"):
            updates.stage_candidate(self.context, self.bundle(newer, coherent=False))
        for name, content in self.original.items():
            self.assertEqual((self.root / "apps/fixture" / name).read_bytes(), content)

    def test_changed_selected_input_is_detected_before_candidate_validation(self):
        save(self.root / "apps/fixture/package.json", b"foreign edit\n")
        with self.assertRaisesRegex(safety.Error, "application lock digest mismatch"):
            updates.validate_local_locks(self.context)
        self.assertEqual((self.root / "apps/fixture/package.json").read_bytes(), b"foreign edit\n")

    def test_standalone_candidate_keeps_one_source_origin_through_promotion(self):
        shutil.copytree(self.base / "native/engine", self.root / "native/engine")
        context = manifest.compose(self.root, self.root, self.home, "linux-x64")
        newer = {**self.original, "package.json": b'{"name":"fixture","version":"2"}\n'}
        staged = updates.stage_candidate(context, self.bundle(newer))
        self.assertEqual(
            (self.root / "apps/fixture/package.json").read_bytes(), self.original["package.json"]
        )
        updates.promote(context, staged["id"])
        self.assertEqual(
            (self.root / "apps/fixture/package.json").read_bytes(), newer["package.json"]
        )
        updates.recover_candidate(context, staged["id"])
        self.assertEqual(
            (self.root / "apps/fixture/package.json").read_bytes(), self.original["package.json"]
        )

    def test_standalone_repository_aliases_cannot_target_the_same_input_twice(self):
        shutil.copytree(self.base / "native/engine", self.root / "native/engine")
        context = manifest.compose(self.root, self.root, self.home, "linux-x64")
        bundle = self.bundle(self.original)
        value = json.loads(bundle.read_text())
        value["files"].append({**value["files"][0], "repository": "public"})
        save(bundle, value)
        with self.assertRaisesRegex(safety.Error, "duplicate candidate target"):
            updates.stage_candidate(context, bundle)
        self.assertEqual(
            (self.root / "apps/fixture/package.json").read_bytes(), self.original["package.json"]
        )


if __name__ == "__main__":
    unittest.main()
