from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "oh-my-zsh/plugins/zsh-kubecolor/zsh-kubecolor.plugin.zsh"


class ShellAliasesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.log = self.home / "calls.jsonl"
        self.zsh = shutil.which("zsh")
        self.assertIsNotNone(self.zsh, "zsh is required for shell regression tests")
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "CALL_LOG": str(self.log),
            "PLUGIN": str(PLUGIN),
        }
        self.executable("kubectl", '''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
if os.environ.get("KUBECTL_FAIL"):
    sys.exit(12)
if args[:2] == ["get", "pods"]:
    print("pod-one\\npod-two")
''')
        self.executable("kubecolor", '''
import json, os, sys
with open(os.environ["CALL_LOG"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
''')
        self.executable("fzf", '''
import os, sys
if os.environ.get("FZF_CANCEL"):
    sys.exit(130)
if not os.environ.get("FZF_EMPTY"):
    rows = sys.stdin.read().splitlines()
    if rows:
        print(rows[-1])
''')

    def executable(self, name: str, body: str) -> None:
        script = self.bin / name
        script.write_text(f"#!{sys.executable}\n" + body)
        script.chmod(0o755)

    def run_zsh(self, body: str) -> subprocess.CompletedProcess[str]:
        # compinit needs standard utilities; no host Kubernetes binary is reachable.
        for name in ("bash", "mkdir", "chmod", "rm", "sed", "grep"):
            command = shutil.which(name)
            if command and not (self.bin / name).exists():
                (self.bin / name).symlink_to(command)
        setup = 'autoload -Uz compinit\ncompinit -u -D\nsource "$PLUGIN"\n'
        # Parse invocations after the plugin has installed aliases, as a prompt does.
        scenario = self.home / "scenario.zsh"
        scenario.write_text(body)
        return subprocess.run(
            [self.zsh, "-f", "-c", setup + 'source "$1"\n',
             "shell-regressions", str(scenario)],
            env=self.env, text=True, capture_output=True, check=False,
        )

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_resource_aliases_keep_clusterroles_distinct_from_certificate_requests(self) -> None:
        result = self.run_zsh('''
kgcr role-a
kdcr role-a
kdelcr role-a
kgcreq request-a
kdcreq request-a
kdelcreq request-a
kgcreqa
kdelc cert-a
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [
            ["get", "clusterroles", "role-a"],
            ["describe", "clusterroles", "role-a"],
            ["delete", "clusterroles", "role-a"],
            ["get", "certificaterequest", "request-a"],
            ["describe", "certificaterequest", "request-a"],
            ["delete", "certificaterequest", "request-a"],
            ["get", "certificaterequest", "-A"],
            ["delete", "certificate", "cert-a"],
        ])

    def test_edit_namespace_lookup_and_interactive_exec_remain_distinct(self) -> None:
        result = self.run_zsh('''
kep pod-one
kgpn kube-system
kexec --context "context with spaces" -n team -- sh -c "echo hello"
ke pod-one sh -c "echo hello"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [
            ["edit", "pods", "pod-one"],
            ["get", "pods", "-n", "kube-system"],
            ["get", "pods", "--context", "context with spaces", "-n", "team",
             "--no-headers", "--field-selector=status.phase=Running",
             "--output=custom-columns=NAME:.metadata.name"],
            ["exec", "-ti", "--context", "context with spaces", "-n", "team",
             "pod-two", "--", "sh", "-c", "echo hello"],
            ["exec", "-it", "pod-one", "--", "sh", "-c", "echo hello"],
        ])

    def test_picker_defaults_to_bash(self) -> None:
        result = self.run_zsh("kexec -n team\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1], [
            "exec", "-ti", "-n", "team", "pod-two", "--", "bash",
        ])

    def test_cancelled_picker_never_executes_a_pod(self) -> None:
        self.env["FZF_CANCEL"] = "1"
        result = self.run_zsh("kexec\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.calls())
        self.assertTrue(all(call[0] != "exec" for call in self.calls()))

    def test_empty_picker_never_executes_a_pod(self) -> None:
        self.env["FZF_EMPTY"] = "1"
        result = self.run_zsh("kexec\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.calls())
        self.assertTrue(all(call[0] != "exec" for call in self.calls()))

    def test_failed_lookup_never_executes_a_pod(self) -> None:
        self.env["KUBECTL_FAIL"] = "1"
        result = self.run_zsh("kexec\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0][:2], ["get", "pods"])

    def test_ksync_uses_retained_helper_without_vendor_path(self) -> None:
        self.executable("rsync", '''
import json, os, sys
with open(os.environ["CALL_LOG"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
    stream.write(json.dumps([os.environ.get(key) for key in
        ("KUBE_CONTEXT", "POD_NAMESPACE", "POD_CONTAINER")]) + "\\n")
''')
        result = self.run_zsh('''
ksync --context "local context" -n team -c app -- -av "local dir/" pod:/data
print -r -- "$PATH"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.bin))
        args, environment = self.calls()
        self.assertEqual(args, ["--blocking-io", f"--rsh={PLUGIN.parent}/bin/kubectl-rsync",
                                "-av", "local dir/", "pod:/data"])
        self.assertEqual(environment, ["local context", "team", "app"])

    def test_ksync_remote_shell_preserves_context_and_command_boundaries(self) -> None:
        self.env.update({
            "KUBECTL_RSYNC_RSH": "true",
            "KUBE_CONTEXT": "local context",
            "POD_CONTAINER": "app",
        })
        result = self.run_zsh('ksync -l pod team rsync --server "remote dir/"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [[
            "exec", "--context=local context", "--namespace=team", "--container=app",
            "pod", "-i", "--", "rsync", "--server", "remote dir/",
        ]])

    def test_ksync_propagates_rsync_failure(self) -> None:
        self.executable("rsync", "import sys\nsys.exit(23)\n")
        result = self.run_zsh("ksync -- -av local/ pod:/data\n")
        self.assertEqual(result.returncode, 23, result.stderr)

    def test_relocated_plugin_never_generates_completion_at_runtime(self) -> None:
        # A consumer's HOME has no checkout; the plugin is installed elsewhere.
        plugin = self.home / "store-shaped-plugin"
        shutil.copytree(ROOT / "oh-my-zsh/plugins/zsh-kubecolor", plugin)
        for path in plugin.rglob("*"):
            path.chmod(0o555 if path.is_dir() or path.name == "kubectl-rsync" else 0o444)
        plugin.chmod(0o555)

        def make_writable() -> None:
            plugin.chmod(0o755)
            for path in plugin.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)

        self.addCleanup(make_writable)
        self.env["PLUGIN"] = str(plugin / "zsh-kubecolor.plugin.zsh")
        result = self.run_zsh("kgp\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.calls(), [["get", "pods"]])
        self.assertFalse((plugin / "completion/kubectl_completion").exists())


if __name__ == "__main__":
    unittest.main()
