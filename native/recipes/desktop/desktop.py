#!/usr/bin/env python3
"""Pinned Noble desktop recipes; no daemon, dependency solver or implicit activation.

CLI: ACTION --context CONTEXT [--output DIRECTORY] [--id ID]
ACTION: plan/build/provision/check/retire/status/inspect/acquire/candidate/recover.
IDs: niri, xwayland-satellite, dux-runtime, hyprland-ppa, nordic-theme,
meslolgs-nf, jetbrains-mono-nf, nordzy-cursors. --candidate FILE selects a complete
reviewed sources.json candidate;
`candidate` builds all three source packages before atomically publishing the
coherent candidate manifest/receipt index under --output. It never edits the
repository lock or installs anything. `inspect` performs explicit public upstream
queries; `status` is local only. `recover` restores the journal's previous native
package/current selection without service restarts. Root mutation is exclusively
provision/retire/recover; services have a separate caller-owned lifecycle.

Build prerequisites are reported by plan; install them through the stock Noble
APT provider, not hidden inside a source build. Runtime SDK artifacts are for the
operator's local use only; publication of source recipes does not authorize
redistribution of assembled Qt artifacts. Actual SDK notices/provenance survive
assembly, with no commercial entitlement or source-offer claim.

Update workflow: inspect reports upstream/provider/installed separately; acquire
refreshes actual stable release tags/commits/Cargo locks, the exact upstream
FetchContent closure and official SDK archive hashes into a NEW managed repository
candidate JSON. No hand-invented hash file is needed. Default acquisition advances
the qualified Qt minor's public patch release; --qt-version explicitly selects a
new minor and still rebuilds the whole private ABI family. --rust-version,
--wayland-version and --protocols-version are explicit build compatibility pins.
The Qt vendor LTS date is NOT treated as a free/commercial patch entitlement.
`candidate --candidate FILE` then builds the complete source family on Noble,
keeping the selected lock/runtime unchanged. Qualify it before source promotion
and explicit provision; never commit the local SDK/deb cache or build index.
"""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import re
import os
import sys
import subprocess
import urllib.request

# Script-local imports also work when this entrypoint is loaded by fixture tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import desktop_appearance as appearance
import desktop_build as builder
import desktop_lifecycle as lifecycle
import desktop_updates as updates

SOURCE_LOCK = Path(__file__).with_name("sources.json")
APPEARANCE_LOCK = Path(__file__).with_name("appearance.json")
APPEARANCE_IDS = ("nordic-theme", "meslolgs-nf", "jetbrains-mono-nf", "nordzy-cursors")
IDS = ("niri", "xwayland-satellite", "dux-runtime", "hyprland-ppa", *APPEARANCE_IDS)
RELEASES = {
    "niri": "niri-wm/niri",
    "xwayland-satellite": "Supreeeme/xwayland-satellite",
    "quickshell": "quickshell-mirror/quickshell",
    "dux": "duxlabs-dev/dux-shell",
    "hyprlock": "hyprwm/hyprlock",
    "hypridle": "hyprwm/hypridle",
}


def validate_lock(lock):
    if lock.get("schema_version") != 1 or (
        lock.get("platform"),
        lock.get("distribution"),
        lock.get("release"),
    ) != ("linux-x64", "ubuntu", "24.04"):
        raise ValueError("Only a complete Noble amd64 desktop lock is accepted")
    for package_id in ("niri", "xwayland-satellite"):
        record = lock[package_id]
        if (
            not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", record["version"])
            or record["tag"] != "v" + record["version"]
        ):
            raise ValueError("Stable source tag/version mismatch")
        if not re.fullmatch(r"[0-9a-f]{40}", record["commit"]) or not re.fullmatch(
            r"[0-9a-f]{64}", record["cargo_lock_sha256"]
        ):
            raise ValueError("Source commit and Cargo.lock SHA256 required")
    if (
        set(lock["niri"]["features"]) != {"dbus", "systemd", "xdp-gnome-screencast"}
        or lock["xwayland-satellite"]["features"] != ["systemd"]
        or lock["xwayland-satellite"]["startup_owner"] != "niri-on-demand"
    ):
        raise ValueError("Compositor feature/startup contract changed")
    runtime = lock["runtime"]
    if (
        set(runtime["features"]) != builder.PRODUCTION_FEATURES
        or runtime["private_abi"] is not True
        or runtime["rebuild_quickshell_every_qt_release"] is not True
        or runtime["commercial_entitlement_assumed"] is not False
    ):
        raise ValueError("Complete private-ABI/runtime support contract required")
    datetime.date.fromisoformat(runtime["qt_standard_support_until"])
    if set(runtime["sources"]) != {
        "quickshell",
        "wayland",
        "protocols",
        "cpptrace",
        "libdwarf",
        "zstd",
        "dux-source",
        "dms",
        "bzip2",
    }:
        raise ValueError("Complete source and vendored dependency closure required")
    if (
        set(r["module"] for r in runtime["qt_archives"])
        != {
            "qtbase",
            "qtdeclarative",
            "qtwayland",
            "qtsvg",
            "qttranslations",
            "icu",
            "qtshadertools",
            "qtmultimedia",
            "qtimageformats",
            "qt5compat",
        }
        or len(runtime["qt_archives"]) != 10
    ):
        raise ValueError("Incomplete or duplicate Qt SDK modules")
    if runtime.get("deployment_exclusions") != builder.sdk_exclusions(runtime["qt_version"]):
        raise ValueError("SDK deployment selection differs from the reviewed Dux/QS closure")
    if type(runtime.get("build_revision")) is not int or runtime["build_revision"] < 1:
        raise ValueError("A positive immutable runtime build_revision is required")
    for field in (
        "version",
        "qt_version",
        "wayland_version",
        "protocols_version",
        "quickshell_version",
        "bzip2_version",
    ):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", runtime[field]):
            raise ValueError("Stable runtime versions required: " + field)
    if (
        runtime.get("bzip2_soname") != "libbz2.so.1"
        or runtime["bzip2_version"] not in runtime["sources"]["bzip2"]["url"]
    ):
        raise ValueError("Private SDK compression ABI/source version mismatch")
    quickshell = runtime["sources"]["quickshell"]
    if not re.fullmatch(r"[0-9a-f]{40}", quickshell["commit"]) or not quickshell["url"].endswith(
        "/" + quickshell["commit"]
    ):
        raise ValueError("Quickshell exact source commit mismatch")
    if runtime.get("artifact_policy") != "local-operator-use-only" or set(
        runtime.get("licenses", {})
    ) != {"ICU-73-license", "LGPL-3.0", "GPL-3.0", "Qt-GPL-exception-1.0"}:
        raise ValueError("Local artifact policy and actual upstream license texts are required")
    records = [
        lock["rust"],
        *runtime["sources"].values(),
        *runtime["qt_archives"],
        *runtime["licenses"].values(),
    ]
    for record in records:
        if not record["url"].startswith("https://") or not re.fullmatch(
            r"[0-9a-f]{64}", record["sha256"]
        ):
            raise ValueError("Every build/artifact input needs HTTPS and exact SHA256")
    if not all(runtime["qt_version"] in record["url"] for record in runtime["qt_archives"]):
        raise ValueError("Qt candidate modules must update coherently")
    for name, version in (
        ("dms", runtime["version"]),
        ("dux-source", runtime["version"]),
        ("wayland", runtime["wayland_version"]),
        ("protocols", runtime["protocols_version"]),
    ):
        if version not in runtime["sources"][name]["url"]:
            raise ValueError("Runtime source/artifact version mismatch: " + name)
    ppa = lock["ppa"]
    if (
        ppa["suite"] != "noble"
        or ppa["url"] != "https://ppa.launchpadcontent.net/cppiber/hyprland/ubuntu"
        or ppa["fingerprint"] != "A54D23B62FF3FCC76EFF71E8FDBAAA1CF0CCF48E"
    ):
        raise ValueError("Unreviewed PPA trust/suite change")
    allowed = {
        "hyprlock",
        "hypridle",
        "libhyprgraphics4",
        "libhyprlang2",
        "libhyprutils13",
        "libsdbus-c++2",
        "libxkbcommon0",
        "wayland-protocols",
    }
    if (
        set(ppa["scope"]) != allowed
        or not set(ppa["install"]).issubset(allowed)
        or not {"hyprlock", "hypridle"}.issubset(ppa["install"])
    ):
        raise ValueError("PPA scope is not the reviewed desktop dependency boundary")
    if any(not re.fullmatch(r"[0-9][A-Za-z0-9.+:~_-]*", v) for v in ppa["scope"].values()):
        raise ValueError("Invalid exact PPA package version")
    return lock


def load_lock(path=SOURCE_LOCK):
    value = json.loads(Path(path).read_text())
    return validate_lock(value["sources"] if "receipts" in value else value)


def support(lock):
    horizon = datetime.date.fromisoformat(lock["runtime"]["qt_standard_support_until"])
    return {
        "qt_standard_support_until": str(horizon),
        "supported_today": datetime.date.today() <= horizon,
        "private_abi_rebuild": "Every Qt release requires a fresh Quickshell build and runtime qualification",
        "delivery": "self-maintained Noble; local artifacts only",
        "commercial_entitlement_assumed": False,
    }


def local_status(lock):
    selected = {
        "niri": lock["niri"]["version"],
        "xwayland-satellite": lock["xwayland-satellite"]["version"],
        "dux-runtime": lock["runtime"]["version"],
    }
    rows = []
    for package_id, version in selected.items():
        record = lifecycle.read_record(package_id)
        installed_package = (
            record["package"] if record else package_id if package_id != "dux-runtime" else None
        )
        rows.append(
            {
                "id": package_id,
                "selected": version,
                "provider": "Noble source recipe"
                if package_id != "dux-runtime"
                else "official Dux/Qt SDK plus isolated source assembly",
                "installed": lifecycle.installed_version(installed_package)
                if installed_package
                else None,
                "owned": record is not None,
                "upstream": "not queried; use inspect",
            }
        )
    rows.append(
        {
            "id": "hyprland-ppa",
            "selected": {n: lock["ppa"]["scope"][n] for n in lock["ppa"]["install"]},
            "reviewed_not_adopted": {
                n: v for n, v in lock["ppa"]["scope"].items() if n not in lock["ppa"]["install"]
            },
            "provider": "cppiber/hyprland Noble signed PPA",
            "installed": {n: lifecycle.installed_version(n) for n in ("hyprlock", "hypridle")},
            "upstream": "not queried; use inspect",
        }
    )
    return {"packages": rows, "support": support(lock)}


def latest_release(repository):
    request = urllib.request.Request(
        "https://api.github.com/repos/" + repository + "/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "dotfiles-desktop-inspection",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            release = json.load(response)
        if release.get("draft") or release.get("prerelease"):
            raise ValueError("Latest endpoint did not return a stable release")
        return {
            "version": release["tag_name"].removeprefix("v"),
            "url": release["html_url"],
            "published_at": release.get("published_at"),
        }
    except Exception as error:
        return {"error": str(error), "version": None}


def latest_qt():
    base = "https://download.qt.io/official_releases/qt/"
    try:
        with urllib.request.urlopen(base, timeout=60) as response:
            series = re.findall(r'href="(6\.[0-9]+)/"', response.read().decode())
        latest = max(series, key=lambda value: tuple(map(int, value.split("."))))
        with urllib.request.urlopen(base + latest + "/", timeout=60) as response:
            releases = re.findall(r'href="(6\.[0-9]+\.[0-9]+)/"', response.read().decode())
        version = max(releases, key=lambda value: tuple(map(int, value.split("."))))
        return {
            "version": version,
            "url": base + latest + "/" + version + "/",
            "source": "official stable release inventory",
        }
    except Exception as error:
        return {"version": None, "error": str(error)}


def inspect(lock):
    result = local_status(lock)
    result["upstream"] = {name: latest_release(repository) for name, repository in RELEASES.items()}
    result["qt_upstream"] = {
        **latest_qt(),
        "release_policy": "https://doc.qt.io/qt-6/qt-releases.html",
        "selected": lock["runtime"]["qt_version"],
        "candidate_rule": "All SDK/plugin/ICU/Wayland/QS pins and private ABI qualification update together; no commercial entitlement inferred",
    }
    result["provider_candidates"] = {
        name: builder.run(["apt-cache", "policy", name])
        for name in ("hyprlock", "hypridle", "libhyprutils13")
    }
    return result


def recover(context, package_id):
    builder.noble(context)
    if package_id == "hyprland-ppa":
        pending = json.loads((lifecycle.STATE / "ppa-pending.json").read_text())
        lock = validate_lock(pending["lock"])
        return lifecycle.provision_ppa(context, lock, resume=True)
    with lifecycle.mutation():
        journal_path = lifecycle.STATE / "journal.json"
        if not journal_path.is_file():
            raise ValueError("No interrupted desktop transition")
        journal = json.loads(journal_path.read_text())
        candidate, previous = journal["candidate"], journal["previous"]
        if candidate["id"] != package_id:
            raise ValueError("Recovery package id does not match the interrupted transition")
        # Validate selection and every launcher before APT, policy reconciliation,
        # receipt publication, or cleanup can mutate an interrupted runtime.
        if package_id == "dux-runtime":
            lifecycle.recovery_selection_preflight(candidate, previous)
        if previous:
            restores = [previous, *journal.get("retained", [])]
            for restored in restores:
                if builder.digest(restored["deb"]) != restored["deb_sha256"]:
                    raise ValueError("Recovery artifact modified")
                for relative, expected in restored["payload"].items():
                    path = Path("/") / relative
                    if path.exists() or path.is_symlink():
                        try:
                            lifecycle.verify_payload({"payload": {relative: expected}})
                        except ValueError:
                            incoming = candidate["payload"].get(relative)
                            if incoming is None:
                                raise
                            lifecycle.verify_payload({"payload": {relative: incoming}})
            lifecycle.apt(
                ["install", "--reinstall", "--allow-downgrades", *[r["deb"] for r in restores]]
            )
            for restored in restores:
                lifecycle.verify_payload(restored)
            if previous.get("selection"):
                for name, target in lifecycle.LAUNCHERS.items():
                    launcher = Path("/usr/local/bin") / name
                    lifecycle.atomic_link(target, launcher)
                lifecycle.atomic_link(previous["selection"], lifecycle.RUNTIME / "current")
            builder.atomic_json(lifecycle.record_path(previous["id"]), previous)
        if not previous or previous["package"] != candidate["package"]:
            # A failed first install has no working selection to restore. Remove
            # only the recorded candidate, accepting missing partial-install leaves
            # but never foreign replacements or a different native package owner.
            for relative, expected in candidate["payload"].items():
                path = Path("/") / relative
                if path.exists() or path.is_symlink():
                    lifecycle.verify_payload({"payload": {relative: expected}})
                    if lifecycle.owner(path) != candidate["package"]:
                        raise ValueError("Foreign package owner in failed candidate: " + str(path))
            if package_id == "dux-runtime" and not previous:
                current = lifecycle.RUNTIME / "current"
                if current.exists() or current.is_symlink():
                    current.unlink()
                for name, target in lifecycle.LAUNCHERS.items():
                    path = Path("/usr/local/bin") / name
                    if path.exists() or path.is_symlink():
                        # All launcher ownership was checked before recovery mutation.
                        path.unlink()
            try:
                version = builder.run(
                    ["dpkg-query", "-W", "-f=${Version}", candidate["package"]]
                ).strip()
            except subprocess.CalledProcessError as error:
                if error.returncode != 1:
                    raise
                version = None
            if version is not None:
                if version != candidate["version"]:
                    raise ValueError("Failed candidate was replaced by another native version")
                lifecycle.apt(["remove", candidate["package"]])
            if not previous:
                lifecycle.record_path(package_id).unlink(missing_ok=True)
        journal_path.unlink()
        return {
            "recovered": package_id,
            "version": previous["version"] if previous else None,
            "retired": candidate["package"] if not previous else None,
            "services": "not restarted",
        }


def envelope(context, package_id, action, result):
    statuses = {
        "plan": "planned",
        "build": "built",
        "provision": "provisioned",
        "check": "verified",
        "retire": "retired",
        "candidate": "built",
        "acquire": "planned",
        "status": "inspected",
        "inspect": "inspected",
        "recover": "provisioned",
    }
    paths = []
    executables = {}
    if action == "acquire":
        paths.append(Path(result["source_candidate"]))
    elif action == "build":
        paths.append(Path(result["receipt"]))
        built = json.loads(Path(result["receipt"]).read_text())
        if "deb" in built:
            paths.append(Path(built["deb"]))
    elif action == "candidate":
        paths.append(Path(result["candidate"]))
        paths.extend(Path(p) for p in result["receipts"].values())
    elif action in ("provision", "check", "recover") and not result.get("retired"):
        if package_id in APPEARANCE_IDS:
            paths.append(
                Path(context["paths"]["state"]) / "desktop-appearance" / (package_id + ".json")
            )
        else:
            paths.append(lifecycle.record_path(package_id))
            commands = {
                "niri": {"niri": "/usr/bin/niri", "niri-session": "/usr/bin/niri-session"},
                "xwayland-satellite": {"xwayland-satellite": "/usr/bin/xwayland-satellite"},
                "dux-runtime": {
                    "dms": "/usr/local/bin/dms",
                    "quickshell": "/usr/local/bin/quickshell",
                    "qs": "/usr/local/bin/qs",
                },
                "hyprland-ppa": {"hyprlock": "/usr/bin/hyprlock", "hypridle": "/usr/bin/hypridle"},
            }
            executables = commands[package_id]
            paths.extend(Path(p).resolve(strict=True) for p in executables.values())
    artifacts = []
    for path in dict.fromkeys(paths):
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ValueError(
                "Recipe output artifact must be an absolute regular file: " + str(path)
            )
        artifacts.append({"path": str(path), "sha256": builder.digest(path)})
    return {
        "id": package_id,
        "status": "retired" if action == "recover" and result.get("retired") else statuses[action],
        "artifacts": artifacts,
        "executables": executables,
        "details": result,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "plan",
            "build",
            "provision",
            "check",
            "retire",
            "status",
            "inspect",
            "acquire",
            "candidate",
            "recover",
        ),
    )
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--id", choices=IDS, default="niri")
    parser.add_argument(
        "--candidate",
        type=Path,
        help="Complete reviewed desktop source lock; never a partial version override",
    )
    parser.add_argument(
        "--candidate-output",
        type=Path,
        help="Managed/versioned repository JSON destination; default is source-repository candidates/sources-<digest>.json",
    )
    parser.add_argument(
        "--qt-version",
        help="Explicit new Qt minor/patch SDK; default refreshes the qualified minor's latest public patch",
    )
    parser.add_argument("--rust-version", help="Official stable Rust build-toolchain version")
    parser.add_argument("--wayland-version", help="Explicit private Wayland source release")
    parser.add_argument(
        "--protocols-version", help="Explicit private wayland-protocols XML release"
    )
    args = parser.parse_args(argv)
    context = json.loads(args.context.read_text())
    lock = load_lock(args.candidate or SOURCE_LOCK)
    output = args.output or Path(context["paths"]["cache"]) / "desktop-builds"
    allowed_platforms = ("linux-x64", "macos-arm64") if args.id == "meslolgs-nf" else ("linux-x64",)
    if context.get("platform") not in allowed_platforms:
        raise ValueError("Selected desktop recipe is unsupported on this platform")
    if args.id in APPEARANCE_IDS:
        appearance_lock = json.loads(APPEARANCE_LOCK.read_text())
        if args.action == "plan":
            result = {
                "id": args.id,
                "source": appearance_lock[args.id],
                "actions": ["build", "provision", "check", "retire"],
                "services": "none",
            }
        elif args.action == "build":
            result = {"receipt": str(appearance.assemble(appearance_lock, args.id, output))}
        elif args.action in ("status", "inspect"):
            path = Path(context["paths"]["state"]) / "desktop-appearance" / (args.id + ".json")
            record = json.loads(path.read_text()) if path.exists() else None
            result = {
                "id": args.id,
                "selected": appearance_lock[args.id]["version"],
                "provider": "official pinned upstream appearance archive",
                "installed": record.get("version") if record else None,
                "owned": record is not None,
            }
            if args.action == "inspect":
                repositories = {
                    "nordic-theme": "EliverLara/Nordic",
                    "jetbrains-mono-nf": "ryanoasis/nerd-fonts",
                    "nordzy-cursors": "guillaumeboehm/Nordzy-cursors",
                }
                result["upstream"] = (
                    latest_release(repositories[args.id])
                    if args.id in repositories
                    else {
                        "policy": "project",
                        "reason": "MesloLGS NF is selected by reviewed immutable commit, not a release channel",
                    }
                )
        else:
            result = appearance.reconcile(context, appearance_lock, args.id, args.action, output)
    elif args.action == "plan":
        result = {
            "id": args.id,
            "platform": "Ubuntu 24.04 amd64",
            "sources": lock,
            "build_prerequisites": builder.BUILD_DEPENDENCIES,
            "maintainer": "identity.package_maintainer or identity.name/email",
            "root_actions": ["provision", "retire", "recover"],
            "services_owner": "explicit caller; no build/install/check service starts",
            "support": support(lock),
        }
    elif args.action == "status":
        result = local_status(lock)
    elif args.action == "inspect":
        result = inspect(lock)
    elif args.action == "acquire":
        candidate, details = updates.acquire(
            lock,
            Path(context["paths"]["cache"]) / "desktop-sources",
            qt_version=args.qt_version,
            rust_version=args.rust_version,
            wayland_version=args.wayland_version,
            protocols_version=args.protocols_version,
        )
        validate_lock(candidate)
        target = args.candidate_output or SOURCE_LOCK.parent / "candidates" / (
            "sources-" + builder.lock_digest(candidate)[:16] + ".json"
        )
        if target.is_symlink():
            raise ValueError("Candidate destination is a foreign symlink")
        target = target.resolve()
        if target == SOURCE_LOCK.resolve():
            raise ValueError(
                "Acquisition must not overwrite the selected source lock before qualification"
            )
        if target.exists():
            if json.loads(target.read_text()) != candidate:
                raise ValueError(
                    "Candidate destination has foreign/different data; choose a new managed repository candidate path"
                )
        else:
            builder.atomic_json(target, candidate)
        result = {
            "source_candidate": str(target),
            "source_lock_sha256": builder.lock_digest(candidate),
            "acquisition": details,
            "next": "Build/qualify using candidate --candidate this managed repository file; source promotion and package provision remain explicit",
        }
    elif args.action == "candidate":
        if not args.candidate:
            raise ValueError("candidate requires a complete --candidate source manifest")
        receipts = {
            name: str(builder.build(context, lock, name, output))
            for name in ("niri", "xwayland-satellite", "dux-runtime")
        }
        target = output / ("candidate-" + builder.lock_digest(lock)[:16] + ".json")
        builder.atomic_json(
            target,
            {
                "sources": lock,
                "receipts": receipts,
                "promotion": "Explicit provision only; runtime/UI/hardware qualification remains a separate required gate",
            },
        )
        result = {"candidate": str(target), "receipts": receipts}
    elif args.action == "build":
        result = {"receipt": str(builder.build(context, lock, args.id, output))}
    elif args.action == "provision":
        if args.id == "dux-runtime" and not support(lock)["supported_today"]:
            raise ValueError(
                "Qt baseline exceeded standard support; qualify a supported coherent candidate"
            )
        if args.id == "hyprland-ppa":
            result = lifecycle.provision_ppa(context, lock)
        else:
            receipt = (
                output.resolve() / (args.id + "-" + builder.lock_digest(lock)[:16]) / "receipt.json"
            )
            if not receipt.is_file():
                raise ValueError(
                    "Build the selected source package unprivileged before privileged provision"
                )
            result = lifecycle.install(context, lock, receipt)
    elif args.action == "check":
        result = lifecycle.check(context, lock, args.id)
    elif args.action == "retire":
        result = lifecycle.retire(context, args.id)
    else:
        result = recover(context, args.id)
    print(json.dumps(envelope(context, args.id, args.action, result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, PermissionError, OSError) as error:
        print("desktop recipe: " + str(error), file=sys.stderr)
        raise SystemExit(1)
    except subprocess.CalledProcessError as error:
        print("desktop recipe command failed: " + " ".join(map(str, error.cmd)), file=sys.stderr)
        print(error.stdout or "", file=sys.stderr)
        print(error.stderr or "", file=sys.stderr)
        raise SystemExit(error.returncode or 1)
