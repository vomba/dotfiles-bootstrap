"""Production freshness resolvers exercised with isolated public-metadata fixtures.

No provider commands, downloads, credentials or real HTTP requests are allowed.
Versions are deliberately synthetic release transitions, not current pin copies.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PUBLIC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PUBLIC / "native/engine"))
import manifest
import updates

CHROMIUM = "https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Linux&num=1"
SNAP = "https://api.snapcraft.io/v2/snaps/info/chromium"


class Response(io.BytesIO):
    def __init__(self, url, payload):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.url = url


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dotfiles-freshness-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        gui = json.loads((PUBLIC / "native/manifests/gui-linux.json").read_text())
        self.package = copy.deepcopy(next(p for p in gui["packages"] if p["id"] == "gui-chromium"))
        self.package["version"] = "20.0.0.0"
        self.package["_origin"] = str(PUBLIC)
        self.context = {"packages": [self.package], "platform": "linux-x64", "artifacts": {}}
        self.observed = {
            "check": {
                "id": self.package["id"],
                "status": "verified",
                "artifacts": [],
                "executables": {},
                "details": {"installed_version": "20.0.0.0"},
            }
        }
        self.metadata = {}
        self.calls = []
        transport = patch.object(updates.urllib.request, "urlopen", side_effect=self.urlopen)
        transport.start()
        self.addCleanup(transport.stop)
        installed = patch.object(
            updates, "package_status", side_effect=lambda *_: copy.deepcopy(self.observed)
        )
        installed.start()
        self.addCleanup(installed.stop)
        commands = patch.object(
            updates,
            "run",
            side_effect=AssertionError("freshness must not invoke a provider for a recipe"),
        )
        commands.start()
        self.addCleanup(commands.stop)
        self.releases("20.0.0.0", "20.0.0.0")

    def urlopen(self, request, timeout):
        self.assertEqual(timeout, 30)
        self.calls.append(request.full_url)
        self.assertIn(request.full_url, self.metadata, "unexpected metadata endpoint")
        payload = self.metadata[request.full_url]
        if isinstance(payload, Exception):
            raise payload
        return Response(request.full_url, payload)

    def releases(self, project, provider):
        self.metadata[CHROMIUM] = [{"version": project, "channel": "Stable", "platform": "Linux"}]
        self.metadata[SNAP] = {
            "channel-map": [
                {
                    "channel": {
                        "track": "latest",
                        "risk": "stable",
                        "name": "stable",
                        "architecture": "amd64",
                    },
                    "version": provider,
                    "revision": 77,
                    "download": {
                        "url": "https://example.invalid/chromium.snap",
                        "sha3-384": "a" * 96,
                    },
                }
            ]
        }

    def row(self, refresh=True):
        return updates.report(self.context, self.root / "context.json", refresh=refresh)[
            "packages"
        ][0]

    def statuses(self, row):
        return tuple(
            row["freshness"][name]["status"]
            for name in ("selected_vs_upstream", "provider_vs_upstream", "installed_vs_provider")
        )

    def test_declared_project_and_provider_are_independent(self):
        self.assertEqual(self.package["update"], {"provider": "chromiumdash", "url": CHROMIUM})
        self.assertEqual(
            self.package["provider_update"],
            {"provider": "snap", "name": "chromium", "channel": "latest/stable"},
        )
        self.releases("21.0.0.0", "20.0.0.0")
        row = self.row()
        self.assertEqual(self.statuses(row), ("behind", "behind", "current"))
        self.assertEqual(row["upstream"]["version"], "21.0.0.0")
        self.assertEqual(row["provider_latest"]["version"], "20.0.0.0")
        self.assertIn("unsigned store metadata only", row["provider_latest"]["integrity"])
        self.assertIn("snapd assertion/provenance", row["provider_latest"]["integrity"])
        self.assertEqual(set(self.calls), {CHROMIUM, SNAP})
        self.assertEqual(row["provider_latest"]["revision"], 77)

    def test_installed_provider_and_project_gaps_are_distinct(self):
        self.releases("22.0.0.0", "21.0.0.0")
        self.assertEqual(self.statuses(self.row()), ("behind", "behind", "behind"))
        self.observed["check"]["details"]["installed_version"] = "21.0.0.0"
        row = self.row()
        self.assertEqual(self.statuses(row), ("behind", "behind", "current"))
        self.assertEqual(row["freshness"]["installed_vs_provider"]["version"], "21.0.0.0")
        self.assertEqual(row["selected"], "20.0.0.0")

    def test_project_metadata_lag_and_equality_do_not_relabel_provider(self):
        self.releases("19.0.0.0", "20.0.0.0")
        self.assertEqual(self.statuses(self.row()), ("ahead", "ahead", "current"))
        self.releases("20.0.0.0", "20.0.0.0")
        self.assertEqual(self.statuses(self.row()), ("current", "current", "current"))

    def test_project_policy_remains_visible_beside_real_gap(self):
        self.package.update(policy="project", reason="Reviewed project compatibility constraint")
        self.releases("21.0.0.0", "21.0.0.0")
        row = self.row()
        self.assertEqual(row["policy"], "project")
        self.assertEqual(row["reason"], "Reviewed project compatibility constraint")
        self.assertEqual(self.statuses(row), ("behind", "current", "behind"))

    def test_backward_fields_are_preserved_without_treating_desired_as_latest(self):
        row = self.row()
        self.assertTrue(
            {
                "id",
                "provider",
                "selected",
                "policy",
                "reason",
                "installed",
                "provider_candidate",
                "upstream",
            }
            <= row.keys()
        )
        self.assertEqual(row["installed"], self.observed)
        self.assertEqual(
            row["provider_candidate"], {"version": "20.0.0.0", "source": "committed manifest"}
        )
        del self.package["provider_update"]
        row = self.row()
        self.assertEqual(row["provider_latest"]["status"], "not-declared")
        self.assertEqual(self.statuses(row), ("current", "unknown", "unknown"))

    def test_no_refresh_makes_no_network_freshness_claim(self):
        row = self.row(refresh=False)
        self.assertEqual(self.calls, [])
        self.assertEqual(row["upstream"]["status"], "not-refreshed")
        self.assertEqual(row["provider_latest"]["status"], "not-refreshed")
        self.assertEqual(self.statuses(row), ("unknown", "unknown", "unknown"))

    def test_network_failures_do_not_hide_the_other_release_source(self):
        for endpoint in (CHROMIUM, SNAP):
            with self.subTest(endpoint=endpoint):
                self.releases("21.0.0.0", "21.0.0.0")
                self.metadata[endpoint] = OSError("fixture metadata offline")
                row = self.row()
                failed = "upstream" if endpoint == CHROMIUM else "provider_latest"
                other = "provider_latest" if endpoint == CHROMIUM else "upstream"
                self.assertEqual(row[failed]["status"], "unavailable")
                self.assertIn("offline", row[failed]["error"])
                self.assertEqual(row[other]["version"], "21.0.0.0")
                expected = (
                    ("unknown", "unknown", "behind")
                    if endpoint == CHROMIUM
                    else ("behind", "unknown", "unknown")
                )
                self.assertEqual(self.statuses(row), expected)

    def test_chromium_metadata_is_bounded_and_structurally_validated(self):
        malformed = [
            None,
            {},
            [],
            [None],
            [{"version": "20.0.0.0"}] * 2,
            [{"version": 20}],
            [{"version": "20.0"}],
            [{"version": "1" * 100 + ".0.0.0"}],
            [{"version": "20.0.0.0", "channel": "Beta"}],
            [{"version": "20.0.0.0", "platform": "Win"}],
            b"[",
            b" " * 65537,
        ]
        for payload in malformed:
            with self.subTest(payload=str(payload)[:80]):
                self.metadata[CHROMIUM] = payload
                row = self.row()
                self.assertEqual(row["upstream"]["status"], "unavailable")
                self.assertEqual(self.statuses(row)[:2], ("unknown", "unknown"))
                self.assertEqual(row["provider_latest"]["version"], "20.0.0.0")

    def test_chromium_endpoint_cannot_be_a_generic_http_template(self):
        for url in (
            "http://chromiumdash.appspot.com/fetch_releases",
            "https://example.invalid/{version}",
            CHROMIUM.replace("Stable", "Beta"),
            CHROMIUM + "&num=999",
        ):
            with self.subTest(url=url):
                self.package["update"]["url"] = url
                self.assertEqual(self.row()["upstream"]["status"], "unavailable")
                self.assertNotIn(url, self.calls)

    def test_malformed_snap_and_unknown_installed_are_never_current(self):
        for payload in (
            {},
            {"channel-map": None},
            {"channel-map": []},
            {"channel-map": [None]},
            {
                "channel-map": [
                    {"channel": {"risk": "stable", "architecture": "amd64"}, "version": 20}
                ]
            },
        ):
            with self.subTest(payload=payload):
                self.metadata[SNAP] = payload
                row = self.row()
                self.assertEqual(row["provider_latest"]["status"], "unavailable")
                self.assertEqual(self.statuses(row)[1:], ("unknown", "unknown"))
        self.releases("20.0.0.0", "20.0.0.0")
        for observed in (
            {"status": "unverified", "error": "readback failed"},
            {"check": {"status": "verified", "details": {}}},
            {"check": {"status": "verified", "details": {"installed_version": "not a release"}}},
        ):
            with self.subTest(observed=observed):
                self.observed = observed
                self.assertEqual(self.statuses(self.row())[2], "unknown")

    def test_source_manifest_metadata_reports_both_releases_and_source_failure(self):
        source = json.loads((PUBLIC / "native/gui/linux-sources.json").read_text())["gui-chromium"]
        source["version"] = "20.0.0.0"
        source_path = self.root / "sources.json"
        source_path.write_text(json.dumps({"gui-chromium": source}))
        artifact = {
            "_origin": str(self.root),
            "source_manifest": "sources.json",
            "key": "gui-chromium",
        }
        self.context["artifacts"] = {"chromium-source": artifact}
        self.releases("22.0.0.0", "21.0.0.0")
        row = updates.report(self.context, self.root / "context.json", refresh=True)["artifacts"][
            "chromium-source"
        ]
        self.assertEqual(row["selected"], "20.0.0.0")
        self.assertEqual(row["upstream"]["version"], "22.0.0.0")
        self.assertEqual(row["provider_latest"]["version"], "21.0.0.0")
        self.assertEqual(row["freshness"]["provider_vs_upstream"]["status"], "behind")
        artifact["version"] = "19.0.0.0"
        report = updates.report(self.context, self.root / "context.json", refresh=True)
        row = report["artifacts"]["chromium-source"]
        self.assertIn("disagrees", row["source_error"])
        self.assertEqual(row["upstream"]["status"], "unavailable")
        self.assertEqual(row["provider_latest"]["status"], "unavailable")
        self.assertEqual(report["packages"][0]["upstream"]["version"], "22.0.0.0")
        source_path.unlink()
        self.assertIn(
            "source_error",
            updates.report(self.context, self.root / "context.json")["artifacts"][
                "chromium-source"
            ],
        )

    def test_provider_update_reuses_typed_resolver_without_becoming_project_upstream(self):
        self.package["provider_update"] = {"provider": "npm", "package": "fixture-provider"}
        self.metadata["https://registry.npmjs.org/fixture-provider/latest"] = {
            "version": "23.0.0",
            "dist": {"tarball": "https://example.invalid/provider.tgz"},
        }
        row = self.row()
        self.assertEqual(row["provider_latest"]["version"], "23.0.0")
        self.assertEqual(row["upstream"]["version"], "20.0.0.0")
        self.assertEqual(row["freshness"]["provider_vs_upstream"]["status"], "ahead")
        self.package["provider_update"] = {
            "provider": "http-template",
            "url": "https://example.invalid/{version}",
        }
        self.assertEqual(self.row()["provider_latest"]["status"], "not-declared")

    def test_existing_capacities_and_amo_metadata_resolvers_are_unchanged(self):
        capacities = "https://example.invalid/latest-linux.yml"
        self.metadata[capacities] = b"version: 7.8.9\nfiles:\n  - url: capacities.deb\n"
        resolved = updates.upstream({"update": {"provider": "electron-builder", "url": capacities}})
        self.assertEqual(resolved["version"], "7.8.9")
        self.assertIn("metadata only", resolved["integrity"])
        amo = "https://addons.mozilla.org/api/v5/addons/addon/fixture-addon/"
        self.metadata[amo] = {
            "current_version": {
                "version": "4.5.6",
                "file": {"url": "https://example.invalid/addon.xpi", "hash": "sha256:" + "b" * 64},
            }
        }
        resolved = updates.upstream(
            {"update": {"provider": "firefox-addon", "slug": "fixture-addon"}}
        )
        self.assertEqual(
            resolved,
            {
                "version": "4.5.6",
                "url": "https://example.invalid/addon.xpi",
                "hash": "sha256:" + "b" * 64,
            },
        )

    def test_platform_composition_does_not_resolve_linux_chromium_on_macos(self):
        linux = manifest.compose(
            PUBLIC, PUBLIC, self.root / "home", "linux-x64", ["desktop.enable=true"]
        )
        macos = manifest.compose(
            PUBLIC, PUBLIC, self.root / "home", "macos-arm64", ["desktop.enable=true"]
        )
        linux_record = next(p for p in linux["packages"] if p["id"] == "gui-chromium")
        self.assertEqual(linux_record["update"]["provider"], "chromiumdash")
        self.assertFalse(any(p["id"] == "gui-chromium" for p in macos["packages"]))
        self.assertTrue(any(a.get("key") == "gui-chromium" for a in macos["artifacts"].values()))
        macos["packages"] = [p for p in macos["packages"] if p["id"] == "gui-chromium"]
        macos["artifacts"] = {
            name: a for name, a in macos["artifacts"].items() if a.get("key") == "gui-chromium"
        }
        result = updates.report(macos, self.root / "context.json", refresh=True)
        self.assertEqual(result["packages"], [])
        self.assertEqual(result["artifacts"], {})
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
