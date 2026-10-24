"""Native-host regressions: disposable executables only, never live OS mutation."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[2] / "native/engine"
sys.path.insert(0, str(ENGINE))
import native_host
from safety import Error


class Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dotfiles-host-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve() / "home"
        self.home.mkdir()
        self.prefix = self.home / "prefix"
        self.clt = self.home / "clt"
        for name in ("git", "clang"):
            path = self.clt / "usr/bin" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture Apple tool\n")
            path.chmod(0o755)
        (self.clt / "SDKs/MacOSX.sdk").mkdir(parents=True)
        self.log = self.home / "commands.jsonl"
        self.control = self.home / "control.json"
        self.control.write_text("{}")
        self.payload = b"# verified fixture installer; not executable by the host\n"
        self.pin = native_host._pins()
        self.pin.update(sha256=hashlib.sha256(self.payload).hexdigest(), bytes=len(self.payload))
        self.context = {
            "home": str(self.home),
            "platform": "macos-arm64",
            "base": str(ENGINE.parent.parent),
            "paths": {"state": str(self.home / "state"), "cache": str(self.home / "cache")},
            "_fixture_context": True,
            "_verification_only": True,
            "executables": {},
        }
        tool_body = """import json,os,pathlib,sys
h=pathlib.Path(os.environ['HOME']); c=json.loads((h/'control.json').read_text())
a=sys.argv[1:]; name=pathlib.Path(sys.argv[0]).name; p=h/'prefix'; clt=h/'clt'
with (h/'commands.jsonl').open('a') as f:
 f.write(json.dumps({'tool':name,'args':a,'cwd':os.getcwd(),'env':dict(os.environ)})+'\\n')
if name=='xcode-select':
 if a==['--install']: raise SystemExit(0)
 if c.get('missing_clt'): raise SystemExit(2)
 print(c.get('selected',str(clt)))
elif name=='pkgutil':
 if c.get('receipt_failure'): raise SystemExit(1)
 print('package-id: com.apple.pkg.CLTools_Executables')
elif name=='xcrun':
 if c.get('license'): raise SystemExit(69)
 if '--find' in a: print(c.get('compiler',str(clt/'usr/bin/clang')))
 elif '--show-sdk-path' in a: print(c.get('sdk',str(clt/'SDKs/MacOSX.sdk')))
 else: print('Apple clang fixture')
elif name=='git':
 if 'config' in a: print(c.get('origin','https://github.com/Homebrew/brew'))
 elif 'rev-parse' in a: print('a'*40)
 elif 'cat-file' in a: print('#!/bin/bash\\n# fixture brew')
 else: raise SystemExit(3)
elif name=='brew':
 if a==['--prefix']: print('/foreign' if c.get('wrong_prefix') else p)
 elif a==['--repository']: print(p)
 elif a==['--version']: print('Homebrew fixture')
 else: raise SystemExit(4)
elif name=='bash':
 if c.get('installer_failure'): raise SystemExit(9)
 if c.get('installer_noop'): raise SystemExit(0)
 (p/'.git').mkdir(parents=True); (p/'Library/Homebrew').mkdir(parents=True)
 (p/'bin').mkdir(); b=p/'bin/brew'; b.write_text('#!/bin/bash\\n# fixture brew\\n'); b.chmod(0o755)
else: raise SystemExit(5)
"""
        for name in ("bash", "git", "brew", "xcode-select", "pkgutil", "xcrun"):
            path = self.home / "fixture-bin" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(f"#!{sys.executable}\n" + tool_body)
            path.chmod(0o755)
            self.context["executables"][name] = str(path)
        for target, value in (("PREFIX", self.prefix), ("CLT", self.clt)):
            manager = patch.object(native_host, target, value)
            manager.start()
            self.addCleanup(manager.stop)
        for target, value in (
            ("system", "Darwin"),
            ("machine", "arm64"),
            ("mac_ver", ("15.5", (), "arm64")),
        ):
            manager = patch.object(native_host.platform, target, return_value=value)
            manager.start()
            self.addCleanup(manager.stop)
        manager = patch.object(native_host.os, "geteuid", return_value=501)
        manager.start()
        self.addCleanup(manager.stop)
        manager = patch.object(native_host, "SYSTEM_BREW_ENV", self.home / "system-brew.env")
        manager.start()
        self.addCleanup(manager.stop)
        # The production pin loader remains exercised in dedicated tests. This
        # fixture source is never claimed to be upstream release metadata.
        manager = patch.object(native_host, "_pins", return_value=self.pin)
        self.real_pins = manager.get_original()[0]
        manager.start()
        self.addCleanup(manager.stop)
        self.opener = unittest.mock.Mock()
        self.opener.open.side_effect = lambda url, timeout: Response(self.payload, url)
        manager = patch.object(native_host.urllib.request, "build_opener", return_value=self.opener)
        manager.start()
        self.addCleanup(manager.stop)

    def commands(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def control_values(self, **values):
        self.control.write_text(json.dumps(values))

    def existing_brew(self):
        (self.prefix / ".git").mkdir(parents=True)
        (self.prefix / "Library/Homebrew").mkdir(parents=True)
        (self.prefix / "bin").mkdir()
        brew = self.prefix / "bin/brew"
        brew.write_text("#!/bin/bash\n# fixture brew\n")
        brew.chmod(0o755)
        return brew

    def test_missing_status_is_read_only_and_never_downloads(self):
        result = native_host.status(self.context)
        self.assertEqual(result["homebrew"]["status"], "missing")
        self.assertEqual(result["clt"]["status"], "ready")
        self.assertFalse(Path(self.context["paths"]["state"]).exists())
        self.opener.open.assert_not_called()
        self.assertFalse(any(c["tool"] == "bash" for c in self.commands()))

    def test_explicit_provision_reads_back_and_records_owned_shared_host(self):
        result = native_host.provision(self.context, yes=True)
        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(result["homebrew"]["ownership"], "dotfiles-provisioned-shared")
        record = json.loads((self.home / "state/native-host.json").read_text())
        self.assertEqual(record["installer"]["sha256"], self.pin["sha256"])
        self.assertEqual(
            record["prefix_identity"], [self.prefix.stat().st_dev, self.prefix.stat().st_ino]
        )
        before = len([c for c in self.commands() if c["tool"] == "bash"])
        self.assertEqual(native_host.provision(self.context, yes=True)["status"], "unchanged")
        self.assertEqual(len([c for c in self.commands() if c["tool"] == "bash"]), before)
        self.assertEqual(
            native_host.status(self.context)["homebrew"]["ownership"], "dotfiles-provisioned-shared"
        )

    def test_existing_official_brew_is_shared_not_adopted_or_reinstalled(self):
        brew = self.existing_brew()
        original = brew.read_bytes()
        self.assertEqual(
            native_host.provision(self.context, yes=True)["homebrew"]["ownership"],
            "preexisting-shared",
        )
        self.assertEqual(brew.read_bytes(), original)
        self.assertFalse((self.home / "state/native-host.json").exists())
        self.opener.open.assert_not_called()

    def test_foreign_prefix_file_directory_symlink_and_origin_are_rejected(self):
        self.prefix.write_text("foreign data")
        with self.assertRaisesRegex(Error, "foreign"):
            native_host.provision(self.context, yes=True)
        self.assertEqual(self.prefix.read_text(), "foreign data")
        self.prefix.unlink()
        self.prefix.mkdir()
        with self.assertRaisesRegex(Error, "foreign"):
            native_host.provision(self.context, yes=True)
        self.prefix.rmdir()
        self.prefix.symlink_to(self.clt, target_is_directory=True)
        with self.assertRaisesRegex(Error, "foreign"):
            native_host.provision(self.context, yes=True)
        self.prefix.unlink()
        self.existing_brew()
        self.control_values(origin="https://example.invalid/brew")
        with self.assertRaisesRegex(Error, "official HTTPS"):
            native_host.provision(self.context, yes=True)
        self.assertFalse(any(c["tool"] in ("bash", "brew") for c in self.commands()))
        self.opener.open.assert_not_called()

    def test_modified_brew_entry_is_never_executed(self):
        brew = self.existing_brew()
        brew.write_text("#!/bin/bash\nmalicious\n")
        self.assertEqual(native_host.status(self.context)["homebrew"]["status"], "foreign")
        self.assertFalse(any(c["tool"] == "brew" for c in self.commands()))

    def test_mutations_require_yes_and_nonroot_and_never_bootstrap_consent(self):
        for operation in (native_host.provision, native_host.request_clt):
            with self.assertRaisesRegex(Error, "--yes"):
                operation(self.context)
            with patch.object(native_host.os, "geteuid", return_value=0):
                with self.assertRaisesRegex(Error, "not root"):
                    operation(self.context, yes=True)
        self.assertEqual(self.commands(), [])
        self.opener.open.assert_not_called()

    def test_missing_clt_blocks_installer_until_explicit_request(self):
        (self.clt / "usr/bin/git").unlink()
        with self.assertRaisesRegex(Error, "CLT/SDK consent"):
            native_host.provision(self.context, yes=True)
        self.assertFalse(any(c["args"] == ["--install"] for c in self.commands()))
        self.opener.open.assert_not_called()
        result = native_host.request_clt(self.context, yes=True)
        self.assertEqual(result["status"], "requested")
        self.assertEqual(
            [c["args"] for c in self.commands() if c["args"] == ["--install"]], [["--install"]]
        )
        self.assertNotEqual(native_host.status(self.context)["clt"]["status"], "ready")

    def test_invalid_sdk_license_or_receipt_never_runs_installer(self):
        for values in ({"receipt_failure": True}, {"license": True}, {"selected": str(self.home)}):
            with self.subTest(values=values):
                self.control_values(**values)
                with self.assertRaisesRegex(Error, "CLT/SDK consent"):
                    native_host.provision(self.context, yes=True)
        self.opener.open.assert_not_called()
        self.assertFalse(any(c["tool"] == "bash" for c in self.commands()))

    def test_healthy_clt_request_is_noop(self):
        self.assertEqual(native_host.request_clt(self.context, yes=True)["status"], "unchanged")
        self.assertFalse(any(c["args"] == ["--install"] for c in self.commands()))

    def test_checksum_or_response_source_poisoning_never_executes(self):
        self.opener.open.side_effect = lambda url, timeout: Response(b"poisoned", url)
        with self.assertRaisesRegex(Error, "checksum"):
            native_host.provision(self.context, yes=True)
        self.opener.open.side_effect = lambda url, timeout: Response(
            self.payload, "https://example.invalid/install.sh"
        )
        with self.assertRaisesRegex(Error, "source changed"):
            native_host.provision(self.context, yes=True)
        self.assertFalse(any(c["tool"] == "bash" for c in self.commands()))
        self.assertFalse(self.prefix.exists())

    def test_redirects_are_rejected(self):
        with self.assertRaisesRegex(Error, "redirects"):
            native_host._NoRedirect().redirect_request(
                None, None, 302, "found", {}, "https://example.invalid"
            )

    def test_poisoned_source_pin_is_rejected_before_download(self):
        original = json.loads((ENGINE / "host-pins.json").read_text())
        for key, value in (
            ("url", "https://example.invalid/install.sh"),
            ("repository", "https://github.com/foreign/install"),
            ("prefix", "/tmp/brew"),
            ("revision", "HEAD"),
            ("sha256", "bad"),
        ):
            poisoned = json.loads(json.dumps(original))
            poisoned["homebrew"][key] = value
            with self.subTest(key=key), patch.object(native_host, "load", return_value=poisoned):
                with self.assertRaisesRegex(Error, "source pin"):
                    self.real_pins()
        with patch.object(native_host, "load", return_value=original):
            actual = self.real_pins()
        self.assertEqual(actual["repository"], "https://github.com/Homebrew/install")
        self.assertEqual(actual["url"].split("/")[-2], actual["revision"])

    def test_failed_installer_and_failed_readback_do_not_publish_ownership(self):
        self.control_values(installer_failure=True)
        with self.assertRaisesRegex(Error, "bash failed"):
            native_host.provision(self.context, yes=True)
        self.assertFalse((self.home / "state/native-host.json").exists())
        self.control_values(installer_noop=True)
        with self.assertRaisesRegex(Error, "independent.*readback"):
            native_host.provision(self.context, yes=True)
        self.assertFalse((self.home / "state/native-host.json").exists())
        self.control_values(wrong_prefix=True)
        with self.assertRaisesRegex(Error, "independent.*readback"):
            native_host.provision(self.context, yes=True)
        self.assertTrue(self.prefix.exists())
        self.assertFalse((self.home / "state/native-host.json").exists())

    def test_hostile_environment_is_not_forwarded_to_any_tool(self):
        hostile = {
            "PATH": "/nix/store/evil/bin",
            "HOMEBREW_BREW_GIT_REMOTE": "https://evil.invalid",
            "HOMEBREW_PREFIX": "/tmp/evil",
            "HOMEBREW_INSTALL_FROM_API": "0",
            "BASH_ENV": "/evil",
            "ENV": "/evil",
            "DYLD_INSERT_LIBRARIES": "/evil",
            "LD_PRELOAD": "/evil",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/evil",
            "DEVELOPER_DIR": "/nix/evil",
            "CURL_HOME": "/evil",
            "HTTPS_PROXY": "https://secret@example.invalid",
        }
        with patch.dict(os.environ, hostile):
            native_host.provision(self.context, yes=True)
        for call in self.commands():
            self.assertEqual(call["cwd"], "/")
            self.assertEqual(call["env"]["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
            for key, value in hostile.items():
                self.assertNotEqual(call["env"].get(key), value)
        installer = next(c for c in self.commands() if c["tool"] == "bash")
        self.assertEqual(installer["args"][:2], ["--noprofile", "--norc"])
        self.assertFalse(Path(installer["args"][2]).exists())

    def test_wrong_platform_rosetta_old_macos_and_nix_interpreter_are_rejected(self):
        for target, value in (
            ("system", "FreeBSD"),
            ("machine", "x86_64"),
            ("mac_ver", ("14.7", (), "arm64")),
        ):
            with (
                self.subTest(target=target),
                patch.object(native_host.platform, target, return_value=value),
            ):
                with self.assertRaises(Error):
                    native_host.status(self.context)
        with patch.object(native_host.sys, "executable", "/nix/store/interpreter/bin/python3"):
            with self.assertRaisesRegex(Error, "non-native"):
                native_host.status(self.context)
        self.assertEqual(self.commands(), [])

    def test_nix_fixture_tool_and_live_verification_mutations_are_rejected(self):
        self.context["executables"]["xcode-select"] = "/nix/store/evil/bin/xcode-select"
        self.assertNotEqual(native_host.status(self.context)["clt"]["status"], "ready")
        self.context["_fixture_context"] = False
        with self.assertRaisesRegex(Error, "verification refuses"):
            native_host.provision(self.context, yes=True)
        self.assertEqual(self.commands(), [])

    def test_linux_status_requires_no_homebrew_or_apple_commands(self):
        self.context["platform"] = "linux-x64"
        with (
            patch.object(native_host.platform, "system", return_value="Linux"),
            patch.object(native_host.platform, "machine", return_value="x86_64"),
        ):
            self.assertEqual(native_host.status(self.context)["status"], "not-required")
        self.assertEqual(self.commands(), [])
        self.opener.open.assert_not_called()

    def test_retirement_preserves_everything_without_commands(self):
        brew = self.existing_brew()
        data = self.prefix / "Cellar/important/user-data"
        data.parent.mkdir(parents=True)
        data.write_text("retain me")
        before = brew.read_bytes()
        result = native_host.preserve_on_retirement(self.context)
        self.assertEqual(result["status"], "retained")
        self.assertEqual(data.read_text(), "retain me")
        self.assertEqual(brew.read_bytes(), before)
        self.assertEqual(self.commands(), [])
        with self.assertRaisesRegex(Error, "unknown"):
            native_host.dispatch(self.context, "uninstall", authorize=True)

    def test_replaced_owned_prefix_is_foreign_and_never_removed(self):
        native_host.provision(self.context, yes=True)
        receipt = self.home / "state/native-host.json"
        record = json.loads(receipt.read_text())
        record["prefix_identity"][1] += 1
        receipt.write_text(json.dumps(record))
        before = (self.prefix / "bin/brew").read_bytes()
        with self.assertRaisesRegex(Error, "replaced"):
            native_host.provision(self.context, yes=True)
        self.assertEqual((self.prefix / "bin/brew").read_bytes(), before)

    def test_preexisting_brew_without_clt_never_invokes_apple_git_shim(self):
        self.existing_brew()
        (self.clt / "usr/bin/git").unlink()
        result = native_host.status(self.context)
        self.assertEqual(result["homebrew"]["status"], "unchecked")
        self.assertFalse(any(c["tool"] in ("git", "brew", "bash") for c in self.commands()))
        self.assertFalse(any(c["args"] == ["--install"] for c in self.commands()))

    def test_acquisition_prefix_race_and_source_tamper_are_rejected(self):
        original_download = native_host._download_installer

        def appearing_prefix(pin, destination):
            path = original_download(pin, destination)
            self.prefix.mkdir()
            (self.prefix / "foreign-data").write_text("retain")
            return path

        with patch.object(native_host, "_download_installer", side_effect=appearing_prefix):
            with self.assertRaisesRegex(Error, "appeared"):
                native_host.provision(self.context, yes=True)
        self.assertEqual((self.prefix / "foreign-data").read_text(), "retain")
        (self.prefix / "foreign-data").unlink()
        self.prefix.rmdir()

        def changed_source(pin, destination):
            path = original_download(pin, destination)
            path.write_text("changed after verification")
            return path

        with patch.object(native_host, "_download_installer", side_effect=changed_source):
            with self.assertRaisesRegex(Error, "source changed"):
                native_host.provision(self.context, yes=True)
        self.assertFalse(any(c["tool"] == "bash" for c in self.commands()))

    def test_state_symlink_and_escaping_paths_are_rejected_before_mutation(self):
        outside = self.home.parent / "outside"
        outside.mkdir()
        (self.home / "state").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(Error):
            native_host.provision(self.context, yes=True)
        self.context["paths"]["state"] = str(self.home / "../outside")
        with self.assertRaisesRegex(Error, "escapes"):
            native_host.request_clt(self.context, yes=True)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.commands(), [])

    def test_system_and_prefix_brew_env_overrides_are_never_sourced(self):
        system = self.home / "system-brew.env"
        system.write_text("HOMEBREW_BREW_GIT_REMOTE=https://evil.invalid\\n")
        with self.assertRaisesRegex(Error, "startup overrides"):
            native_host.provision(self.context, yes=True)
        self.opener.open.assert_not_called()
        system.unlink()
        self.existing_brew()
        prefix_env = self.prefix / "etc/homebrew/brew.env"
        prefix_env.parent.mkdir(parents=True)
        prefix_env.write_text("HOMEBREW_BREW_GIT_REMOTE=https://evil.invalid\\n")
        with self.assertRaisesRegex(Error, "startup overrides"):
            native_host.provision(self.context, yes=True)
        self.assertFalse(any(c["tool"] in ("bash", "brew") for c in self.commands()))
        self.assertTrue(prefix_env.exists())

    def test_selected_xcode_outside_applications_uses_xcrun_compiler(self):
        developer = self.home / "Downloads/Xcode.app/Contents/Developer"
        compiler = developer / "Toolchains/XcodeDefault.xctoolchain/usr/bin/clang"
        compiler.parent.mkdir(parents=True)
        compiler.write_text("fixture compiler\n")
        compiler.chmod(0o755)
        sdk = developer / "Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
        sdk.mkdir(parents=True)
        self.control_values(selected=str(developer), compiler=str(compiler), sdk=str(sdk))
        result = native_host.status(self.context)
        self.assertEqual(result["clt"]["status"], "ready")
        self.assertEqual(result["clt"]["sdk"], str(sdk))
        self.assertFalse(any(c["tool"] == "bash" for c in self.commands()))

    def test_compiler_outside_selected_developer_directory_is_rejected(self):
        foreign = self.home / "foreign-compiler"
        foreign.write_text("fixture foreign compiler\n")
        foreign.chmod(0o755)
        self.control_values(compiler=str(foreign))
        result = native_host.status(self.context)
        self.assertEqual(result["clt"]["status"], "blocked")
        self.assertIn("escapes", result["clt"]["reason"])
        self.assertFalse(any(c["tool"] == "bash" for c in self.commands()))


if __name__ == "__main__":
    unittest.main()
