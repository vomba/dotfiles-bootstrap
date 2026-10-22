"""Offline resolution/ownership checks against the evaluated Home Manager inputs.

Local absolute version links are intentional: they exercise genuine backend and
shim dispatch without pretending fixture bytes match downloaded lock artifacts.
No Home Manager activation or user state is used.
"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import tomllib
import unittest


MISE = os.environ["MISE_TEST_BIN"]
INSTALLER = os.environ["MISE_TEST_INSTALLER"]
BASH = os.environ["MISE_TEST_BASH"]
PROFILES = json.loads(Path(os.environ["MISE_TEST_PROFILES"]).read_text())
FIXTURES = json.loads(Path(os.environ["MISE_TEST_FIXTURES"]).read_text())
MANIFEST = tomllib.loads(Path(os.environ["MISE_TEST_MANIFEST"]).read_text())
LOCK = Path(os.environ["MISE_TEST_LOCK"])


class MiseIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mise-integration-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.config = self.home / ".config" / "mise"
        self.data = self.home / ".local" / "share"
        self.installs = self.data / "mise" / "installs"
        self.shims = self.data / "mise-shims"
        self.project = self.root / "project"
        for path in (self.config, self.data, self.project, self.root / "empty-system"):
            path.mkdir(parents=True, exist_ok=True)
        self.empty = self.root / "empty.toml"
        self.empty.write_text("")
        self.global_config = self.config / "config.toml"
        self.global_config.symlink_to(PROFILES["default"]["config"])
        (self.config / "mise.lock").symlink_to(LOCK)
        # Start with no inherited MISE_*, startup variables, tokens, HOME or XDG
        # paths. The native check's PATH contains only declared build inputs.
        self.env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.data),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "PATH": str(self.shims) + os.pathsep + os.environ["PATH"],
            "LC_ALL": "C",
            "NO_COLOR": "1",
            "CI": "1",
            "MISE_CONFIG_DIR": str(self.config),
            "MISE_GLOBAL_CONFIG_FILE": str(self.global_config),
            "MISE_SYSTEM_CONFIG_DIR": str(self.root / "empty-system"),
            "MISE_SYSTEM_CONFIG_FILE": str(self.empty),
            "MISE_SHIMS_DIR": str(self.shims),
            "MISE_CEILING_PATHS": str(self.root),
            "MISE_TRUSTED_CONFIG_PATHS": str(self.root),
            "MISE_AUTO_INSTALL": "0",
            "MISE_NOT_FOUND_AUTO_INSTALL": "0",
            "MISE_NOT_FOUND_SYSTEM_FALLBACK": "0",
            "MISE_OFFLINE": "1",
        }
        for name, version in MANIFEST["tools"].items():
            self.link_version(name, version, FIXTURES[name])

    def link_version(self, name, version, target):
        tool = self.installs / name
        tool.mkdir(parents=True, exist_ok=True)
        # scan_tool_dir reads the sidecar at the tool directory, not version.
        (tool / ".mise.backend.toml").write_text(
            f'short = "{name}"\nfull = "{MANIFEST["tool_alias"][name]}"\n'
            'explicit_backend = true\n'
        )
        (tool / version).symlink_to(target, target_is_directory=True)

    def command(self, argv, *, env=None, cwd=None, status=0):
        result = subprocess.run(
            [str(arg) for arg in argv],
            env=self.env if env is None else env,
            cwd=self.project if cwd is None else cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if status is not None:
            self.assertEqual(result.returncode, status, (argv, result.stdout, result.stderr))
        return result

    def reshim(self):
        self.command([MISE, "reshim", "--force"])

    def assert_shims(self):
        for name in MANIFEST["tools"]:
            with self.subTest(shim=name):
                self.assertTrue((self.shims / name).is_symlink())
                self.assertEqual(os.readlink(self.shims / name), MISE)

    def install_inventory(self):
        return sorted(str(path.relative_to(self.installs)) for path in self.installs.rglob("*"))

    def assert_no_downloads(self):
        downloads = self.data / "mise" / "downloads"
        self.assertFalse(downloads.exists() and any(p.is_file() for p in downloads.rglob("*")))

    def test_generated_profiles_resolve_selected_tools_and_backend_identities(self):
        for profile, details in PROFILES.items():
            if not details["enabled"]:
                continue
            with self.subTest(profile=profile):
                self.global_config.unlink()
                self.global_config.symlink_to(details["config"])
                listing = json.loads(self.command([MISE, "ls", "--current", "--json"]).stdout)
                self.assertEqual(sorted(listing), details["tools"])
                for name in details["tools"]:
                    self.assertEqual(listing[name][0]["version"], MANIFEST["tools"][name])
                    self.assertTrue(listing[name][0]["installed"])
                    backend = self.command([MISE, "tool", name, "--backend"]).stdout.strip()
                    self.assertEqual(backend, MANIFEST["tool_alias"][name])
        self.assert_no_downloads()

    def test_explicit_exec_scopes_project_environment_without_shell_hooks(self):
        (self.project / "mise.toml").write_text(
            '[env]\nMISE_PROJECT_PROBE="project"\n_.file=".env"\n'
        )
        (self.project / ".env").write_text("MISE_DOTENV_PROBE=dotenv\n")
        result = self.command([
            MISE, "exec", "--", BASH, "-p", "-c",
            'printf "%s:%s\\n" "$MISE_PROJECT_PROBE" "$MISE_DOTENV_PROBE"',
        ])
        self.assertEqual(result.stdout, "project:dotenv\n")
        self.assertNotIn("MISE_PROJECT_PROBE", self.env)
        self.assert_no_downloads()

    def test_immutable_global_config_has_installed_sibling_lock(self):
        self.assertTrue(self.global_config.is_symlink())
        self.assertEqual(self.global_config.stat().st_mode & 0o222, 0)
        sibling = self.global_config.with_name("mise.lock")
        self.assertEqual(sibling.read_bytes(), LOCK.read_bytes())
        self.assertEqual(sibling.stat().st_mode & 0o222, 0)
        before = (self.global_config.read_bytes(), sibling.read_bytes())
        self.command([INSTALLER])
        self.assertEqual(before, (self.global_config.read_bytes(), sibling.read_bytes()))
        self.assert_shims()
        self.assert_no_downloads()

    def test_installer_rejects_bad_arguments_and_paths_before_startup_code(self):
        marker = self.root / "startup-ran"
        startup = self.root / "startup.bash"
        startup.write_text(f"printf bad > {shlex.quote(str(marker))}\n")
        env = dict(self.env, BASH_ENV=str(startup))
        self.command([INSTALLER, "--help"], env=env, status=2)
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            for value in ("", "relative/path"):
                with self.subTest(key=key, value=value):
                    self.command([INSTALLER], env=dict(env, **{key: value}), status=2)
        absent_home = dict(env)
        absent_home.pop("HOME")
        self.command([INSTALLER], env=absent_home, status=2)
        self.assertFalse(marker.exists())
        self.assertFalse(self.shims.exists())

    def test_installer_ignores_project_startup_and_environment_poison(self):
        marker = self.root / "poison-ran"
        startup = self.root / "startup.bash"
        startup.write_text(f"printf startup > {shlex.quote(str(marker))}\n")
        poison_bin = self.root / "poison-bin"
        poison_bin.mkdir()
        fake_mise = poison_bin / "mise"
        fake_mise.write_text(f"#!{BASH}\nprintf path > {shlex.quote(str(marker))}\nexit 91\n")
        fake_mise.chmod(0o755)
        malformed = "this is not valid TOML [\n"
        for directory in (self.project, self.root, self.home, self.config, self.root / "empty-system"):
            (directory / ".miserc.toml").write_text(malformed)
            (directory / "miserc.toml").write_text(malformed)
        (self.config / "config.local.toml").write_text(malformed)
        poison_config = self.root / "poison.toml"
        poison_config.write_text(malformed)
        self.shims.mkdir()
        unrelated = self.shims / "foreign-command"
        unrelated.write_text("foreign extensionless file\n")
        executable = self.shims / "foreign-executable"
        executable.write_text(f"#!{BASH}\nexit 0\n")
        executable.chmod(0o755)
        env = dict(self.env, **{
            "PATH": str(poison_bin),
            "BASH_ENV": str(startup),
            "ENV": str(startup),
            "BASH_FUNC_mise%%": f"() {{ printf function > {shlex.quote(str(marker))}; }}",
            "MISE_GLOBAL_CONFIG_FILE": str(poison_config),
            "MISE_SYSTEM_CONFIG_FILE": str(poison_config),
            "MISE_CONFIG_DIR": str(self.root),
            "MISE_SYSTEM_CONFIG_DIR": str(self.root),
            "MISE_AQUA_REGISTRIES": "http://127.0.0.1:1/poison",
            "MISE_GITHUB_API_URL": "http://127.0.0.1:1/poison",
            "MISE_DATA_DIR": str(self.root / "wrong-data"),
            "MISE_SHIMS_DIR": str(self.root / "wrong-shims"),
            "__MISE_ORIG_PATH": str(poison_bin),
            "__MISE_DIFF": "invalid-poison",
            "GITHUB_TOKEN": "synthetic-not-a-credential",
        })
        for _ in range(2):
            self.command([INSTALLER], env=env)
            self.assert_shims()
            self.assertEqual(unrelated.read_text(), "foreign extensionless file\n")
            self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "wrong-shims").exists())
        self.assertFalse((self.root / "wrong-data").exists())
        self.assert_no_downloads()

    def test_installer_refreshes_old_owned_links_without_downloads(self):
        self.command([INSTALLER])
        before = self.install_inventory()
        # A previous qualified installation's dispatcher, not a foreign file.
        old = self.root / "old-mise" / "bin" / "mise"
        old.parent.mkdir(parents=True)
        old.symlink_to(MISE)
        for name in MANIFEST["tools"]:
            shim = self.shims / name
            shim.unlink()
            shim.symlink_to(old)
        self.command([INSTALLER])
        self.assert_shims()
        self.assertEqual(before, self.install_inventory())
        self.assert_no_downloads()

    def test_installer_preserves_foreign_managed_name_and_reports_failure(self):
        self.shims.mkdir()
        collision = self.shims / "cidr"
        content = f"#!{BASH}\nprintf foreign-cidr\\n\n"
        collision.write_text(content)
        collision.chmod(0o755)
        result = self.command([INSTALLER], status=1)
        self.assertIn(f"dotfiles-mise-install: refusing foreign shim: {collision}", result.stderr)
        self.assertFalse(collision.is_symlink())
        self.assertEqual(collision.read_text(), content)
        self.assert_no_downloads()

    def test_bare_shims_use_local_backend_tools_and_preserve_arguments(self):
        self.reshim()
        for name, version in MANIFEST["tools"].items():
            with self.subTest(tool=name):
                result = self.command([name, "space separated", "; not shell syntax", ""])
                self.assertEqual(result.stdout.splitlines(), [
                    f"fixture:{name}:{version}", "<space separated>", "<; not shell syntax>", "<>",
                ])
        self.assert_no_downloads()

    def test_project_version_file_and_nearer_explicit_pin_override_global(self):
        self.link_version("terraform", "1.4.6", os.environ["MISE_TEST_SECOND_TERRAFORM"])
        self.reshim()
        (self.project / ".terraform-version").write_text("1.4.6\n")
        self.assertEqual(self.command(["terraform"]).stdout.splitlines()[0], "fixture:terraform:1.4.6")
        nested = self.project / "nested"
        nested.mkdir()
        (nested / "mise.toml").write_text('[tools]\nterraform = "1.5.7"\n')
        self.assertEqual(self.command(["terraform"], cwd=nested).stdout.splitlines()[0], "fixture:terraform:1.5.7")
        self.assert_no_downloads()

    def test_unresolved_shim_never_falls_through_or_installs(self):
        self.reshim()
        (self.installs / "terraform" / MANIFEST["tools"]["terraform"]).unlink()
        later = self.root / "system-bin"
        later.mkdir()
        sentinel = self.root / "fallback-ran"
        system = later / "terraform"
        system.write_text(f"#!{BASH}\nprintf fallback > {shlex.quote(str(sentinel))}\n")
        system.chmod(0o755)
        env = dict(self.env, PATH=str(self.shims) + os.pathsep + str(later) + os.pathsep + os.environ["PATH"])
        before = self.install_inventory()
        result = self.command(["terraform", "version"], env=env, status=None)
        self.assertNotEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertFalse(sentinel.exists())
        self.assertEqual(before, self.install_inventory())
        self.assert_no_downloads()

    def test_task_launch_does_not_install_missing_global_tool(self):
        self.reshim()
        (self.installs / "terraform" / MANIFEST["tools"]["terraform"]).unlink()
        (self.project / "mise.toml").write_text(
            '[tasks.probe]\nrun = "terraform version"\n'
        )
        before = self.install_inventory()
        result = self.command([MISE, "run", "probe"], status=None)
        self.assertNotEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(before, self.install_inventory())
        self.assert_no_downloads()

    def test_home_manager_collision_guard_preserves_foreign_config_and_lock(self):
        generation = self.root / "generation"
        files = generation / "home-files" / ".config" / "mise"
        files.mkdir(parents=True)
        (files / "config.toml").symlink_to(PROFILES["default"]["config"])
        (files / "mise.lock").symlink_to(LOCK)
        env = dict(self.env, newGenPath=str(generation))
        checker = os.environ["MISE_TEST_COLLISION_CHECK"]
        # Exercise only HM's pre-write guard, never activation/linkGeneration.
        self.command([checker], env=env)
        for name in ("config.toml", "mise.lock"):
            with self.subTest(file=name):
                target = self.config / name
                original = os.readlink(target)
                target.unlink()
                foreign = "foreign configuration must survive\n"
                target.write_text(foreign)
                result = self.command([checker], env=env, status=None)
                self.assertNotEqual(result.returncode, 0, (result.stdout, result.stderr))
                self.assertEqual(target.read_text(), foreign)
                self.assertFalse(target.is_symlink())
                target.unlink()
                target.symlink_to(original)

    def test_filtered_tenv_keeps_other_entrypoints_without_terraform_proxy(self):
        binary_dir = Path(os.environ["MISE_TEST_TENV"]) / "bin"
        for name in ("tenv", "tf", "tofu", "terragrunt", "terramate", "atmos"):
            with self.subTest(command=name):
                self.assertTrue(os.access(binary_dir / name, os.X_OK), name)
        self.assertFalse(os.path.lexists(binary_dir / "terraform"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
