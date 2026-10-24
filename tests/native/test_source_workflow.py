"""Local Git/HTTP fixtures for the production source update lifecycle.

The native mise executable is a protocol fixture, not a replacement resolver in
production. It serves deterministic publisher metadata and native Aube graphs;
all Git checkout/archive, signed-commit, nonforce-push, engine candidate checking
and private-pin promotion use the actual implementation and disposable roots.
No live credentials, provider installation, public network or publication.
"""

from __future__ import annotations

import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch
import urllib.request

PUBLIC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PUBLIC / "native/engine"))
import manifest
import safety
import source
import source_workflow as workflow
import updates


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def lock_text(config, url, checksum, directory):
    """Synthetic native provider output; production never synthesizes locks."""
    lines = ["lockfile_version = 3"]
    for name, selected in config["tools"].items():
        version = selected["version"] if isinstance(selected, dict) else selected
        backend = config["tool_alias"][name]
        lines.extend(
            (
                f"[[tools.{name}]]",
                f'version = "{version}"',
                f'backend = "{backend}"',
                f'specifiers = ["{version}"]',
            )
        )
        if backend.startswith("npm:"):
            graph = directory / ".mise/locks" / name / version
            graph.mkdir(parents=True, exist_ok=True)
            (graph / "aube-lock.yaml").write_text(
                "lockfileVersion: 1\npackages:\n  fixture: sha512-reviewed-fixture\n"
            )
            save(graph / "package.json", {"private": True, "dependencies": {backend[4:]: version}})
            digest = hashlib.sha256((graph / "aube-lock.yaml").read_bytes()).hexdigest()
            lines.append(
                f'aube = {{ path = ".mise/locks/{name}/{version}", digest = "sha256:{digest}" }}'
            )
        else:
            for platform in ("linux-x64", "macos-arm64"):
                lines.extend(
                    (
                        f'[tools.{name}."platforms.{platform}"]',
                        f'url = "{url}"',
                        f'checksum = "sha256:{checksum}"',
                    )
                )
    return "\n".join(lines) + "\n"


class SourceWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dotfiles-source-workflow-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.base = self.directory / "public"
        self.private = self.directory / "private"
        self.home = self.directory / "home"
        for path in (self.base, self.private, self.home):
            path.mkdir()
        self.payload = b"verified fixture provider artifact\n"
        self.checksum = hashlib.sha256(self.payload).hexdigest()
        self.url = "https://publisher.example.invalid/artifact"
        self.requests = []
        self.latest = {"github:fixture/tool": "2.0.0", "npm:fixture-npm": "3.0.0"}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(self.path)
                if self.path == "/artifact":
                    payload = owner.payload
                elif self.path == "/metadata":
                    payload = json.dumps(
                        {"latest": owner.latest, "url": owner.url, "sha256": owner.checksum}
                    ).encode()
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        endpoint = "http://127.0.0.1:" + str(self.server.server_port)
        original_urlopen = urllib.request.urlopen

        def transport(request, *args, **kwargs):
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if url != self.url:
                raise AssertionError("Unexpected public metadata request: " + url)
            response = original_urlopen(endpoint + "/artifact", *args, **kwargs)
            response.url = url  # Preserve declared HTTPS identity at transport seam.
            return response

        network = patch.object(safety.urllib.request, "urlopen", side_effect=transport)
        network.start()
        self.addCleanup(network.stop)
        config = {
            "tool_alias": {
                "tool": "github:fixture/tool",
                "npm-tool": "npm:fixture-npm",
                "node": "core:node",
                "terraform": "aqua:hashicorp/terraform",
            },
            "tools": {"tool": "1.0.0", "npm-tool": "2.0.0", "node": "24.0.0", "terraform": "1.5.7"},
        }
        self.original_config = copy.deepcopy(config)
        mise = self.base / "apps/mise"
        mise.mkdir(parents=True)
        text = "[tool_alias]\n" + "".join(
            f'{key} = "{value}"\n' for key, value in config["tool_alias"].items()
        )
        text += "\n[tools]\n" + "".join(
            f'{key} = "{value}"\n' for key, value in config["tools"].items()
        )
        (mise / "mise.toml").write_text(text)
        (mise / "mise.lock").write_text(lock_text(config, self.url, self.checksum, mise))
        packages = [
            {
                "id": key,
                "provider": "mise",
                "tool": key,
                "version": value,
                "policy": "latest-stable",
            }
            for key, value in config["tools"].items()
        ]
        for package in packages:
            if package["id"] == "node":
                package.update(
                    policy="project",
                    reason="Qualified runtime major; explicit compatibility exception",
                )
            if package["id"] == "terraform":
                package.update(
                    policy="license-pinned",
                    reason="Final MPL release, never silently advance to BUSL",
                )
        save(self.base / "native/manifests/base.json", {"schema_version": 1, "packages": packages})
        save(
            self.private / "native/manifests/private.json",
            {"schema_version": 1, "defaults": {"identity": {"name": "PRIVATE MUST NOT BE COPIED"}}},
        )
        (self.private / "secrets.yaml").write_text("private synthetic encrypted fixture\n")
        engine = self.base / "native/engine"
        engine.mkdir()
        (engine / "__main__.py").write_text("# fixture native engine source\n")
        shutil.copyfile(PUBLIC / "native/engine/pins.json", engine / "pins.json")
        self.git(self.base, "init", "--quiet", "--initial-branch=main")
        self.git(self.base, "add", ".")
        self.git(
            self.base,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--quiet",
            "-m",
            "immutable initial public source",
        )
        self.remote = self.directory / "public.git"
        self.git(self.directory, "clone", "--quiet", "--bare", self.base, self.remote)
        revision = self.git(self.base, "rev-parse", "HEAD").strip()
        archive = self.git(self.base, "archive", "--format=tar", revision, binary=True)
        self.pin = {
            "schema_version": 1,
            "status": "published",
            "repository": self.remote.as_uri(),
            "revision": revision,
            "sha256": hashlib.sha256(archive).hexdigest(),
        }
        save(self.private / "bootstrap.lock.json", self.pin)
        self.git(self.private, "init", "--quiet")
        self.context = manifest.compose(self.base, self.private, self.home, "linux-x64")
        self.context["_verification_only"] = True
        fixture_mise = self.home / "fixture-mise"
        # This executable deliberately asserts no launch-time installer is used.
        fixture_mise.write_text(f"""#!{sys.executable}
import hashlib,json,os,pathlib,sys,tomllib,urllib.request
endpoint = {endpoint!r}
config = pathlib.Path(os.environ["MISE_GLOBAL_CONFIG_FILE"])
assert pathlib.Path.cwd() == pathlib.Path("/")
assert os.environ["MISE_NO_ENV"] == "1" and os.environ["MISE_NO_HOOKS"] == "1"
assert os.environ["MISE_AUTO_INSTALL"] == "0" and "SECRET_TOKEN" not in os.environ
with urllib.request.urlopen(endpoint + "/metadata") as response:
    metadata = json.load(response)
if sys.argv[1] == "latest":
    print(metadata["latest"][sys.argv[2]])
elif sys.argv[1] == "lock":
    assert "--platform" in sys.argv
    request = tomllib.loads(config.read_text())
    lines = ["lockfile_version = 3"]
    for name, version in request["tools"].items():
        backend = request["tool_alias"][name]
        lines.extend([f"[[tools.{{name}}]]", f'version = "{{version}}"', f'backend = "{{backend}}"', f'specifiers = ["{{version}}"]'])
        if backend.startswith("npm:"):
            graph = config.parent / ".mise/locks" / name / version
            graph.mkdir(parents=True, exist_ok=True)
            payload = b"lockfileVersion: 1\\npackages:\\n  fixture: sha512-reviewed-fixture\\n"
            (graph / "aube-lock.yaml").write_bytes(payload)
            (graph / "package.json").write_text(json.dumps({{"private":True,"dependencies":{{backend[4:]:version}}}}))
            checksum = hashlib.sha256(payload).hexdigest()
            lines.append('aube = {{ path = ".mise/locks/' + name + '/' + version + '", digest = "sha256:' + checksum + '" }}')
        else:
            for platform in ("linux-x64","macos-arm64"):
                lines.extend([f'[tools.{{name}}."platforms.{{platform}}"]', 'url = "' + metadata["url"] + '"', 'checksum = "sha256:' + metadata["sha256"] + '"'])
    (config.parent / "mise.lock").write_text("\\n".join(lines) + "\\n")
else:
    raise AssertionError("No installation or shell execution is authorized")
""")
        fixture_mise.chmod(0o755)
        self.context["executables"]["mise"] = str(fixture_mise)
        # Engine source candidates normally use HTTPS. Explicit local fixture
        # roots retain source.resolve's actual fetch/archive/inventory checks.
        original_resolve = source.resolve
        resolver = patch.object(
            source,
            "resolve",
            side_effect=lambda *args, **kwargs: original_resolve(
                *args, **{**kwargs, "verification_only": True}
            ),
        )
        resolver.start()
        self.addCleanup(resolver.stop)

    def git(self, directory, *args, binary=False):
        environment = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }
        result = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(directory),
                *map(str, args),
            ],
            env=environment,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout if binary else result.stdout.decode()

    def prepare(self):
        return workflow.run(self.context, "prepare", {"family": "mise", "yes": True})

    def checked(self):
        prepared = self.prepare()
        result = workflow.run(self.context, "check")
        self.assertEqual(result["id"], prepared["id"])
        return result

    def signing(self):
        key = self.home / "fixture-signing"
        result = subprocess.run(
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        allowed = self.home / "allowed-signers"
        allowed.write_text("fixture@example.invalid " + key.with_suffix(".pub").read_text())
        for name, value in {
            "user.name": "Fixture Operator",
            "user.email": "fixture@example.invalid",
            "user.signingkey": str(key),
            "gpg.format": "ssh",
            "gpg.ssh.allowedSignersFile": str(allowed),
        }.items():
            self.git(self.private, "config", name, value)

    def test_prepare_resolves_declared_backends_and_preserves_policy_graphs(self):
        before = (self.private / "bootstrap.lock.json").read_bytes()
        with patch.dict(
            os.environ,
            {"SECRET_TOKEN": "must-not-leak", "MISE_GLOBAL_CONFIG_FILE": "/hostile/project"},
        ):
            result = self.prepare()
        tree = Path(result["worktree"])
        config = tomllib.loads((tree / "apps/mise/mise.toml").read_text())
        self.assertEqual(
            config["tools"],
            {"tool": "2.0.0", "npm-tool": "3.0.0", "node": "24.0.0", "terraform": "1.5.7"},
        )
        self.assertEqual(config["tool_alias"], self.original_config["tool_alias"])
        rows = {row["tool"]: row for row in result["acquisition"]["mise"]}
        self.assertEqual(rows["terraform"]["policy"], "license-pinned")
        self.assertEqual(rows["node"]["policy"], "project")
        self.assertTrue((tree / "apps/mise/.mise/locks/npm-tool/3.0.0/aube-lock.yaml").is_file())
        self.assertTrue((tree / "apps/mise/.mise/locks/npm-tool/2.0.0/aube-lock.yaml").is_file())
        self.assertEqual(before, (self.private / "bootstrap.lock.json").read_bytes())
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.pin["revision"])
        self.assertFalse((tree / "secrets.yaml").exists())
        self.assertNotIn(
            b"PRIVATE MUST NOT BE COPIED",
            b"".join(
                path.read_bytes() for path in tree.rglob("*.json") if ".git" not in path.parts
            ),
        )
        self.assertIn("/metadata", self.requests)

    def test_source_refresh_updates_all_disjoint_platform_variants(self):
        path = self.base / "native/manifests/base.json"
        declared = safety.load(path)
        linux = next(package for package in declared["packages"] if package.get("tool") == "tool")
        linux["platforms"] = ["linux-x64"]
        mac = copy.deepcopy(linux)
        mac["platforms"] = ["macos-arm64"]
        linux["requires"] = ["sample-native-runtime"]
        declared["packages"].extend(
            [
                mac,
                {
                    "id": "sample-native-runtime",
                    "provider": "apt",
                    "name": "libsample-runtime",
                    "platforms": ["linux-x64"],
                    "policy": "distro-security",
                    "reason": "Synthetic native ABI dependency",
                },
            ]
        )
        save(path, declared)
        pin_before = (self.private / "bootstrap.lock.json").read_bytes()
        workflow._acquire_mise(self.context, self.base)
        for platform in ("linux-x64", "macos-arm64"):
            context = manifest.compose(self.base, self.private, self.home, platform)
            selected = next(
                package for package in context["packages"] if package.get("tool") == "tool"
            )
            self.assertEqual(selected["version"], self.latest["github:fixture/tool"])
            self.assertEqual(
                selected.get("requires", []),
                ["sample-native-runtime"] if platform == "linux-x64" else [],
            )
        self.assertEqual((self.private / "bootstrap.lock.json").read_bytes(), pin_before)

    def test_check_creates_candidate_but_does_not_promote_or_publish(self):
        before = (self.private / "bootstrap.lock.json").read_bytes()
        result = self.checked()
        self.assertEqual(result["status"], "checked")
        self.assertIn("/artifact", self.requests)
        self.assertEqual(before, (self.private / "bootstrap.lock.json").read_bytes())
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.pin["revision"])
        self.assertFalse(result["live_runtime_changed"])

    def test_foreign_edits_in_allowed_or_unapproved_files_are_preserved(self):
        result = self.prepare()
        tree = Path(result["worktree"])
        for name in ("apps/mise/mise.toml", "private-credentials.txt"):
            target = tree / name
            old = target.read_bytes() if target.exists() else None
            target.write_bytes(b"foreign bytes must survive\n")
            with self.assertRaises(safety.Error):
                workflow.run(self.context, "check")
            self.assertEqual(target.read_bytes(), b"foreign bytes must survive\n")
            if old is None:
                target.unlink()
            else:
                target.write_bytes(old)

    def test_failed_refresh_keeps_previous_atomic_workspace_and_private_pin(self):
        previous = self.prepare()
        self.latest["github:fixture/tool"] = "0.1.0"
        with self.assertRaisesRegex(safety.Error, "downgrade"):
            self.prepare()
        self.assertEqual(workflow.run(self.context, "status")["id"], previous["id"])
        self.assertEqual(safety.load(self.private / "bootstrap.lock.json"), self.pin)
        self.assertTrue(list((Path(previous["worktree"]).parents[1]).glob("*/receipt.json")))

    def test_corrupt_pinned_archive_and_unknown_options_fail_closed(self):
        changed = {**self.pin, "sha256": "0" * 64}
        save(self.private / "bootstrap.lock.json", changed)
        with self.assertRaisesRegex(safety.Error, "archive differs"):
            self.prepare()
        for operation, options in (
            ("shell", {}),
            ("prepare", {"command": "whoami"}),
            ("publish", {"branch": "main:other"}),
            ("prepare", {"family": "mise", "qt_version": "6.12.0"}),
        ):
            with self.assertRaises(safety.Error):
                workflow.run(self.context, operation, options)

    def test_invalid_publication_refs_fail_before_state_or_remote_mutation(self):
        for branch in (
            "/main",
            "feat//update",
            "feat/../main",
            "feat/update/",
            "-main",
            "main:other",
            "a" * 256,
        ):
            with self.subTest(branch=branch):
                with self.assertRaisesRegex(safety.Error, "publication branch"):
                    workflow.run(self.context, "publish", {"yes": True, "branch": branch})
                self.assertEqual(
                    self.git(self.remote, "rev-parse", "main").strip(), self.pin["revision"]
                )
        self.assertEqual(safety.load(self.private / "bootstrap.lock.json"), self.pin)

    def test_signed_publication_targets_explicit_namespaced_branch_only(self):
        branch = "feat/mise-adoption"
        self.git(self.remote, "update-ref", "refs/heads/" + branch, self.pin["revision"])
        self.checked()
        self.signing()
        result = workflow.run(self.context, "publish", {"yes": True, "branch": branch})
        self.assertEqual(result["status"], "complete")
        selected = safety.load(self.private / "bootstrap.lock.json")
        self.assertEqual(selected["revision"], self.git(self.remote, "rev-parse", branch).strip())
        self.assertNotEqual(selected["revision"], self.pin["revision"])
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.pin["revision"])
        self.assertEqual(self.git(self.private, "rev-list", "--all"), "")

    def test_publication_requires_check_confirmation_and_operator_signing(self):
        self.prepare()
        with self.assertRaisesRegex(safety.Error, "confirmation"):
            workflow.run(self.context, "publish")
        with self.assertRaisesRegex(safety.Error, "checked"):
            workflow.run(self.context, "publish", {"yes": True})
        workflow.run(self.context, "check")
        with self.assertRaisesRegex(safety.Error, "signing key"):
            workflow.run(self.context, "publish", {"yes": True})
        self.assertEqual(safety.load(self.private / "bootstrap.lock.json"), self.pin)

    def test_signed_nonforce_publication_readback_and_narrow_private_pin(self):
        result = self.checked()
        self.signing()
        private_bytes = (self.private / "native/manifests/private.json").read_bytes()
        result = workflow.run(self.context, "publish", {"yes": True})
        self.assertEqual(result["status"], "complete")
        selected = safety.load(self.private / "bootstrap.lock.json")
        self.assertEqual(selected["revision"], self.git(self.remote, "rev-parse", "main").strip())
        self.assertNotEqual(selected["revision"], self.pin["revision"])
        archive = self.git(
            self.remote, "archive", "--format=tar", selected["revision"], binary=True
        )
        self.assertEqual(selected["sha256"], hashlib.sha256(archive).hexdigest())
        self.assertEqual(
            private_bytes, (self.private / "native/manifests/private.json").read_bytes()
        )
        self.assertEqual(self.git(self.private, "rev-list", "--all"), "")
        self.assertEqual(workflow.run(self.context, "status")["status"], "complete")
        tree = Path(result["worktree"])
        self.git(
            tree,
            "-c",
            "gpg.format=ssh",
            "-c",
            "gpg.ssh.allowedSignersFile=" + str(self.home / "allowed-signers"),
            "verify-commit",
            selected["revision"],
        )

    def test_private_pin_failure_recovers_after_remote_publication_without_second_push(self):
        self.checked()
        self.signing()
        with patch.object(
            updates, "promote", side_effect=safety.Error("fixture interrupted pin write")
        ):
            with self.assertRaisesRegex(safety.Error, "interrupted"):
                workflow.run(self.context, "publish", {"yes": True})
        self.assertEqual(safety.load(self.private / "bootstrap.lock.json"), self.pin)
        published = self.git(self.remote, "rev-parse", "main").strip()
        self.assertNotEqual(published, self.pin["revision"])
        with self.assertRaisesRegex(safety.Error, "recover --yes"):
            workflow.run(self.context, "recover")
        result = workflow.run(self.context, "recover", {"yes": True})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(safety.load(self.private / "bootstrap.lock.json")["revision"], published)
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), published)

    def interrupted_pin_restart(self):
        result = self.checked()
        self.signing()
        original_atomic_json = workflow.atomic_json

        def interrupt_completion(path, value):
            if Path(path).name == "receipt.json" and value.get("status") == "complete":
                raise safety.Error("fixture interruption after completed private pin write")
            return original_atomic_json(path, value)

        with patch.object(workflow, "atomic_json", side_effect=interrupt_completion):
            with self.assertRaisesRegex(safety.Error, "after completed private pin write"):
                workflow.run(self.context, "publish", {"yes": True})
        directory = Path(result["worktree"]).parent
        receipt = safety.load(directory / "receipt.json")
        self.assertEqual(receipt["status"], "pinning")
        pin_path = self.private / "bootstrap.lock.json"
        selected = safety.load(pin_path)
        self.assertNotEqual(selected["revision"], self.pin["revision"])
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), selected["revision"])
        # Model the private entry's next invocation: resolve the updated pin
        # into its real immutable cache and compose from that new public root.
        base = source.resolve(pin_path, Path(self.context["paths"]["cache"]) / "sources")
        self.assertNotEqual(str(base), self.context["base"])
        restarted = manifest.compose(base, self.private, self.home, "linux-x64")
        restarted["_verification_only"] = True
        candidate_path = (
            Path(restarted["paths"]["state"])
            / "candidates"
            / receipt["pin_candidate"]
            / "candidate.json"
        )
        self.assertEqual(
            safety.load(candidate_path)["roots"],
            {"public": self.context["base"], "private": self.context["root"]},
        )
        return restarted, directory, candidate_path

    def test_completed_pin_write_recovers_after_immutable_cache_root_changes(self):
        restarted, directory, candidate_path = self.interrupted_pin_restart()
        pin_before = (self.private / "bootstrap.lock.json").read_bytes()
        receipt = safety.load(directory / "receipt.json")
        original_git = workflow._git

        def no_republication(context, tree, *arguments, **kwargs):
            self.assertNotIn(arguments[0], ("commit", "push"))
            return original_git(context, tree, *arguments, **kwargs)

        # Both durable promotion states can remain after the pin replacement.
        for status in ("promoted", "promoting"):
            with self.subTest(candidate_status=status):
                save(directory / "receipt.json", receipt)
                candidate = safety.load(candidate_path)
                candidate["status"] = status
                save(candidate_path, candidate)
                with (
                    patch.object(workflow, "_git", side_effect=no_republication),
                    patch.object(
                        updates, "stage_candidate", side_effect=AssertionError("cannot restage")
                    ),
                    patch.object(
                        updates, "promote", side_effect=AssertionError("cannot repromote")
                    ),
                ):
                    recovered = workflow.run(restarted, "recover", {"yes": True})
                self.assertEqual(recovered["status"], "complete")
                self.assertEqual(safety.load(candidate_path)["status"], "promoted")
                self.assertEqual((self.private / "bootstrap.lock.json").read_bytes(), pin_before)
                self.assertEqual(
                    self.git(self.remote, "rev-parse", "main").strip(),
                    safety.load(self.private / "bootstrap.lock.json")["revision"],
                )

    def test_completed_pin_recovery_refuses_foreign_recorded_roots(self):
        restarted, directory, candidate_path = self.interrupted_pin_restart()
        original = safety.load(candidate_path)
        for repository in ("public", "private"):
            with self.subTest(repository=repository):
                candidate = copy.deepcopy(original)
                candidate["roots"][repository] = str(self.directory / "foreign-root")
                save(candidate_path, candidate)
                with self.assertRaisesRegex(safety.Error, "candidate receipt changed"):
                    workflow.run(restarted, "recover", {"yes": True})
                self.assertEqual(safety.load(candidate_path), candidate)
        save(candidate_path, original)
        receipt_path = directory / "receipt.json"
        receipt = safety.load(receipt_path)
        receipt["private_root"] = str(self.directory / "foreign-private")
        save(receipt_path, receipt)
        with self.assertRaisesRegex(safety.Error, "another private entry"):
            workflow.run(restarted, "recover", {"yes": True})
        self.assertEqual(safety.load(receipt_path), receipt)

    def test_completed_pin_recovery_preserves_foreign_pin_candidate_and_snapshot_edits(self):
        restarted, directory, candidate_path = self.interrupted_pin_restart()
        targets = (
            candidate_path.parent / "private/bootstrap.lock.json",
            self.private / "bootstrap.lock.json",
            self.private / "native/manifests/private.json",
            directory / "tree/apps/mise/mise.toml",
        )
        for target in targets:
            with self.subTest(target=str(target)):
                original = target.read_bytes()
                # Whitespace remains semantically valid JSON, but is still a
                # foreign byte edit to the selected or staged immutable pin.
                foreign = original + b"\n"
                target.write_bytes(foreign)
                with self.assertRaises(safety.Error):
                    workflow.run(restarted, "recover", {"yes": True})
                self.assertEqual(target.read_bytes(), foreign)
                self.assertEqual(safety.load(directory / "receipt.json")["status"], "pinning")
                target.write_bytes(original)
        candidate = safety.load(candidate_path)
        candidate["files"][0]["sha256"] = "0" * 64
        save(candidate_path, candidate)
        with self.assertRaisesRegex(safety.Error, "candidate receipt changed"):
            workflow.run(restarted, "recover", {"yes": True})
        self.assertEqual(safety.load(candidate_path), candidate)

    def test_completed_pin_recovery_preserves_foreign_candidate_symlink(self):
        restarted, directory, candidate_path = self.interrupted_pin_restart()
        staged_pin = candidate_path.parent / "private/bootstrap.lock.json"
        foreign = self.directory / "foreign-pin.json"
        foreign.write_bytes(staged_pin.read_bytes())
        staged_pin.unlink()
        staged_pin.symlink_to(foreign)
        with self.assertRaisesRegex(safety.Error, "candidate bytes changed"):
            workflow.run(restarted, "recover", {"yes": True})
        self.assertTrue(staged_pin.is_symlink())
        self.assertEqual(foreign.read_bytes(), (self.private / "bootstrap.lock.json").read_bytes())
        self.assertEqual(safety.load(directory / "receipt.json")["status"], "pinning")

    def test_source_lock_and_foreign_private_edit_are_preserved(self):
        result = self.checked()
        state = Path(result["worktree"]).parents[1]
        with safety.locked(state / ".lock"):
            with self.assertRaisesRegex(safety.Error, "another dotfiles operation"):
                workflow.run(self.context, "prepare", {"family": "mise"})
        self.signing()
        save(self.private / "bootstrap.lock.json", {**self.pin, "reason": "foreign operator edit"})
        with self.assertRaisesRegex(safety.Error, "changed since check"):
            workflow.run(self.context, "publish", {"yes": True})
        self.assertEqual(
            safety.load(self.private / "bootstrap.lock.json")["reason"], "foreign operator edit"
        )

    def test_immutable_cache_update_guard_remains_in_force(self):
        result = self.prepare()
        tree = Path(result["worktree"])
        (self.base / ".bootstrap-source.tar").write_bytes(b"fixture immutable marker")
        selected = tree / "apps/mise/mise.lock"
        bundle = self.directory / "forbidden-bundle.json"
        save(
            bundle,
            {
                "schema_version": 1,
                "files": [
                    {
                        "repository": "public",
                        "path": "apps/mise/mise.lock",
                        "source": selected.relative_to(self.directory).as_posix(),
                        "sha256": safety.digest(selected),
                    }
                ],
            },
        )
        with self.assertRaisesRegex(safety.Error, "immutable public manifests"):
            updates.stage_candidate(self.context, bundle)

    def test_foreign_staged_index_cannot_smuggle_publication_content(self):
        result = self.checked()
        self.signing()
        tree = Path(result["worktree"])
        target = tree / "native/engine/__main__.py"
        original = target.read_bytes()
        target.write_bytes(b"# foreign staged code must never publish\n")
        self.git(tree, "add", "native/engine/__main__.py")
        target.write_bytes(original)
        with self.assertRaisesRegex(safety.Error, "foreign staged content"):
            workflow.run(self.context, "publish", {"yes": True})
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.pin["revision"])
        self.assertEqual(safety.load(self.private / "bootstrap.lock.json"), self.pin)

    def test_remote_branch_advance_never_forces_or_selects_candidate(self):
        self.checked()
        self.signing()
        (self.base / "remote-change.txt").write_text("another public operator\n")
        self.git(self.base, "add", "remote-change.txt")
        self.git(
            self.base,
            "-c",
            "user.name=Other",
            "-c",
            "user.email=other@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--quiet",
            "-m",
            "concurrent public source",
        )
        self.git(self.base, "push", "--quiet", self.remote, "main")
        advanced = self.git(self.remote, "rev-parse", "main").strip()
        with self.assertRaisesRegex(safety.Error, "branch advanced"):
            workflow.run(self.context, "publish", {"yes": True})
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), advanced)
        self.assertEqual(safety.load(self.private / "bootstrap.lock.json"), self.pin)

    def test_graph_corruption_and_executable_candidate_modes_fail_before_check(self):
        result = self.prepare()
        tree = Path(result["worktree"])
        graph = tree / "apps/mise/.mise/locks/npm-tool/3.0.0/aube-lock.yaml"
        original = graph.read_bytes()
        graph.write_bytes(b"foreign native graph\n")
        with self.assertRaises(safety.Error):
            workflow.run(self.context, "check")
        graph.write_bytes(original)
        (tree / "apps/mise/mise.lock").chmod(0o755)
        with self.assertRaisesRegex(safety.Error, "nonexecutable"):
            workflow.run(self.context, "check")

    def test_interrupted_signed_commit_receipt_recovers_without_pushing(self):
        result = self.checked()
        self.signing()
        original_atomic_json = workflow.atomic_json
        interrupted = False

        def interrupt_receipt(path, value):
            nonlocal interrupted
            if (
                Path(path).name == "receipt.json"
                and value.get("status") == "committed"
                and not interrupted
            ):
                interrupted = True
                raise safety.Error("fixture interruption after signed commit")
            return original_atomic_json(path, value)

        with patch.object(workflow, "atomic_json", side_effect=interrupt_receipt):
            with self.assertRaisesRegex(safety.Error, "after signed commit"):
                workflow.run(self.context, "publish", {"yes": True})
        self.assertEqual(
            workflow.run(self.context, "recover", {"yes": True})["status"], "committed"
        )
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.pin["revision"])
        self.assertEqual(safety.load(self.private / "bootstrap.lock.json"), self.pin)
        self.assertEqual(workflow.run(self.context, "publish", {"yes": True})["status"], "complete")
        self.assertNotEqual(
            self.git(Path(result["worktree"]), "rev-parse", "HEAD").strip(), self.pin["revision"]
        )

    def test_desktop_prepare_returns_one_complete_validated_source_family(self):
        current = json.loads((PUBLIC / workflow.DESKTOP).read_text())
        save(self.base / workflow.DESKTOP, current)
        save(
            self.base / "native/manifests/desktop.json",
            {
                "schema_version": 1,
                "artifacts": {
                    "noble-desktop": {
                        "source_manifest": workflow.DESKTOP,
                        "qt_standard_support_until": current["runtime"][
                            "qt_standard_support_until"
                        ],
                    }
                },
            },
        )
        self.git(self.base, "add", ".")
        self.git(
            self.base,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--quiet",
            "-m",
            "fixture desktop family",
        )
        self.git(self.base, "push", "--quiet", self.remote, "main")
        revision = self.git(self.base, "rev-parse", "HEAD").strip()
        self.pin.update(
            revision=revision,
            sha256=hashlib.sha256(
                self.git(self.base, "archive", "--format=tar", revision, binary=True)
            ).hexdigest(),
        )
        save(self.private / "bootstrap.lock.json", self.pin)
        desktop, producer = workflow._desktop_modules(PUBLIC)
        acquired = copy.deepcopy(current)
        with (
            patch.object(workflow, "_desktop_modules", return_value=(desktop, producer)),
            patch.object(
                producer,
                "acquire",
                return_value=(acquired, {"source": "fixture official publications"}),
            ) as acquire,
        ):
            result = workflow.run(
                self.context,
                "prepare",
                {"family": "desktop", "qt_version": current["runtime"]["qt_version"]},
            )
        candidate = Path(result["desktop_candidate"])
        self.assertEqual(candidate, Path(result["worktree"]) / workflow.DESKTOP)
        self.assertEqual(json.loads(candidate.read_text()), current)
        self.assertEqual(acquire.call_args.kwargs["qt_version"], current["runtime"]["qt_version"])
        self.assertEqual(desktop.validate_lock(json.loads(candidate.read_text())), current)
        self.assertEqual(safety.load(self.private / "bootstrap.lock.json"), self.pin)
        self.assertFalse(result["live_runtime_changed"])

    def test_private_application_graph_change_invalidates_checked_publication(self):
        save(self.private / "apps/sample/package.json", {"dependencies": {"fixture": "1.0.0"}})
        save(self.private / "apps/sample/source-lock.json", {"sha256": "1" * 64})
        lock = self.private / "apps/sample/pnpm-lock.yaml"
        lock.write_text("lockfileVersion: '9.0'\n")
        self.checked()
        lock.write_text("lockfileVersion: '9.0'\n# concurrent private graph edit\n")
        with self.assertRaisesRegex(safety.Error, "changed since check"):
            workflow.run(self.context, "publish", {"yes": True})
        self.assertEqual(safety.load(self.private / "bootstrap.lock.json"), self.pin)
        self.assertEqual(self.git(self.remote, "rev-parse", "main").strip(), self.pin["revision"])


if __name__ == "__main__":
    unittest.main()
