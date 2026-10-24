"""Behavioral tests for authored native configuration and mutable profile boundaries.

Rendering tests use the real chezmoi CLI against disposable HOME. They never
provision packages, contact extension servers, inspect real browser state or run
services. Shell tests use native Zsh without loading the live user's startup.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "native_configure", ROOT / "native/assets/configure.py"
)
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


def context(home, platform="linux-x64", enable=True):
    data = {
        "home": str(home),
        "root": str(ROOT),
        "base": str(ROOT),
        "platform": platform,
        "identity": {},
        "selectors": {},
        "packages": [],
        "tasks": {},
        "paths": {
            "config": str(home / ".config"),
            "bin": str(home / ".local/bin"),
            "data": str(home / ".local/share/dotfiles"),
            "state": str(home / ".local/state/dotfiles"),
            "cache": str(home / ".cache/dotfiles"),
            "runtime": str(home / ".local/share/dotfiles/runtime"),
            "completions": str(home / ".local/share/dotfiles/completions"),
            "omz": str(home / ".local/share/dotfiles/runtime/oh-my-zsh/current"),
        },
        "executables": {
            name: "/native/bin/" + name
            for name in ("firefox", "gpg-agent", "gpgconf", "pinentry", "bat")
        },
        "dotfiles": {
            "theme": {"nord": {"enable": enable}},
            "shell": {
                "enable": enable,
                "zsh": {"enable": enable},
                "git": {"enable": enable},
                "gpg": {"enable": enable},
                "gnuTools": {"enable": enable},
            },
            "apps": {
                "enable": enable,
                "firefox": {"enable": enable, "extensions": []},
                "editors": {"enable": enable},
                "yazi": {"enable": enable, "shellWrapperName": "yy", "enableZshIntegration": True},
            },
            "dev": {
                "enable": enable,
                "go": {"env": {}},
                "mise": {"enable": enable},
                "cloud": {"enable": enable},
            },
        },
    }
    return data


class FirefoxConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.context = context(self.home)

    def test_plan_has_no_filesystem_side_effects(self):
        before = list(self.home.rglob("*"))
        plan = configure.operate("plan", self.context, "firefox-profile")
        self.assertEqual(plan["extensions"], [])
        self.assertEqual(list(self.home.rglob("*")), before)
        configure.operate("retire", self.context, "firefox-profile")
        self.assertEqual(list(self.home.rglob("*")), before)

    def test_profiles_preserve_foreign_entries_comments_and_unknown_fields(self):
        root, profile, _ = configure.firefox_paths(self.context)
        root.mkdir(parents=True)
        profiles = root / "profiles.ini"
        profiles.write_text(
            "; user annotation\n[Profile7]\nName=personal\nPath=personal\nIsRelative=1\nDefault=1\nCustom=value\n\n[InstallABC]\nDefault=personal\nLocked=1\n"
        )
        (root / "installs.ini").write_text("[ABC]\nDefault=personal\nCustom=keep\n")
        configure.operate("provision", self.context, "firefox-profile")
        self.assertIn("; user annotation", profiles.read_text())
        parsed = configure.ini_data(profiles)
        self.assertEqual(parsed["Profile7"]["Custom"], "value")
        self.assertEqual(parsed["Profile7"]["Path"], "personal")
        self.assertNotIn("Default", parsed["Profile7"])
        self.assertEqual(parsed["InstallABC"]["Locked"], "1")
        self.assertEqual(parsed["InstallABC"]["Default"], "default")
        self.assertEqual(configure.ini_data(root / "installs.ini")["ABC"]["Custom"], "keep")
        self.assertTrue(profile.is_dir())
        first = profiles.read_bytes()
        configure.operate("provision", self.context, "firefox-profile")
        self.assertEqual(profiles.read_bytes(), first)
        self.assertTrue(configure.operate("check", self.context, "firefox-profile")["verified"])

    def test_profile_collision_is_not_overwritten(self):
        root, _, _ = configure.firefox_paths(self.context)
        root.mkdir(parents=True)
        path = root / "profiles.ini"
        original = "[Profile0]\nName=default\nPath=user-profile\nIsRelative=1\n"
        path.write_text(original)
        with self.assertRaisesRegex(ValueError, "different path"):
            configure.operate("provision", self.context, "firefox-profile")
        self.assertEqual(path.read_text(), original)
        self.assertFalse((root / "default/search.json.mozlz4").exists())

    def test_profile_directory_symlink_is_not_followed(self):
        root, profile, _ = configure.firefox_paths(self.context)
        root.mkdir(parents=True)
        foreign = Path(self.temp.name) / "foreign"
        foreign.mkdir()
        profile.symlink_to(foreign, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            configure.operate("provision", self.context, "firefox-profile")
        self.assertEqual(list(foreign.iterdir()), [])

    def test_search_merges_managed_fields_and_preserves_user_searches(self):
        _, profile, _ = configure.firefox_paths(self.context)
        previous = {
            "version": 12,
            "engines": [
                {
                    "id": "personal",
                    "_name": "My Engine",
                    "_metaData": {"alias": "@mine"},
                    "custom": 42,
                },
                {
                    "id": "google",
                    "_isAppProvided": True,
                    "_metaData": {"hidden": False, "custom": "keep"},
                },
            ],
            "metaData": {"userField": "keep"},
            "futureField": {"keep": True},
        }
        original = copy.deepcopy(previous)
        result = configure.desired_search(profile, previous)
        self.assertEqual(previous, original, "planning must not mutate caller-owned search data")
        self.assertEqual(result["futureField"], {"keep": True})
        self.assertEqual(result["metaData"]["userField"], "keep")
        engines = {record["id"]: record for record in result["engines"]}
        self.assertEqual(engines["personal"]["custom"], 42)
        self.assertEqual(
            engines["google"]["_metaData"], {"hidden": True, "custom": "keep", "order": 9}
        )
        self.assertEqual(engines["youtube"]["_definedAliases"], ["@yt"])
        self.assertEqual(result["metaData"]["defaultEngineId"], "ddg")
        self.assertEqual(configure.desired_search(profile, result), result)
        self.assertEqual(configure.decode_search(configure.encode_search(result)), result)

    def test_search_codec_accepts_real_lz4_backreferences_and_rejects_corruption(self):
        # A literal JSON prefix followed by an overlapping LZ4 match, not only
        # this recipe's literal-only encoder; represents {"a":"aaaaaa"}.
        prefix = b'{"a":"a'
        block = bytes([len(prefix) << 4 | 1]) + prefix + b"\x01\x00" + b'\x20"}'
        container = b"mozLz40\0" + struct.pack("<I", 14) + block
        self.assertEqual(configure.decode_search(container), {"a": "aaaaaa"})
        for malformed in (
            b"wrong",
            container[:-1],
            b"mozLz40\0" + struct.pack("<I", 3) + b"\x00\x00\x00",
        ):
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                configure.decode_search(malformed)

    def test_unknown_search_schema_is_preserved(self):
        _, profile, _ = configure.firefox_paths(self.context)
        profile.mkdir(parents=True)
        search = profile / "search.json.mozlz4"
        original = configure.encode_search({"version": 99, "engines": []})
        search.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "unsupported Firefox search schema"):
            configure.operate("provision", self.context, "firefox-profile")
        self.assertEqual(search.read_bytes(), original)

    def test_only_recorded_unchanged_extensions_are_retired(self):
        _, profile, _ = configure.firefox_paths(self.context)
        directory = profile / "extensions"
        directory.mkdir(parents=True)
        owned = directory / "owned@example.invalid.xpi"
        owned.write_bytes(b"synthetic owned payload")
        foreign = directory / "user@example.invalid.xpi"
        foreign.write_bytes(b"user payload")
        state, _ = configure.extension_state(self.context)
        digest = hashlib.sha256(owned.read_bytes()).hexdigest()
        configure.atomic(
            state,
            json.dumps(
                {"schema_version": 1, "extensions": {"owned@example.invalid": digest}}
            ).encode(),
        )
        configure.reconcile_extensions(self.context, profile, retire=True)
        self.assertFalse(owned.exists())
        self.assertEqual(foreign.read_bytes(), b"user payload")

    def test_locally_updated_extensions_and_malformed_ownership_are_not_deleted(self):
        _, profile, _ = configure.firefox_paths(self.context)
        directory = profile / "extensions"
        directory.mkdir(parents=True)
        owned = directory / "owned@example.invalid.xpi"
        owned.write_bytes(b"updated by browser")
        state, _ = configure.extension_state(self.context)
        configure.atomic(
            state,
            json.dumps(
                {"schema_version": 1, "extensions": {"owned@example.invalid": "0" * 64}}
            ).encode(),
        )
        with self.assertRaisesRegex(ValueError, "locally updated"):
            configure.reconcile_extensions(self.context, profile, retire=True)
        self.assertEqual(owned.read_bytes(), b"updated by browser")
        configure.atomic(
            state,
            json.dumps({"schema_version": 1, "extensions": {"../../outside": "0" * 64}}).encode(),
        )
        with self.assertRaisesRegex(ValueError, "ownership entry"):
            configure.reconcile_extensions(self.context, profile, retire=True)

    def test_darwin_profile_path_and_disabled_feature_are_independent(self):
        self.context["platform"] = "macos-arm64"
        _, profile, relative = configure.firefox_paths(self.context)
        self.assertEqual(relative, "Profiles/default")
        self.assertEqual(
            profile, self.home / "Library/Application Support/Firefox/Profiles/default"
        )
        self.context["dotfiles"]["apps"]["firefox"]["enable"] = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            configure.operate("provision", self.context, "firefox-profile")
        self.assertFalse(profile.exists())

    def test_receipts_include_only_owned_leaves_and_preserve_sensitive_app_state(self):
        _, profile, _ = configure.firefox_paths(self.context)
        profile.mkdir(parents=True)
        sensitive = profile / "logins.json"
        sensitive.write_bytes(b"synthetic browser-owned credential state")
        details = configure.operate("provision", self.context, "firefox-profile")
        receipt = configure.cli_result("provision", self.context, "firefox-profile", details, None)
        self.assertEqual(receipt["status"], "provisioned")
        self.assertEqual(
            [record["path"] for record in receipt["artifacts"]],
            [str(configure.extension_state(self.context)[0])],
        )
        for artifact in receipt["artifacts"]:
            self.assertEqual(
                artifact["sha256"], hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest()
            )
        configure.operate("retire", self.context, "firefox-profile")
        self.assertEqual(sensitive.read_bytes(), b"synthetic browser-owned credential state")


class BtopConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.context = context(self.home)
        self.target = self.home / ".config/btop/btop.conf"
        self.target.parent.mkdir(parents=True)

    def test_theme_reconciliation_preserves_mutable_settings_and_restores_old_field(self):
        original = '# application setting\ncolor_theme = "Default"\nupdate_ms = 1000\n'
        self.target.write_text(original)
        configure.operate("provision", self.context, "btop-nord-settings")
        self.assertEqual(self.target.read_text(), original.replace('"Default"', '"nord-contrast"'))
        configure.operate("check", self.context, "btop-nord-settings")
        # Application writes an unrelated setting after provisioning.
        self.target.write_text(self.target.read_text().replace("1000", "1500"))
        configure.operate("retire", self.context, "btop-nord-settings")
        self.assertEqual(self.target.read_text(), original.replace("1000", "1500"))

    def test_foreign_theme_change_is_preserved(self):
        self.target.write_text('color_theme = "Default"\n')
        configure.operate("provision", self.context, "btop-nord-settings")
        self.target.write_text('color_theme = "user-theme"\n')
        with self.assertRaisesRegex(ValueError, "changed outside"):
            configure.operate("retire", self.context, "btop-nord-settings")
        self.assertEqual(self.target.read_text(), 'color_theme = "user-theme"\n')


@unittest.skipUnless(
    shutil.which("bat") and "/nix/" not in shutil.which("bat"),
    "native Bat is required for cache discovery",
)
class BatCacheTests(unittest.TestCase):
    def test_staged_native_cache_exposes_authored_nord_theme(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            data = context(home)
            data["executables"]["bat"] = shutil.which("bat")
            output = Path(directory) / "staged"
            details = configure.operate("build", data, "bat-nord-cache", output)
            self.assertTrue((Path(details["cache"]) / "themes.bin").is_file())
            receipt = configure.cli_result("build", data, "bat-nord-cache", details, output)
            self.assertEqual(receipt["status"], "built")
            self.assertTrue(
                any(item["path"].endswith("/themes.bin") for item in receipt["artifacts"])
            )
            self.assertFalse((home / ".config/bat").exists(), "build must not install into HOME")


@unittest.skipUnless(
    shutil.which("chezmoi"), "native chezmoi is required for source-composition regressions"
)
class ChezmoiCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.home = self.directory / "home"
        self.home.mkdir()

    def apply(self, platform="linux-x64", enable=True, overrides=None):
        data = context(self.home, platform, enable)
        if overrides:
            overrides(data)
        config = self.directory / "chezmoi.json"
        config.write_text(json.dumps({"data": data}))
        env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "XDG_CONFIG_HOME": str(self.directory / "tool-config"),
            "XDG_CACHE_HOME": str(self.directory / "tool-cache"),
            "XDG_DATA_HOME": str(self.directory / "tool-data"),
        }
        command = [
            shutil.which("chezmoi"),
            "--source",
            str(ROOT / "native/chezmoi"),
            "--destination",
            str(self.home),
            "--config",
            str(config),
            "--no-tty",
            "apply",
        ]
        result = subprocess.run(command, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return data

    def test_linux_enabled_profile_uses_discoverable_source_assets_and_one_completion_owner(self):
        self.apply()
        self.assertEqual(
            (self.home / ".config/helix/themes/nord-contrast.toml").resolve(),
            ROOT / "native/assets/nord/helix-nord-contrast.toml",
        )
        self.assertTrue((self.home / ".config/yazi/flavors/nord.yazi/flavor.toml").is_file())
        self.assertTrue((self.home / ".mozilla/firefox/default/user.js").is_file())
        self.assertFalse((self.home / "Library/LaunchAgents/org.dotfiles.gpg-agent.plist").exists())
        shell = (self.home / ".config/zsh/.zshrc").read_text()
        self.assertLess(shell.index("fpath=("), shell.index('source "$ZSH/oh-my-zsh.sh"'))
        self.assertLess(
            shell.index('source "$ZSH/oh-my-zsh.sh"'),
            shell.index('source "$XDG_CONFIG_HOME/dotfiles/private.zsh"'),
        )
        self.assertNotRegex(shell, r"(?m)^\s*compinit\b")
        self.assertNotIn("fzf --zsh", shell)
        self.assertNotIn("zoxide)", shell)
        self.assertNotIn("/nix/store", shell)

    def test_disabled_profile_does_not_claim_optional_files(self):
        self.apply(enable=False)
        self.assertFalse((self.home / ".zshenv").exists())
        for leaf in (
            ".config/zsh/.zshrc",
            ".config/git/config",
            ".gnupg/gpg-agent.conf",
            ".config/yazi/theme.toml",
            ".mozilla/firefox/default/user.js",
        ):
            self.assertFalse((self.home / leaf).exists(), leaf)

    def test_independent_git_and_editor_options_do_not_require_shell_theme(self):
        def overrides(data):
            data["dotfiles"]["shell"]["git"]["enable"] = True
            data["dotfiles"]["apps"]["editors"]["enable"] = True

        self.apply(enable=False, overrides=overrides)
        git = (self.home / ".config/git/config").read_text()
        self.assertIn("sops -d", git)
        self.assertIn("private.conf", git)
        self.assertNotIn("nord-contrast", git)
        self.assertTrue((self.home / ".config/helix/languages.toml").is_file())
        self.assertFalse((self.home / ".config/helix/themes/nord-contrast.toml").exists())

    def test_darwin_profile_has_launchd_instead_of_linux_service(self):
        self.apply("macos-arm64")
        self.assertTrue(
            (self.home / "Library/Application Support/Firefox/Profiles/default/user.js").is_file()
        )
        self.assertTrue((self.home / "Library/LaunchAgents/org.dotfiles.gpg-agent.plist").is_file())
        self.assertFalse((self.home / ".config/systemd/user/gpg-agent.service").exists())
        self.assertFalse((self.home / ".mozilla/firefox/default/user.js").exists())

    def test_go_native_config_and_telemetry_are_independent_of_zsh_and_mise(self):
        def overrides(data):
            data["dotfiles"]["shell"]["zsh"]["enable"] = False
            data["dotfiles"]["dev"]["mise"]["enable"] = False
            data["dotfiles"]["dev"]["go"]["env"] = {"GOPRIVATE": "modules.example.invalid"}

        self.apply("macos-arm64", overrides=overrides)
        go = self.home / "Library/Application Support/go"
        self.assertEqual(
            (go / "env").read_text().splitlines(),
            ["GOPATH=" + str(self.home / ".go"), "GOPRIVATE=modules.example.invalid"],
        )
        self.assertEqual((go / "telemetry/mode").read_text(), "off 1970-01-01")
        self.assertFalse((self.home / ".config/go/env").exists())
        self.assertFalse((self.home / ".zshenv").exists())

    def test_linux_go_config_uses_xdg_location(self):
        self.apply()
        self.assertIn(
            "GOPATH=" + str(self.home / ".go"), (self.home / ".config/go/env").read_text()
        )
        self.assertEqual((self.home / ".config/go/telemetry/mode").read_text(), "off 1970-01-01")
        self.assertFalse((self.home / "Library/Application Support/go/env").exists())

    def test_global_mise_config_uses_selected_versions_aliases_and_strict_policy(self):
        def overrides(data):
            data["packages"] = [
                {"id": "terraform", "provider": "mise", "tool": "terraform", "version": "1.5.7"},
                {"id": "bat", "provider": "mise", "tool": "bat", "version": "0.26.1"},
                {"id": "git", "provider": "apt", "name": "git"},
            ]
            data["tasks"] = {
                "dotfiles:status": {
                    "run": "bootstrap status",
                    "description": "Inspect native state",
                }
            }

        self.apply(overrides=overrides)
        config = tomllib.loads((self.home / ".config/mise/config.toml").read_text())
        self.assertEqual(config["tools"], {"bat": "0.26.1", "terraform": "1.5.7"})
        self.assertEqual(config["tool_alias"]["terraform"], "aqua:hashicorp/terraform")
        self.assertNotIn("helmfile", config["tools"])
        for field in (
            "auto_install",
            "not_found_auto_install",
            "not_found_system_fallback",
            "registry_floating",
        ):
            self.assertIs(config["settings"][field], False)
        self.assertEqual(config["settings"]["locked_scopes"], ["global"])
        self.assertIs(config["settings"]["task"]["run_auto_install"], False)
        self.assertEqual(config["tasks"]["dotfiles:status"]["run"], "bootstrap status")
        launcher = (self.home / ".local/bin/dotfiles-mise-install").read_text()
        self.assertIn("exec /bin/sh -p", launcher)
        self.assertIn("--id 'terraform'", launcher)
        self.assertNotIn('"$@"', launcher)

    @unittest.skipUnless(shutil.which("zsh"), "native Zsh is required for noninteractive startup")
    def test_noninteractive_startup_exports_xdg_editor_and_native_gnu_paths(self):
        self.apply("macos-arm64")
        script = 'source "$HOME/.zshenv"; print -r -- "$ZDOTDIR"; print -r -- "$EDITOR"; print -r -- "$PATH"'
        result = subprocess.run(
            [shutil.which("zsh"), "-f", "-c", script],
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], str(self.home / ".config/zsh"))
        self.assertEqual(lines[1], "hx")
        paths = lines[2].split(":")
        self.assertEqual(paths[0], str(self.home / ".local/share/mise-shims"))
        self.assertLess(
            paths.index("/opt/homebrew/opt/coreutils/libexec/gnubin"), paths.index("/usr/bin")
        )
        self.assertEqual(len(paths), len(set(paths)))


if __name__ == "__main__":
    unittest.main()
