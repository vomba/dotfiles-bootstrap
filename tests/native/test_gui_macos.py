"""Disposable macOS desktop behavior fixtures; no actual apps/providers/GUI run."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

PUBLIC = Path(__file__).resolve().parents[2]
ENGINE = PUBLIC / "native/engine"
sys.path.insert(0, str(ENGINE))
import manifest
import providers
import recovery
import safety
import updates


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


macos = module("gui_macos_fixture", PUBLIC / "native/gui/macos.py")
cli = module("gui_macos_engine_cli", ENGINE / "__main__.py")
GUI = json.loads((PUBLIC / "native/manifests/gui-macos.json").read_text())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def macho(path, cpu=macos.ARM64):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<IIIIIIII", 0xFEEDFACF, cpu, 0, 2, 0, 0, 0, 0))
    path.chmod(0o755)


def font(path, family):
    encoded = family.encode("utf-16-be")
    table = (
        struct.pack(">HHH", 0, 1, 18)
        + struct.pack(">HHHHHH", 3, 1, 0x409, 16, len(encoded), 0)
        + encoded
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        struct.pack(">IHHHH", 0x10000, 1, 0, 0, 0)
        + struct.pack(">4sIII", b"name", 0, 28, len(table))
        + table
    )


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dotfiles-macos-gui-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve() / "home"
        self.home.mkdir()
        self.fixture = self.home / "native"
        self.context = {
            "home": str(self.home),
            "base": str(PUBLIC),
            "root": str(PUBLIC),
            "platform": "macos-arm64",
            "dotfiles": {"desktop": {"enable": True}},
            "packages": [],
            "services": [],
            "_verification_only": True,
            "_fixture_context": True,
            "_macos_fixture_root": str(self.fixture),
            "paths": {
                key: str(self.home / value)
                for key, value in {
                    "config": ".config",
                    "data": ".local/share/dotfiles",
                    "state": ".local/state/dotfiles",
                    "cache": ".cache/dotfiles",
                    "bin": ".local/bin",
                    "runtime": ".local/share/dotfiles/runtime",
                }.items()
            },
            "executables": {},
        }
        self.context_path = self.home / "context.json"
        self.brew = self.home / "bin/brew"
        self.brew.parent.mkdir()
        self.brew.write_text("#!/bin/sh\nexit 99\n")
        self.brew.chmod(0o755)
        self.context["executables"]["brew"] = str(self.brew)
        save(self.context_path, self.context)
        self.calls = []

    def package(self, identifier="gui-kitty"):
        return copy.deepcopy(
            next(record for record in GUI["packages"] if record["id"] == identifier)
        )

    def info(self, package, installed=True):
        if package["provider"] == "brew":
            return {
                "formulae": [
                    {
                        "name": package["name"],
                        "versions": {"stable": package["version"]},
                        "installed": [{"version": package["version"]}] if installed else [],
                    }
                ]
            }
        spec = package["macos"]
        artifacts = (
            [{"app": [spec["app"]["name"]]}]
            if "app" in spec
            else [{"font": [face]} for face in spec["font"]["required_faces"]]
        )
        return {
            "casks": [
                {
                    "token": package["name"],
                    "version": package["version"],
                    "installed": package["version"] if installed else None,
                    "artifacts": artifacts,
                }
            ]
        }

    def install_app(self, package, version=None, cpu=macos.ARM64):
        desired = package["macos"]["app"]
        app = self.fixture / "Applications" / desired["name"]
        executable = app / desired["binary"]
        macho(executable, cpu)
        for declared in package.get("executables", {}).values():
            macho(app / Path(declared).relative_to(Path("/Applications") / desired["name"]), cpu)
        with (app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump(
                {
                    "CFBundleIdentifier": desired["bundle_id"],
                    "CFBundleExecutable": executable.name,
                    "CFBundleShortVersionString": version or package["version"],
                },
                stream,
            )
        (app / "Contents/Resources").mkdir()
        (app / "Contents/Resources/payload").write_text("immutable application bytes")
        return app

    def fake_manager(self, package, info):
        def run(arguments, context, **kwargs):
            arguments = [str(item) for item in arguments]
            self.calls.append(arguments)
            if arguments[1] == "info":
                return json.dumps(info)
            if arguments[1] in ("install", "upgrade"):
                self.install_app(package, info["casks"][0]["version"])
                info["casks"][0]["installed"] = info["casks"][0]["version"]
                return ""
            if arguments[1] == "uninstall":
                shutil.rmtree(self.fixture / "Applications" / package["macos"]["app"]["name"])
                info["casks"][0]["installed"] = None
                return ""
            raise AssertionError(arguments)

        return run


class CompositionTests(Fixture):
    def test_desktop_independence_and_trusted_native_command_paths(self):
        base = self.home / "public"
        (base / "native/engine").mkdir(parents=True)
        shutil.copyfile(ENGINE / "pins.json", base / "native/engine/pins.json")
        save(
            base / "native/manifests/base.json",
            {
                "schema_version": 1,
                "defaults": {
                    "dotfiles": {
                        "desktop": {"enable": True, "niri": {"enable": False}},
                        "apps": {"enable": False, "editors": {"enable": False}},
                        "dev": {"enable": False},
                    }
                },
            },
        )
        save(base / "native/manifests/gui-macos.json", GUI)
        context = manifest.compose(base, base, self.home, "macos-arm64", [])
        self.assertEqual(
            {p["id"] for p in context["packages"]},
            {"gui-kitty", "gui-slack", "gui-tabiew", "jetbrains-mono-nf"},
        )
        self.assertEqual(
            context["executables"]["kitty"], "/Applications/kitty.app/Contents/MacOS/kitty"
        )
        self.assertEqual(
            context["executables"]["kitten"], "/Applications/kitty.app/Contents/MacOS/kitten"
        )
        self.assertEqual(context["executables"]["tw"], "/opt/homebrew/opt/tabiew/bin/tw")
        self.assertEqual(
            manifest.compose(base, base, self.home, "macos-arm64", ["desktop.enable=false"])[
                "packages"
            ],
            [],
        )
        self.assertEqual(manifest.compose(base, base, self.home, "linux-x64", [])["packages"], [])
        self.assertNotIn("cursor", context["dotfiles"]["apps"])

    def test_unconsumed_cursor_default_is_retired_not_new_app(self):
        base = json.loads((PUBLIC / "native/manifests/base.json").read_text())
        coverage = json.loads((PUBLIC / "native/coverage.json").read_text())
        self.assertNotIn("cursor", base["defaults"]["dotfiles"]["apps"])
        entry = next(
            item for item in coverage["ownership"] if item["old_owner"] == "apps.cursor.enable"
        )
        self.assertEqual(entry["status"], "retired")
        self.assertIn("Unconsumed", entry["reason"])


class NativePayloadTests(Fixture):
    def test_app_readback_requires_bundle_version_and_native_arm64(self):
        package = self.package()
        app = self.install_app(package)
        info = self.info(package)
        observed = macos.check(self.context, package, info)
        self.assertEqual(observed["executable"], str(app / "Contents/MacOS/kitty"))
        self.assertEqual(observed["architecture"], "arm64")
        info["casks"][0]["version"] = "99.0"  # Provider candidate is not the installed version.
        self.assertEqual(macos.check(self.context, package, info), observed)
        info["casks"][0]["installed"] = "99.0"
        with self.assertRaisesRegex(safety.Error, "version differs"):
            macos.check(self.context, package, info)
        info["casks"][0]["installed"] = package["version"]
        macho(app / "Contents/MacOS/kitty", 0x01000007)
        with self.assertRaisesRegex(safety.Error, "no Rosetta"):
            macos.check(self.context, package, info)

    def test_universal_native_slice_is_bounded_and_truncated_headers_refuse(self):
        path = self.home / "native-command"
        thin = struct.pack("<IIIIIIII", 0xFEEDFACF, macos.ARM64, 0, 2, 0, 0, 0, 0)
        prefix = struct.pack(">II", 0xCAFEBABE, 1)
        path.write_bytes(prefix + struct.pack(">IIIII", macos.ARM64, 0, 28, len(thin), 0) + thin)
        macos.macho_arm64(path)
        path.write_bytes(prefix + struct.pack(">IIIII", macos.ARM64, 0, 4096, len(thin), 0) + thin)
        with self.assertRaisesRegex(safety.Error, "no Rosetta"):
            macos.macho_arm64(path)
        path.write_bytes(thin[:8])
        with self.assertRaisesRegex(safety.Error, "no Rosetta"):
            macos.macho_arm64(path)

    def test_foreign_collision_and_nix_apps_link_are_preserved(self):
        package = self.package()
        app = self.fixture / "Applications/kitty.app"
        app.mkdir(parents=True)
        foreign = app / "foreign"
        foreign.write_text("preserve")
        with self.assertRaisesRegex(safety.Error, "collision"):
            macos.preflight(self.context, package, self.info(package, False))
        self.assertEqual(foreign.read_text(), "preserve")
        shutil.rmtree(app)
        app.symlink_to("/nix/store/foreign-kitty/Applications/kitty.app")
        with self.assertRaisesRegex(safety.Error, "collision"):
            macos.preflight(self.context, package, self.info(package, False))
        self.assertTrue(app.is_symlink())

    def test_owned_payload_replacement_and_escaped_bundle_commands_refuse(self):
        package = self.package()
        app = self.install_app(package)
        info = self.info(package)
        owned = macos.check(self.context, package, info)
        (app / "Contents/Resources/payload").write_text("foreign replacement")
        with self.assertRaisesRegex(safety.Error, "changed native payload"):
            macos.preflight(self.context, package, info, owned)
        self.assertEqual((app / "Contents/Resources/payload").read_text(), "foreign replacement")
        package["executables"]["kitty"] = "/usr/local/bin/kitty"
        with self.assertRaisesRegex(safety.Error, "not in the declared native bundle"):
            macos.check(self.context, package, info)

    def test_formula_opt_path_native_architecture_and_owner(self):
        package = self.package("gui-tabiew")
        prefix = self.fixture / "opt/homebrew"
        keg = prefix / "Cellar/tabiew" / package["version"]
        macho(keg / "bin/tw")
        (prefix / "opt").mkdir()
        opt = prefix / "opt/tabiew"
        opt.symlink_to(keg)
        observed = macos.check(self.context, package, self.info(package))
        self.assertEqual(observed["executables"]["tw"]["path"], str(opt / "bin/tw"))
        opt.unlink()
        opt.symlink_to(self.home)
        with self.assertRaisesRegex(safety.Error, "outside its native Cellar owner"):
            macos.check(self.context, package, self.info(package))

    def test_font_family_all_faces_and_foreign_font_collisions(self):
        package = self.package("jetbrains-mono-nf")
        info = self.info(package)
        faces = package["macos"]["font"]["required_faces"]
        fonts = self.home / "Library/Fonts"
        for face in faces:
            font(fonts / face, "JetBrainsMono Nerd Font")
        observed = macos.check(self.context, package, info)
        self.assertEqual(len(observed["files"]), 4)
        self.assertTrue(
            all("JetBrainsMono Nerd Font" in item["families"] for item in observed["files"])
        )
        with self.assertRaisesRegex(safety.Error, "collision"):
            macos.preflight(self.context, package, self.info(package, False))
        font(fonts / faces[0], "A foreign family")
        with self.assertRaisesRegex(safety.Error, "wrong font family"):
            macos.check(self.context, package, info)
        self.assertEqual(macos.font_families(fonts / faces[0]), ["A foreign family"])

    def test_symlink_font_directory_never_redirects_installation(self):
        package = self.package("jetbrains-mono-nf")
        (self.home / "Library").mkdir()
        (self.home / "Library/Fonts").symlink_to(self.home / "foreign-fonts")
        with self.assertRaisesRegex(safety.Error, "unsafe destination ancestor"):
            macos.preflight(self.context, package, self.info(package, False))

    def test_fixture_path_override_requires_both_isolation_markers(self):
        package = self.package()
        for marker in ("_verification_only", "_fixture_context"):
            context = dict(self.context)
            context.pop(marker)
            with self.assertRaisesRegex(safety.Error, "isolated verification"):
                macos.preflight(context, package, self.info(package, False))


class EngineLifecycleTests(Fixture):
    def test_owned_install_retire_preserves_app_data_and_honest_recovery(self):
        package = self.package()
        self.context["packages"] = [package]
        info = self.info(package, False)
        records = {"home": str(self.home), "packages": {}, "links": {}}
        data = self.home / "Library/Application Support/kitty/session"
        data.parent.mkdir(parents=True)
        data.write_text("user session and credentials stay")
        manager = self.fake_manager(package, info)
        with (
            patch.object(providers, "run", side_effect=manager),
            patch.object(cli, "run", side_effect=manager),
        ):
            providers.provision(self.context, self.context_path, records, [package["id"]])
            self.assertFalse(records["packages"][package["id"]]["preexisting"])
            self.assertIn("macos", records["packages"][package["id"]]["observed"])
            cli.retire_packages(self.context, self.context_path, [package["id"]])
            self.assertFalse((self.fixture / "Applications/kitty.app").exists())
            self.assertEqual(data.read_text(), "user session and credentials stay")
            uninstall = next(call for call in self.calls if call[1] == "uninstall")
            self.assertEqual(uninstall[2:], ["--cask", "--", "kitty"])
            self.assertNotIn("--zap", uninstall)
            with self.assertRaisesRegex(safety.Error, "no retained package selection"):
                recovery.recover_packages(self.context, self.context_path, [package["id"]])
            # Reinstallation is explicit provision of the available provider;
            # it is not mislabeled an exact historical Homebrew rollback.
            info["casks"][0]["version"] = "0.49.3"
            retained = safety.load(Path(self.context["paths"]["state"]) / "packages.json")
            providers.provision(self.context, self.context_path, retained, [package["id"]])
            self.assertEqual(retained["packages"][package["id"]]["observed"]["installed"], "0.49.3")
            self.assertEqual(data.read_text(), "user session and credentials stay")

    def test_preexisting_app_is_neither_upgraded_nor_owned_for_retirement(self):
        package = self.package("gui-slack")
        self.context["packages"] = [package]
        app = self.install_app(package)
        info = self.info(package)
        records = {"home": str(self.home), "packages": {}, "links": {}}
        with patch.object(providers, "run", side_effect=self.fake_manager(package, info)):
            providers.provision(self.context, self.context_path, records, [package["id"]])
            self.assertTrue(records["packages"][package["id"]]["preexisting"])
            self.assertFalse(any(call[1] in ("install", "upgrade") for call in self.calls))
            with self.assertRaisesRegex(safety.Error, "predates engine ownership"):
                cli.retire_packages(self.context, self.context_path, [package["id"]])
        self.assertTrue(app.is_dir())

    def test_changed_owned_app_blocks_provider_uninstall(self):
        package = self.package()
        self.context["packages"] = [package]
        info = self.info(package, False)
        records = {"home": str(self.home), "packages": {}, "links": {}}
        with (
            patch.object(providers, "run", side_effect=self.fake_manager(package, info)),
            patch.object(cli, "run") as remove,
        ):
            providers.provision(self.context, self.context_path, records, [package["id"]])
            path = self.fixture / "Applications/kitty.app/Contents/Resources/payload"
            path.write_text("foreign replacement")
            with self.assertRaisesRegex(safety.Error, "changed since provision"):
                cli.retire_packages(self.context, self.context_path, [package["id"]])
            remove.assert_not_called()
            self.assertEqual(path.read_text(), "foreign replacement")

    def test_freshness_reports_upstream_provider_and_installed_separately(self):
        package = self.package()
        self.context["packages"] = [package]
        self.install_app(package)
        info = self.info(package)
        info["casks"][0]["version"] = "0.49.3"
        with (
            patch.object(providers, "run", side_effect=self.fake_manager(package, info)),
            patch.object(updates, "run", return_value=json.dumps(info)),
            patch.object(
                updates,
                "upstream",
                return_value={"version": "0.49.4", "url": "https://example.invalid/release"},
            ),
        ):
            row = updates.report(self.context, self.context_path, refresh=True)["packages"][0]
        self.assertEqual(row["selected"], "0.49.2")
        self.assertEqual(row["installed"]["installed"], "0.49.2")
        self.assertEqual(row["provider_candidate"]["casks"][0]["version"], "0.49.3")
        self.assertEqual(row["upstream"]["version"], "0.49.4")
        self.assertFalse(any(call[1] in ("install", "upgrade", "uninstall") for call in self.calls))


if __name__ == "__main__":
    unittest.main()
