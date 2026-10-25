"""Isolated recipe lifecycle and genuine ZLE completion regressions.

Run via unittest discovery. No test reads the user's startup or provider state.
DOTFILES_COMPLETION_CONTEXT optionally enables the installed-native integration
matrix (explicit fixture context, not paths.state/context.json). All builds and
ZLE writes still use a temporary HOME. Native tool installation belongs to the
parent provision gate, not this suite.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import sys
import tempfile
import time
import unittest


BASE = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "native_completions", BASE / "native/recipes/completions.py"
)
RECIPE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECIPE)
ZSH = shutil.which("zsh")
if ZSH and "/nix/" in str(Path(ZSH).resolve()):
    ZSH = None


def native_asset(directory, command, candidates):
    directory.mkdir(parents=True, exist_ok=True)
    asset = directory / ("_" + command)
    specs = " ".join(shlex.quote(value + "[fixture option]") for value in candidates)
    asset.write_text(f"#compdef {command}\n_arguments {specs}\n")
    return asset


class CompletionFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="native-completion-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.assets = self.root / "package"
        self.supplemental = self.root / "supplemental"
        self.supplemental.mkdir()
        self.destination = self.root / "built"
        self.context = {
            "home": str(self.home),
            "platform": "macos-arm64" if sys.platform == "darwin" else "linux-x64",
            "paths": {"completions": str(self.destination), "cache": str(self.root / "cache")},
            "dotfiles": {
                "shell": {"zsh": {"enable": True}, "git": {"enable": True}},
                "apps": {"enable": True, "yazi": {"enable": True}},
                "dev": {
                    "cloud": {"enable": True},
                    "kubernetes": {"enable": True},
                    "mise": {"enable": True},
                },
            },
            "executables": {},
            "packages": [],
            "completion_providers": [
                {"id": "native-fixture", "version": "7.2.1", "assets": [str(self.assets)]},
                {
                    "id": "supplemental-fixture",
                    "version": "1",
                    "tier": "supplemental",
                    "assets": [str(self.supplemental)],
                },
            ],
        }
        for command, options in (
            ("ansible", ["--version"]),
            ("openstack", ["--os-cloud"]),
            ("yazi", ["--help"]),
            ("terraform", ["--version"]),
        ):
            native_asset(self.assets, command, options)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.marker = self.root / "generator-invocations"
        for generator in RECIPE.policy()["generators"]:
            command = generator["command"]
            generated_name = "kubelogin" if command == "kubectl-oidc_login" else command
            flags = ["--help", "--list", "--path", "--namespace", "--verbose"]
            script = f"#compdef {generated_name}\n"
            body = (
                "_arguments " + " ".join(shlex.quote(flag + "[fixture]") for flag in flags) + "\n"
            )
            if command == "lazygit":
                script += "_lazygit() {\n" + body + "}\ncompdef _lazygit lazygit\n"
            else:
                script += body
            executable = self.bin / command
            executable.write_text(
                "#!"
                + sys.executable
                + "\nimport json, os, pathlib, sys\n"
                + "root = pathlib.Path("
                + repr(str(self.root))
                + ")\n"
                + "assert pathlib.Path(os.environ['HOME']).parent != root\n"
                + "assert 'AWS_SECRET_ACCESS_KEY' not in os.environ\n"
                + "assert 'OS_PASSWORD' not in os.environ\n"
                + "assert 'SOPS_AGE_KEY_FILE' not in os.environ\n"
                + "if sys.argv[1:] == ['--version']:\n    print('fixture 7.2.1')\n"
                + "elif sys.argv[1:] == ['completion', 'zsh']:\n"
                + "    with (root / 'generator-invocations').open('a') as stream: stream.write("
                + repr(command + "\n")
                + ")\n"
                + "    print("
                + repr(script)
                + ")\n"
                + "else:\n    raise SystemExit('unexpected live provider query')\n"
            )
            executable.chmod(0o755)
            self.context["executables"][command] = str(executable)
            self.context["packages"].append({"id": command, "version": "7.2.1"})
        crossplane = self.bin / "crossplane"
        crossplane.write_text("#!/bin/sh\nprintf '%s\\n' --help --version\n")
        crossplane.chmod(0o755)
        self.context["executables"]["crossplane"] = str(crossplane)

    def build(self):
        return RECIPE.build(self.context, self.destination)

    def test_native_precedence_and_versioned_ownership(self):
        native_asset(self.assets, "act", ["--native"])
        native_asset(self.supplemental, "act", ["--supplemental"])
        receipt = self.build()
        self.assertNotIn("act", self.marker.read_text().splitlines())
        self.assertNotIn("generated/_act", receipt["assets"])
        self.assertEqual(receipt["assets"]["package-native/_act"]["version"], "7.2.1")
        self.assertEqual(receipt["assets"]["supplemental/_act"]["provider"], "supplemental-fixture")
        RECIPE.check(self.context)
        if ZSH:
            candidates, _ = complete(self.context, "act --", self.root / "zle", ZSH)
            self.assertIn("--native", candidates)
            self.assertNotIn("--supplemental", candidates)

    def test_generators_receive_no_credentials_or_user_home(self):
        saved = {
            key: os.environ.get(key)
            for key in ("AWS_SECRET_ACCESS_KEY", "OS_PASSWORD", "SOPS_AGE_KEY_FILE")
        }
        try:
            os.environ.update({key: "synthetic-do-not-inherit" for key in saved})
            receipt = self.build()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(
            set(self.marker.read_text().splitlines()),
            {g["command"] for g in RECIPE.policy()["generators"]},
        )
        self.assertEqual(
            Path(receipt["assets"]["generated/_helmfile"]["source"]).resolve(),
            Path(self.context["executables"]["helmfile"]).resolve(),
        )
        self.assertFalse((self.home / ".kube").exists())

    def test_conflicting_asset_owners_fail_before_promotion(self):
        self.build()
        previous = (self.destination / "ownership.json").read_bytes()
        other = self.root / "other"
        native_asset(other, "yazi", ["--foreign"])
        self.context["completion_providers"].append(
            {"id": "other", "version": "2", "assets": [str(other)]}
        )
        with self.assertRaisesRegex(RECIPE.CompletionError, "ownership collision"):
            self.build()
        self.assertEqual(previous, (self.destination / "ownership.json").read_bytes())

    @unittest.skipUnless(ZSH, "native Zsh required")
    def test_homebrew_shared_completion_links_keep_formula_ownership(self):
        manager = self.root / "manager"
        brew = manager / "bin/brew"
        brew.parent.mkdir(parents=True)
        formula = self.root / "formula"
        assets = formula / "share/zsh/site-functions"
        shutil.copytree(self.assets, assets)
        git = native_asset(assets, "git", ["--version"])
        shared = manager / "share/zsh/site-functions"
        shared.mkdir(parents=True)
        (shared / "_git").symlink_to(git)
        installed_version = self.root / "installed-version"
        installed_version.write_text("2_1")
        brew.write_text(
            "#!"
            + sys.executable
            + "\nimport json, sys\nfrom pathlib import Path\n"
            + "if sys.argv[1:] == ['--prefix', 'git']:\n    print("
            + repr(str(formula))
            + ")\n"
            + "elif sys.argv[1:] == ['info', '--json=v2', 'git']:\n"
            + "    print(json.dumps({'formulae': [{'installed': [{'version': Path("
            + repr(str(installed_version))
            + ").read_text()}]}]}))\n"
            + "elif sys.argv[1:] == ['--version']:\n    print('Homebrew fixture')\n"
            + "else:\n    raise SystemExit('unexpected package-manager operation')\n"
        )
        brew.chmod(0o755)
        self.context["executables"].update({"brew": str(brew), "zsh": ZSH})
        self.context["packages"].append(
            {"id": "git", "name": "git", "provider": "brew", "version": "2"}
        )
        supplemental = self.context.pop("completion_providers")[1]
        self.context["completion_providers"] = [
            *RECIPE.discover_providers(self.context),
            supplemental,
        ]
        receipt = self.build()
        self.assertEqual(receipt["assets"]["package-native/_git"]["provider"], "git")
        self.assertEqual((self.destination / "package-native/_git").read_bytes(), git.read_bytes())
        del self.context["completion_providers"]
        RECIPE.check(self.context)
        installed_version.write_text("2_2")
        with self.assertRaisesRegex(
            RECIPE.CompletionError, "stale completion provider version: git"
        ):
            RECIPE.check(self.context)

    def test_failed_generator_preserves_previous_tree(self):
        self.build()
        previous = (self.destination / "generated/_act").read_bytes()
        Path(self.context["executables"]["act"]).write_text("#!/bin/sh\nprintf partial\nexit 9\n")
        with self.assertRaisesRegex(RECIPE.CompletionError, "command failed"):
            self.build()
        self.assertEqual(previous, (self.destination / "generated/_act").read_bytes())

    def test_foreign_replacements_block_build_and_retire(self):
        self.build()
        changed = self.destination / "generated/_act"
        changed.write_text("foreign content")
        for operation in (lambda: self.build(), lambda: RECIPE.retire(self.context)):
            with self.assertRaisesRegex(RECIPE.CompletionError, "modified completion asset"):
                operation()
        self.assertEqual(changed.read_text(), "foreign content")

    def test_foreign_tree_and_unrecorded_asset_are_not_deleted(self):
        self.destination.mkdir()
        foreign = self.destination / "unrelated"
        foreign.write_text("preserve")
        with self.assertRaisesRegex(RECIPE.CompletionError, "unowned"):
            self.build()
        self.assertEqual(foreign.read_text(), "preserve")

    def test_disable_and_retire_preserve_private_namespace(self):
        self.build()
        private = self.destination / "private"
        private.mkdir()
        (private / "_work").write_text("private-owner")
        self.context["dotfiles"]["shell"]["zsh"]["enable"] = False
        self.build()
        self.assertFalse((self.destination / "generated/_act").exists())
        self.assertEqual((private / "_work").read_text(), "private-owner")
        RECIPE.retire(self.context)
        self.assertEqual((private / "_work").read_text(), "private-owner")
        self.assertEqual(RECIPE.retire(self.context), {"retired": False})
        self.context["dotfiles"]["shell"]["zsh"]["enable"] = True
        self.build()
        self.assertEqual((private / "_work").read_text(), "private-owner")
        self.assertTrue((self.destination / "generated/_act").is_file())

    def test_feature_transition_removes_only_owned_generated_assets(self):
        self.build()
        self.context["dotfiles"]["dev"]["kubernetes"]["enable"] = False
        self.context["dotfiles"]["apps"]["enable"] = False
        receipt = self.build()
        self.assertNotIn("generated/_act", receipt["assets"])
        self.assertNotIn("generated/_golazo", receipt["assets"])
        self.assertNotIn("generated/_helmfile", receipt["assets"])
        self.assertNotIn("generated/_sonobuoy", receipt["assets"])
        self.assertIn("generated/_upctl", receipt["assets"])
        if ZSH:
            _, mapping = complete(self.context, "upctl --he", self.root / "disabled-zle", ZSH)
            self.assertEqual(mapping.get("yy", ""), "_yazi", "Yazi has an independent feature flag")
            self.assertEqual(mapping.get("crossplane", ""), "")

    def test_no_cidr_or_removed_nix_tool_grammar(self):
        for command in ("cidr", "cachix", "nix-tree"):
            native_asset(self.supplemental, command, ["--invalid"])
        receipt = self.build()
        self.assertFalse(
            any(
                relative.endswith(("/_cidr", "/_cachix", "/_nix-tree"))
                for relative in receipt["assets"]
            )
        )

    def test_version_and_executable_changes_invalidate_generated_assets(self):
        self.build()
        package = next(p for p in self.context["packages"] if p["id"] == "helmfile")
        package["version"] = "7.2.2"
        with self.assertRaisesRegex(RECIPE.CompletionError, "stale generated"):
            RECIPE.check(self.context)
        package["version"] = "7.2.1"
        binary = Path(self.context["executables"]["helmfile"])
        binary.write_text(binary.read_text() + "\n# changed executable\n")
        with self.assertRaisesRegex(RECIPE.CompletionError, "stale generated"):
            RECIPE.check(self.context)

    def test_bad_supplemental_source_checksum_fails_without_promotion(self):
        source = json.loads(RECIPE.SOURCES.read_text())["zsh-completions"]
        cache = Path(self.context["paths"]["cache"]) / "completion-sources"
        cache.mkdir(parents=True)
        (cache / ("zsh-completions-" + source["version"] + ".tar.gz")).write_bytes(
            b"corrupt archive"
        )
        with self.assertRaisesRegex(RECIPE.CompletionError, "checksum mismatch"):
            RECIPE.supplemental(self.context, lambda *args: self.fail("unverified asset admitted"))
        self.assertFalse(self.destination.exists())

    def test_empty_generator_output_is_not_success(self):
        Path(self.context["executables"]["act"]).write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaisesRegex(RECIPE.CompletionError, "empty or invalid"):
            self.build()
        self.assertFalse(self.destination.exists())

    def test_unrecorded_files_and_empty_directories_block_retirement(self):
        self.build()
        foreign = self.destination / "foreign-directory"
        foreign.mkdir()
        with self.assertRaisesRegex(RECIPE.CompletionError, "foreign completion directory"):
            RECIPE.retire(self.context)
        self.assertTrue(foreign.is_dir())
        foreign.rmdir()
        (self.destination / "generated/_unrecorded").write_text("foreign")
        with self.assertRaisesRegex(RECIPE.CompletionError, "foreign or missing"):
            self.build()
        self.assertEqual((self.destination / "generated/_unrecorded").read_text(), "foreign")

    @unittest.skipUnless(ZSH, "native zsh required for genuine ZLE behavior")
    def test_package_native_sourceable_lazygit_works_on_first_tab(self):
        source = self.assets / "_lazygit"
        source.write_text(
            "#compdef lazygit\n_lazygit() {\n  _arguments '--path[fixture]'\n}\ncompdef _lazygit lazygit\n"
        )
        self.build()
        self.assertNotIn("lazygit", self.marker.read_text().splitlines())
        found, _ = complete(self.context, "lazygit --pa", self.root / "native-lazygit", ZSH)
        self.assertIn("--path", found)
        RECIPE.check(self.context)

    @unittest.skipUnless(ZSH, "native zsh required for genuine ZLE behavior")
    def test_disabled_wrapper_mappings_do_not_offer_native_flags(self):
        self.context["dotfiles"]["apps"]["yazi"]["shellWrapperName"] = "browse"
        self.context["dotfiles"]["apps"]["yazi"]["enableZshIntegration"] = False
        self.context["dotfiles"]["shell"]["git"]["lazygitWrapperName"] = "gitui"
        self.context["dotfiles"]["shell"]["git"]["lazygitZshIntegration"] = False
        self.build()
        for number, (line, candidate) in enumerate(
            (("browse --he", "--help"), ("gitui --pa", "--path"))
        ):
            found, mapping = complete(self.context, line, self.root / f"no-wrapper-{number}", ZSH)
            self.assertNotIn(candidate, found)
            self.assertEqual(mapping["browse"], "")
            self.assertEqual(mapping["gitui"], "")

    @unittest.skipUnless(ZSH, "native zsh required for genuine ZLE behavior")
    def test_openstack_bash_fallback_uses_real_bash_compatibility(self):
        (self.assets / "_openstack").unlink()
        binary = self.bin / "openstack"
        binary.write_text("#!/bin/sh\nprintf 'openstack fixture 1\\n'\n")
        binary.chmod(0o755)
        self.context["executables"]["openstack"] = str(binary)
        source = self.root / "share/bash-completion/completions/openstack"
        source.parent.mkdir(parents=True)
        source.write_text(
            "_openstack() { COMPREPLY=( --os-cloud --help ); }\ncomplete -F _openstack openstack\n"
        )
        receipt = self.build()
        self.assertEqual(receipt["assets"]["generated/_openstack"]["kind"], "bash-compat")
        found, _ = complete(self.context, "openstack --os-cl", self.root / "openstack-bash", ZSH)
        self.assertIn("--os-cloud", found)

    @unittest.skipUnless(ZSH, "native zsh required for genuine ZLE behavior")
    def test_real_mise_local_task_catalog_without_task_execution(self):
        mise = shutil.which("mise")
        if not mise or "/nix/" in str(Path(mise).resolve()):
            self.skipTest("native mise unavailable")
        self.context["executables"]["mise"] = str(Path(mise).resolve())
        next(p for p in self.context["packages"] if p["id"] == "mise")["version"] = "latest"
        self.build()
        tasks = (
            "[settings]\nauto_install = false\n"
            '[tasks."work:fixture"]\nrun = "echo forbidden > task-executed"\n'
            '[tasks."dotfiles:status"]\nrun = "echo status"\n'
        )
        for number, (line, expected) in enumerate(
            (("mise run work:fi", "work:fixture"), ("mise run dotfiles:st", "dotfiles:status"))
        ):
            root = self.root / f"mise-tasks-{number}"
            found, _ = complete(self.context, line, root, ZSH, tasks)
            self.assertIn(expected, found)
            self.assertFalse((root / "task-executed").exists())

    @unittest.skipUnless(ZSH, "native zsh required for genuine ZLE behavior")
    def test_first_tab_wrappers_native_flags_and_crossplane_protocol(self):
        self.context["dotfiles"]["apps"]["yazi"]["shellWrapperName"] = "browse"
        self.context["dotfiles"]["shell"]["git"]["lazygitWrapperName"] = "gitui"
        self.build()
        before = self.marker.read_bytes()
        for number, (line, expected) in enumerate(
            (
                ("act --li", "--list"),
                ("golazo --he", "--help"),
                ("upctl --he", "--help"),
                ("sonobuoy --he", "--help"),
                ("kubectl-oidc_login --he", "--help"),
                ("helmfile --he", "--help"),
                ("lazygit --pa", "--path"),
                ("gitui --pa", "--path"),
                ("browse --he", "--help"),
                ("crossplane --he", "--help"),
            )
        ):
            with self.subTest(line=line):
                candidates, mapping = complete(self.context, line, self.root / f"zle-{number}", ZSH)
                self.assertIn(expected, candidates)
                self.assertEqual(mapping["browse"], "_yazi")
                self.assertEqual(mapping["gitui"], "_lazygit")
        self.assertEqual(
            before, self.marker.read_bytes(), "startup/Tab must never regenerate assets"
        )


def complete(context, line, root, zsh, task_config=None):
    """Collect actual compadd candidates in an isolated interactive ZLE context."""
    root.mkdir(parents=True)
    home = root / "home"
    home.mkdir()
    for path in ("config", "cache", "data", "state", "runtime", "bin"):
        (root / path).mkdir()
    for command, binary in context["executables"].items():
        if Path(binary).is_file():
            (root / "bin" / command).symlink_to(binary)
    config = root / "config/mise"
    config.mkdir()
    (config / "config.toml").write_text(task_config or "[settings]\nauto_install = false\n")
    candidates = root / "candidates"
    done = root / "done"
    mapping = root / "mapping"
    completions = Path(context["paths"]["completions"])
    fpath = " ".join(shlex.quote(str(completions / tier)) for tier in RECIPE.TIERS)
    startup = root / "fixture.zsh"
    startup.write_text(
        "fpath=("
        + fpath
        + " $fpath)\nautoload -Uz compinit\ncompinit -D -i\n"
        + "source "
        + shlex.quote(str(completions / "init.zsh"))
        + "\n"
        + r"""
compadd() {
  local -a captured
  builtin compadd -A captured "$@"
  print -rl -- "${captured[@]}" >> "$TEST_CANDIDATES"
  builtin compadd "$@"
}
_test_complete() {
  _main_complete
  local command
  for command in yy browse lazygit gitui crossplane terraform kubectl openstack ansible mise; do
    print -r -- "$command=${_comps[$command]}"
  done > "$TEST_MAPPING"
  print done > "$TEST_DONE"
}
zle -C test-complete complete-word _test_complete
bindkey '^X' test-complete
PS1='native-completion-test> '
"""
    )
    env = {
        "HOME": str(home),
        "ZDOTDIR": str(home),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
        "PATH": str(root / "bin") + ":/usr/bin:/bin",
        "SHELL": str(zsh),
        "TERM": "xterm",
        "LC_ALL": "C",
        "MISE_OFFLINE": "1",
        "MISE_AUTO_INSTALL": "0",
        "MISE_NOT_FOUND_AUTO_INSTALL": "0",
        "MISE_NOT_FOUND_SYSTEM_FALLBACK": "0",
        "MISE_GLOBAL_CONFIG_FILE": str(config / "config.toml"),
        "MISE_SYSTEM_CONFIG_DIR": str(root / "absent-system"),
        "MISE_CEILING_PATHS": str(root),
        "KUBECONFIG": str(root / "absent-kubeconfig"),
        "TEST_CANDIDATES": str(candidates),
        "TEST_DONE": str(done),
        "TEST_MAPPING": str(mapping),
    }
    pid, terminal = pty.fork()
    if pid == 0:
        os.chdir(root)
        os.execve(zsh, [zsh, "-dfi"], env)
    transcript = bytearray()
    deadline = time.monotonic() + 30
    sent = False
    os.write(terminal, ("source " + shlex.quote(str(startup)) + "\n").encode())
    try:
        while not done.exists() and time.monotonic() < deadline:
            readable, _, _ = select.select([terminal], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(terminal, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                transcript.extend(chunk)
                if not sent and b"native-completion-test>" in transcript:
                    os.write(terminal, line.encode() + b"\x18")
                    sent = True
        if not done.exists():
            raise AssertionError((line, transcript.decode(errors="replace")))
        found = candidates.read_text().splitlines() if candidates.exists() else []
        providers = dict(row.split("=", 1) for row in mapping.read_text().splitlines())
        installs = root / "data/mise/installs"
        if installs.exists() and any(installs.iterdir()):
            raise AssertionError("completion unexpectedly installed tools")
        return found, providers
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(terminal)


@unittest.skipUnless(
    os.environ.get("DOTFILES_COMPLETION_CONTEXT"), "installed-native fixture context not provided"
)
class InstalledNativeCompletions(unittest.TestCase):
    def test_actual_native_root_candidates_and_local_task_catalog(self):
        context = json.loads(Path(os.environ["DOTFILES_COMPLETION_CONTEXT"]).read_text())
        zsh = str(RECIPE.executable(context, "zsh"))
        with tempfile.TemporaryDirectory(prefix="installed-native-completion-") as temporary:
            root = Path(temporary)
            context = copy.deepcopy(context)
            context["paths"]["completions"] = str(root / "built")
            context["paths"]["cache"] = str(root / "build-cache")
            RECIPE.build(context, root / "built")
            RECIPE.check(context)
            cases = (
                ("act --li", "--list"),
                ("golazo --h", "--help"),
                ("upctl --h", "--help"),
                ("sonobuoy --h", "--help"),
                ("kubectl-oidc_login --h", "--help"),
                ("helmfile --h", "--help"),
                ("crossplane --h", "--help"),
                ("kubectl --names", "--namespace"),
                ("openstack --os-cl", "--os-cloud"),
                ("ansible --ver", "--version"),
                ("mise --verb", "--verbose"),
                ("lazygit --pa", "--path"),
                ("yy --he", "--help"),
            )
            cases += (("terraform pl", "plan"),)
            for number, (line, expected) in enumerate(cases):
                with self.subTest(line=line):
                    found, _ = complete(context, line, root / f"zle-{number}", zsh)
                    self.assertIn(expected, found)
            tasks = (
                "[settings]\nauto_install = false\n"
                '[tasks."dotfiles:status"]\nrun = "echo status"\n'
                '[tasks."work:fixture"]\nrun = "echo must-not-run > task-executed"\n'
            )
            for number, (line, expected) in enumerate(
                (("mise run dotfiles:st", "dotfiles:status"), ("mise run work:fi", "work:fixture"))
            ):
                task_root = root / f"tasks-{number}"
                found, _ = complete(context, line, task_root, zsh, tasks)
                self.assertIn(expected, found)
                self.assertFalse((task_root / "task-executed").exists())


if __name__ == "__main__":
    unittest.main()
