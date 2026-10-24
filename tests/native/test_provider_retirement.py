"""Production APT retirement with disposable executables, state and workloads."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PUBLIC = Path(__file__).resolve().parents[2]
ENGINE = PUBLIC / "native/engine"
sys.path.insert(0, str(ENGINE))
import manifest
import safety

SPEC = importlib.util.spec_from_file_location("provider_retirement_entry", ENGINE / "__main__.py")
ENTRY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENTRY)


class NativeProviderRetirement(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dotfiles-retirement-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.context = manifest.compose(PUBLIC, PUBLIC, self.home, "linux-x64")
        self.context.update({"_fixture_context": True, "_verification_only": True})
        self.state = Path(self.context["paths"]["state"])
        self.state.mkdir(parents=True)
        self.database = self.home / "apt-fixture.json"
        self.log = self.home / "provider-calls.jsonl"
        self.record = self.state / "packages.json"
        self.context_path = self.root / "context.json"
        self.packages = {package["id"]: package for package in self.context["packages"]}
        self.data = {
            "installed": {"skopeo": "1.0", "pre-commit": "2.0", "libc6": "system"},
            "canonical": {},
            "plans": {},
            "fail_simulation": False,
        }
        self.save_database()
        for command in ("apt-get", "dpkg-query"):
            executable = self.home / command
            executable.write_text(
                "#!"
                + sys.executable
                + "\n"
                + f"""import json,os,pathlib,sys
p=pathlib.Path({str(self.database)!r}); db=json.loads(p.read_text())
args=sys.argv[1:]; name=args[-1]; tool=pathlib.Path(sys.argv[0]).name
with pathlib.Path({str(self.log)!r}).open('a') as log:
 log.write(json.dumps({{'tool':tool,'args':args,'locale':os.environ.get('LC_ALL')}})+'\\n')
if tool=='dpkg-query':
 if name not in db['installed']: sys.exit(1)
 if '-f=${{binary:Package}}' in args: print(db['canonical'].get(name,name))
 else: print('install ok installed\\t'+db['installed'][name])
elif '--simulate' in args:
 if db['fail_simulation']: sys.exit(23)
 print(db['plans'].get(name,'Remv '+name+' ['+db['installed'][name]+']'))
else:
 if args != ['remove','--yes','--no-auto-remove','--',name]: sys.exit(24)
 policy=pathlib.Path({str(self.state / "fixture-policy-rc.d")!r})
 if policy.read_text() != '#!/bin/sh\\nexit 101\\n': sys.exit(25)
 del db['installed'][name];p.write_text(json.dumps(db))
"""
            )
            executable.chmod(0o755)
            self.context["executables"][command] = str(executable)
        self.context_path.write_text(json.dumps(self.context))
        self.own("skopeo")
        self.user_data = self.home / "application-and-login-data"
        self.user_data.write_text("unrelated shared host state")

    def save_database(self):
        self.database.write_text(json.dumps(self.data))

    def own(self, *identifiers):
        ownership = {
            "home": self.context["home"],
            "links": {},
            "packages": {
                identifier: {
                    "selection": self.packages[identifier],
                    "preexisting": False,
                    "observed": {
                        "installed": self.data["installed"][self.packages[identifier]["name"]]
                    },
                }
                for identifier in identifiers
            },
        }
        self.record.write_text(json.dumps(ownership))

    def calls(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def assert_no_mutation(self, before):
        self.assertEqual(self.record.read_bytes(), before)
        self.assertTrue(
            all(call["tool"] != "apt-get" or "--simulate" in call["args"] for call in self.calls())
        )
        self.assertFalse((self.state / "fixture-policy-rc.d").exists())
        self.assertFalse((self.state / "package-journal.json").exists())
        self.assertEqual(self.user_data.read_text(), "unrelated shared host state")

    def active_workload(self):
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"],
            cwd=self.home,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)

        self.addCleanup(cleanup)
        return process

    def test_empty_removal_plan_fails_closed(self):
        self.data["plans"]["skopeo"] = "0 upgraded, 0 newly installed, 0 to remove."
        self.save_database()
        before = self.record.read_bytes()
        with self.assertRaises(safety.Error):
            ENTRY.retire_packages(self.context, self.context_path, ["skopeo"])
        self.assert_no_mutation(before)

    def test_failed_simulation_fails_closed(self):
        self.data["fail_simulation"] = True
        self.save_database()
        before = self.record.read_bytes()
        with self.assertRaisesRegex(safety.Error, "command failed"):
            ENTRY.retire_packages(self.context, self.context_path, ["skopeo"])
        self.assert_no_mutation(before)

    def test_other_package_names_and_nonremoval_actions_are_not_normalized_away(self):
        for plan in (
            "Remv skopeo-tools [1.0]\n",
            "Remv skopeo [1.0]\nInst foreign-library [2.0]\n",
            "Remv skopeo [1.0]\nConf foreign-library [2.0]\n",
            "Purg skopeo [1.0]\n",
        ):
            with self.subTest(plan=plan):
                self.data["plans"]["skopeo"] = plan
                self.save_database()
                before = self.record.read_bytes()
                with self.assertRaises(safety.Error):
                    ENTRY.retire_packages(self.context, self.context_path, ["skopeo"])
                self.assert_no_mutation(before)

    def test_foreign_cascade_blocks_all_requested_packages_without_stopping_workload(self):
        workload = self.active_workload()
        self.own("pre-commit", "skopeo")
        self.data["plans"]["skopeo"] = "Remv skopeo [1.0]\nRemv libc6:amd64 [system]\n"
        self.save_database()
        before = self.record.read_bytes()
        with self.assertRaises(safety.Error):
            ENTRY.retire_packages(self.context, self.context_path, ["pre-commit", "skopeo"])
        self.assert_no_mutation(before)
        self.assertIsNone(workload.poll())
        self.assertEqual(json.loads(self.database.read_text())["installed"], self.data["installed"])

    def test_exact_owned_removal_normalizes_only_architecture_and_preserves_workload(self):
        workload = self.active_workload()
        self.data["plans"]["skopeo"] = "Remv skopeo:amd64 [1.0]\n"
        self.save_database()
        result = ENTRY.retire_packages(self.context, self.context_path, ["skopeo"])
        self.assertEqual(result["retired_packages"], ["skopeo"])
        installed = json.loads(self.database.read_text())["installed"]
        self.assertEqual(installed, {"pre-commit": "2.0", "libc6": "system"})
        ownership = json.loads(self.record.read_text())
        self.assertNotIn("skopeo", ownership["packages"])
        self.assertIn("skopeo", ownership["retired"])
        apt_calls = [call for call in self.calls() if call["tool"] == "apt-get"]
        self.assertEqual(len([call for call in apt_calls if "--simulate" in call["args"]]), 2)
        for call in apt_calls:
            if "--simulate" in call["args"]:
                self.assertEqual(call["locale"], "C")
                self.assertEqual(
                    call["args"], ["--simulate", "remove", "--no-auto-remove", "--", "skopeo"]
                )
            else:
                self.assertEqual(
                    call["args"], ["remove", "--yes", "--no-auto-remove", "--", "skopeo"]
                )
        self.assertFalse((self.state / "fixture-policy-rc.d").exists())
        self.assertIsNone(workload.poll())
        self.assertEqual(self.user_data.read_text(), "unrelated shared host state")

    def test_preexisting_or_changed_package_never_reaches_removal_simulation(self):
        for preexisting in (True, False):
            with self.subTest(preexisting=preexisting):
                self.own("skopeo")
                ownership = json.loads(self.record.read_text())
                if preexisting:
                    ownership["packages"]["skopeo"]["preexisting"] = True
                else:
                    ownership["packages"]["skopeo"]["observed"]["installed"] = "foreign-old"
                self.record.write_text(json.dumps(ownership))
                before = self.record.read_bytes()
                with self.assertRaises(safety.Error):
                    ENTRY.retire_packages(self.context, self.context_path, ["skopeo"])
                self.assert_no_mutation(before)
                self.assertFalse(any(call["tool"] == "apt-get" for call in self.calls()))


if __name__ == "__main__":
    unittest.main()
