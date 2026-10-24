"""Real controller regressions with disposable executable/package/unit fixtures.

No test invokes the host systemctl, sudo, package database or service manager.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[2] / "native/engine"
sys.path.insert(0, str(ENGINE))
import configuration
import manifest
import providers
import safety
import services

spec = importlib.util.spec_from_file_location("service_engine_entry", ENGINE / "__main__.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class ExistingSystemUnitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dotfiles-system-services-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve() / "home"
        self.home.mkdir()
        self.unit = self.home / "root/usr/lib/systemd/system/snapd.socket"
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text(
            "[Socket]\nListenStream=/run/snapd.socket\n[Install]\nWantedBy=sockets.target\n"
        )
        self.sums = self.home / "root/var/lib/dpkg/info/snapd.md5sums"
        self.sums.parent.mkdir(parents=True)
        self.refresh_checksum()
        self.state = self.home / "fixture-system.json"
        self.log = self.home / "fixture-commands.jsonl"
        self.write_state()
        self.systemctl = self.executable(
            "systemctl",
            """import json,os,pathlib,sys
home=pathlib.Path(os.environ['HOME'])
p=home/'fixture-system.json'; s=json.loads(p.read_text()); args=sys.argv[1:]
if args[:2]==['--global','list-unit-files']:
 if s.get('global_conflict'): print(args[2]+' enabled enabled')
 raise SystemExit(0)
a=args[1]
if a=='show':
 name=args[2]
 if name=='hypridle.service':
  print('ActiveState='+('active' if s.get('user_conflict') else 'inactive'))
  print('UnitFileState='+('enabled' if s.get('user_conflict') else 'disabled'))
  print('LoadState=loaded'); raise SystemExit(0)
 print('ActiveState='+s['active']); print('UnitFileState='+s['enabled'])
 print('LoadState=loaded'); print('Id='+s.get('id','snapd.socket'))
 print('FragmentPath='+str(home/'root/usr/lib/systemd/system/snapd.socket'))
 print('DropInPaths='+s.get('dropins','')); print('SourcePath=')
else:
 with (home/'fixture-commands.jsonl').open('a') as f: f.write(json.dumps(args)+'\\n')
 if a in ('start','restart'): s['active']='active'
 elif a=='stop': s['active']='inactive'
 elif a=='enable': s['enabled']='enabled'
 elif a=='disable': s['enabled']='disabled'
 elif a=='daemon-reload': pass
 else: raise SystemExit(3)
 p.write_text(json.dumps(s))
 if s.get('fail')==a: raise SystemExit(9)
""",
        )
        query = self.executable(
            "dpkg-query",
            """import json,os,pathlib,sys
h=pathlib.Path(os.environ['HOME']); s=json.loads((h/'fixture-system.json').read_text())
if not s.get('installed',True): raise SystemExit(1)
if sys.argv[1]=='--show': print('install ok installed\\t2.63')
elif sys.argv[1]=='--listfiles': print(h/('foreign' if s.get('foreign_owner') else 'root/usr/lib/systemd/system/snapd.socket'))
elif sys.argv[1]=='--control-path': print(h/'root/var/lib/dpkg/info/snapd.md5sums')
else: raise SystemExit(2)
""",
        )
        sudo = self.executable(
            "sudo",
            """import os,sys
assert sys.argv[1]=='--'
os.execv(sys.argv[2],sys.argv[2:])
""",
        )
        self.service = {
            "id": "snapd-socket",
            "manager": "systemd-system",
            "name": "snapd.socket",
            "existing_unit": True,
            "unit_package": "snapd",
            "package_id": "gui-snapd",
            "platforms": ["linux-x64"],
        }
        self.package = {"id": "gui-snapd", "provider": "apt", "name": "snapd"}
        self.context = {
            "home": str(self.home),
            "root": str(self.home),
            "base": str(self.home),
            "platform": "linux-x64",
            "identity": {},
            "dotfiles": {},
            "packages": [self.package],
            "services": [self.service],
            "_verification_only": True,
            "_fixture_context": True,
            "executables": {"systemctl": self.systemctl, "dpkg-query": query, "sudo": sudo},
            "paths": {
                "state": str(self.home / ".local/state/dotfiles"),
                "data": str(self.home / ".local/share/dotfiles"),
                "cache": str(self.home / ".cache/dotfiles"),
                "config": str(self.home / ".config"),
            },
        }
        self.context["paths"].update(
            {
                "bin": str(self.home / ".local/bin"),
                "runtime": str(self.home / ".local/share/dotfiles/runtime"),
                "completions": str(self.home / ".local/share/dotfiles/completions"),
            }
        )

    def executable(self, name, body):
        path = self.home / "fixture-bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)
        return str(path)

    def refresh_checksum(self):
        self.sums.write_text(
            hashlib.md5(self.unit.read_bytes(), usedforsecurity=False).hexdigest()
            + "  "
            + str(self.unit).lstrip("/")
            + "\n"
        )

    def write_state(self, **changes):
        state = {"active": "inactive", "enabled": "disabled", "installed": True}
        state.update(changes)
        self.state.write_text(json.dumps(state))

    def mutations(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def record(self, name):
        return safety.load(Path(self.context["paths"]["state"]) / name)

    def test_externally_enabled_healthy_service_preserved_without_adoption(self):
        self.write_state(active="active", enabled="enabled")
        self.assertTrue(
            services.control(self.context, "enable")["snapd-socket"]["preserved_existing"]
        )
        services.control(self.context, "start")
        services.require_services(
            self.context, {"id": "gui-chromium", "requires_services": ["snapd-socket"]}
        )
        self.assertEqual(self.mutations(), [])
        self.assertFalse((Path(self.context["paths"]["state"]) / "services.json").exists())
        with self.assertRaisesRegex(safety.Error, "unowned"):
            services.control(self.context, "stop")

    def test_explicit_enable_start_journals_native_origin_and_prior_state(self):
        services.control(self.context, "enable")
        services.control(self.context, "start")
        record = self.record("services.json")["services"]["snapd-socket"]
        self.assertEqual(record["prior_state"]["UnitFileState"], "disabled")
        self.assertEqual(record["observed"]["origin"]["fingerprint"], safety.fingerprint(self.unit))
        self.assertEqual(record["observed"]["origin"]["package"], "snapd")
        self.assertEqual(
            self.mutations(),
            [["--system", "enable", "snapd.socket"], ["--system", "start", "snapd.socket"]],
        )
        self.assertEqual(
            self.record("service-journal.json")["services"]["snapd-socket"]["status"], "committed"
        )

    def test_replaced_payload_dropin_and_package_owner_refused(self):
        for change in (
            {"dropins": "/etc/systemd/system/snapd.socket.d/foreign.conf"},
            {"foreign_owner": True},
            {"id": "other.socket"},
        ):
            with self.subTest(change=change):
                self.write_state(**change)
                with self.assertRaises(safety.Error):
                    services.control(self.context, "enable")
        self.write_state()
        self.unit.write_text("foreign replacement")
        with self.assertRaisesRegex(safety.Error, "replaced"):
            services.control(self.context, "enable")
        self.assertEqual(self.mutations(), [])

    def test_recorded_unit_replacement_refused_even_if_package_checksum_changes(self):
        services.control(self.context, "enable")
        self.unit.write_text("different package payload")
        self.refresh_checksum()
        with self.assertRaisesRegex(safety.Error, "replaced"):
            services.control(self.context, "start")
        with self.assertRaisesRegex(safety.Error, "changed descriptor"):
            services.control(self.context, "recover")
        self.assertEqual(len(self.mutations()), 1)

    def test_recovery_restores_enable_prior_state(self):
        services.control(self.context, "enable")
        services.control(self.context, "recover")
        self.assertEqual(json.loads(self.state.read_text())["enabled"], "disabled")
        self.assertEqual(self.record("services.json")["services"], {})

    def test_interrupted_start_recovery_preserves_external_enablement(self):
        self.write_state(enabled="enabled", fail="start")
        with self.assertRaises(safety.Error):
            services.control(self.context, "start")
        self.assertEqual(
            self.record("service-journal.json")["services"]["snapd-socket"]["status"], "pending"
        )
        with self.assertRaisesRegex(safety.Error, "unfinished service transition"):
            services.control(self.context, "start")
        services.control(self.context, "recover")
        state = json.loads(self.state.read_text())
        self.assertEqual((state["enabled"], state["active"]), ("enabled", "inactive"))
        self.assertEqual(
            self.mutations(),
            [["--system", "start", "snapd.socket"], ["--system", "stop", "snapd.socket"]],
        )

    def test_external_enablement_cannot_be_disabled_after_engine_start(self):
        self.write_state(enabled="enabled")
        services.control(self.context, "start")
        with self.assertRaisesRegex(safety.Error, "predates"):
            services.control(self.context, "disable")

    def test_shared_service_not_stopped_by_application_or_configuration_retirement(self):
        services.control(self.context, "enable")
        services.control(self.context, "start")
        self.context["services"] = []
        entry.require_stopped_services(self.context)
        self.assertEqual(json.loads(self.state.read_text())["active"], "active")
        packages = {
            "home": self.context["home"],
            "packages": {
                "gui-snapd": {
                    "selection": self.package,
                    "shared_service": True,
                    "preexisting": False,
                }
            },
        }
        safety.atomic_json(Path(self.context["paths"]["state"]) / "packages.json", packages)
        result = entry.retire_packages(self.context, self.home / "context.json", ["gui-snapd"])
        self.assertEqual(result["retired_packages"], [])
        self.assertEqual(
            result["retained_shared_runtime"]["gui-snapd"]["status"], "retained-shared-runtime"
        )
        self.assertIn(
            "gui-snapd",
            safety.load(Path(self.context["paths"]["state"]) / "packages.json")["packages"],
        )
        self.assertEqual(json.loads(self.state.read_text())["active"], "active")
        self.assertEqual(len(self.mutations()), 2)

    def test_provision_requires_explicit_service_intent_before_recipe(self):
        recipe = self.home / "chromium.py"
        recipe.write_text(
            "raise AssertionError('recipe must not run before service prerequisite')\n"
        )
        chromium = {
            "id": "gui-chromium",
            "provider": "recipe",
            "path": "chromium.py",
            "_origin": str(self.home),
            "requires_services": ["snapd-socket"],
        }
        self.context["packages"].append(chromium)
        with self.assertRaisesRegex(
            safety.Error, "services enable.*services start.*resume.*provision"
        ):
            providers.provision(
                self.context, self.home / "context.json", {"packages": {}}, ["gui-chromium"]
            )
        self.assertEqual(self.mutations(), [])

    def test_fixture_and_privilege_guards_reject_real_or_missing_executables(self):
        for name in ("systemctl", "dpkg-query", "sudo"):
            with self.subTest(name=name):
                saved = self.context["executables"].pop(name)
                try:
                    with self.assertRaisesRegex(safety.Error, "verification refuses live"):
                        services.control(self.context, "enable")
                finally:
                    self.context["executables"][name] = saved
        self.context["_fixture_context"] = False
        with self.assertRaisesRegex(safety.Error, "verification refuses live"):
            services.control(self.context, "start")
        self.assertEqual(self.mutations(), [])

    def test_unsupported_platform_and_missing_package_fail_without_mutation(self):
        self.context["platform"] = "macos-arm64"
        with self.assertRaisesRegex(safety.Error, "systemd service selected on macOS"):
            services.control(self.context, "enable")
        self.context["platform"] = "linux-x64"
        self.context["packages"] = []
        with self.assertRaisesRegex(safety.Error, "declared APT package"):
            services.control(self.context, "enable")
        self.context["packages"] = [self.package]
        self.write_state(installed=False)
        with self.assertRaises(safety.Error):
            services.control(self.context, "enable")
        self.assertEqual(self.mutations(), [])

    def test_system_descriptor_schema_requires_linux_apt_metadata(self):
        manifest.validate({"schema_version": 1, "services": [self.service]}, self.home)
        for changes in (
            {"platforms": ["macos-arm64"]},
            {"existing_unit": False},
            {"target": "owned-unit"},
            {"unit_package": ""},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(safety.Error):
                    manifest.validate(
                        {"schema_version": 1, "services": [{**self.service, **changes}]}, self.home
                    )

    def test_user_and_global_hypridle_conflicts_are_never_disabled_by_name(self):
        service = {
            "id": "site-hypridle",
            "name": "dotfiles-hypridle.service",
            "manager": "systemd-user",
            "conflicts": ["hypridle.service"],
        }
        for state in ({"user_conflict": True}, {"global_conflict": True}):
            with self.subTest(state=state):
                self.write_state(**state)
                with self.assertRaisesRegex(safety.Error, "ownership-aware handoff"):
                    services.conflict_preflight(self.context, service)
        self.assertEqual(self.mutations(), [])

    def test_provision_resumes_after_explicit_service_actions_without_reactivation(self):
        recipe = self.home / "chromium.py"
        recipe.write_text("""import hashlib,json,os,pathlib,sys
p=pathlib.Path(os.environ['HOME'])/'fixture-chromium'
if sys.argv[1]=='provision': p.write_text('installed browser')
print(json.dumps({'id':'gui-chromium','status':{'provision':'provisioned','check':'verified'}[sys.argv[1]],'artifacts':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}],'executables':{}}))
""")
        chromium = {
            "id": "gui-chromium",
            "provider": "recipe",
            "path": "chromium.py",
            "_origin": str(self.home),
            "requires_services": ["snapd-socket"],
        }
        self.context["packages"].append(chromium)
        services.control(self.context, "enable")
        services.control(self.context, "start")
        records = providers.provision(
            self.context,
            self.home / "context.json",
            {"packages": {}, "home": str(self.home)},
            ["gui-chromium"],
        )
        self.assertEqual((self.home / "fixture-chromium").read_text(), "installed browser")
        self.assertIn("gui-chromium", records["packages"])
        self.assertEqual(len(self.mutations()), 2)

    def test_apply_has_no_service_controller_call(self):
        arguments = entry.parser().parse_args(
            ["apply", "--verification-only", "--yes", "--home", str(self.home)]
        )
        self.context["packages"] = []
        with (
            patch.object(entry, "compose", return_value=self.context),
            patch.object(entry, "native"),
            patch.object(configuration, "stage", return_value=self.home / "stage"),
            patch.object(configuration, "apply", return_value={"applied": True}),
            patch.object(
                services, "control", side_effect=AssertionError("apply must not control services")
            ),
        ):
            self.assertEqual(entry.execute(arguments), {"applied": True})
        self.assertEqual(self.mutations(), [])

    def test_entrypoint_service_action_uses_shared_operation_lock(self):
        arguments = entry.parser().parse_args(
            ["services", "enable", "--verification-only", "--yes", "--home", str(self.home)]
        )
        self.context["paths"].update(
            {
                "bin": str(self.home / ".local/bin"),
                "runtime": str(self.home / ".local/share/dotfiles/runtime"),
                "completions": str(self.home / ".local/share/dotfiles/completions"),
            }
        )
        with (
            safety.locked(self.context["paths"]["state"]),
            patch.object(entry, "compose", return_value=self.context),
        ):
            with self.assertRaisesRegex(safety.Error, "another dotfiles operation"):
                entry.execute(arguments)
        self.assertEqual(self.mutations(), [])


if __name__ == "__main__":
    unittest.main()
