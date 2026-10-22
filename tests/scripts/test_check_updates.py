from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-updates.py"
SPEC = importlib.util.spec_from_file_location("check_updates", SCRIPT)
assert SPEC and SPEC.loader
CHECK_UPDATES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK_UPDATES)

STUB = '''import io
import json
import os
import pathlib
import subprocess
import urllib.request

original_run = subprocess.run

def urlopen(request, *args, **kwargs):
    url = request.full_url if hasattr(request, "full_url") else request
    with open(os.environ["UPDATES_REQUEST_LOG"], "a", encoding="utf-8") as log:
        log.write(url + "\\n")
    if os.environ.get("UPDATES_API_FAILURE_AFTER") and len(pathlib.Path(os.environ["UPDATES_REQUEST_LOG"]).read_text(encoding="utf-8").splitlines()) > int(os.environ["UPDATES_API_FAILURE_AFTER"]):
        raise OSError("synthetic API outage")
    repo = url.split("/repos/", 1)[1].split("/releases/", 1)[0]
    if os.environ.get("UPDATES_API_BODY"):
        return io.BytesIO(os.environ["UPDATES_API_BODY"].encode())
    version = json.loads(os.environ["UPDATES_RELEASES"])[repo]
    return io.BytesIO(json.dumps({"tag_name": "v" + version}).encode())

def run(command, *args, **kwargs):
    if isinstance(command, list) and command[:3] == ["nix", "store", "prefetch-file"]:
        url = command[-1]
        with open(os.environ["UPDATES_PREFETCH_LOG"], "a", encoding="utf-8") as log:
            log.write(url + "\\n")
        if os.environ.get("UPDATES_CHANGE_FILE") and not os.path.exists(os.environ["UPDATES_CHANGE_FILE"] + ".once"):
            pathlib.Path(os.environ["UPDATES_CHANGE_FILE"]).write_text("concurrent edit\\n", encoding="utf-8")
            pathlib.Path(os.environ["UPDATES_CHANGE_FILE"] + ".once").touch()
        if os.environ.get("UPDATES_FAIL_MATCH", "__never__") in url:
            failure = os.environ.get("UPDATES_FAIL_KIND")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 120)
            if failure == "nonzero":
                return subprocess.CompletedProcess(command, 1, "", "synthetic failure")
            if failure == "missing-hash":
                return subprocess.CompletedProcess(command, 0, "{}", "")
            if failure == "invalid-hash":
                return subprocess.CompletedProcess(command, 0, '{"hash":"sha256-invalid"}', "")
            return subprocess.CompletedProcess(command, 0, "not json", "")
        import base64
        import hashlib
        digest = base64.b64encode(hashlib.sha256(url.encode()).digest()).decode()
        return subprocess.CompletedProcess(command, 0, json.dumps({"hash": "sha256-" + digest}), "")
    return original_run(command, *args, **kwargs)

urllib.request.urlopen = urlopen
subprocess.run = run
'''


class GitHubReleaseLookupTests(unittest.TestCase):
    def test_raises_update_check_error_when_github_is_unavailable(self) -> None:
        with mock.patch.object(CHECK_UPDATES.urllib.request, "urlopen", side_effect=URLError("offline")):
            with self.assertRaises(CHECK_UPDATES.UpdateCheckError):
                CHECK_UPDATES.get_latest_github_release("owner", "repo")


class UpdaterCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        (self.root / "overlays").mkdir()
        (self.root / "stubs").mkdir()
        shutil.copy2(SCRIPT, self.root / "scripts" / SCRIPT.name)
        (self.root / "stubs" / "sitecustomize.py").write_text(STUB, encoding="utf-8")
        self.api_log = self.root / "api.log"
        self.prefetch_log = self.root / "prefetch.log"

    def add_overlay(self, name: str) -> Path:
        target = self.root / "overlays" / name
        shutil.copy2(ROOT / "overlays" / name, target)
        return target

    def invoke(self, *args: str, input_text: str | None = None, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if key != "GITHUB_TOKEN"}
        env.update({
            "PYTHONPATH": str(self.root / "stubs"),
            "UPDATES_REQUEST_LOG": str(self.api_log),
            "UPDATES_PREFETCH_LOG": str(self.prefetch_log),
            "UPDATES_RELEASES": json.dumps({
                "gtema/openstack": "0.13.9", "jkroepke/helm-secrets": "4.7.10",
            }),
        })
        env.update(overrides)
        return subprocess.run(
            [sys.executable, str(self.root / "scripts" / SCRIPT.name), *args],
            input=input_text, text=True, capture_output=True, env=env, timeout=15,
        )

    def urls(self) -> list[str]:
        return self.prefetch_log.read_text(encoding="utf-8").splitlines() if self.prefetch_log.exists() else []

    def test_check_and_dry_run_report_availability_without_fetching_or_writing(self) -> None:
        overlay = self.add_overlay("openstack-tui.nix")
        original = overlay.read_bytes()
        for args in [(), ("--dry-run",)]:
            result = self.invoke(*args)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("UPDATE AVAILABLE", result.stdout)
            self.assertEqual(overlay.read_bytes(), original)
        self.assertEqual(self.urls(), [])

    def test_declined_apply_and_invalid_combinations_are_read_only(self) -> None:
        overlay = self.add_overlay("openstack-tui.nix")
        original = overlay.read_bytes()
        self.assertEqual(self.invoke("--apply", input_text="n\n").returncode, 0)
        for args in [("--apply", "--dry-run"), ("--yes",), ("--apply-force-rev",), ("--apply", "--apply-force-rev", "--yes")]:
            result = self.invoke(*args)
            self.assertEqual(result.returncode, 2, (args, result.stdout, result.stderr))
        self.assertEqual(overlay.read_bytes(), original)
        self.assertEqual(self.urls(), [])

    def test_apply_pairs_eight_openstack_hashes_and_one_v_urls(self) -> None:
        overlay = self.add_overlay("openstack-tui.nix")
        overlay.chmod(0o640)
        result = self.invoke("--apply", "--yes")
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        urls = self.urls()
        self.assertEqual(len(urls), 8)
        self.assertTrue(all("/v0.13.9/" in url and "/vv" not in url for url in urls))
        self.assertEqual(len(set(urls)), 8)
        content = overlay.read_text(encoding="utf-8")
        self.assertIn('version = "0.13.9";', content)
        self.assertEqual(overlay.stat().st_mode & 0o777, 0o640)
        for url in urls:
            digest = base64.b64encode(hashlib.sha256(url.encode()).digest()).decode()
            self.assertIn("sha256-" + digest, content)
            kind = "cli" if "openstack_cli-" in url else "tui"
            system = url.rsplit(f"openstack_{kind}-", 1)[1].split(".tar.xz", 1)[0]
            source = content.split(f"  {kind}-src = fetchurl {{", 1)[1].split("  };", 1)[0]
            self.assertIn(f'"{system}" = "sha256-{digest}"', source)

    def test_apply_pairs_all_three_helm_secrets_artifacts(self) -> None:
        overlay = self.add_overlay("helm.nix")
        result = self.invoke("--apply", "--yes")
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        urls = self.urls()
        self.assertEqual(len(urls), 3)
        self.assertTrue(all("/v4.7.10/" in url and "/vv" not in url for url in urls))
        content = overlay.read_text(encoding="utf-8")
        self.assertEqual(content.count('version = "4.7.10";'), 3)
        for url in urls:
            artifact = url.rsplit("/", 1)[1].split("-4.7.10.tgz", 1)[0]
            plugin = "helm-secrets" if artifact == "secrets" else f"helm-{artifact}"
            source = content.split(f'pluginName = "{plugin}";', 1)[1].split("};", 1)[0]
            digest = base64.b64encode(hashlib.sha256(url.encode()).digest()).decode()
            self.assertIn(f'hash = "sha256-{digest}"', source)

    def test_up_to_date_helm_plugins_do_not_fetch_or_write(self) -> None:
        overlay = self.add_overlay("helm.nix")
        original = overlay.read_bytes()
        for args in [(), ("--dry-run",), ("--apply", "--yes")]:
            with self.subTest(args=args):
                result = self.invoke(
                    *args,
                    UPDATES_RELEASES=json.dumps({"jkroepke/helm-secrets": "4.7.9"}),
                )
                self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
                self.assertEqual(overlay.read_bytes(), original)
        self.assertEqual(self.urls(), [])
        self.assertCountEqual(
            self.api_log.read_text(encoding="utf-8").splitlines(),
            ["https://api.github.com/repos/jkroepke/helm-secrets/releases/latest"] * 3,
        )

    def test_any_prefetch_failure_preserves_entire_file(self) -> None:
        for kind in ("timeout", "nonzero", "malformed", "missing-hash", "invalid-hash"):
            with self.subTest(kind=kind):
                overlay = self.add_overlay("openstack-tui.nix")
                original = overlay.read_bytes()
                result = self.invoke("--apply", "--yes", UPDATES_FAIL_MATCH="openstack_tui-aarch64-apple", UPDATES_FAIL_KIND=kind)
                self.assertEqual(result.returncode, 2, (kind, result.stdout, result.stderr))
                self.assertEqual(overlay.read_bytes(), original)

    def test_third_plugin_failure_preserves_helm_file_and_permissions(self) -> None:
        overlay = self.add_overlay("helm.nix")
        overlay.chmod(0o640)
        original = overlay.read_bytes()
        result = self.invoke("--apply", "--yes", UPDATES_FAIL_MATCH="secrets-post-renderer-", UPDATES_FAIL_KIND="nonzero")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(overlay.read_bytes(), original)
        self.assertEqual(overlay.stat().st_mode & 0o777, 0o640)

    def test_swapped_cli_artifact_fails_without_publishing_wrong_binary(self) -> None:
        overlay = self.add_overlay("openstack-tui.nix")
        original = overlay.read_text(encoding="utf-8").replace(
            "openstack_cli-${system}.tar.xz", "openstack_tui-${system}.tar.xz", 1,
        )
        overlay.write_text(original, encoding="utf-8")
        result = self.invoke("--apply", "--yes")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(overlay.read_text(encoding="utf-8"), original)
        self.assertEqual(self.urls(), [])

    def test_swapped_plugin_artifact_fails_without_publishing_wrong_plugin(self) -> None:
        overlay = self.add_overlay("helm.nix")
        original = overlay.read_text(encoding="utf-8").replace(
            "secrets-${version}.tgz", "secrets-getter-${version}.tgz", 1,
        )
        overlay.write_text(original, encoding="utf-8")
        result = self.invoke("--apply", "--yes")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(overlay.read_text(encoding="utf-8"), original)
        self.assertEqual(self.urls(), [])

    def test_unknown_version_bound_source_prevents_partial_artifact_update(self) -> None:
        for name, insertion in (
            ("openstack-tui.nix", '  extra-src = fetchurl { url = "https://github.com/gtema/openstack/releases/download/v${version}/extra.tar.xz"; hash = "sha256-EsUuRXuDsNVMTFkTWDVk6gx0EU/+O0W3vPJQt7cvKUQ="; };\n'),
            ("helm.nix", '  extra-src = super.fetchurl {\n    url = "https://github.com/jkroepke/helm-secrets/releases/download/v${version}/extra-${version}.tgz";\n    hash = "sha256-EsUuRXuDsNVMTFkTWDVk6gx0EU/+O0W3vPJQt7cvKUQ=";\n  };\n'),
        ):
            with self.subTest(name=name):
                overlay = self.add_overlay(name)
                original = overlay.read_text(encoding="utf-8").replace("let\n", "let\n" + insertion, 1)
                overlay.write_text(original, encoding="utf-8")
                result = self.invoke("--apply", "--yes")
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(overlay.read_text(encoding="utf-8"), original)
                self.assertEqual(self.urls(), [])

    def test_malformed_release_document_returns_error_status_not_traceback(self) -> None:
        self.add_overlay("openstack-tui.nix")
        for body in ("[]", "null", '{"tag_name": 42}'):
            with self.subTest(body=body):
                result = self.invoke("--dry-run", UPDATES_API_BODY=body)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn("Error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_missing_platform_or_hash_blocks_update(self) -> None:
        overlay = self.add_overlay("openstack-tui.nix")
        for old, new in [('"aarch64-apple-darwin" =', '"unknown-darwin" ='), ('sha256-wnk8bcjgIB0Re9xvsRxvAopoayn8F9Uv0uBELRPnY78=', '')]:
            with self.subTest(old=old):
                original = (ROOT / "overlays" / "openstack-tui.nix").read_text(encoding="utf-8").replace(old, new)
                overlay.write_text(original, encoding="utf-8")
                result = self.invoke("--apply", "--yes")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(overlay.read_text(encoding="utf-8"), original)

    def test_concurrent_edit_is_not_overwritten(self) -> None:
        overlay = self.add_overlay("openstack-tui.nix")
        result = self.invoke("--apply", "--yes", UPDATES_CHANGE_FILE=str(overlay))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(overlay.read_text(encoding="utf-8"), "concurrent edit\n")

    def test_nordic_variants_are_individually_named_and_pinned(self) -> None:
        overlay = self.add_overlay("nordic.nix")
        original = overlay.read_bytes()
        result = self.invoke("--apply", "--yes")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        for name in ("Nordic", "Nordic-standard-buttons", "Nordic-darker", "Nordic-darker-standard-buttons", "Nordic-bluish-accent", "Nordic-bluish-accent-standard-buttons", "Nordic-Polar", "Nordic-Polar-standard-buttons"):
            self.assertIn(name + ": pinned", result.stdout)
        self.assertEqual(overlay.read_bytes(), original)
        self.assertFalse(self.api_log.exists())

    def test_missing_nordic_source_is_error_not_a_blind_head_update(self) -> None:
        overlay = self.add_overlay("nordic.nix")
        original = overlay.read_text(encoding="utf-8").replace('name = "Nordic-Polar-standard-buttons";', 'name = "Unknown-Polar-standard-buttons";')
        overlay.write_text(original, encoding="utf-8")
        result = self.invoke("--apply", "--yes")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(overlay.read_text(encoding="utf-8"), original)
        self.assertFalse(self.api_log.exists())

    def test_unsupported_source_shape_errors_without_modifying_other_records(self) -> None:
        overlay = self.add_overlay("helm.nix")
        original = overlay.read_text(encoding="utf-8")
        plugin_hash = next(CHECK_UPDATES.PLUGIN.finditer(original))["hash"]
        original = original.replace(f'hash = "{plugin_hash}";', f'sha256 = "{plugin_hash}";')
        overlay.write_text(original, encoding="utf-8")
        result = self.invoke("--apply", "--yes")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(overlay.read_text(encoding="utf-8"), original)

    def test_later_failure_reports_earlier_applied_file_as_partial(self) -> None:
        helm = self.add_overlay("helm.nix")
        openstack = self.add_overlay("openstack-tui.nix")
        original = openstack.read_bytes()
        result = self.invoke(
            "--apply", "--yes", UPDATES_FAIL_MATCH="openstack_tui-aarch64-apple",
            UPDATES_FAIL_KIND="nonzero",
        )
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn('version = "4.7.10";', helm.read_text(encoding="utf-8"))
        self.assertEqual(openstack.read_bytes(), original)
        self.assertIn("Partial update: already applied helm.nix", result.stderr)

    def test_later_api_failure_keeps_prior_partial_result_and_errors(self) -> None:
        helm = self.add_overlay("helm.nix")
        openstack = self.add_overlay("openstack-tui.nix")
        originals = {overlay: overlay.read_bytes() for overlay in (helm, openstack)}
        result = self.invoke("--dry-run", UPDATES_API_FAILURE_AFTER="1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("UPDATE AVAILABLE", result.stdout)
        self.assertIn("Error:", result.stderr)
        for overlay, original in originals.items():
            self.assertEqual(overlay.read_bytes(), original)
        self.assertEqual(self.urls(), [])

    def test_promoted_overlay_inventory_is_read_only_in_check_mode(self) -> None:
        originals = {}
        for source in (ROOT / "overlays").glob("*.nix"):
            overlay = self.add_overlay(source.name)
            originals[overlay] = overlay.read_bytes()
        result = self.invoke("--dry-run")
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        for overlay, original in originals.items():
            self.assertEqual(overlay.read_bytes(), original, overlay.name)
        self.assertEqual(self.urls(), [])
        self.assertCountEqual(
            self.api_log.read_text(encoding="utf-8").splitlines(),
            [
                f"https://api.github.com/repos/{repo}/releases/latest"
                for repo in {"jkroepke/helm-secrets", "gtema/openstack"}
            ],
        )
