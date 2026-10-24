"""Disposable fixtures for production desktop recipes; no Qt/Rust builds or services."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "native/engine"
sys.path.insert(0, str(ENGINE))
manifest_spec = importlib.util.spec_from_file_location("desktop_manifest", ENGINE / "manifest.py")
MANIFEST = importlib.util.module_from_spec(manifest_spec)
manifest_spec.loader.exec_module(MANIFEST)
RECIPE = ROOT / "native/recipes/desktop"
sys.path.insert(0, str(RECIPE))
spec = importlib.util.spec_from_file_location("desktop_recipe", RECIPE / "desktop.py")
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)
builder = desktop.builder
lifecycle = desktop.lifecycle
appearance = desktop.appearance


class DesktopSourceContract(unittest.TestCase):
    def setUp(self):
        self.lock = desktop.load_lock()

    def test_source_inputs_are_complete_and_coherently_selected(self):
        self.assertEqual(self.lock["niri"]["commit"], "8ed0da44d974c32c6877d2f4630c314da0717ecb")
        self.assertEqual(self.lock["xwayland-satellite"]["startup_owner"], "niri-on-demand")
        self.assertEqual(
            self.lock["runtime"]["features"], list(dict.fromkeys(self.lock["runtime"]["features"]))
        )
        self.assertEqual(set(self.lock["runtime"]["features"]), builder.PRODUCTION_FEATURES)
        self.assertIn("protocols", self.lock["runtime"]["sources"])
        self.assertIn("libdwarf", self.lock["runtime"]["sources"])
        self.assertIn("cpptrace", self.lock["runtime"]["sources"])
        self.assertIn("zstd", self.lock["runtime"]["sources"])
        self.assertEqual(self.lock["runtime"]["protocols_version"], "1.45")
        self.assertFalse(self.lock["runtime"]["commercial_entitlement_assumed"])

    def test_rejects_partial_qt_module_update(self):
        candidate = copy.deepcopy(self.lock)
        candidate["runtime"]["qt_version"] = "6.12.0"
        candidate["runtime"]["deployment_exclusions"] = builder.sdk_exclusions("6.12.0")
        with self.assertRaisesRegex(ValueError, "coherently"):
            desktop.validate_lock(candidate)

    def test_rejects_disabled_production_feature(self):
        candidate = copy.deepcopy(self.lock)
        candidate["runtime"]["features"].remove("SERVICE_PAM")
        with self.assertRaisesRegex(ValueError, "private-ABI"):
            desktop.validate_lock(candidate)

    def test_rejects_missing_fetchcontent_lock(self):
        candidate = copy.deepcopy(self.lock)
        del candidate["runtime"]["sources"]["libdwarf"]
        with self.assertRaisesRegex(ValueError, "closure"):
            desktop.validate_lock(candidate)

    def test_rejects_mixed_ubuntu_suite_and_changed_ppa_key(self):
        for field, value in (("suite", "resolute"), ("fingerprint", "0" * 40)):
            candidate = copy.deepcopy(self.lock)
            candidate["ppa"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "trust/suite"):
                desktop.validate_lock(candidate)

    def test_rejects_unscoped_ppa_package(self):
        candidate = copy.deepcopy(self.lock)
        candidate["ppa"]["scope"]["libqt6core6t64"] = "6.11.3"
        with self.assertRaisesRegex(ValueError, "scope"):
            desktop.validate_lock(candidate)

    def test_runtime_identity_changes_for_private_abi_inputs(self):
        before = builder.lock_digest(self.lock)
        candidate = copy.deepcopy(self.lock)
        candidate["runtime"]["sources"]["cpptrace"]["sha256"] = "1" * 64
        self.assertNotEqual(before, builder.lock_digest(candidate))

    def test_declared_maintainer_is_required_and_not_synthesized(self):
        with self.assertRaisesRegex(ValueError, "Declare identity"):
            builder.maintainer({})
        self.assertEqual(
            builder.maintainer(
                {"identity": {"name": "Fixture Packager", "email": "fixture@example.test"}}
            ),
            "Fixture Packager <fixture@example.test>",
        )
        with self.assertRaises(ValueError):
            builder.maintainer(
                {"identity": {"package_maintainer": "Name <a@example.test>\nDepends: injected"}}
            )

    def test_ppa_uses_signed_by_and_deny_by_default(self):
        source, pins = lifecycle.ppa_configuration(self.lock)
        self.assertIn("Suites: noble\n", source)
        self.assertIn("Signed-By: /etc/apt/keyrings/dotfiles-cppiber-hyprland.gpg", source)
        self.assertIn("Package: *\nPin: release o=LP-PPA-cppiber-hyprland\nPin-Priority: -1", pins)
        for name in self.lock["ppa"]["install"]:
            version = self.lock["ppa"]["scope"][name]
            self.assertIn(f"Package: {name}\nPin: version {version}\nPin-Priority: 700", pins)
        self.assertNotIn("Package: libxkbcommon0\n", pins)
        self.assertNotIn("Package: wayland-protocols\n", pins)

    def test_production_build_environment_scrubs_project_loader_controls(self):
        with patch.dict(
            os.environ,
            {
                "LD_LIBRARY_PATH": "/foreign",
                "QT_PLUGIN_PATH": "/foreign",
                "CARGO_HOME": "/foreign",
                "RUSTFLAGS": "--cfg fake",
                "CMAKE_PREFIX_PATH": "/foreign",
            },
        ):
            environment = builder.environment()
        for name in (
            "LD_LIBRARY_PATH",
            "QT_PLUGIN_PATH",
            "CARGO_HOME",
            "RUSTFLAGS",
            "CMAKE_PREFIX_PATH",
        ):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")


class DesktopManifestContract(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="desktop-manifest-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / "home"
        self.home.mkdir()

    def test_default_profiles_compose_the_complete_desktop_dependency_graph(self):
        linux_recipes = {
            "niri",
            "xwayland-satellite",
            "dux-runtime",
            "hyprland-ppa",
            "nordic-theme",
            "nordzy-cursors",
        }
        for platform in MANIFEST.PLATFORMS:
            with self.subTest(platform=platform):
                context = MANIFEST.compose(ROOT, ROOT, self.home, platform)
                selected = {package["id"]: package for package in context["packages"]}
                self.assertEqual(selected["meslolgs-nf"]["provider"], "recipe")
                self.assertEqual(
                    selected["jetbrains-mono-nf"]["provider"],
                    "recipe" if platform == "linux-x64" else "cask",
                )
                if platform == "linux-x64":
                    self.assertTrue(linux_recipes <= selected.keys())
                else:
                    self.assertFalse(linux_recipes & selected.keys())
                predecessors = set()
                for package in context["packages"]:
                    self.assertTrue(
                        set(package.get("requires", [])) <= predecessors,
                        f"{platform}: unresolved or unordered dependency of {package['id']}",
                    )
                    predecessors.add(package["id"])

    def test_shared_host_runtimes_are_retained_but_desktop_tools_are_not(self):
        context = MANIFEST.compose(ROOT, ROOT, self.home, "linux-x64")
        selected = {package["id"]: package for package in context["packages"]}
        shared = {
            "desktop-runtime-pulse",
            "desktop-dbus",
            "desktop-portal",
            "desktop-pipewire",
            "desktop-wireplumber",
            "desktop-polkit",
            "desktop-keyring",
            "desktop-keyring-pam",
            "desktop-network",
            "desktop-bluetooth",
            "desktop-power",
            "desktop-cups",
            "desktop-mesa-dri",
            "desktop-mesa-egl",
            "desktop-mesa-vulkan",
            "desktop-firmware",
            "desktop-fontconfig",
            "desktop-gtk4",
        }
        for identifier in shared:
            with self.subTest(shared_runtime=identifier):
                self.assertTrue(selected[identifier].get("retain_shared_runtime"))
        for identifier in (
            "desktop-fuzzel",
            "desktop-grim",
            "desktop-slurp",
            "niri",
            "dux-runtime",
        ):
            with self.subTest(desktop_tool=identifier):
                self.assertFalse(selected[identifier].get("retain_shared_runtime", False))


class DesktopDisposableArtifacts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="desktop-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_download_hash_verification_and_atomic_cache(self):
        data = b"official fixture payload"
        record = {
            "url": "https://example.test/source.tar.xz",
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        with patch.object(builder.urllib.request, "urlopen", return_value=io.BytesIO(data)):
            acquired = builder.fetch(record, self.root / "cache")
        self.assertEqual(acquired.read_bytes(), data)
        with patch.object(
            builder.urllib.request, "urlopen", side_effect=AssertionError("cache must not download")
        ):
            self.assertEqual(builder.fetch(record, self.root / "cache"), acquired)
        acquired.write_bytes(b"foreign replacement")
        with self.assertRaisesRegex(ValueError, "Corrupt cached"):
            builder.fetch(record, self.root / "cache")

    def test_bad_hash_never_publishes_cache_entry(self):
        cache = self.root / "cache"
        record = {"url": "https://example.test/source", "sha256": "0" * 64}
        with patch.object(builder.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong")):
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                builder.fetch(record, cache)
        self.assertEqual(list(cache.iterdir()), [])

    def test_source_archive_rejects_escape(self):
        archive = self.root / "source.tar"
        with tarfile.open(archive, "w") as stream:
            entry = tarfile.TarInfo("../outside")
            entry.size = 6
            stream.addfile(entry, io.BytesIO(b"escape"))
        with self.assertRaises(tarfile.FilterError):
            builder.untar(archive, self.root / "unpacked")
        self.assertFalse((self.root / "outside").exists())

    def test_source_archive_rejects_escaping_symlink(self):
        archive = self.root / "source.tar"
        with tarfile.open(archive, "w") as stream:
            entry = tarfile.TarInfo("source/foreign")
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/etc/shadow"
            stream.addfile(entry)
        with self.assertRaises(tarfile.FilterError):
            builder.untar(archive, self.root / "unpacked")

    def test_native_payload_detects_modified_file_and_symlink(self):
        stage = self.root / "stage"
        binary = stage / "usr/bin/niri"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"fixture ELF stand-in used for filesystem ownership only")
        binary.chmod(0o755)
        link = stage / "usr/bin/niri-fixture"
        link.symlink_to("niri")
        record = {"payload": builder.payload_manifest(stage)}
        lifecycle.verify_payload(record, stage)
        binary.write_bytes(b"replacement")
        with self.assertRaisesRegex(ValueError, "replaced"):
            lifecycle.verify_payload(record, stage)

    def test_collision_preflight_never_adopts_foreign_file(self):
        root = self.root / "root"
        foreign = root / "usr/bin/niri"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("foreign package")
        record = {"payload": {"usr/bin/niri": {"sha256": "0" * 64, "mode": 0o755}}}
        with self.assertRaisesRegex(ValueError, "collision"):
            lifecycle.preflight(record, root=root)
        self.assertEqual(foreign.read_text(), "foreign package")

    def test_asset_copy_preserves_complete_upstream_session_contract(self):
        source = self.root / "source"
        stage = self.root / "stage"
        for relative, _, _ in builder.NIRI_ASSETS:
            origin = source / relative
            origin.parent.mkdir(parents=True, exist_ok=True)
            origin.write_text("upstream fixture " + relative)
        builder.copy_assets(source, stage, builder.NIRI_ASSETS)
        self.assertTrue((stage / "usr/bin/niri-session").stat().st_mode & 0o111)
        self.assertTrue((stage / "usr/lib/systemd/user/niri.service").is_file())
        self.assertTrue((stage / "usr/lib/systemd/user/niri-shutdown.target").is_file())
        self.assertTrue((stage / "usr/share/xdg-desktop-portal/niri-portals.conf").is_file())
        self.assertTrue((stage / "usr/share/doc/niri/copyright").is_file())
        satellite = [target for _, target, _ in builder.SATELLITE_ASSETS]
        self.assertFalse(any(target.startswith("usr/lib/systemd") for target in satellite))

    def test_atomic_selection_replaces_link_not_target(self):
        first = self.root / "first"
        first.mkdir()
        second = self.root / "second"
        second.mkdir()
        selection = self.root / "current"
        lifecycle.atomic_link("first", selection)
        lifecycle.atomic_link("second", selection)
        self.assertEqual(os.readlink(selection), "second")
        self.assertEqual(selection.resolve(), second)
        self.assertTrue(first.exists())
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()), ["current", "first", "second"]
        )

    def test_appearance_links_respect_native_font_locations(self):
        record = {"links": ["fonts/MesloLGS NF Regular.ttf"]}
        home = self.root / "home"
        linux = list(appearance.destinations({"home": str(home), "platform": "linux-x64"}, record))[
            0
        ][1]
        mac = list(appearance.destinations({"home": str(home), "platform": "macos-arm64"}, record))[
            0
        ][1]
        self.assertEqual(linux, home / ".local/share/fonts/MesloLGS NF Regular.ttf")
        self.assertEqual(mac, home / "Library/Fonts/MesloLGS NF Regular.ttf")

    def test_appearance_owned_retirement_refuses_replaced_link(self):
        home = self.root / "home"
        stage = self.root / "runtime"
        (stage / "themes/Nordic").mkdir(parents=True)
        (stage / "themes/Nordic/gtk.css").write_text("fixture theme")
        state = self.root / "state/desktop-appearance"
        state.mkdir(parents=True)
        record = {
            "id": "nordic-theme",
            "stage": str(stage),
            "lock_sha256": "0" * 64,
            "links": ["themes/Nordic"],
            "payload": builder.payload_manifest(stage),
        }
        builder.atomic_json(state / "nordic-theme.json", record)
        foreign = home / ".local/share/themes/Nordic"
        foreign.parent.mkdir(parents=True)
        foreign.mkdir()
        context = {
            "home": str(home),
            "platform": "linux-x64",
            "paths": {"state": str(self.root / "state")},
        }
        with self.assertRaisesRegex(ValueError, "Foreign appearance replacement"):
            appearance.reconcile(context, {}, "nordic-theme", "retire", self.root / "output")
        self.assertTrue(foreign.is_dir())
        self.assertTrue(stage.exists())

    def test_service_inhibition_restores_administrator_policy_on_failure(self):
        policy = self.root / "policy-rc.d"
        original = "#!/bin/sh\n# fixture administrator policy\nexit 0\n"
        policy.write_text(original)
        policy.chmod(0o640)
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with lifecycle.inhibit_services(policy):
                self.assertEqual(policy.read_text(), "#!/bin/sh\nexit 101\n")
                self.assertEqual(policy.stat().st_mode & 0o777, 0o755)
                raise RuntimeError("fixture failure")
        self.assertEqual(policy.read_text(), original)
        self.assertEqual(policy.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.root.glob(".dotfiles-policy-*")), [])

    def test_service_inhibition_preserves_foreign_symlink(self):
        original = self.root / "foreign-policy"
        original.write_text("do not alter")
        policy = self.root / "policy-rc.d"
        policy.symlink_to(original)
        original_link = os.readlink(policy)
        with lifecycle.inhibit_services(policy):
            self.assertFalse(policy.is_symlink())
        self.assertTrue(policy.is_symlink())
        self.assertEqual(os.readlink(policy), original_link)
        self.assertEqual(policy.resolve(), original)
        self.assertEqual(original.read_text(), "do not alter")

    def test_apt_refuses_removal_of_unowned_reverse_dependency(self):
        with patch.object(
            lifecycle, "run", return_value="Remv niri [26.04]\nRemv foreign-desktop [1.0]\n"
        ) as execute:
            with self.assertRaisesRegex(ValueError, "foreign-desktop"):
                lifecycle.apt(["remove", "niri"])
        self.assertEqual(execute.call_count, 1)
        self.assertIn("--simulate", execute.call_args.args[0])

    def test_payload_rejects_setuid_escalation(self):
        stage = self.root / "stage"
        stage.mkdir()
        binary = stage / "unexpected"
        binary.write_text("fixture")
        binary.chmod(0o4755)
        with self.assertRaisesRegex(ValueError, "Privileged mode"):
            builder.payload_manifest(stage)

    def test_build_result_reports_real_receipt_without_installed_claim(self):
        receipt = self.root / "receipt.json"
        receipt.write_text('{"fixture": true}')
        result = desktop.envelope({}, "niri", "build", {"receipt": str(receipt)})
        self.assertEqual(result["status"], "built")
        self.assertEqual(result["executables"], {})
        self.assertEqual(
            result["artifacts"], [{"path": str(receipt), "sha256": builder.digest(receipt)}]
        )

    def test_nordic_assembly_and_owned_retirement_cover_all_variants(self):
        lock = json.loads((RECIPE / "appearance.json").read_text())
        archives = {}
        for variant in lock["nordic-theme"]["variants"]:
            archive = self.root / (variant + ".tar")
            files = {
                "gtk-3.0/gtk.css": "gtk3",
                "gtk-4.0/gtk.css": "gtk4",
                "gtk-2.0/gtkrc": "must not install",
                "LICENSE": "fixture upstream license",
            }
            if variant == "Nordic":
                files.update(
                    {
                        "extras/wallpapers/nord.png": "wallpaper",
                        "kde/folders/Nordic-bluish/index.theme": "[Icon Theme]\nName=Nordic-bluish\n",
                    }
                )
            with tarfile.open(archive, "w") as stream:
                for name, value in files.items():
                    entry = tarfile.TarInfo("source/" + name)
                    entry.size = len(value.encode())
                    stream.addfile(entry, io.BytesIO(value.encode()))
            archives[lock["nordic-theme"]["variants"][variant]["url"]] = archive
        home = self.root / "home"
        context = {
            "home": str(home),
            "platform": "linux-x64",
            "paths": {"state": str(self.root / "state"), "runtime": str(self.root / "runtime")},
        }
        with patch.object(
            appearance, "fetch", side_effect=lambda record, _: archives[record["url"]]
        ):
            installed = appearance.reconcile(
                context, lock, "nordic-theme", "provision", self.root / "build"
            )
        self.assertEqual(len(list((Path(installed["stage"]) / "themes").iterdir())), 8)
        self.assertFalse(any(Path(installed["stage"]).rglob("gtk-2.0")))
        self.assertTrue((home / ".local/share/icons/Nordic-bluish/index.theme").is_file())
        self.assertTrue((home / ".local/share/wallpapers/Nordic/nord.png").is_file())
        appearance.reconcile(context, lock, "nordic-theme", "check", self.root / "build")
        appearance.reconcile(context, lock, "nordic-theme", "retire", self.root / "build")
        self.assertFalse((home / ".local/share/themes/Nordic").is_symlink())
        self.assertFalse(Path(installed["stage"]).exists())

    def test_sdk_deployment_selection_preserves_real_application_resources(self):
        lock = desktop.load_lock()
        runtime = lock["runtime"]
        qt = self.root / "qt"
        directory_exclusions = {
            "include/QtQuick3DSpatialAudio",
            "lib/cmake/Qt6Quick3DSpatialAudioPrivate",
            "qml/QtQuick3D/SpatialAudio",
        }
        for relative in runtime["deployment_exclusions"]:
            path = qt / relative
            if relative in directory_exclusions:
                path.mkdir(parents=True, exist_ok=True)
                (path / "LICENSE").write_text("actual excluded fixture notice")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("unused SDK fixture resource")
        retained = (
            "plugins/sqldrivers/libqsqlite.so",
            "plugins/multimedia/libffmpegmediaplugin.so",
            "qml/QtMultimedia/libquickmultimediaplugin.so",
        )
        for relative in retained:
            path = qt / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture application resource; ELF execution belongs to native gates")
        notices = self.root / "notices"
        builder.legal_files(qt, notices)
        builder.select_sdk_runtime(qt, runtime)
        self.assertTrue(all((qt / relative).is_file() for relative in retained))
        self.assertFalse(
            any((qt / relative).exists() for relative in runtime["deployment_exclusions"])
        )
        self.assertTrue((notices / "include/QtQuick3DSpatialAudio/LICENSE").is_file())
        self.assertEqual(set(runtime["features"]), builder.PRODUCTION_FEATURES)

    def test_sdk_selection_cannot_remove_needed_multimedia_plugin(self):
        runtime = copy.deepcopy(desktop.load_lock()["runtime"])
        runtime["deployment_exclusions"].append("plugins/multimedia/libffmpegmediaplugin.so")
        with self.assertRaisesRegex(ValueError, "reviewed application closure"):
            builder.select_sdk_runtime(self.root, runtime)

    def test_private_compression_rejects_source_version_before_compiling(self):
        source = self.root / "bzip2"
        source.mkdir()
        (source / "bzlib_private.h").write_text('#define BZ_VERSION "0.0.0, fixture"\n')
        with patch.object(
            builder, "run", side_effect=AssertionError("must not compile mismatched source")
        ):
            with self.assertRaisesRegex(ValueError, "source version mismatch"):
                builder.private_bzip2(
                    source,
                    self.root / "qt",
                    desktop.load_lock()["runtime"],
                    self.root,
                    builder.environment(),
                )

    def test_native_payload_permissions_ignore_restrictive_caller_umask(self):
        stage = self.root / "stage"
        binary = stage / "bin/tool"
        license_file = stage / "share/LICENSE"
        binary.parent.mkdir(parents=True)
        license_file.parent.mkdir(parents=True)
        binary.write_text("fixture executable")
        license_file.write_text("fixture notice")
        stage.chmod(0o700)
        binary.chmod(0o700)
        license_file.chmod(0o600)
        builder.normalize_debian_payload(stage)
        self.assertEqual(stage.stat().st_mode & 0o777, 0o755)
        self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
        self.assertEqual(license_file.stat().st_mode & 0o777, 0o644)

    def test_source_acquisition_verifies_qt_publisher_digest_atomically(self):
        cache = self.root / "cache"
        with patch.object(
            desktop.updates.urllib.request, "urlopen", return_value=io.BytesIO(b"fixture SDK bytes")
        ):
            with self.assertRaisesRegex(ValueError, "SHA1 mismatch"):
                desktop.updates.download(
                    "https://download.qt.io/fixture.7z", cache, expected_sha1="0" * 40
                )
        self.assertEqual(list(cache.iterdir()), [])

    def test_qt_acquisition_selects_one_family_not_unrequested_sdk_tools(self):
        version = "6.11.3"
        prefix = "qt.qt6.6113."
        core = "qtbase-X.7z, qtdeclarative-X.7z, qtwayland-X.7z, qtsvg-X.7z, qttranslations-X.7z, icu-X.7z, qttools-X.7z, qtdoc-X.7z"
        entries = [(prefix + "linux_gcc_64", core)]
        entries += [
            (prefix + "addons." + name + ".linux_gcc_64", name + "-X.7z")
            for name in ("qtshadertools", "qtmultimedia", "qtimageformats", "qt5compat")
        ]
        xml = (
            "<Updates>"
            + "".join(
                "<PackageUpdate><Name>"
                + name
                + "</Name><Version>6.11.3-0-fixture</Version><DownloadableArchives>"
                + archives
                + "</DownloadableArchives></PackageUpdate>"
                for name, archives in entries
            )
            + "</Updates>"
        )
        records = desktop.updates.qt_archive_records(xml, version)
        self.assertEqual(len(records), 10)
        self.assertNotIn("qttools", {record["module"] for record in records})
        self.assertTrue(all("/6.11.3-0-fixture" in record["url"] for record in records))
        inconsistent = xml.replace(
            "<Version>6.11.3-0-fixture</Version>", "<Version>6.11.3-1-fixture</Version>", 1
        )
        with self.assertRaisesRegex(ValueError, "coherent"):
            desktop.updates.qt_archive_records(inconsistent, version)

    def test_noble_ppa_candidate_cannot_come_from_another_suite(self):
        lock = desktop.load_lock()

        def policy(argv):
            name = argv[-1]
            version = lock["ppa"]["scope"][name]
            return f"{name}:\n  Installed: (none)\n  Candidate: {version}\n  Version table:\n     {version} 700\n       -1 {lock['ppa']['url']} noble/main amd64 Packages\n"

        with patch.object(lifecycle, "run", side_effect=policy):
            lifecycle.require_ppa_versions(lock)
        with patch.object(
            lifecycle,
            "run",
            side_effect=lambda argv: policy(argv).replace("noble/main", "resolute/main"),
        ):
            with self.assertRaisesRegex(ValueError, "Unreviewed source or suite"):
                lifecycle.require_ppa_versions(lock)

    def test_public_qt_support_never_assumes_five_year_lts_entitlement(self):
        import datetime

        class FixedDate(datetime.date):
            @classmethod
            def today(cls):
                return cls(2026, 10, 24)

        html = "<table><tr><th>Version</th><th>Latest</th><th>Standard support until</th></tr><tr><td>Qt 6.12</td><td>Qt 6.12.0</td><td>2031-09-30</td></tr><tr><td>Qt 6.8 LTS</td><td>Qt 6.8.8 (LTS, commercial only)</td><td>2029-10-08</td></tr></table>"
        with patch.object(desktop.updates.datetime, "date", FixedDate):
            support = desktop.updates.qt_support(html, "6.12.0")
            self.assertEqual(support["horizon"], "2027-09-30")
            self.assertEqual(support["vendor_until"], "2031-09-30")
            self.assertFalse(support["commercial_entitlement_assumed"])
            with self.assertRaisesRegex(ValueError, "commercial-only"):
                desktop.updates.qt_support(html, "6.8.8")

    def test_acquired_candidate_is_not_reported_as_built_or_installed(self):
        candidate = self.root / "sources.candidate.json"
        builder.atomic_json(candidate, desktop.load_lock())
        result = desktop.envelope({}, "niri", "acquire", {"source_candidate": str(candidate)})
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["executables"], {})
        self.assertEqual(
            result["artifacts"], [{"path": str(candidate), "sha256": builder.digest(candidate)}]
        )


if __name__ == "__main__":
    unittest.main()
