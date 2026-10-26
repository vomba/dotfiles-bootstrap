"""Behavioral package-selection tests over the production native manifest."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[2]
ENGINE = BASE / "native/engine"
sys.path.insert(0, str(ENGINE))
SPEC = importlib.util.spec_from_file_location("inventory_manifest", ENGINE / "manifest.py")
MANIFEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANIFEST)
import configuration
import safety

CHEZMOI = os.environ.get("DOTFILES_TEST_CHEZMOI")
ZSH = shutil.which("zsh")


def boolean_leaves(value, prefix=""):
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            yield from boolean_leaves(item, name)
        elif isinstance(item, bool):
            yield name


class NativeInventory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.base = self.root / "public"
        self.home = self.root / "home"
        self.home.mkdir()
        (self.base / "native/manifests").mkdir(parents=True)
        (self.base / "native/engine").mkdir()
        self.fragment = json.loads((BASE / "native/manifests/base.json").read_text())
        self.save_fragment()
        shutil.copyfile(ENGINE / "pins.json", self.base / "native/engine/pins.json")
        self.off = [
            f"{name}=false" for name in boolean_leaves(self.fragment["defaults"]["dotfiles"])
        ]

    def save_fragment(self):
        (self.base / "native/manifests/base.json").write_text(json.dumps(self.fragment))

    def context(self, platform="linux-x64", flags=(), disabled=False):
        return MANIFEST.compose(
            self.base,
            self.base,
            self.home,
            platform,
            flags=(self.off if disabled else []) + list(flags),
        )

    def selected(self, **kwargs):
        return {record["id"]: record for record in self.context(**kwargs)["packages"]}

    def test_independent_features_do_not_inherit_parent_group_switches(self):
        examples = {
            "shell.enable": "fzf",
            "shell.zsh.enable": "zsh",
            "shell.git.enable": "delta",
            "shell.gpg.enable": "gnupg",
            "shell.gnuTools.enable": "coreutils",
            "dev.enable": "go",
            "dev.cloud.enable": "upctl",
            "dev.kubernetes.enable": "kubie",
            "dev.lsp.enable": "marksman",
            "apps.enable": "golazo",
            "apps.firefox.enable": "firefox",
            "apps.editors.enable": "helix",
            "apps.yazi.enable": "yazi",
        }
        for flag, package in examples.items():
            with self.subTest(flag=flag):
                self.assertIn(package, self.selected(flags=[flag + "=true"], disabled=True))

    def test_jless_native_abi_dependency_closure_does_not_require_a_desktop(self):
        linux = self.selected(disabled=True, flags=["apps.enable=true"])
        dependencies = [linux[name] for name in linux["jless"]["requires"]]
        self.assertEqual(
            {package["name"] for package in dependencies},
            {"libxcb1", "libxcb-render0", "libxcb-shape0", "libxcb-xfixes0"},
        )
        self.assertTrue(all(package["provider"] == "apt" for package in dependencies))
        self.assertTrue(all(package["retain_shared_runtime"] for package in dependencies))
        mac = self.selected(platform="macos-arm64", disabled=True, flags=["apps.enable=true"])
        self.assertEqual(mac["jless"].get("requires", []), [])
        self.assertFalse(
            any(package.get("name", "").startswith("libxcb") for package in mac.values())
        )

    def test_disabling_one_group_does_not_disable_independent_modules(self):
        context = self.selected(
            flags=["apps.enable=false", "dev.enable=false", "shell.enable=false"]
        )
        for package in ("firefox", "helix", "yazi", "marksman", "kubie", "upctl", "zsh", "delta"):
            self.assertIn(package, context)
        self.assertNotIn("golazo", context)
        self.assertNotIn("skopeo", context)
        # Dependency closure keeps required runtimes, not unrelated sibling groups.
        self.assertIn("node", context)
        self.assertIn("go", context)

    def test_legacy_mise_tools_require_integration_and_their_feature(self):
        flags = ["dev.cloud.enable=true", "dev.kubernetes.enable=true"]
        selected = self.selected(disabled=True, flags=flags)
        self.assertFalse({"terraform", "cidr", "helmfile"} & selected.keys())
        selected = self.selected(disabled=True, flags=flags + ["dev.mise.enable=true"])
        self.assertTrue({"terraform", "cidr", "helmfile"} <= selected.keys())
        selected = self.selected(disabled=True, flags=["dev.mise.enable=true"])
        self.assertFalse({"terraform", "cidr", "helmfile"} & selected.keys())

    def test_shared_owners_follow_or_predicates(self):
        for flag in ("apps.enable", "dev.lsp.enable"):
            self.assertIn("jq", self.selected(disabled=True, flags=[flag + "=true"]))
        for flag in ("dev.kubernetes.enable", "shell.zsh.enable"):
            self.assertIn("kubectl", self.selected(disabled=True, flags=[flag + "=true"]))

    def test_yazi_dependencies_survive_disabled_shell_and_apps_groups(self):
        selected = self.selected(disabled=True, flags=["apps.yazi.enable=true"])
        self.assertTrue(
            {
                "yazi",
                "ffmpeg",
                "poppler",
                "sevenzip",
                "imagemagick",
                "jq",
                "fd",
                "ripgrep",
                "fzf",
                "zoxide",
            }
            <= selected.keys()
        )
        self.assertNotIn("golazo", selected)
        self.assertNotIn("starship", selected)

    def test_apps_credentials_do_not_require_enabling_gpg_configuration(self):
        for platform in ("linux-x64", "macos-arm64"):
            context = self.context(platform, flags=["apps.enable=true"], disabled=True)
            selected = {package["id"] for package in context["packages"]}
            self.assertTrue({"rbw", "pass", "pinentry", "bash", "gnupg", "git"} <= selected)
            self.assertFalse(context["dotfiles"]["shell"]["gpg"]["enable"])
            for command in ("rbw", "pass", "pinentry", "bash", "gpg"):
                self.assertTrue(Path(context["executables"][command]).is_absolute())

    def test_global_python_and_openstack_interpreters_have_independent_feature_scopes(self):
        shutil.copyfile(
            BASE / "native/manifests/tools.json", self.base / "native/manifests/tools.json"
        )
        shutil.copytree(BASE / "native/recipes/tools", self.base / "native/recipes/tools")
        global_version = "3.15.42"
        for package in self.fragment["packages"]:
            if package["id"] == "python":
                # The wheel-locked compatibility interpreter must not become the global default.
                self.assertGreater(tuple(map(int, package["version"].split(".")[:2])), (3, 12))
                package["version"] = global_version
        self.save_fragment()
        for platform in ("linux-x64", "macos-arm64"):
            for apps in (False, True):
                for cloud in (False, True):
                    with self.subTest(platform=platform, apps=apps, cloud=cloud):
                        context = self.context(
                            platform,
                            disabled=True,
                            flags=[
                                f"apps.enable={str(apps).lower()}",
                                f"dev.cloud.enable={str(cloud).lower()}",
                            ],
                        )
                        selected = {record["id"]: record for record in context["packages"]}
                        self.assertEqual("python" in selected, apps)
                        self.assertEqual("python312" in selected, cloud)
                        self.assertEqual("openstackclient-full" in selected, cloud)
                        for identifier in ("python-venv", "python-pip"):
                            self.assertEqual(
                                identifier in selected, cloud and platform == "linux-x64"
                            )
                        if apps:
                            global_python = selected["python"]
                            self.assertEqual(
                                (
                                    global_python["provider"],
                                    global_python["tool"],
                                    global_python["version"],
                                    global_python["policy"],
                                ),
                                ("mise", "python", global_version, "latest-stable"),
                            )
                            expected = (
                                Path(context["paths"]["data"])
                                / f"mise/installs/python/{global_version}/bin/python3"
                            )
                            self.assertEqual(context["executables"]["python3"], str(expected))
                        if cloud:
                            scoped = selected["python312"]
                            self.assertEqual(set(scoped["executables"]), {"python3.12"})
                            expected = (
                                "/usr/bin/python3.12"
                                if platform == "linux-x64"
                                else "/opt/homebrew/opt/python@3.12/bin/python3.12"
                            )
                            self.assertEqual(context["executables"]["python3.12"], expected)
                            self.assertNotEqual(context["executables"]["python3"], expected)
                            self.assertIn("python312", selected["openstackclient-full"]["requires"])
                            self.assertNotIn("python", selected["openstackclient-full"]["requires"])
                            if platform == "macos-arm64":
                                self.assertEqual(
                                    (scoped["provider"], scoped["name"], scoped["policy"]),
                                    ("brew", "python@3.12", "project"),
                                )
                                self.assertNotIn("version", scoped)
                        else:
                            self.assertNotIn("python3.12", context["executables"])

    def test_shell_helper_commands_have_platform_native_owners(self):
        for platform in ("linux-x64", "macos-arm64"):
            context = self.context(platform, flags=["shell.zsh.enable=true"], disabled=True)
            for command in (
                "kubectl",
                "kubectx",
                "kubens",
                "jq",
                "fx",
                "yh",
                "openssl",
                "ssh",
                "ssh-keygen",
            ):
                self.assertIn(command, context["executables"])
                self.assertTrue(Path(context["executables"][command]).is_absolute())

    def test_ssh_client_selection_preserves_platform_and_independent_features(self):
        for platform in ("linux-x64", "macos-arm64"):
            for feature in ("shell.zsh.enable", "shell.gpg.enable"):
                with self.subTest(platform=platform, feature=feature):
                    context = self.context(platform, disabled=True, flags=[feature + "=true"])
                    selected = {package["id"]: package for package in context["packages"]}
                    self.assertEqual(context["executables"]["ssh"], "/usr/bin/ssh")
                    self.assertTrue(selected["openssh"]["retain_shared_runtime"])
                    prefix = "/usr" if platform == "linux-x64" else "/opt/homebrew/opt/openssh"
                    for command in ("ssh-add", "ssh-agent", "ssh-keygen"):
                        self.assertEqual(context["executables"][command], f"{prefix}/bin/{command}")
            disabled = self.context(platform, disabled=True)
            self.assertNotIn("ssh", disabled["executables"])
            self.assertNotIn("openssh", {package["id"] for package in disabled["packages"]})

    def ssh_configuration(self, platform="macos-arm64", flags=(), bin_relative=".local/bin"):
        # Render the production source and its real manifest together, not a
        # copied template or a reduced fixture that could conceal missing data.
        context = MANIFEST.compose(BASE, BASE, self.home, platform, flags=self.off + list(flags))
        context["paths"]["bin"] = str(Path(context["home"]) / bin_relative)
        context["executables"]["chezmoi"] = str(Path(CHEZMOI).resolve())
        return context

    @unittest.skipUnless(CHEZMOI, "set DOTFILES_TEST_CHEZMOI for real SSH exposure lifecycle")
    def test_mac_ssh_exposure_follows_independent_features_and_owned_disable(self):
        target = self.home / ".local/bin/ssh"
        ssh_config = self.home / ".ssh/config"
        ssh_config.parent.mkdir()
        ssh_config.write_text("Host *\n    UseKeychain yes\n")
        disabled = self.ssh_configuration()
        for flags in (
            ["shell.zsh.enable=true"],
            ["shell.gpg.enable=true"],
            ["shell.zsh.enable=true", "shell.gpg.enable=true"],
        ):
            with self.subTest(flags=flags):
                enabled = self.ssh_configuration(flags=flags)
                stage = configuration.stage(enabled)
                self.assertEqual(os.readlink(stage / "home/.local/bin/ssh"), "/usr/bin/ssh")
                configuration.apply(enabled, stage)
                configuration.apply(enabled, configuration.stage(enabled))
                self.assertEqual(os.readlink(target), enabled["executables"]["ssh"])
                self.assertEqual(
                    configuration.read_record(enabled)["files"][".local/bin/ssh"],
                    {"kind": "symlink", "target": "/usr/bin/ssh"},
                )
                configuration.apply(disabled, configuration.stage(disabled))
                self.assertFalse(target.is_symlink())
                self.assertNotIn(".local/bin/ssh", configuration.read_record(disabled)["files"])
                self.assertEqual(ssh_config.read_text(), "Host *\n    UseKeychain yes\n")
        linux = self.ssh_configuration("linux-x64", flags=["shell.zsh.enable=true"])
        linux_stage = configuration.stage(linux)
        self.assertFalse((linux_stage / "home/.local/bin/ssh").is_symlink())
        self.assertNotIn(".local/bin/ssh", safety.load(linux_stage / "render.json")["files"])

    @unittest.skipUnless(CHEZMOI, "set DOTFILES_TEST_CHEZMOI for real SSH collision boundaries")
    def test_mac_ssh_exposure_preserves_foreign_files_links_and_owned_replacements(self):
        enabled = self.ssh_configuration(flags=["shell.zsh.enable=true"])
        stage = configuration.stage(enabled)
        target = self.home / ".local/bin/ssh"
        target.parent.mkdir(parents=True)
        target.write_text("foreign client")
        with self.assertRaisesRegex(safety.Error, "foreign"):
            configuration.apply(enabled, stage)
        self.assertEqual(target.read_text(), "foreign client")
        target.unlink()
        # Even an identical target is not authority to adopt an unrecorded leaf.
        target.symlink_to("/usr/bin/ssh")
        with self.assertRaisesRegex(safety.Error, "foreign"):
            configuration.apply(enabled, stage)
        self.assertEqual(os.readlink(target), "/usr/bin/ssh")
        target.unlink()
        configuration.apply(enabled, stage)
        target.unlink()
        target.symlink_to(sys.executable)
        disabled = self.ssh_configuration()
        for operation in (
            lambda: configuration.apply(disabled, configuration.stage(disabled)),
            lambda: configuration.apply(enabled, retire=True),
        ):
            with self.assertRaisesRegex(safety.Error, "modified"):
                operation()
            self.assertEqual(os.readlink(target), sys.executable)

    @unittest.skipUnless(CHEZMOI and ZSH, "native chezmoi and Zsh required for managed SSH PATH")
    def test_mac_managed_shell_path_resolves_exposed_client(self):
        for bin_relative in (".local/bin", "bin", ".commands/nested", ".local/bin/custom"):
            with self.subTest(bin_relative=bin_relative):
                context = self.ssh_configuration(
                    flags=["shell.zsh.enable=true"], bin_relative=bin_relative
                )
                configuration.apply(context, configuration.stage(context))
                result = subprocess.run(
                    [ZSH, "-c", "command -v ssh"],
                    env={
                        "HOME": str(self.home),
                        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
                        "ZDOTDIR": str(self.home),
                    },
                    text=True,
                    capture_output=True,
                    check=True,
                )
                exposed = Path(result.stdout.strip())
                self.assertEqual(exposed, Path(context["paths"]["bin"]) / "ssh")
                self.assertEqual(exposed.resolve(), Path(context["executables"]["ssh"]).resolve())
                disabled = self.ssh_configuration(bin_relative=bin_relative)
                configuration.apply(disabled, configuration.stage(disabled))
                self.assertFalse(exposed.is_symlink())

    @unittest.skipUnless(CHEZMOI, "native chezmoi required for custom bin source merging")
    def test_custom_bin_merges_generated_commands_and_rejects_source_collisions(self):
        private_body = "#!/bin/sh\nprintf 'private-command\\n'\n"

        def generate_private_command(context, action, context_path, source_dir, **kwargs):
            destination = source_dir / configuration.encoded(
                Path(context["paths"]["bin"]).relative_to(context["home"])
            )
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "executable_private-helper").write_text(private_body)

        def generate_collision(context, action, context_path, source_dir, **kwargs):
            generate_private_command(context, action, context_path, source_dir)
            destination = source_dir / configuration.encoded(
                Path(context["paths"]["bin"]).relative_to(context["home"])
            )
            (destination / "symlink_ssh.tmpl").write_text("/private/client\n")

        for bin_relative in (".commands/nested", ".local/bin/custom", ".local/bin/custom/deep"):
            with self.subTest(bin_relative=bin_relative):
                context = self.ssh_configuration(
                    flags=["shell.zsh.enable=true"], bin_relative=bin_relative
                )
                with patch.object(configuration, "site", side_effect=generate_private_command):
                    stage = configuration.stage(context)
                rendered_bin = stage / "home" / bin_relative
                self.assertEqual((rendered_bin / "private-helper").read_text(), private_body)
                self.assertEqual(os.readlink(rendered_bin / "ssh"), "/usr/bin/ssh")
                self.assertEqual(
                    {path.relative_to(rendered_bin).as_posix() for path in rendered_bin.rglob("*")},
                    {"private-helper", "ssh"},
                )
                configuration.apply(context, stage)
                target = Path(context["paths"]["bin"]) / "private-helper"
                self.assertEqual(target.read_text(), private_body)
                with (
                    patch.object(configuration, "site", side_effect=generate_collision),
                    self.assertRaisesRegex(safety.Error, "custom bin source collision"),
                ):
                    configuration.stage(context)
                self.assertEqual(target.read_text(), private_body)
                self.assertEqual(os.readlink(Path(context["paths"]["bin"]) / "ssh"), "/usr/bin/ssh")
                disabled = self.ssh_configuration(bin_relative=bin_relative)
                with patch.object(configuration, "site", side_effect=generate_private_command):
                    disabled_stage = configuration.stage(disabled)
                configuration.apply(disabled, disabled_stage)
                self.assertFalse((Path(context["paths"]["bin"]) / "ssh").is_symlink())
                self.assertEqual(target.read_text(), private_body)

        mise = self.ssh_configuration(
            flags=["dev.mise.enable=true"], bin_relative=".commands/nested"
        )
        mise_stage = configuration.stage(mise)
        self.assertTrue((mise_stage / "home/.commands/nested/dotfiles-mise-install").is_file())
        self.assertFalse((mise_stage / "home/.commands/nested/ssh").is_symlink())
        linux = self.ssh_configuration(
            "linux-x64", flags=["shell.zsh.enable=true"], bin_relative=".commands/nested"
        )
        self.assertFalse((configuration.stage(linux) / "home/.commands/nested/ssh").is_symlink())

    @unittest.skipUnless(
        CHEZMOI and sys.platform == "darwin",
        "native macOS and chezmoi required for Apple UseKeychain behavior",
    )
    def test_mac_selected_and_exposed_ssh_accept_apple_keychain_config(self):
        context = self.ssh_configuration(flags=["shell.zsh.enable=true"])
        configuration.apply(context, configuration.stage(context))
        config = self.home / "synthetic-ssh-config"
        config.write_text("Host *\n    UseKeychain yes\n    IdentityFile none\n")
        for executable in (
            context["executables"]["ssh"],
            str(Path(context["paths"]["bin"]) / "ssh"),
        ):
            with self.subTest(executable=executable):
                # -G evaluates only this synthetic config: no SSH connection,
                # authentication, key access or ambient user config is needed.
                result = subprocess.run(
                    [executable, "-G", "-F", str(config), "keychain-test.invalid"],
                    env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertIn("hostname keychain-test.invalid", result.stdout)

    def test_generic_gui_abi_dependencies_are_linux_only_and_shared(self):
        gui = {"gui-alsa", "gui-vulkan", "gui-xkbcommon-x11", "gui-xcb-xfixes", "gui-x11-xcb"}
        linux = self.selected(platform="linux-x64", disabled=True, flags=["apps.enable=true"])
        macos = self.selected(platform="macos-arm64", disabled=True, flags=["apps.enable=true"])
        self.assertTrue(gui <= linux.keys())
        self.assertFalse(gui & macos.keys())
        firefox = self.selected(disabled=True, flags=["apps.firefox.enable=true"])
        self.assertIn("gui-alsa", firefox["firefox"]["requires"])
        self.assertIn("gui-alsa", firefox)

    def test_feature_retirement_preserves_production_shared_host_runtimes(self):
        entry_spec = importlib.util.spec_from_file_location(
            "inventory_engine_entry", ENGINE / "__main__.py"
        )
        entry = importlib.util.module_from_spec(entry_spec)
        entry_spec.loader.exec_module(entry)
        import providers
        import recovery

        common = {
            "bash",
            "zsh",
            "git",
            "gnupg",
            "openssl",
            "openssh",
            "ca-certificates",
            "curl",
            "tar",
            "unzip",
            "coreutils",
            "grep",
            "gnu-sed",
        }
        noble = {
            "python312",
            "python-venv",
            "python-pip",
            "findutils",
            "diffutils",
            "procps",
            "gtk3",
            "firefox-dbus",
            "firefox-x11",
            "gui-alsa",
            "gui-vulkan",
            "gui-xkbcommon-x11",
            "gui-xcb-xfixes",
            "gui-x11-xcb",
            "gui-abi-check",
        }
        for platform in ("linux-x64", "macos-arm64"):
            with self.subTest(platform=platform):
                # Real fragment composition includes desktop libc/loader owners
                # and their consumers, not a copied policy-only test manifest.
                enabled = MANIFEST.compose(BASE, BASE, self.home, platform)
                disabled = MANIFEST.compose(BASE, BASE, self.home, platform, flags=self.off)
                retained = {
                    package["id"]: package
                    for package in enabled["packages"]
                    if package.get("retain_shared_runtime")
                }
                self.assertTrue(
                    (common | (noble if platform == "linux-x64" else set())) <= retained.keys()
                )
                self.assertNotIn("zsh", {package["id"] for package in disabled["packages"]})
                self.assertNotIn("firefox", {package["id"] for package in disabled["packages"]})
                state = Path(disabled["paths"]["state"])
                state.mkdir(parents=True, exist_ok=True)
                context_path = self.root / "disabled-context.json"
                context_path.write_text(json.dumps(disabled))
                ownership = {
                    "home": disabled["home"],
                    "packages": {
                        identifier: {
                            "selection": package,
                            "observed": {"installed": "fixture-native"},
                            "preexisting": False,
                        }
                        for identifier, package in retained.items()
                    },
                    "links": {},
                }
                record_path = state / "packages.json"
                record_path.write_text(json.dumps(ownership))
                user_data = self.home / "shared-runtime-user-data"
                user_data.write_text("login, application sessions and signing state")
                # Even status probing or a service stop would be an unintended
                # provider operation on this retained branch.
                with (
                    patch.object(providers, "run", side_effect=AssertionError("provider called")),
                    patch.object(entry, "run", side_effect=AssertionError("provider called")),
                    patch.object(
                        entry,
                        "require_stopped_services",
                        side_effect=AssertionError("service called"),
                    ),
                ):
                    result = entry.retire_packages(disabled, context_path, list(retained))
                    restored = recovery.recover_packages(disabled, context_path, list(retained))
                self.assertEqual(result["retired_packages"], [])
                self.assertEqual(set(result["retained_shared_runtime"]), set(retained))
                after = json.loads(record_path.read_text())
                self.assertEqual(after["packages"], ownership["packages"])
                self.assertFalse(after.get("retired"))
                for identifier in retained:
                    self.assertEqual(
                        result["retained_shared_runtime"][identifier]["status"],
                        "retained-shared-runtime",
                    )
                    self.assertFalse(restored[identifier]["restored"])
                self.assertEqual(
                    user_data.read_text(), "login, application sessions and signing state"
                )

    def test_ordinary_apps_and_scoped_project_runtimes_are_not_permanent(self):
        for platform in ("linux-x64", "macos-arm64"):
            selected = self.selected(platform=platform)
            for identifier in (
                "skopeo",
                "pre-commit",
                "shellcheck",
                "firefox",
                "golazo",
                "helix",
                "yazi",
                "python",
                "delta",
                "pinentry",
            ):
                with self.subTest(platform=platform, package=identifier):
                    self.assertFalse(selected[identifier].get("retain_shared_runtime", False))
            if platform == "macos-arm64":
                self.assertFalse(selected["python312"].get("retain_shared_runtime", False))

    def test_platform_selection_has_one_owner_per_id(self):
        for platform in ("linux-x64", "macos-arm64"):
            with self.subTest(platform=platform):
                context = self.context(platform)
                records = context["packages"]
                self.assertEqual(len(records), len({item["id"] for item in records}))
                self.assertTrue(all(platform in item["platforms"] for item in records))
                selected = {item["id"]: item for item in records}
                self.assertEqual(
                    selected["firefox"]["provider"],
                    "artifact" if platform == "linux-x64" else "cask",
                )
                self.assertEqual(
                    selected["btop"]["provider"], "mise" if platform == "linux-x64" else "brew"
                )
                self.assertEqual(
                    selected["pinentry"]["name"],
                    "pinentry-qt" if platform == "linux-x64" else "pinentry-mac",
                )
                self.assertEqual("powershell" in selected, platform == "linux-x64")
                self.assertEqual("xcodegen" in selected, platform == "macos-arm64")
                self.assertEqual(
                    context["executables"]["ps"],
                    "/usr/bin/ps" if platform == "linux-x64" else "/bin/ps",
                )

    def test_competing_same_platform_provider_is_rejected(self):
        self.fragment["packages"].append(
            {"id": "terraform", "provider": "brew", "name": "terraform", "platforms": ["linux-x64"]}
        )
        self.save_fragment()
        with self.assertRaisesRegex(MANIFEST.Error, "duplicate|conflict"):
            self.context()

    def test_terraform_license_and_exact_single_provider(self):
        for platform in ("linux-x64", "macos-arm64"):
            terraform = self.selected(platform=platform)["terraform"]
            self.assertEqual(
                (terraform["provider"], terraform["tool"], terraform["version"]),
                ("mise", "terraform", "1.5.7"),
            )
            self.assertEqual(terraform["policy"], "license-pinned")
            self.assertTrue(terraform["reason"])
        config = tomllib.loads((BASE / "apps/mise/mise.toml").read_text())
        self.assertEqual(config["tools"]["terraform"], "1.5.7")

    def test_canonical_versions_match_every_mise_record(self):
        config = tomllib.loads((BASE / "apps/mise/mise.toml").read_text())
        for package in self.fragment["packages"]:
            if package["provider"] != "mise":
                continue
            with self.subTest(tool=package["tool"]):
                selection = config["tools"][package["tool"]]
                self.assertEqual(
                    package["version"],
                    selection["version"] if isinstance(selection, dict) else selection,
                )
                self.assertIn(package["tool"], config["tool_alias"])

    def test_source_backends_declare_real_verification_boundary(self):
        selected = self.selected()
        for name in ("gopls", "delve"):
            verification = selected[name]["verification"]
            self.assertEqual(verification["toolchain"], "go@" + selected["go"]["version"])
            self.assertEqual(verification["sumdb"], "sum.golang.org")
            self.assertIn("go", selected[name]["requires"])

    def test_coverage_points_to_concrete_owners_or_explicit_retirement(self):
        coverage = json.loads((BASE / "native/coverage.json").read_text())
        owners = set()
        for path in (BASE / "native/manifests").glob("*.json"):
            owners.update(
                record["id"] for record in json.loads(path.read_text()).get("packages", [])
            )
        for entry in coverage["ownership"]:
            with self.subTest(source=entry["source"], owner=entry["old_owner"]):
                self.assertIn(entry["status"], ("native", "retired", "private"))
                self.assertTrue(
                    entry.get("native_owners")
                    or entry.get("config_consumer")
                    or entry.get("reason")
                )
                self.assertTrue(set(entry.get("native_owners", [])) <= owners)
        retired = {
            entry["old_owner"] for entry in coverage["ownership"] if entry["status"] == "retired"
        }
        self.assertTrue(
            {"pkgs.nixfmt", "pkgs.nixd", "pkgs.nix-tree", "nix.package", "nix.gc"} <= retired
        )
        self.assertEqual(coverage["feature_defaults"], self.fragment["defaults"]["dotfiles"])

    def test_retired_private_cursor_flag_is_rejected_without_public_ai_payload(self):
        with self.assertRaises(MANIFEST.Error):
            self.selected(disabled=True, flags=["apps.cursor.enable=true"])
        self.assertNotIn("identity", self.fragment["defaults"])
        self.assertTrue(
            all(record["provider"] in MANIFEST.PROVIDERS for record in self.fragment["packages"])
        )


if __name__ == "__main__":
    unittest.main()
