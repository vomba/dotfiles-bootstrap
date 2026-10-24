"""Verification fixtures use disposable HOME, tiny owned artifacts and real subprocesses.

No gate runs at import; no provider is installed or contacted by these fixtures.
Full checker integration additionally needs the explicitly provisioned pinned tools.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import patch

PUBLIC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PUBLIC / "native/engine"))
import providers
import safety

spec = importlib.util.spec_from_file_location(
    "native_verification_fixture", PUBLIC / "native/verification/verify.py"
)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dotfiles-verification-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "native/fixtures").mkdir(parents=True)
        self.context = {
            "base": str(PUBLIC),
            "root": str(self.repo),
            "home": str(self.home),
            "platform": "linux-x64",
            "dotfiles": {},
            "packages": [],
            "services": [],
            "_fixture_context": True,
            "_verification_only": True,
            "_verification_executables": {"git": "/usr/bin/git", "bash": "/bin/bash"},
            "paths": {
                key: str(self.home / path)
                for key, path in {
                    "runtime": "data/runtime",
                    "cache": "cache",
                    "state": "state",
                }.items()
            },
            "executables": {"ruff": "/nix/store/hostile/bin/ruff"},
        }
        self.log = self.root / "commands.jsonl"

    def executable(self, name, body):
        path = self.root / "tools" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)
        return str(path)

    def logging_tool(self, name, exit_code=0):
        return self.executable(
            name,
            "import json, os, sys\n"
            f"with open({str(self.log)!r}, 'a') as stream:\n"
            "    stream.write(json.dumps({'argv':sys.argv,'home':os.environ.get('HOME'),"
            "'token':os.environ.get('SECRET_TOKEN'),'auto':os.environ.get('MISE_AUTO_INSTALL'),"
            "'base':os.environ.get('DOTFILES_BOOTSTRAP_SOURCE')})+'\\n')\n"
            f"print('SYNTHETIC_SCANNER_SECRET')\nsys.exit({exit_code})\n",
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def host(self):
        return verify.host_context(self.context)

    def receipt(self, name, selection, files):
        context = self.host()
        target = verify.destination(context, name, selection)
        target.mkdir(parents=True)
        for relative, data in files.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o755)
        save(
            target / verify.RECEIPT,
            {
                "schema_version": 1,
                "selection": selection,
                "platform": context["platform"],
                "files": safety.inventory(target),
            },
        )
        return target

    def tiny_archive(self, data=b"#!/bin/sh\nexit 0\n"):
        archive = self.root / "fixture.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            item = tarfile.TarInfo("bundle/tool")
            item.size = len(data)
            item.mode = 0o755
            stream.addfile(item, io.BytesIO(data))
        checksum = safety.digest(archive)
        selection = {
            "version": "1.0",
            "bin": "bundle/tool",
            "sources": [
                {"url": "https://fixture.invalid/tool.tar.gz", "sha256": checksum, "archive": "tar"}
            ],
        }
        cache = Path(self.context["paths"]["cache"]) / "verification/downloads"
        cache.mkdir(parents=True)
        (cache / checksum).write_bytes(archive.read_bytes())
        return selection

    def canonical_specs(self, lock_text, host):
        lock = self.repo / "canonical.lock"
        lock.write_text(lock_text)
        pins = self.repo / "verification.json"
        save(
            pins,
            {
                "tools": {
                    "gitleaks": {
                        "canonical_mise_tool": "gitleaks",
                        "canonical_lock": lock.name,
                    }
                },
                "schema_wheels": {},
            },
        )
        with patch.object(verify, "BASE", self.repo), patch.object(verify, "LOCK", pins):
            return verify.tool_specs({**self.context, "platform": host})["gitleaks"]

    def test_gitleaks_selection_reuses_actual_generated_canonical_lock_on_both_hosts(self):
        pin = safety.load(verify.LOCK)["tools"]["gitleaks"]
        lock = PUBLIC / pin["canonical_lock"]
        original = lock.read_bytes()
        records = tomllib.loads(original.decode())["tools"][pin["canonical_mise_tool"]]
        record = records[0] if isinstance(records, list) else records
        for host in ("macos-arm64", "linux-x64"):
            with self.subTest(host=host):
                context = {**self.context, "platform": host}
                metadata = providers.mise_metadata(
                    context,
                    {"tool": pin["canonical_mise_tool"], "version": record["version"]},
                )
                entry = metadata["selection"]
                artifact = entry.get("platforms." + host, entry.get("platforms", {}).get(host))
                selected = verify.tool_specs(context)["gitleaks"]
                self.assertEqual(selected["version"], record["version"])
                self.assertEqual(
                    selected["sources"],
                    [
                        {
                            "url": artifact["url"],
                            "sha256": artifact["checksum"].removeprefix("sha256:"),
                            "archive": "tar",
                            "bin": "gitleaks",
                        }
                    ],
                )
                self.assertEqual(selected["bin"], "gitleaks")
        self.assertEqual(lock.read_bytes(), original)

    def test_canonical_selection_supports_nested_and_literal_dotted_platform_tables(self):
        checksum = "a" * 64
        for host in ("macos-arm64", "linux-x64"):
            for table in ("[tools.gitleaks]", "[[tools.gitleaks]]"):
                for platform_table in (
                    f'[tools.gitleaks."platforms.{host}"]',
                    f"[tools.gitleaks.platforms.{host}]",
                ):
                    with self.subTest(host=host, table=table, platform_table=platform_table):
                        url = f"https://fixture.invalid/{host}/gitleaks.tar.gz"
                        selected = self.canonical_specs(
                            f'{table}\nversion = "fixture-version"\n'
                            f'{platform_table}\nurl = "{url}"\nchecksum = "sha256:{checksum}"\n',
                            host,
                        )
                        self.assertEqual(
                            selected,
                            {
                                "version": "fixture-version",
                                "sources": [
                                    {
                                        "url": url,
                                        "sha256": checksum,
                                        "archive": "tar",
                                        "bin": "gitleaks",
                                    }
                                ],
                                "bin": "gitleaks",
                            },
                        )

    def test_canonical_selection_rejects_missing_or_malformed_host_artifact_and_checksum(self):
        prefix = '[[tools.gitleaks]]\nversion = "fixture-version"\n'
        url = 'url = "https://fixture.invalid/gitleaks.tar.gz"\n'
        checksum = f'checksum = "sha256:{"a" * 64}"\n'
        malformed_artifacts = (
            "",
            'url = ""\n' + checksum,
            "url = 17\n" + checksum,
            'url = "not-an-artifact-url"\n' + checksum,
            'url = "https:///missing-host.tar.gz"\n' + checksum,
            'url = "https://[invalid"\n' + checksum,
            'url = "https://user:credential@fixture.invalid/tool.tar.gz"\n' + checksum,
            url,
            url + "checksum = 17\n",
            url + 'checksum = "sha512:' + "a" * 64 + '"\n',
            url + 'checksum = "sha256:short"\n',
            url + 'checksum = "sha256:' + "a" * 65 + '"\n',
            url + 'checksum = "sha256:' + "g" * 64 + '"\n',
            url + 'checksum = "sha256:' + "A" * 64 + '"\n',
        )
        for host in ("macos-arm64", "linux-x64"):
            for platform_table in (
                f'[tools.gitleaks."platforms.{host}"]\n',
                f"[tools.gitleaks.platforms.{host}]\n",
            ):
                for artifact in malformed_artifacts:
                    with self.subTest(host=host, platform_table=platform_table, artifact=artifact):
                        with self.assertRaises(safety.Error):
                            self.canonical_specs(prefix + platform_table + artifact, host)
            for malformed in (
                prefix,
                prefix + 'platforms = "invalid"\n',
                prefix + f'"platforms.{host}" = "invalid"\n',
            ):
                with self.subTest(host=host, malformed=malformed):
                    with self.assertRaises(safety.Error):
                        self.canonical_specs(malformed, host)

    def test_host_tools_ignore_disabled_target_profile_and_inherited_executables(self):
        other = "macos-arm64" if self.host()["platform"] == "linux-x64" else "linux-x64"
        self.context["platform"] = other
        self.context["packages"] = [{"id": "target", "provider": "absent"}]
        host = self.host()
        self.assertNotEqual(host["platform"], other)
        self.assertEqual(host["packages"], [])
        self.assertEqual(set(host["executables"]), {"python3"})
        self.assertEqual(self.context["packages"][0]["id"], "target")
        prerequisites = verify.host_prerequisites(self.context)
        self.assertEqual({p["id"] for p in prerequisites}, {"git", "bash", "zsh"})
        self.assertTrue(
            all("when" not in p and host["platform"] in p["platforms"] for p in prerequisites)
        )

    def test_nix_interpreter_and_unsupported_hosts_fail_before_acquisition(self):
        with (
            patch.object(verify.sys, "executable", "/nix/store/python/bin/python3"),
            patch.object(verify, "download", side_effect=AssertionError("network")),
        ):
            with self.assertRaisesRegex(safety.Error, "non-native"):
                self.host()
        with patch.object(
            verify.manifest, "platform", side_effect=safety.Error("unsupported platform")
        ):
            with self.assertRaisesRegex(safety.Error, "unsupported"):
                self.host()

    def test_scope_filters_secrets_only_from_format_and_refuses_escape(self):
        (self.repo / "secrets.yaml").write_text("not: formatter-input\n")
        (self.repo / "source.py").write_text("x=1\n")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules/vendor.py").write_text("broken")
        files = ["secrets.yaml", "source.py", "node_modules/vendor.py", "deleted.py"]
        formatted = verify.selected_files(self.host(), files, formatting=True)
        scanned = verify.selected_files(self.host(), files)
        self.assertEqual([p.name for p in formatted], ["source.py"])
        self.assertEqual([p.name for p in scanned], ["secrets.yaml", "source.py"])
        with self.assertRaises(safety.Error):
            verify.selected_files(self.host(), ["../outside"])
        (self.repo / "link.py").symlink_to(self.repo / "source.py")
        with self.assertRaisesRegex(safety.Error, "symlink"):
            verify.selected_files(self.host(), ["link.py"])

    def test_integrity_inputs_are_not_formatted_but_remain_syntax_checked_and_scanned(self):
        inputs = {
            "apps/mise/.mise/locks/server/1/package.json": b'{"upstream":true}',
            "apps/mise/.mise/locks/server/1/aube.yaml": b"upstream: exact\n",
            "apps/sample/package.json": b'{"upstream":true}',
            "apps/sample/pnpm-lock.yaml": b"lockfileVersion: '9.0'\n",
            "native/recipes/tools/wheels.lock.json": b"{broken",
            "native/recipes/desktop/sources.json": b'{"hash_bound":true}',
            "native/gui/linux-sources.json": b'{"hash_bound":true}',
            "native/sample/vendor/copied.py": b"def upstream( ):pass\n",
            "native/upstream/broken.sh": b"#!/bin/bash\nif then\n",
            "native/assets/nord/theme.json": b'{"upstream":true}',
            "package-lock.json": b'{"lockfileVersion":3}',
            "unclassified.json": b'{"not_an_authored_json_scope":true}',
        }
        for name, content in inputs.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        with patch.object(
            verify, "readback", side_effect=AssertionError("no formatter tools required")
        ):
            verify.format_files(self.context, list(inputs))
        self.assertEqual({name: (self.repo / name).read_bytes() for name in inputs}, inputs)
        scanner = self.executable(
            "gitleaks",
            "import json, pathlib, sys\n"
            "root=pathlib.Path(sys.argv[-1])\n"
            f"pathlib.Path({str(self.log)!r}).write_text(json.dumps({{'seen':sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file())}})+'\\n')\n",
        )
        tools = {
            "gitleaks": scanner,
            "schema": str(self.root / "uninstalled-schema"),
            "ruff": "/missing/ruff",
            "shellcheck": "/missing/shellcheck",
        }
        with patch.object(
            verify, "readback", side_effect=lambda context, name, selection: tools[name]
        ):
            results = verify.check(self.context, list(inputs), run_tests=False)
        statuses = {result["name"]: result["status"] for result in results}
        self.assertEqual(statuses["python-format"], "passed")
        self.assertEqual(statuses["json-format"], "passed")
        self.assertEqual(statuses["json-syntax"], "failed")
        self.assertFalse(any(name.startswith("shell-syntax:") for name in statuses))
        self.assertEqual(set(self.calls()[0]["seen"]), set(inputs))
        self.assertEqual({name: (self.repo / name).read_bytes() for name in inputs}, inputs)

    def test_static_checks_reject_missing_foreign_platform_download_integrity(self):
        self.context["base"] = str(self.repo)
        pins = self.repo / "native/engine/pins.json"
        pins.parent.mkdir(parents=True)
        shutil.copyfile(PUBLIC / "native/engine/pins.json", pins)
        save(
            self.repo / "native/manifests/base.json",
            {
                "schema_version": 1,
                "packages": [
                    {
                        "id": "sample",
                        "provider": "mise",
                        "tool": "sample",
                        "version": "1.0.0",
                        "platforms": ["linux-x64", "macos-arm64"],
                    }
                ],
            },
        )
        directory = self.repo / "apps/mise"
        directory.mkdir(parents=True)
        (directory / "mise.toml").write_text(
            '[tool_alias]\nsample = "github:fixture/sample"\n[tools]\nsample = "1.0.0"\n'
        )
        header = (
            'lockfile_version = 1\n[[tools.sample]]\nversion = "1.0.0"\n'
            'backend = "github:fixture/sample"\nspecifiers = ["1.0.0"]\n'
        )
        digest = 'checksum = "sha256:' + "a" * 64 + '"\n'
        linux = (
            '[tools.sample."platforms.linux-x64"]\n'
            'url = "https://publisher.example.invalid/linux"\n' + digest
        )
        mac = (
            '[tools.sample."platforms.macos-arm64"]\n'
            'url = "https://publisher.example.invalid/macos"\n'
        )
        lock = directory / "mise.lock"
        lock.write_text(header + linux + mac + digest)
        with patch.object(
            verify, "download", side_effect=AssertionError("static check downloaded")
        ):
            results = verify.check(self.context, [], run_tests=False)
            integrity = next(result for result in results if result["name"] == "provider-locks")
            self.assertEqual(integrity["status"], "passed", integrity["detail"])
            lock.write_text(header + linux + mac)
            before = lock.read_bytes()
            results = verify.check(self.context, [], run_tests=False)
        integrity = next(result for result in results if result["name"] == "provider-locks")
        self.assertEqual(integrity["status"], "failed")
        self.assertIn("sample", integrity["detail"])
        self.assertEqual(lock.read_bytes(), before)

    @unittest.skipUnless(os.environ.get("DOTFILES_TEST_CHEZMOI"), "native chezmoi required")
    def test_static_only_checks_reject_invalid_rendered_shell(self):
        source = self.repo / "native/chezmoi/dot_local/bin/executable_sample.tmpl"
        source.parent.mkdir(parents=True)
        source.write_text("#!/bin/bash\n{{ if true }}if then\n{{ end }}")
        save(self.repo / "native/manifests/base.json", {"schema_version": 1})
        pins = self.repo / "native/engine/pins.json"
        pins.parent.mkdir(parents=True)
        shutil.copyfile(PUBLIC / "native/engine/pins.json", pins)
        self.context.update(base=str(self.repo))
        tools = {name: self.logging_tool(name) for name in ("ruff", "gitleaks", "shellcheck")}
        tools["schema"] = str(self.root / "uninstalled-schema")
        tools["node"] = sys.executable  # No behavior/Node process is started in this scenario.

        def qualify(context, unused):
            context["_verification_test_tools"] = {
                "DOTFILES_TEST_CHEZMOI": os.environ["DOTFILES_TEST_CHEZMOI"]
            }

        with (
            patch.object(verify, "fixture_tools", side_effect=qualify),
            patch.object(verify, "readback", side_effect=lambda c, name, s: tools[name]),
        ):
            results = verify.check(self.context, [str(source.relative_to(self.repo))], False)
            rendered = next(result for result in results if result["name"] == "rendered-shells")
            self.assertEqual(rendered["status"], "failed")
            self.assertIn("syntax", rendered["detail"])
            source.write_text("#!/bin/bash\n{{ if true }}printf 'valid\\n'\n{{ end }}")
            results = verify.check(self.context, [str(source.relative_to(self.repo))], False)
            rendered = next(result for result in results if result["name"] == "rendered-shells")
            self.assertEqual(rendered["status"], "passed", rendered["detail"])

    def test_git_union_scans_untracked_and_current_worktree_not_only_head(self):
        git = "/usr/bin/git"
        env = {**os.environ, "HOME": str(self.home), "GIT_CONFIG_GLOBAL": "/dev/null"}
        subprocess.run([git, "init", "-q", str(self.repo)], env=env, check=True)
        (self.repo / "tracked.py").write_text("before\n")
        subprocess.run([git, "-C", str(self.repo), "add", "tracked.py"], env=env, check=True)
        (self.repo / "tracked.py").write_text("after\n")
        (self.repo / "new file.py").write_text("new\n")
        (self.repo / ".gitignore").write_text("ignored\n")
        (self.repo / "ignored").write_text("ignored\n")
        union = verify.file_union(self.host())
        self.assertIn("tracked.py", union)
        self.assertIn("new file.py", union)
        self.assertNotIn("ignored", union)

    def test_actual_commands_fail_independently_and_scanner_never_discloses_output(self):
        (self.repo / "source.py").write_text("x=1\n")
        (self.repo / "broken.sh").write_text("#!/bin/bash\nif then\n")
        (self.repo / "native/fixtures/bad.json").write_text('{"x":1}')
        tools = {
            name: self.logging_tool(name, 7 if name in ("ruff", "gitleaks") else 0)
            for name in ("ruff", "gitleaks", "shellcheck")
        }
        tools["schema"] = str(self.root / "uninstalled-schema")
        with (
            patch.object(
                verify, "readback", side_effect=lambda context, name, selection: tools[name]
            ),
            patch.object(verify, "download", side_effect=AssertionError("no implicit downloads")),
            patch.dict(os.environ, {"SECRET_TOKEN": "must-not-propagate"}),
        ):
            results = verify.check(
                self.context,
                ["source.py", "broken.sh", "native/fixtures/bad.json"],
                run_tests=False,
            )
        statuses = {r["name"]: r["status"] for r in results}
        self.assertEqual(statuses["python-format"], "failed")
        self.assertEqual(statuses["json-format"], "failed")
        self.assertEqual(statuses["json-schema"], "failed")
        self.assertEqual(statuses["shell-syntax:broken.sh"], "failed")
        self.assertEqual(statuses["shellcheck:broken.sh"], "passed")
        self.assertEqual(statuses["gitleaks"], "failed")
        scan = next(r for r in results if r["name"] == "gitleaks")
        self.assertNotIn("SYNTHETIC_SCANNER_SECRET", scan["detail"])
        calls = self.calls()
        self.assertTrue(all(c["home"] != str(self.home) and c["token"] is None for c in calls))
        self.assertTrue(all(c["auto"] == "0" for c in calls))
        self.assertTrue(all(c["base"] == str(PUBLIC) for c in calls))
        ruff = next(c["argv"] for c in calls if Path(c["argv"][0]).name == "ruff")
        self.assertIn("--check", ruff)
        self.assertIn(str(verify.CONFIG), ruff)
        scanargv = next(c["argv"] for c in calls if Path(c["argv"][0]).name == "gitleaks")
        self.assertIn("--redact=100", scanargv)
        self.assertIn("--config", scanargv)
        self.assertIn("--gitleaks-ignore-path", scanargv)

    def test_missing_tools_aggregate_without_creating_state_or_network(self):
        (self.repo / "source.py").write_text("x = 1\n")
        with patch.object(verify, "download", side_effect=AssertionError("network")):
            results = verify.check(self.context, ["source.py"], run_tests=False)
        failed = {
            r["name"]
            for r in results
            if r["name"].startswith("prerequisite:") and r["status"] == "failed"
        }
        self.assertEqual(
            failed, {"prerequisite:" + name for name in verify.tool_specs(self.host())}
        )
        self.assertFalse(Path(self.context["paths"]["runtime"]).exists())
        self.assertFalse(Path(self.context["paths"]["state"]).exists())
        self.assertTrue(any("--context" in r["detail"] for r in results))

    def test_explicit_format_union_has_serial_ruff_and_native_json_without_yaml(self):
        (self.repo / "only.py").write_text("x=1\n")
        (self.repo / "untouched.py").write_text("y=2\n")
        (self.repo / "native/fixtures/selected.json").write_text('{"value":[1,2]}')
        encrypted = b"sops: {encrypted: unchanged}\n"
        (self.repo / "secrets.yaml").write_bytes(encrypted)
        ruff = self.logging_tool("ruff")
        with (
            patch.object(verify, "readback", return_value=ruff),
            patch.object(verify, "download", side_effect=AssertionError("network")),
        ):
            verify.format_files(
                self.context, ["only.py", "native/fixtures/selected.json", "secrets.yaml"]
            )
        self.assertEqual(
            (self.repo / "native/fixtures/selected.json").read_text(),
            '{\n  "value": [\n    1,\n    2\n  ]\n}\n',
        )
        self.assertEqual((self.repo / "secrets.yaml").read_bytes(), encrypted)
        self.assertEqual((self.repo / "untouched.py").read_text(), "y=2\n")
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn(str(self.repo / "secrets.yaml"), self.calls()[0]["argv"])
        with self.assertRaisesRegex(safety.Error, "explicit"):
            verify.format_files(self.context, None)

    def test_format_refuses_concurrent_union_without_writing_state(self):
        import fcntl

        descriptor = os.open(self.repo, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(safety.Error, "source union lock"):
                verify.format_files(self.context, [])
        finally:
            os.close(descriptor)
        self.assertFalse(Path(self.context["paths"]["state"]).exists())

    def test_invalid_json_prevents_all_formatter_writes(self):
        original = '{"safe":1}'
        (self.repo / "native/fixtures/a.json").write_text(original)
        (self.repo / "native/fixtures/b.json").write_text("{broken")
        (self.repo / "source.py").write_text("x=1\n")
        with patch.object(verify, "invoke", side_effect=AssertionError("format must not start")):
            with self.assertRaises(ValueError):
                verify.format_files(
                    self.context, ["native/fixtures/a.json", "native/fixtures/b.json", "source.py"]
                )
        self.assertEqual((self.repo / "native/fixtures/a.json").read_text(), original)

    def test_cached_acquisition_is_versioned_verified_and_idempotent_without_global_links(self):
        selection = self.tiny_archive()
        with (
            patch.object(verify, "tool_specs", return_value={"tiny": selection}),
            patch("urllib.request.urlopen", side_effect=AssertionError("cache must be sufficient")),
        ):
            result = verify.provision(self.context)
            self.assertEqual(result, verify.provision(self.context))
        self.assertTrue(
            Path(result["tiny"]).is_relative_to(self.home / "data/runtime/verification")
        )
        self.assertFalse((self.home / ".local/bin").exists())
        target = verify.destination(self.host(), "tiny", selection)
        self.assertEqual(verify.readback(self.host(), "tiny", selection), result["tiny"])
        (target / "bundle/tool").write_text("changed\n")
        with self.assertRaisesRegex(safety.Error, "payload changed"):
            verify.readback(self.host(), "tiny", selection)

    def test_foreign_prefix_collision_preserves_data_and_refuses_download(self):
        selection = self.tiny_archive()
        target = verify.destination(self.host(), "tiny", selection)
        target.mkdir(parents=True)
        (target / "foreign").write_text("keep")
        with (
            patch.object(verify, "tool_specs", return_value={"tiny": selection}),
            patch.object(verify, "download", side_effect=AssertionError("no foreign replacement")),
        ):
            with self.assertRaisesRegex(safety.Error, "unavailable/unowned"):
                verify.provision(self.context)
        self.assertEqual((target / "foreign").read_text(), "keep")

    def test_corrupt_cached_artifact_never_publishes_receipt(self):
        selection = self.tiny_archive()
        checksum = selection["sources"][0]["sha256"]
        cached = Path(self.context["paths"]["cache"]) / "verification/downloads" / checksum
        cached.write_bytes(b"tampered")
        with patch.object(verify, "tool_specs", return_value={"tiny": selection}):
            with self.assertRaisesRegex(safety.Error, "corrupt cached"):
                verify.provision(self.context)
        self.assertFalse(verify.destination(self.host(), "tiny", selection).exists())

    def test_selection_drift_symlink_state_and_concurrent_acquisition_fail_closed(self):
        selection = self.tiny_archive()
        self.receipt("tiny", selection, {"bundle/tool": b"#!/bin/sh\nexit 0\n"})
        changed = copy.deepcopy(selection)
        changed["sources"][0]["sha256"] = "1" * 64
        with self.assertRaisesRegex(safety.Error, "provenance mismatch"):
            verify.readback(self.host(), "tiny", changed)
        state = Path(self.context["paths"]["state"]) / "verification"
        with (
            safety.locked(state),
            patch.object(verify, "tool_specs", return_value={"tiny": selection}),
        ):
            with self.assertRaisesRegex(safety.Error, "holds the lock"):
                verify.provision(self.context)
        state.rename(state.with_name("old"))
        state.symlink_to(state.with_name("old"))
        with self.assertRaisesRegex(safety.Error, "unsafe destination ancestor"):
            verify.provision(self.context)

    def test_unit_runner_executes_assertions_and_propagates_failure(self):
        tests = self.repo / "tests/native"
        tests.mkdir(parents=True)
        (tests / "test_behavior.py").write_text(
            "import unittest\n"
            "class Behavior(unittest.TestCase):\n"
            "    def test_failure(self):\n        self.assertEqual(2 + 2, 5)\n"
        )
        with self.assertRaisesRegex(safety.Error, "exited 1"):
            verify.invoke(
                self.host(), [sys.executable, "-I", "-B", verify.__file__, "_unit", self.repo]
            )
        (tests / "test_behavior.py").write_text(
            "import unittest\n"
            "class Behavior(unittest.TestCase):\n"
            "    def test_success(self):\n        self.assertEqual(2 + 2, 4)\n"
        )
        verify.invoke(
            self.host(), [sys.executable, "-I", "-B", verify.__file__, "_unit", self.repo]
        )

    def test_empty_unit_suite_is_failure_not_green(self):
        with self.assertRaisesRegex(safety.Error, "zero behavioral tests"):
            verify.invoke(
                self.host(), [sys.executable, "-I", "-B", verify.__file__, "_unit", self.repo]
            )

    def test_exclusive_publication_preserves_even_empty_foreign_directory(self):
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "owned").write_text("candidate")
        target = self.root / "collision"
        target.mkdir()
        with self.assertRaises(OSError):
            verify.publish_exclusive(stage, target)
        self.assertTrue((stage / "owned").exists())
        self.assertEqual(list(target.iterdir()), [])

    def test_archive_traversal_and_assembly_collisions_do_not_publish(self):
        first = self.tiny_archive()
        combined = copy.deepcopy(first)
        combined["sources"].append(copy.deepcopy(first["sources"][0]))
        with patch.object(verify, "tool_specs", return_value={"tiny": combined}):
            with self.assertRaisesRegex(safety.Error, "assembly collision"):
                verify.provision(self.context)
        self.assertFalse(verify.destination(self.host(), "tiny", combined).exists())
        archive = self.root / "traversal.tar"
        with tarfile.open(archive, "w") as stream:
            item = tarfile.TarInfo("../escape")
            item.size = 4
            stream.addfile(item, io.BytesIO(b"evil"))
        checksum = safety.digest(archive)
        source = copy.deepcopy(first)
        source["sources"][0]["sha256"] = checksum
        cache = Path(self.context["paths"]["cache"]) / "verification/downloads"
        (cache / checksum).write_bytes(archive.read_bytes())
        with patch.object(verify, "tool_specs", return_value={"tiny": source}):
            with self.assertRaises(safety.Error):
                verify.provision(self.context)
        self.assertFalse(verify.destination(self.host(), "tiny", source).exists())
        self.assertFalse((self.root / "escape").exists())

    def test_actual_schema_command_accepts_valid_and_rejects_invalid_manifest(self):
        library = os.environ.get("DOTFILES_VERIFICATION_SCHEMA_LIBRARY")
        if not library:
            self.skipTest(
                "requires declared schema tools; full verifier passes its readback-verified library"
            )
        path = self.repo / "native/manifests/fixture.json"
        save(
            path,
            {
                "schema_version": 1,
                "packages": [
                    {"id": "fixture", "provider": "apt", "name": "git", "platforms": ["linux-x64"]}
                ],
            },
        )
        argv = [
            sys.executable,
            "-I",
            "-B",
            verify.__file__,
            "_schema",
            library,
            self.repo,
            "native/manifests/fixture.json",
        ]
        verify.invoke(self.host(), argv)
        save(
            path,
            {
                "schema_version": 1,
                "packages": [{"id": "bad id", "provider": "invented", "platforms": ["wrong"]}],
            },
        )
        with self.assertRaisesRegex(safety.Error, "schema validation failed"):
            verify.invoke(self.host(), argv)

    def test_shell_detection_reads_real_shebang_and_excludes_zsh_from_shellcheck(self):
        for name, content, expected in (
            ("entry", "#!/bin/sh -p\n", "sh"),
            ("zsh-entry", "#!/usr/bin/env zsh\n", "zsh"),
            (".zshrc", "autoload -Uz compinit\n", "zsh"),
            ("installer.tmpl", "#!/bin/bash\n{{ if .enabled }}\n{{ end }}\n", None),
            ("script.py", "#!/usr/bin/env python3\n", None),
        ):
            path = self.repo / name
            path.write_text(content)
            self.assertEqual(verify.shell_kind(path), expected)

    def test_node_fixture_runtime_uses_canonical_lock_and_only_runtime_license_members(self):
        for host in ("linux-x64", "macos-arm64"):
            selection = verify.tool_specs({**self.context, "platform": host})["node"]
            record = tomllib.loads((PUBLIC / "apps/mise/mise.lock").read_text())["tools"]["node"][0]
            artifact = record.get("platforms." + host, record.get("platforms", {}).get(host))
            source = selection["sources"][0]
            self.assertEqual(source["url"], artifact["url"])
            self.assertEqual(source["sha256"], artifact["checksum"].removeprefix("sha256:"))
            self.assertEqual(
                source["members"],
                [selection["bin"], selection["bin"].removesuffix("/bin/node") + "/LICENSE"],
            )

    def test_host_context_discards_ambient_fixture_tool_overrides(self):
        self.context["_verification_test_tools"] = {
            "DOTFILES_TEST_MISE": "/hostile/mise",
            "PATH": "/hostile",
        }
        with patch.dict(
            os.environ,
            {"DOTFILES_TEST_MISE": "/ambient/mise", "DOTFILES_AI_NATIVE_NODE": "/ambient/node"},
        ):
            host = self.host()
            self.assertNotIn("_verification_test_tools", host)
            environment = verify.environment(host, self.root / "child")
            self.assertNotIn("DOTFILES_TEST_MISE", environment)
            self.assertNotIn("DOTFILES_AI_NATIVE_NODE", environment)
            self.assertNotIn("/hostile", environment["PATH"])

    def test_behavioral_reporting_exposes_counts_but_quiet_scanner_never_prints(self):
        from contextlib import redirect_stdout, redirect_stderr

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            verify.invoke(
                self.host(),
                [
                    sys.executable,
                    "-c",
                    "import sys; print('Ran 3 tests', file=sys.stderr); print('OK (skipped=1)', file=sys.stderr)",
                ],
                report=True,
            )
        self.assertIn("Ran 3 tests", err.getvalue())
        self.assertIn("skipped=1", err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            verify.invoke(
                self.host(),
                [sys.executable, "-c", "print('SYNTHETIC_SCANNER_SECRET')"],
                quiet=True,
                report=True,
            )
        self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_fixture_tool_environment_requires_bootstrap_receipt_readback(self):
        host = self.host()
        pins = {}
        paths = {}
        for name in ("mise", "chezmoi"):
            spec = {
                "url": "https://fixture.invalid/" + name,
                "sha256": "a" * 64,
                "bin": name,
                "archive": "tar",
            }
            pins[name] = {"version": "fixture", "platforms": {host["platform"]: spec}}
            package = {
                "id": "bootstrap/" + name,
                "version": "fixture",
                "platforms": {host["platform"]: spec},
            }
            target = providers.prefix(host, package)
            target.mkdir(parents=True)
            binary = target / name
            binary.write_text("#!/bin/sh\\nexit 0\\n")
            binary.chmod(0o755)
            paths[name] = binary
            save(
                target / ".dotfiles-artifact.json",
                {"source": spec, "version": "fixture", "files": safety.inventory(target)},
            )
        save(self.repo / "native/engine/pins.json", pins)
        node = self.executable("node-fixture", "pass\\n")
        with patch.object(verify, "BASE", self.repo):
            verify.fixture_tools(host, {"node": node})
            environment = verify.environment(host, self.root / "child")
            self.assertEqual(environment["DOTFILES_TEST_MISE"], str(paths["mise"]))
            self.assertEqual(environment["DOTFILES_TEST_CHEZMOI"], str(paths["chezmoi"]))
            self.assertEqual(environment["DOTFILES_AI_NATIVE_NODE"], node)
            paths["mise"].write_text("#!/bin/sh\\nexit 97\\n")
            fresh = self.host()
            with self.assertRaisesRegex(safety.Error, "payload verification"):
                verify.fixture_tools(fresh, {"node": node})
            self.assertNotIn("_verification_test_tools", fresh)


if __name__ == "__main__":
    unittest.main()
