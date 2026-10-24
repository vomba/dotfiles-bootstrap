"""Disposable native engine regressions; no live HOME/providers/services.

Set DOTFILES_TEST_CHEZMOI to an acquired native chezmoi executable to exercise
real Go-template rendering and live-target apply semantics. Those tests skip
honestly without it; source/composition/ownership/CLI tests require only Python
and native Git. No bootstrap downloads are performed by this suite.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

PUBLIC = Path(__file__).resolve().parents[2]
ENGINE = PUBLIC / "native/engine"
sys.path.insert(0, str(ENGINE))
import configuration
import manifest
import providers
import safety
import services
import source
import updates

CHEZMOI = os.environ.get("DOTFILES_TEST_CHEZMOI")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dotfiles-engine-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.base, self.root, self.home = (
            self.directory / name for name in ("public", "private", "home")
        )
        for root in (self.base, self.root):
            (root / "native/manifests").mkdir(parents=True)
            (root / "native/chezmoi").mkdir(parents=True)
        (self.base / "native/engine").mkdir()
        shutil.copyfile(ENGINE / "pins.json", self.base / "native/engine/pins.json")
        save(
            self.base / "native/manifests/base.json",
            {
                "schema_version": 1,
                "defaults": {
                    "dotfiles": {
                        "shell": {"enable": True},
                        "cloud": {"enable": True},
                        "kube": {"enable": False},
                    },
                    "go_env": {"GOPROXY": "https://proxy.golang.org"},
                },
                "profiles": {
                    "linux-x64": {"selectors": {"profile": "linux"}},
                    "macos-arm64": {"selectors": {"profile": "mac"}},
                },
            },
        )

    def compose(self, flags=(), platform="linux-x64"):
        context = manifest.compose(self.base, self.root, self.home, platform, flags)
        if CHEZMOI:
            context["executables"]["chezmoi"] = str(Path(CHEZMOI).resolve())
        return context

    def fragment(self, value, private=False, name="extra"):
        root = self.root if private else self.base
        save(root / "native/manifests" / f"{name}.json", {"schema_version": 1, **value})

    def path(self, context):
        path = self.directory / "context.json"
        save(path, context)
        return path


class CompositionTests(Fixture):
    def test_private_profiles_merge_and_independent_flags(self):
        self.fragment(
            {
                "defaults": {
                    "go_env": {"GOPRIVATE": "example.invalid/private"},
                    "identity": {"name": "Fixture User"},
                },
                "profiles": {"linux-x64": {"dotfiles": {"kube": {"enable": True}}}},
            },
            private=True,
        )
        context = self.compose(["cloud.enable=false"])
        self.assertFalse(context["dotfiles"]["cloud"]["enable"])
        self.assertTrue(context["dotfiles"]["kube"]["enable"])
        self.assertTrue(context["dotfiles"]["shell"]["enable"])
        self.assertEqual(context["selectors"]["profile"], "linux")
        self.assertEqual(
            context["go_env"],
            {"GOPROXY": "https://proxy.golang.org", "GOPRIVATE": "example.invalid/private"},
        )
        self.assertEqual(self.compose(platform="macos-arm64")["selectors"]["profile"], "mac")

    def test_requires_and_and_or_preserve_origin(self):
        self.fragment(
            {
                "packages": [
                    {"id": "shared", "provider": "apt", "name": "shared", "when": "kube.enable"},
                    {
                        "id": "chosen",
                        "provider": "apt",
                        "name": "chosen",
                        "when": ["shell.enable", "cloud.enable"],
                        "requires": ["shared"],
                    },
                    {
                        "id": "either",
                        "provider": "apt",
                        "name": "either",
                        "when": {"any": ["kube.enable", "shell.enable"]},
                    },
                ]
            }
        )
        context = self.compose()
        self.assertEqual([p["id"] for p in context["packages"]], ["shared", "chosen", "either"])
        self.assertTrue(all(p["_origin"] == str(self.base) for p in context["packages"]))
        self.assertEqual(
            [p["id"] for p in self.compose(["cloud.enable=false"])["packages"]], ["either"]
        )

    def test_disjoint_variants_but_not_overlapping_ids(self):
        records = [
            {"id": "same", "provider": "apt", "name": "same", "platforms": ["linux-x64"]},
            {"id": "same", "provider": "brew", "name": "same", "platforms": ["macos-arm64"]},
        ]
        self.fragment({"packages": records})
        self.assertEqual(self.compose()["packages"][0]["provider"], "apt")
        self.assertEqual(self.compose(platform="macos-arm64")["packages"][0]["provider"], "brew")
        records.append(dict(records[0]))
        self.fragment({"packages": records})
        with self.assertRaisesRegex(safety.Error, "duplicate conflicting"):
            self.compose(platform="macos-arm64")

    def test_hostile_schema_paths_features_providers_and_cycles(self):
        bad = [
            {"packages": [{"id": "x", "provider": "unknown"}]},
            {"packages": [{"id": "x", "provider": "recipe", "path": "../escape.py"}]},
            {"packages": [{"id": "x", "provider": "apt", "name": "x", "when": "typo.enable"}]},
            {"packages": [{"id": "x", "provider": "apt", "name": "x", "requires": ["missing"]}]},
            {"packages": [{"id": "x", "provider": "apt", "name": "x", "requires": ["x"]}]},
            {"defaults": {"paths": {"state": str(self.home / ".." / "escaped")}}},
            {
                "packages": [
                    {"id": "x", "provider": "apt", "name": "x", "executables": {"x": "../escape"}}
                ]
            },
        ]
        for fragment in bad:
            with self.subTest(fragment=fragment):
                self.fragment(fragment)
                with self.assertRaises(safety.Error):
                    self.compose()
        self.fragment({})
        with self.assertRaisesRegex(safety.Error, "unknown feature"):
            self.compose(["unknown.enable=true"])

    def test_duplicate_json_keys_rejected(self):
        path = self.directory / "duplicate.json"
        path.write_text('{"schema_version":1,"schema_version":2}')
        with self.assertRaisesRegex(safety.Error, "duplicate JSON"):
            safety.load(path)

    def test_artifact_origins_and_runtime_alias(self):
        self.fragment(
            {
                "artifacts": {"fixture-source": {"version": "1"}},
                "packages": [
                    {
                        "id": "assembly",
                        "runtime_id": "actual",
                        "version": "1",
                        "provider": "artifact",
                        "executables": {"fixture": "bin/fixture"},
                        "sources": {
                            platform: {
                                "url": "https://example.invalid/fixture.tar.gz",
                                "sha256": "0" * 64,
                            }
                            for platform in manifest.PLATFORMS
                        },
                    }
                ],
            },
            private=True,
        )
        context = self.compose()
        self.assertEqual(context["artifacts"]["fixture-source"]["_origin"], str(self.root))
        self.assertEqual(
            context["executables"]["fixture"],
            str(Path(context["paths"]["runtime"]) / "actual/1/bin/fixture"),
        )


class SafetyTests(Fixture):
    def test_operation_lock_excludes_concurrent_mutation(self):
        state = self.directory / "state"
        with safety.locked(state):
            with self.assertRaisesRegex(safety.Error, "another dotfiles"):
                with safety.locked(state):
                    self.fail("second writer entered")
        with safety.locked(state):
            pass

    def test_archive_traversal_and_escaping_symlinks_refused(self):
        for name, link in (("../outside", None), ("safe/link", "../../outside")):
            archive = self.directory / "hostile.tar"
            with tarfile.open(archive, "w") as stream:
                item = tarfile.TarInfo(name)
                if link:
                    item.type = tarfile.SYMTYPE
                    item.linkname = link
                    stream.addfile(item)
                else:
                    item.size = 1
                    stream.addfile(item, io.BytesIO(b"x"))
            with self.assertRaises(safety.Error):
                safety.extract(archive, self.directory / "output")
        self.assertFalse((self.directory / "outside").exists())

    def test_native_executables_never_resolve_nix_or_relative(self):
        for path in ("python3", "/nix/store/fixture/bin/python3"):
            with self.assertRaises(safety.Error):
                safety.native(path, exists=False)
        linked = self.directory / "native-looking"
        linked.symlink_to("/nix/store/fixture/bin/python3")
        with self.assertRaises(safety.Error):
            safety.native(linked, exists=False)

    def test_source_hooks_and_foreign_parent_refused(self):
        for name in (
            "run_once_install.sh",
            "encrypted_secret",
            ".chezmoiexternal.toml",
            "modify_settings",
        ):
            path = self.base / "native/chezmoi" / name
            path.write_text("unsafe")
            with self.assertRaises(safety.Error):
                configuration.validate_source(path.parent)
            path.unlink()
        self.home.mkdir()
        (self.home / ".config").symlink_to(self.directory / "foreign")
        with self.assertRaises(safety.Error):
            configuration.collisions(self.compose(), {".config/file": {}}, {"files": {}})

    def test_real_environment_not_forwarded_to_children(self):
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "synthetic",
                "GOSUMDB": "off",
                "GOPRIVATE": "*",
                "MISE_CONFIG_FILE": "/tmp/hostile",
                "PATH": "/nix/store/fixture/bin",
            },
        ):
            env = safety.environment(self.compose())
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GOPRIVATE", env)
        self.assertNotIn("MISE_CONFIG_FILE", env)
        self.assertNotIn("/nix/", env["PATH"])


@unittest.skipUnless(
    CHEZMOI, "set DOTFILES_TEST_CHEZMOI to acquired native chezmoi for real rendering/apply"
)
class ChezmoiTests(Fixture):
    def author(self, name, content, private=False):
        root = self.root if private else self.base
        path = root / "native/chezmoi" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_render_composition_and_explicit_override(self):
        self.author("dot_fixture.tmpl", "{{ .identity.name }}:{{ .platform }}")
        self.fragment({"defaults": {"identity": {"name": "Fixture"}}}, private=True)
        context = self.compose()
        staged = configuration.stage(context)
        self.assertEqual((staged / "home/.fixture").read_text(), "Fixture:linux-x64")
        self.assertFalse((self.home / ".fixture").exists())
        self.author("dot_fixture.tmpl", "private", private=True)
        with self.assertRaisesRegex(safety.Error, "unacknowledged"):
            configuration.stage(context)
        self.fragment({"overrides": ["dot_fixture.tmpl"]}, private=True)
        self.assertEqual(
            (configuration.stage(self.compose()) / "home/.fixture").read_text(), "private"
        )

    def test_apply_repeat_disable_owned_retire_and_recover(self):
        path = self.author("dot_fixture", "first")
        context = self.compose()
        configuration.apply(context, configuration.stage(context))
        configuration.apply(context, configuration.stage(context))
        self.assertEqual((self.home / ".fixture").read_text(), "first")
        self.assertEqual(configuration.verify(context)["verified_files"], 1)
        path.write_text("second")
        configuration.apply(context, configuration.stage(context))
        self.assertEqual((self.home / ".fixture").read_text(), "second")
        configuration.recover(context)
        self.assertEqual((self.home / ".fixture").read_text(), "first")
        path.unlink()
        configuration.apply(context, configuration.stage(context))
        self.assertFalse((self.home / ".fixture").exists())
        self.author("dot_fixture", "retire")
        configuration.apply(context, configuration.stage(context))
        configuration.apply(context, retire=True)
        self.assertFalse((self.home / ".fixture").exists())
        configuration.recover(context)
        self.assertEqual((self.home / ".fixture").read_text(), "retire")

    def test_foreign_files_never_adopted_and_modified_owned_preserved(self):
        self.author("dot_fixture", "desired")
        self.home.mkdir()
        (self.home / ".fixture").write_text("foreign")
        context = self.compose()
        with self.assertRaisesRegex(safety.Error, "foreign"):
            configuration.apply(context, configuration.stage(context))
        self.assertEqual((self.home / ".fixture").read_text(), "foreign")
        (self.home / ".fixture").unlink()
        configuration.apply(context, configuration.stage(context))
        (self.home / ".fixture").write_text("user replacement")
        for operation in (
            lambda: configuration.apply(context, retire=True),
            lambda: configuration.recover(context),
        ):
            with self.assertRaises(safety.Error):
                operation()
        self.assertEqual((self.home / ".fixture").read_text(), "user replacement")

    def test_apply_does_not_provision_or_activate(self):
        self.author("dot_fixture", "safe")
        self.fragment(
            {"packages": [{"id": "uninstalled", "provider": "apt", "name": "uninstalled"}]}
        )
        context = self.compose()
        with (
            patch.object(providers, "provision", side_effect=AssertionError("install forbidden")),
            patch.object(services, "control", side_effect=AssertionError("activation forbidden")),
        ):
            configuration.apply(context, configuration.stage(context))
        self.assertEqual((self.home / ".fixture").read_text(), "safe")

    def test_custom_xdg_and_chezmoi_ignore(self):
        self.author("dot_config/fixture.tmpl", "{{ .paths.config }}")
        self.author("dot_skipped", "not selected")
        self.author(".chezmoiignore", "{{ if not .dotfiles.kube.enable }}\n.skipped\n{{ end }}\n")
        self.fragment({"defaults": {"paths": {"config": str(self.home / ".custom-config")}}})
        context = self.compose()
        staged = configuration.stage(context)
        self.assertEqual(
            (staged / "home/.custom-config/fixture").read_text(), str(self.home / ".custom-config")
        )
        self.assertFalse((staged / "home/.skipped").exists())


class RecipeAndServiceTests(Fixture):
    def test_recipe_provision_check_before_publication(self):
        recipe = self.root / "native/fixture.py"
        recipe.write_text(
            """import argparse,hashlib,json,pathlib\np=argparse.ArgumentParser();p.add_argument("action");p.add_argument("--context");p.add_argument("--id");p.add_argument("--output");a=p.parse_args();c=json.loads(pathlib.Path(a.context).read_text());m=pathlib.Path(c["home"])/"installed"\nif a.action=="provision": m.parent.mkdir(parents=True,exist_ok=True);m.write_text("owned")\nelif a.action=="check": assert m.read_text()=="owned"\nelse: raise SystemExit(2)\nprint(json.dumps({"id":a.id,"status":"provisioned" if a.action=="provision" else "verified","artifacts":[{"path":str(m),"sha256":hashlib.sha256(m.read_bytes()).hexdigest()}],"executables":{}}))\n"""
        )
        self.fragment(
            {"packages": [{"id": "fixture", "provider": "recipe", "path": "native/fixture.py"}]},
            private=True,
        )
        context = self.compose()
        record = {"home": context["home"], "packages": {}, "links": {}}
        providers.provision(context, self.path(context), record)
        self.assertEqual((self.home / "installed").read_text(), "owned")
        published = safety.load(Path(context["paths"]["state"]) / "packages.json")
        self.assertEqual(published["packages"]["fixture"]["selection"]["_origin"], str(self.root))
        self.assertEqual(
            published["packages"]["fixture"]["observed"]["check"]["status"], "verified"
        )
        self.assertEqual(
            published["packages"]["fixture"]["observed"]["check"]["artifacts"][0]["sha256"],
            hashlib.sha256(b"owned").hexdigest(),
        )

    def test_failed_recipe_does_not_publish_success(self):
        recipe = self.root / "native/failing.py"
        recipe.write_text("raise SystemExit(7)\n")
        self.fragment(
            {"packages": [{"id": "fixture", "provider": "recipe", "path": "native/failing.py"}]},
            private=True,
        )
        context = self.compose()
        with self.assertRaises(safety.Error):
            providers.provision(
                context, self.path(context), {"home": context["home"], "packages": {}, "links": {}}
            )
        self.assertFalse((Path(context["paths"]["state"]) / "packages.json").exists())

    def test_service_refuses_unowned_descriptor_before_command(self):
        self.fragment(
            {
                "services": [
                    {
                        "id": "fixture",
                        "manager": "systemd-user",
                        "name": "fixture.service",
                        "source": ".config/systemd/user/fixture.service",
                        "target": ".config/systemd/user/fixture.service",
                        "platforms": ["linux-x64"],
                    }
                ]
            }
        )
        context = self.compose()
        with patch.object(services, "run", side_effect=AssertionError("must not contact host")):
            with self.assertRaisesRegex(safety.Error, "not an unchanged owned"):
                services.control(context, "enable")


class SourceTests(Fixture):
    def test_unpublished_source_has_no_checkout_fallback(self):
        lock = self.directory / "bootstrap.lock.json"
        save(
            lock,
            {
                "schema_version": 1,
                "status": "unpublished",
                "repository": "https://github.com/example/repo.git",
                "revision": None,
                "sha256": None,
            },
        )
        with self.assertRaisesRegex(safety.Error, "unpublished"):
            source.resolve(lock, self.directory / "cache")
        with self.assertRaisesRegex(safety.Error, "verification-only"):
            source.resolve(lock, self.directory / "cache", override=self.base)

    def test_pinned_git_acquisition_cache_integrity_and_offline_reuse(self):
        repository = self.directory / "git-source"
        repository.mkdir()
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.directory),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }

        def git(*arguments):
            return subprocess.check_output(
                ["/usr/bin/git", "-C", str(repository), *arguments], env=environment
            )

        git("init", "--quiet")
        (repository / "native/engine").mkdir(parents=True)
        (repository / "native/engine/__main__.py").write_text("print('fixture')\n")
        git("add", ".")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        )
        revision = git("rev-parse", "HEAD").decode().strip()
        checksum = hashlib.sha256(git("archive", "--format=tar", revision)).hexdigest()
        lock = self.directory / "source.lock.json"
        save(
            lock,
            {
                "schema_version": 1,
                "status": "published",
                "repository": repository.as_uri(),
                "revision": revision,
                "sha256": checksum,
            },
        )
        destination = source.resolve(lock, self.directory / "cache", verification_only=True)
        shutil.rmtree(repository)
        self.assertEqual(
            source.resolve(lock, self.directory / "cache", verification_only=True), destination
        )
        (destination / "native/engine/__main__.py").write_text("changed")
        with self.assertRaisesRegex(safety.Error, "integrity"):
            source.resolve(lock, self.directory / "cache", verification_only=True)


class CliTests(Fixture):
    def command(self, *arguments):
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(ENGINE / "__main__.py"),
                "--root",
                str(self.root),
                "--base",
                str(self.base),
                "--verification-only",
                "--home",
                str(self.home),
                "--platform",
                "linux-x64",
                *arguments,
            ],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
        )

    def test_help_and_plan_need_no_packages_or_download(self):
        result = self.command("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("provision", result.stdout)
        result = self.command("plan", "--set", "cloud.enable=false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["context"]["dotfiles"]["cloud"]["enable"])
        self.assertFalse(self.home.exists())

    def test_mutation_requires_intent_and_local_override_requires_isolation(self):
        result = self.command("apply")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --yes", result.stderr)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(ENGINE / "__main__.py"),
                "plan",
                "--bootstrap-source",
                str(self.base),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("verification-only", result.stderr)
        result = self.command("plan", "--home", str(Path.home()))
        self.assertEqual(result.returncode, 2)
        self.assertIn("disposable", result.stderr)


class NativeProviderFixtureTests(Fixture):
    def executable(self, name, body):
        path = self.home / "fixture-bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o755)
        return str(path)

    def test_apt_is_explicit_verifies_installed_state_and_suppresses_services(self):
        installed = self.home / "installed-apt"
        query = self.executable(
            "dpkg-query",
            f"import pathlib,sys\np=pathlib.Path({str(installed)!r})\nif not p.exists(): raise SystemExit(1)\nprint('install ok installed\\t'+p.read_text())\n",
        )
        installer = self.executable(
            "apt-get",
            f"import pathlib,sys\nassert sys.argv[1]=='install'\np=pathlib.Path({str(installed)!r});p.write_text('1.2.3')\n",
        )
        self.fragment({"packages": [{"id": "fixture", "provider": "apt", "name": "fixture"}]})
        context = self.compose()
        context.update({"_verification_only": True, "_fixture_context": True})
        context["executables"].update({"apt-get": installer, "dpkg-query": query})
        record = {"home": context["home"], "packages": {}, "links": {}}
        providers.provision(context, self.path(context), record)
        self.assertEqual(record["packages"]["fixture"]["observed"]["installed"], "1.2.3")
        self.assertFalse(record["packages"]["fixture"]["preexisting"])
        self.assertFalse((Path(context["paths"]["state"]) / "fixture-policy-rc.d").exists())
        self.assertFalse((Path(context["paths"]["state"]) / "services.json").exists())

    def test_verification_refuses_real_native_mutators(self):
        context = self.compose()
        context["_verification_only"] = True
        for name, path in (
            ("apt-get", "/usr/bin/apt-get"),
            ("brew", "/opt/homebrew/bin/brew"),
            ("systemctl", "/usr/bin/systemctl"),
        ):
            with (
                self.subTest(provider=name),
                self.assertRaisesRegex(safety.Error, "verification refuses live"),
            ):
                safety.command(context, name, path, mutating=True)

    def test_services_require_ownership_and_support_narrow_selection_recovery(self):
        state_file = self.home / "service-state.json"
        binary = self.executable(
            "systemctl",
            f"""import json,pathlib,sys
p=pathlib.Path({str(state_file)!r})
s=json.loads(p.read_text()) if p.exists() else {{'active':False,'enabled':False}}
a=sys.argv[2]
if a=='show':
 print('ActiveState='+('active' if s['active'] else 'inactive'))
 print('UnitFileState='+('enabled' if s['enabled'] else 'disabled'))
 print('LoadState=loaded')
elif a=='daemon-reload': pass
else:
 if a in ('start','restart'): s['active']=True
 elif a=='stop': s['active']=False
 elif a=='enable': s['enabled']=True
 elif a=='disable': s['enabled']=False
 else: raise SystemExit(2)
 p.write_text(json.dumps(s))
""",
        )
        target = self.home / ".config/systemd/user/fixture.service"
        target.parent.mkdir(parents=True)
        target.write_text("[Service]\nExecStart=/usr/bin/true\n")
        self.fragment(
            {
                "services": [
                    {
                        "id": "fixture",
                        "manager": "systemd-user",
                        "name": "fixture.service",
                        "target": ".config/systemd/user/fixture.service",
                        "platforms": ["linux-x64"],
                    }
                ]
            }
        )
        context = self.compose()
        context.update({"_verification_only": True, "_fixture_context": True})
        context["executables"]["systemctl"] = binary
        save(
            Path(context["paths"]["state"]) / "files.json",
            {
                "home": context["home"],
                "files": {target.relative_to(self.home).as_posix(): safety.fingerprint(target)},
                "generation": "fixture",
            },
        )
        services.control(context, "enable")
        services.control(context, "start")
        self.assertTrue(json.loads(state_file.read_text())["active"])
        services.control(context, "stop")
        services.control(context, "disable")
        self.assertFalse(json.loads(state_file.read_text())["enabled"])
        restored = services.control(context, "recover", ["fixture"])
        self.assertTrue(restored["fixture"]["selection"]["UnitFileState"] == "enabled")
        self.assertFalse(json.loads(state_file.read_text())["active"])
        target.write_text("foreign replacement")
        with self.assertRaises(safety.Error):
            services.control(context, "start")

    def test_recipe_cannot_claim_wrong_artifact_digest(self):
        artifact = self.home / "claimed"
        artifact.parent.mkdir()
        artifact.write_text("actual")
        result = {
            "id": "fixture",
            "status": "verified",
            "artifacts": [{"path": str(artifact), "sha256": "0" * 64}],
            "executables": {},
        }
        path = self.root / "native/claim.py"
        path.write_text("print(" + repr(json.dumps(result)) + ")\n")
        self.fragment(
            {"packages": [{"id": "fixture", "provider": "recipe", "path": "native/claim.py"}]},
            private=True,
        )
        context = self.compose()
        with self.assertRaisesRegex(safety.Error, "artifact readback"):
            providers.package_status(context, context["packages"][0], self.path(context))


@unittest.skipUnless(
    os.environ.get("DOTFILES_TEST_MISE"),
    "set DOTFILES_TEST_MISE to trusted official native binary for config discovery proof",
)
class MiseIsolationTests(Fixture):
    def test_real_mise_ignores_hostile_home_and_ancestor_configs(self):
        canonical = self.base / "apps/mise"
        canonical.mkdir(parents=True)
        (canonical / "mise.toml").write_text("[tools]\n")
        (canonical / "mise.lock").write_text("lockfile_version = 3\n")
        (self.directory / "mise.toml").write_text(
            '[env]\nDOTFILES_HOSTILE_ANCESTOR = "must-not-load"\n'
        )
        hostile_home = self.home / ".config/mise/config.toml"
        hostile_home.parent.mkdir(parents=True)
        hostile_home.write_text('[env]\nDOTFILES_HOSTILE_HOME = "must-not-load"\n')
        context = self.compose()
        context["executables"]["mise"] = str(Path(os.environ["DOTFILES_TEST_MISE"]).resolve())
        text = providers.mise_run(context, ["config", "ls", "--json"])
        json.loads(text)
        self.assertNotIn(str(hostile_home), text)
        self.assertNotIn(str(self.directory / "mise.toml"), text)
        self.assertIn("dotfiles-mise-", text)


class LockAndCandidateTests(Fixture):
    def test_native_npm_graph_digest_and_exact_manifest_binding(self):
        root = self.base / "apps/mise"
        graph_root = root / ".mise/locks/demo/1.0.0"
        graph_root.mkdir(parents=True)
        graph = graph_root / "aube-lock.yaml"
        graph.write_text(
            "lockfileVersion: '9.0'\npackages:\n  demo@1.0.0:\n    resolution: {integrity: sha512-c3ludGhldGlj}\n"
        )
        save(
            graph_root / "package.json",
            {"name": "mise-npm-install", "private": True, "dependencies": {"demo": "1.0.0"}},
        )
        (root / "mise.toml").write_text('[tool_alias]\ndemo="npm:demo"\n[tools]\ndemo="1.0.0"\n')
        (root / "mise.lock").write_text(
            'lockfile_version=3\n[[tools.demo]]\nversion="1.0.0"\nbackend="npm:demo"\naube={path=".mise/locks/demo/1.0.0",digest="sha256:'
            + safety.digest(graph)
            + '"}\n'
        )
        package = {"id": "demo", "provider": "mise", "tool": "demo", "version": "1.0.0"}
        context = self.compose()
        metadata = providers.mise_metadata(context, package)
        self.assertEqual(metadata["graph"]["approved_builds"], [])
        graph.write_bytes(graph.read_bytes().replace(b"\n", b"\r\n"))
        providers.mise_metadata(context, package)
        graph.write_text("tampered")
        with self.assertRaisesRegex(safety.Error, "graph digest"):
            providers.mise_metadata(context, package)

    def test_candidate_promotion_and_foreign_aware_recovery(self):
        context = self.compose()
        source_file = self.directory / "reviewed/candidate.json"
        save(source_file, {"schema_version": 1, "defaults": {"identity": {"name": "Candidate"}}})
        bundle = self.directory / "reviewed/bundle.json"
        save(
            bundle,
            {
                "schema_version": 1,
                "files": [
                    {
                        "repository": "private",
                        "path": "native/manifests/candidate.json",
                        "source": "candidate.json",
                        "sha256": safety.digest(source_file),
                    }
                ],
            },
        )
        staged = updates.stage_candidate(context, bundle)
        target = self.root / "native/manifests/candidate.json"
        self.assertFalse(target.exists())
        updates.promote(context, staged["id"])
        self.assertEqual(safety.digest(target), safety.digest(source_file))
        target.write_text("foreign source edit")
        with self.assertRaisesRegex(safety.Error, "foreign source edit"):
            updates.recover_candidate(context, staged["id"])
        target.write_bytes(source_file.read_bytes())
        updates.recover_candidate(context, staged["id"])
        self.assertFalse(target.exists())

    def test_owned_artifact_retirement_retains_payload_and_recovers_links(self):
        archive = self.directory / "fixture.tar"
        with tarfile.open(archive, "w") as stream:
            payload = b"#!/bin/sh\nexit 0\n"
            item = tarfile.TarInfo("bin/fixture")
            item.size, item.mode = len(payload), 0o755
            stream.addfile(item, io.BytesIO(payload))
        spec = {
            "url": "https://example.invalid/fixture.tar",
            "sha256": safety.digest(archive),
            "archive": "tar",
            "bin": {"fixture": "bin/fixture"},
        }
        self.fragment(
            {
                "packages": [
                    {
                        "id": "fixture",
                        "provider": "artifact",
                        "version": "1.0.0",
                        "platforms": ["linux-x64"],
                        "sources": {"linux-x64": spec},
                    }
                ]
            }
        )
        context = self.compose()
        cached = Path(context["paths"]["cache"]) / "downloads" / spec["sha256"]
        cached.parent.mkdir(parents=True)
        shutil.copyfile(archive, cached)
        records = {"home": context["home"], "packages": {}, "links": {}}
        providers.provision(context, self.path(context), records)
        module_spec = importlib.util.spec_from_file_location(
            "engine_entry_fixture", ENGINE / "__main__.py"
        )
        entry = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(entry)
        link = Path(context["paths"]["bin"]) / "fixture"
        runtime = providers.prefix(context, context["packages"][0])
        entry.retire_packages(context, self.path(context), ["fixture"])
        self.assertFalse(link.exists())
        self.assertTrue(runtime.is_dir())
        from recovery import recover_packages

        recover_packages(context, self.path(context), ["fixture"])
        self.assertTrue(link.is_symlink())
        entry.retire_packages(context, self.path(context), ["fixture"])
        link.write_text("foreign replacement")
        with self.assertRaisesRegex(safety.Error, "foreign replacement"):
            recover_packages(context, self.path(context), ["fixture"])
        self.assertEqual(link.read_text(), "foreign replacement")


class GuiAdapterIntegrationTests(Fixture):
    def test_real_macos_verifier_wraps_cask_lifecycle_without_owning_payload(self):
        adapter = self.base / "native/gui/macos.py"
        adapter.parent.mkdir()
        shutil.copyfile(PUBLIC / "native/gui/macos.py", adapter)
        root = self.home / "native-root"
        app = root / "Applications/Fixture.app"
        info_path, calls = self.home / "brew-info.json", self.home / "brew-calls"
        save(
            info_path,
            {
                "casks": [
                    {
                        "token": "fixture",
                        "version": "1.0.0",
                        "installed": None,
                        "artifacts": [{"app": ["Fixture.app"]}],
                    }
                ]
            },
        )
        brew = self.home / "fixture-brew"
        brew.write_text(
            "#!"
            + sys.executable
            + "\n"
            + f"""import json,pathlib,plistlib,struct,sys
p=pathlib.Path({str(info_path)!r}); info=json.loads(p.read_text()); action=sys.argv[1]
if action=='info': print(json.dumps(info))
elif action in ('install','upgrade'):
 app=pathlib.Path({str(app)!r}); binary=app/'Contents/MacOS/fixture'; binary.parent.mkdir(parents=True,exist_ok=True)
 binary.write_bytes(struct.pack('<IIIIIIII',0xFEEDFACF,0x0100000C,0,2,0,0,0,0)); binary.chmod(0o755)
 (app/'Contents/Info.plist').write_bytes(plistlib.dumps({{'CFBundleIdentifier':'com.example.fixture','CFBundleShortVersionString':'1.0.0','CFBundleExecutable':'fixture'}}))
 info['casks'][0]['installed']='1.0.0';p.write_text(json.dumps(info))
 log=pathlib.Path({str(calls)!r});log.write_text((log.read_text() if log.exists() else '')+action+'\\n')
else: raise SystemExit(9)
"""
        )
        brew.chmod(0o755)
        self.fragment(
            {
                "packages": [
                    {
                        "id": "fixture",
                        "provider": "cask",
                        "name": "fixture",
                        "platforms": ["macos-arm64"],
                        "macos": {
                            "app": {
                                "name": "Fixture.app",
                                "bundle_id": "com.example.fixture",
                                "binary": "Contents/MacOS/fixture",
                            }
                        },
                    }
                ]
            }
        )
        context = self.compose(platform="macos-arm64")
        context.update(
            {"_fixture_context": True, "_verification_only": True, "_macos_fixture_root": str(root)}
        )
        context["executables"]["brew"] = str(brew)
        records = {"home": context["home"], "packages": {}, "links": {}}
        providers.provision(context, self.path(context), records)
        self.assertEqual(calls.read_text(), "install\n")
        self.assertEqual(
            records["packages"]["fixture"]["observed"]["macos"]["architecture"], "arm64"
        )
        # An existing, verified provider installation remains preexisting and
        # is not silently upgraded/adopted as removable engine-owned payload.
        existing = {"home": context["home"], "packages": {}, "links": {}}
        providers.provision(context, self.path(context), existing)
        self.assertTrue(existing["packages"]["fixture"]["preexisting"])
        self.assertEqual(calls.read_text(), "install\n")
        # Restore the genuinely owned engine receipt, then replace app bytes.
        save(Path(context["paths"]["state"]) / "packages.json", records)
        binary = app / "Contents/MacOS/fixture"
        binary.write_bytes(binary.read_bytes() + b"foreign")
        module_spec = importlib.util.spec_from_file_location(
            "engine_gui_entry_fixture", ENGINE / "__main__.py"
        )
        entry = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(entry)
        with self.assertRaisesRegex(safety.Error, "package changed"):
            entry.retire_packages(context, self.path(context), ["fixture"])
        self.assertTrue(app.exists())
        self.assertEqual(calls.read_text(), "install\n")

    def test_public_gui_freshness_resolvers_do_not_install(self):
        snap = {
            "channel-map": [
                {
                    "channel": {
                        "name": "stable",
                        "track": "latest",
                        "risk": "stable",
                        "architecture": "amd64",
                    },
                    "version": "123.4",
                    "revision": 42,
                    "download": {
                        "url": "https://example.invalid/chromium.snap",
                        "sha3-384": "a" * 96,
                    },
                }
            ]
        }
        with patch.object(updates, "public_json", return_value=snap):
            row = updates.upstream({"update": {"provider": "snap", "name": "chromium"}})
            self.assertEqual(row["revision"], 42)
            self.assertEqual(row["version"], "123.4")
        packages = b"Package: slack-desktop\nVersion: 4.9.0\nArchitecture: amd64\n\nPackage: slack-desktop\nVersion: 4.10.0\nArchitecture: amd64\n\n"
        with patch.object(updates, "public_bytes", return_value=packages):
            row = updates.upstream(
                {
                    "update": {
                        "provider": "apt-repository",
                        "url": "https://example.invalid/Packages",
                        "package": "slack-desktop",
                    }
                }
            )
            self.assertEqual(row["version"], "4.10.0")
        with patch.object(
            updates, "public_bytes", return_value=b"<h2><span>Slack</span> 4.52.182</h2>"
        ):
            self.assertEqual(
                updates.upstream({"update": {"provider": "slack-macos"}})["version"], "4.52.182"
            )
        self.assertLess(updates.compare_debian_versions("1.0~rc1", "1.0"), 0)
        self.assertGreater(updates.compare_debian_versions("1:1.0", "0:99.0"), 0)


if __name__ == "__main__":
    unittest.main()
