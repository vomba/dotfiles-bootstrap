"""Exercise upstream completion functions in genuine ZLE completion contexts.

Only root flags/subcommands are requested: no providers, resources, or credentials.
Home Manager startup is evaluated from the public, GPG-disabled fixture, never
from a user's generated configuration. Every writable path is disposable.
"""

import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import tempfile
import time
import unittest


PROFILES = json.loads(Path(os.environ["COMPLETION_PROFILES"]).read_text())
ZSH = os.environ["COMPLETION_ZSH"]


class ShellCompletions(unittest.TestCase):
    def test_feature_transitions_and_completion_only_ownership(self):
        expected = {"act", "golazo", "upctl", "sonobuoy", "kubectl-oidc_login", "helmfile", "lazygit"}
        removed = {
            "default": set(),
            "apps-disabled": {"act", "golazo"},
            "cloud-disabled": {"upctl"},
            "kubernetes-disabled": {"sonobuoy", "kubectl-oidc_login", "helmfile"},
            "mise-disabled": {"helmfile"},
            "yazi-disabled": set(),
            "renamed-yazi": set(),
            "git-disabled": {"lazygit"},
            "renamed-lazygit": set(),
            "lazygit-wrapper-disabled": set(),
        }
        for name, absent in removed.items():
            with self.subTest(profile=name):
                profile = PROFILES[name]
                self.assertEqual(set(profile["executables"]), expected - absent)
                package = Path(profile["package"])
                self.assertFalse((package / "bin").exists())
                functions = package / "share/zsh/site-functions"
                self.assertEqual({p.name for p in functions.iterdir()}, {"_" + n for n in expected - absent})
        self.assertIsNone(PROFILES["zsh-disabled"]["package"])

    def complete(self, line, profile="default"):
        with tempfile.TemporaryDirectory(prefix="shell-completion-") as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            for relative in (".config/mise", ".cache/oh-my-zsh", ".local/share", ".local/state", "runtime"):
                (home / relative).mkdir(parents=True, exist_ok=True)
            (home / ".config/mise/config.toml").symlink_to(os.environ["COMPLETION_MISE_CONFIG"])
            startup = Path(PROFILES[profile]["startup"]).read_text().replace(os.environ["COMPLETION_HOME"], str(home))
            results = root / "candidates"
            done = root / "done"
            mapping = root / "mapping"
            script = root / "fixture.zsh"
            script.write_text(
                "DISABLE_AUTO_UPDATE=true\nzstyle ':omz:update' mode disabled\n"
                + startup
                + r'''
# Record actual eligible compadd matches, without replacing native generators.
compadd() {
  local -a captured
  builtin compadd -A captured "$@"
  print -rl -- "${captured[@]}" >> "$TEST_CANDIDATES"
  builtin compadd "$@"
}
_test_complete() {
  _main_complete
  print -r -- "${_comps[terraform]} ${_comps[kubectl]} ${_comps[openstack]} ${_comps[ansible]} ${_comps[mise]} ${_comps[$TEST_WRAPPER]}" > "$TEST_MAPPING"
  print done > "$TEST_DONE"
}
zle -C test-complete complete-word _test_complete
bindkey '^X' test-complete
PS1='completion-test> '
'''
            )
            env = {
                "HOME": str(home),
                "ZDOTDIR": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local/share"),
                "XDG_STATE_HOME": str(home / ".local/state"),
                "XDG_RUNTIME_DIR": str(home / "runtime"),
                "PATH": os.environ["COMPLETION_BIN"] + os.pathsep + os.environ["PATH"],
                "SHELL": ZSH,
                "TERM": "xterm",
                "LC_ALL": "C",
                "NIX_PROFILES": os.environ["COMPLETION_ASSETS"],
                "ZSH": os.environ["COMPLETION_OMZ"],
                "ZSH_CACHE_DIR": str(home / ".cache/oh-my-zsh"),
                "ZSH_DISABLE_COMPFIX": "true",
                "MISE_OFFLINE": "1",
                "MISE_AUTO_INSTALL": "0",
                "MISE_NOT_FOUND_AUTO_INSTALL": "0",
                "MISE_NOT_FOUND_SYSTEM_FALLBACK": "0",
                "MISE_SYSTEM_CONFIG_DIR": str(root / "empty-system"),
                "MISE_GLOBAL_CONFIG_FILE": str(home / ".config/mise/config.toml"),
                "MISE_CEILING_PATHS": str(root),
                "KUBECONFIG": str(root / "absent-kubeconfig"),
                "TEST_CANDIDATES": str(results),
                "TEST_DONE": str(done),
                "TEST_MAPPING": str(mapping),
                "TEST_WRAPPER": PROFILES[profile]["yaziWrapper"],
            }
            pid, terminal = pty.fork()
            if pid == 0:
                os.chdir(root)
                os.execve(ZSH, [ZSH, "-dfi"], env)
            transcript = bytearray()
            deadline = time.monotonic() + 45
            sent = False
            os.write(terminal, ("source " + shlex.quote(str(script)) + "\n").encode())
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
                        if not sent and b"completion-test>" in transcript:
                            os.write(terminal, line.encode() + b"\x18")
                            sent = True
                self.assertTrue(done.exists(), (line, transcript.decode(errors="replace")))
                candidates = results.read_text().splitlines() if results.exists() else []
                providers = mapping.read_text().split()
                self.assertEqual(providers[:5], ["_terraform", "_kubectl", "_openstack", "_ansible", "_mise"])
                expected_wrapper = ["_yazi"] if PROFILES[profile]["yaziEnabled"] else []
                self.assertEqual(providers[5:], expected_wrapper)
                self.last_completion_transcript = transcript.decode(errors="replace")
                installs = home / ".local/share/mise/installs"
                self.assertFalse(installs.exists() and any(installs.iterdir()))
                return candidates
            finally:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                os.close(terminal)

    def test_native_root_candidates_and_preserved_providers(self):
        cases = [
            ("act --li", "--list"),
            ("golazo --h", "--help"),
            ("upctl --h", "--help"),
            ("sonobuoy --h", "--help"),
            ("kubectl-oidc_login --h", "--help"),
            ("helmfile --h", "--help"),
            ("crossplane --h", "--help"),
            ("terraform pl", "plan"),
            ("kubectl --names", "--namespace"),
            ("openstack --os-cl", "--os-cloud"),
            ("ansible --ver", "--version"),
            ("mise --verb", "--verbose"),
            ("lazygit --pa", "--path"),
        ]
        for line, candidate in cases:
            with self.subTest(line=line):
                self.assertIn(candidate, self.complete(line), self.last_completion_transcript)

    def test_evaluated_yazi_wrapper_inherits_native_candidates(self):
        for profile in ("default", "renamed-yazi"):
            with self.subTest(profile=profile):
                wrapper = PROFILES[profile]["yaziWrapper"]
                self.assertIn("--help", self.complete(wrapper + " --he", profile), self.last_completion_transcript)

    def test_evaluated_lazygit_wrapper_inherits_native_candidates(self):
        profile = "renamed-lazygit"
        wrapper = PROFILES[profile]["lazygitWrapper"]
        self.assertIn("--path", self.complete(wrapper + " --pa", profile), self.last_completion_transcript)

    def test_disabled_wrappers_do_not_offer_native_flags(self):
        for profile, key, prefix, candidate in (
            ("yazi-disabled", "yaziWrapper", " --he", "--help"),
            ("lazygit-wrapper-disabled", "lazygitWrapper", " --pa", "--path"),
        ):
            with self.subTest(profile=profile):
                wrapper = PROFILES[profile][key]
                self.assertNotIn(candidate, self.complete(wrapper + prefix, profile))


if __name__ == "__main__":
    unittest.main()
