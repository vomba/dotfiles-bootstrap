"""Explicit native installation/retirement; root-owned receipts, no service owner.

install(context, lock, receipt_path), check(context, lock, package_id), and
retire(context, package_id) operate only on recorded paths/packages. Runtime
selection is an atomic current symlink switch after installation/linkage checks;
previous versioned packages remain available until explicit owned retirement.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import tempfile
import uuid

from desktop_build import (
    atomic_json,
    digest,
    environment,
    inspect_elf,
    lock_digest,
    noble,
    payload_manifest,
    run,
)

STATE = Path("/var/lib/dotfiles/desktop")
CACHE = Path("/var/cache/dotfiles/desktop")
RUNTIME = Path("/opt/dotfiles/dux")
LAUNCHERS = {
    "dms": "/opt/dotfiles/dux/current/bin/dms",
    "quickshell": "/opt/dotfiles/dux/current/bin/quickshell",
    "qs": "/opt/dotfiles/dux/current/bin/qs",
}


def public_root_directory(path):
    """Create only missing protected namespaces, independent of caller umask."""
    missing = []
    for entry in [path, *path.parents]:
        if entry.is_symlink():
            raise ValueError("Foreign symlink in native ownership namespace: " + str(entry))
        if not entry.exists():
            missing.append(entry)
            continue
        mode = entry.stat().st_mode
        if not entry.is_dir() or entry.stat().st_uid != 0 or mode & 0o022 or mode & 0o005 != 0o005:
            raise ValueError(
                "Native ownership namespace must be protected and publicly traversable: "
                + str(entry)
            )
    for entry in reversed(missing):
        entry.mkdir(mode=0o755)
        entry.chmod(0o755)


@contextmanager
def mutation(state=STATE):
    if os.geteuid() != 0:
        raise PermissionError("Desktop package mutation requires explicit privileged provision")
    public_root_directory(state)
    with (state / "operation.lock").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def record_path(package_id, state=STATE):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]+", package_id):
        raise ValueError("Invalid desktop package id")
    return state / (package_id + ".json")


def read_record(package_id, state=STATE):
    path = record_path(package_id, state)
    return json.loads(path.read_text()) if path.exists() else None


def verify_payload(record, root=Path("/")):
    for relative, expected in record["payload"].items():
        path = root / relative
        if "link" in expected:
            if not path.is_symlink() or os.readlink(path) != expected["link"]:
                raise ValueError("Owned link replaced: " + str(path))
        elif (
            not path.is_file()
            or path.is_symlink()
            or digest(path) != expected["sha256"]
            or path.stat().st_mode & 0o7777 != expected["mode"]
        ):
            raise ValueError("Owned package path replaced: " + str(path))


def installed_version(package):
    try:
        result = (
            run(["dpkg-query", "-W", "-f=${db:Status-Status}\t${Version}", package])
            .strip()
            .split("\t")
        )
    except Exception as error:
        import subprocess

        if isinstance(error, subprocess.CalledProcessError) and error.returncode == 1:
            return None
        raise
    return result[1] if len(result) == 2 and result[0] == "installed" else None


def owner(path):
    import subprocess

    try:
        result = run(["dpkg-query", "-S", str(path)]).strip()
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            return None
        raise
    return result.rsplit(": ", 1)[0].split(":", 1)[0]


def preflight(record, previous=None, root=Path("/")):
    if previous:
        verify_payload(previous, root)
    for relative in record["payload"]:
        path = root / relative
        if not path.exists() and not path.is_symlink():
            continue
        expected = previous and previous["payload"].get(relative)
        if expected is None:
            raise ValueError(
                f"Foreign package/path collision: {path}; identify and explicitly hand off its owner first"
            )
        if root == Path("/") and owner(path) != previous["package"]:
            raise ValueError(f"Native package owner changed: {path}")
    if root == Path("/") and installed_version(record["package"]) and not previous:
        raise ValueError("Existing package has no dotfiles ownership receipt: " + record["package"])


def policy_identity(path):
    """Identify the exact policy entry, without following administrator symlinks."""
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    identity = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": info.st_mode,
        "uid": info.st_uid,
        "gid": info.st_gid,
    }
    if path.is_symlink():
        identity["link"] = os.readlink(path)
    elif path.is_file():
        identity["sha256"] = digest(path)
    else:
        raise ValueError("Unsupported administrator service policy: " + str(path))
    return identity


def sync_policy_directory(policy):
    descriptor = os.open(policy.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def reconcile_service_policy(policy=Path("/usr/sbin/policy-rc.d")):
    """Restore a journalled inhibitor, refusing changed policy or backup entries.

    The journal precedes both renames, so recovery also handles a crash before
    replacement, between renames, or after restoration but before journal removal.
    Original entries are renamed, not copied: permissions and symlinks survive.
    """
    journal_path = policy.parent / ".dotfiles-policy-transaction.json"
    if not journal_path.exists() and not journal_path.is_symlink():
        return
    if journal_path.is_symlink() or not journal_path.is_file():
        raise ValueError("Foreign service policy transaction journal")
    journal = json.loads(journal_path.read_text())
    transaction = journal["transaction"]
    if not re.fullmatch(r"[0-9a-f]{32}", transaction) or journal["policy"] != str(policy):
        raise ValueError("Invalid service policy transaction")
    backup = policy.parent / (".dotfiles-policy-" + transaction + ".backup")
    staged = policy.parent / (".dotfiles-policy-" + transaction + ".inhibitor")
    original, inhibitor = journal["original"], journal["inhibitor"]
    current = policy_identity(policy)
    saved, prepared = policy_identity(backup), policy_identity(staged)
    # Validate every entry before making even a cleanup mutation. A matching
    # script alone is not ownership: recorded inode/metadata must match as well.
    if saved is not None and (original is None or saved != original):
        raise ValueError("Foreign service policy backup replacement")
    if prepared is not None and prepared != inhibitor:
        raise ValueError("Foreign staged service inhibitor replacement")
    if current not in (None, original, inhibitor):
        raise ValueError("Foreign service policy replacement")
    if original is not None:
        if current == original:
            if saved is not None:
                raise ValueError("Ambiguous service policy backup")
        elif saved == original:
            os.replace(backup, policy)
        else:
            raise ValueError("Original service policy backup is missing")
    elif current == inhibitor:
        policy.unlink()
    if prepared is not None:
        staged.unlink()
    sync_policy_directory(policy)
    journal_path.unlink()
    sync_policy_directory(policy)


@contextmanager
def inhibit_services(policy=Path("/usr/sbin/policy-rc.d")):
    """Persist recovery evidence before temporarily blocking package service actions."""
    reconcile_service_policy(policy)
    original = policy_identity(policy)
    transaction = uuid.uuid4().hex
    backup = policy.parent / (".dotfiles-policy-" + transaction + ".backup")
    staged = policy.parent / (".dotfiles-policy-" + transaction + ".inhibitor")
    journal_path = policy.parent / ".dotfiles-policy-transaction.json"
    with staged.open("x") as stream:
        stream.write("#!/bin/sh\nexit 101\n")
        stream.flush()
        os.fchmod(stream.fileno(), 0o755)
        os.fsync(stream.fileno())
    atomic_json(
        journal_path,
        {
            "transaction": transaction,
            "policy": str(policy),
            "original": original,
            "inhibitor": policy_identity(staged),
        },
    )
    sync_policy_directory(policy)
    try:
        if original is not None:
            os.replace(policy, backup)
            sync_policy_directory(policy)
        os.replace(staged, policy)
        sync_policy_directory(policy)
        yield
    finally:
        reconcile_service_policy(policy)


def apt(argv):
    reconcile_service_policy()
    simulation = run(["apt-get", "--simulate", "--no-install-recommends", *argv])
    removed = {
        line.split()[1].split(":", 1)[0]
        for line in simulation.splitlines()
        if line.startswith("Remv ")
    }
    allowed_removals = set(argv[1:]) if argv and argv[0] == "remove" else set()
    if not removed.issubset(allowed_removals):
        raise ValueError(
            "Native APT transaction would remove foreign packages: "
            + ", ".join(sorted(removed - allowed_removals))
        )
    with inhibit_services():
        env = dict(environment(), DEBIAN_FRONTEND="noninteractive")
        run(
            [
                "apt-get",
                "-y",
                "--no-install-recommends",
                "-o",
                "Dpkg::Options::=--force-confold",
                *argv,
            ],
            env=env,
        )


def receipt(path, lock):
    record = json.loads(Path(path).read_text())
    if record["lock_sha256"] != lock_digest(lock) or record["deb_sha256"] != digest(record["deb"]):
        raise ValueError("Candidate receipt/lock/package hash mismatch")
    if record.get("source_lock") != lock:
        raise ValueError("Candidate does not retain its exact source provenance")
    package_id = record["id"]
    if package_id in ("niri", "xwayland-satellite"):
        expected_name = package_id
        expected_version = lock[package_id]["version"] + "-0noble1"
        from desktop_build import NIRI_ASSETS, SATELLITE_ASSETS

        assets = NIRI_ASSETS if package_id == "niri" else SATELLITE_ASSETS
        if set(record["payload"]) != {target for _, target, _ in assets}:
            raise ValueError("Native package payload exceeds its upstream resource scope")
        if any(
            record["payload"][target].get("mode") != mode or "link" in record["payload"][target]
            for _, target, mode in assets
        ):
            raise ValueError("Native upstream resources must retain their regular-file modes")
    elif package_id == "dux-runtime":
        expected_name = "dotfiles-dux-" + lock_digest(lock)[:12]
        expected_version = lock["runtime"]["version"] + "-0noble1"
        runtime_id = (
            "dms-"
            + lock["runtime"]["version"]
            + "-qt-"
            + lock["runtime"]["qt_version"]
            + "-"
            + lock_digest(lock)[:12]
        )
        prefix = "opt/dotfiles/dux/" + runtime_id + "/"
        if not record["payload"] or any(
            not p.startswith(prefix) or ".." in Path(p).parts for p in record["payload"]
        ):
            raise ValueError("Runtime package escapes its versioned prefix")
        for relative, data in record["payload"].items():
            if "link" in data:
                target = data["link"]
                resolved = posixpath.normpath(
                    target
                    if target.startswith("/")
                    else posixpath.join("/", posixpath.dirname(relative), target)
                )
                if not resolved.startswith("/" + prefix):
                    raise ValueError("Runtime package contains an escaping symlink")
    else:
        raise ValueError("Unknown native package receipt")
    if record["package"] != expected_name or record["version"] != expected_version:
        raise ValueError("Candidate native identity is not the selected source version")
    if (
        run(["dpkg-deb", "-f", record["deb"], "Package"]).strip() != record["package"]
        or run(["dpkg-deb", "-f", record["deb"], "Version"]).strip() != record["version"]
    ):
        raise ValueError("Candidate Debian identity mismatch")
    # Derive payload from actual archive, not caller-provided receipt assertions.
    with tempfile.TemporaryDirectory(prefix="dotfiles-deb-check-") as directory:
        run(["dpkg-deb", "-x", record["deb"], directory])
        control = Path(directory) / ".control"
        run(["dpkg-deb", "-e", record["deb"], control])
        if any(p.name not in ("control", "md5sums") for p in control.iterdir()):
            raise ValueError(
                "Source-built desktop package must not contain maintainer scripts/triggers"
            )
        headers = {
            line.split(":", 1)[0]
            for line in (control / "control").read_text().splitlines()
            if line and not line[0].isspace()
        }
        if headers != {
            "Package",
            "Version",
            "Architecture",
            "Maintainer",
            "Depends",
            "Description",
        }:
            raise ValueError("Unexpected native package control fields")
        if run(["dpkg-deb", "-f", record["deb"], "Architecture"]).strip() != "amd64":
            raise ValueError("Native package architecture mismatch")
        shutil.rmtree(control)
        if payload_manifest(Path(directory)) != record["payload"]:
            raise ValueError("Candidate payload receipt mismatch")
    return record


def selection_preflight(previous):
    current = RUNTIME / "current"
    if previous and not current.is_symlink():
        raise ValueError("Owned runtime selection is missing")
    if current.exists() or current.is_symlink():
        if (
            not previous
            or not current.is_symlink()
            or os.readlink(current) != previous.get("selection")
        ):
            raise ValueError("Foreign runtime selection")
    for name, target in LAUNCHERS.items():
        path = Path("/usr/local/bin") / name
        if previous and not path.is_symlink():
            raise ValueError("Owned runtime launcher is missing: " + str(path))
        if path.exists() or path.is_symlink():
            if not previous or not path.is_symlink() or os.readlink(path) != target:
                raise ValueError("Foreign runtime launcher: " + str(path))


def recovery_selection_preflight(candidate, previous):
    """Recovery owns only the two journalled runtime selections and launchers."""
    selections = {
        record["selection"]
        for record in (previous, candidate)
        if record and record.get("selection")
    }
    names = {
        Path(path).parts[3] for path in candidate["payload"] if path.startswith("opt/dotfiles/dux/")
    }
    if len(names) != 1:
        raise ValueError("Candidate has multiple private runtime roots")
    selections.update(names)
    current = RUNTIME / "current"
    if current.exists() or current.is_symlink():
        if not current.is_symlink() or os.readlink(current) not in selections:
            raise ValueError("Foreign runtime selection during recovery")
    for name, target in LAUNCHERS.items():
        path = Path("/usr/local/bin") / name
        if path.exists() or path.is_symlink():
            if not path.is_symlink() or os.readlink(path) != target:
                raise ValueError("Foreign launcher during recovery: " + str(path))


def atomic_link(target, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ("." + path.name + ".dotfiles-" + str(os.getpid()))
    try:
        os.symlink(target, temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_installed_checks(record):
    # The first directory component below /opt/dotfiles/dux is the runtime ID.
    names = {Path(p).parts[3] for p in record["payload"] if p.startswith("opt/dotfiles/dux/")}
    if len(names) != 1:
        raise ValueError("Candidate has multiple private runtime roots")
    prefix = RUNTIME / names.pop()
    for binary in (prefix / "bin/quickshell", prefix / "bin/dms"):
        inspect_elf(binary, runtime=True)
    if (prefix / "bin/qt.conf").read_text() != "[Paths]\nPrefix=../qt\n":
        raise ValueError("Runtime qt.conf mismatch")
    return prefix


def install(context, lock, receipt_path):
    noble(context)
    record = receipt(receipt_path, lock)
    with mutation():
        if (STATE / "journal.json").exists() or (STATE / "ppa-pending.json").exists():
            raise ValueError("Recover the interrupted desktop transition before another install")
        previous = read_record(record["id"])
        # Dux versions have distinct native package names; previous payloads are retained.
        candidate_previous = (
            previous if previous and previous["package"] == record["package"] else None
        )
        retained_candidate = STATE / "retained" / (record["package"] + ".json")
        if candidate_previous is None and retained_candidate.is_file():
            candidate_previous = json.loads(retained_candidate.read_text())
        if (
            record["id"] == "dux-runtime"
            and candidate_previous
            and record["payload"] != candidate_previous["payload"]
        ):
            raise ValueError(
                "An immutable Dux prefix cannot change bytes in place; increment the reviewed runtime build_revision"
            )
        preflight(record, candidate_previous)
        if record["id"] == "dux-runtime":
            public_root_directory(RUNTIME)
            selection_preflight(previous)
        public_root_directory(CACHE)
        cached = CACHE / (record["deb_sha256"] + ".deb")
        if cached.is_symlink():
            raise ValueError("Native recovery artifact is a foreign symlink")
        if cached.exists() and digest(cached) != record["deb_sha256"]:
            raise ValueError("Corrupt recovery artifact")
        if not cached.exists():
            fd, temporary = tempfile.mkstemp(prefix=".candidate-", dir=CACHE)
            try:
                with os.fdopen(fd, "wb") as target, Path(record["deb"]).open("rb") as source:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                if digest(temporary) != record["deb_sha256"]:
                    raise ValueError("Candidate changed during privileged acquisition")
                Path(temporary).chmod(0o644)
                os.replace(temporary, cached)
            finally:
                Path(temporary).unlink(missing_ok=True)
        record["deb"] = str(cached)
        journal = {
            "operation": "provision",
            "candidate": record,
            "previous": previous,
            "phase": "prepared",
        }
        atomic_json(STATE / "journal.json", journal)
        try:
            apt(["install", "--reinstall", str(cached)])
            verify_payload(record)
            if installed_version(record["package"]) != record["version"]:
                raise ValueError("Installed Debian version differs from candidate")
            if record["id"] == "dux-runtime":
                prefix = runtime_installed_checks(record)
                record["selection"] = prefix.name
                for name, target in LAUNCHERS.items():
                    atomic_link(target, Path("/usr/local/bin") / name)
                atomic_link(prefix.name, RUNTIME / "current")
            # Receipt publication follows the complete verified install/selection.
            atomic_json(record_path(record["id"]), record)
            if previous and previous["package"] != record["package"]:
                atomic_json(STATE / "retained" / (previous["package"] + ".json"), previous)
            retained_candidate.unlink(missing_ok=True)
            (STATE / "journal.json").unlink()
        except BaseException:
            journal["phase"] = "failed"
            atomic_json(STATE / "journal.json", journal)
            # Do not touch mutable application data or restart the running session.
            if (
                previous
                and previous["package"] == record["package"]
                and Path(previous["deb"]).is_file()
                and digest(previous["deb"]) == previous["deb_sha256"]
            ):
                apt(["install", "--reinstall", "--allow-downgrades", previous["deb"]])
                verify_payload(previous)
            if previous and previous.get("selection"):
                atomic_link(previous["selection"], RUNTIME / "current")
            elif record["id"] == "dux-runtime":
                current = RUNTIME / "current"
                if current.is_symlink() and os.readlink(current) == record.get("selection"):
                    current.unlink()
                for name, target in LAUNCHERS.items():
                    path = Path("/usr/local/bin") / name
                    if path.is_symlink() and os.readlink(path) == target:
                        path.unlink()
            raise
    return record


def check(context, lock, package_id):
    noble(context)
    if package_id == "hyprland-ppa":
        return check_ppa(lock)
    record = read_record(package_id)
    if not record or record["lock_sha256"] != lock_digest(lock):
        raise ValueError("Missing or stale desktop ownership receipt")
    verify_payload(record)
    if installed_version(record["package"]) != record["version"]:
        raise ValueError("Native installed version mismatch")
    if package_id == "dux-runtime":
        runtime_installed_checks(record)
        selection_preflight(record)
    else:
        inspect_elf(Path("/usr/bin") / package_id)
        if package_id == "niri":
            with tempfile.TemporaryDirectory(prefix="dotfiles-niri-check-") as home:
                run(
                    [
                        "/usr/bin/niri",
                        "validate",
                        "--config",
                        "/usr/share/doc/niri/default-config.kdl",
                    ],
                    env=dict(environment(), HOME=home),
                )
    return record


def retire(context, package_id):
    noble(context)
    with mutation():
        if (STATE / "journal.json").exists() or (STATE / "ppa-pending.json").exists():
            raise ValueError("Recover the interrupted desktop transition before retirement")
        if package_id == "hyprland-ppa":
            return retire_ppa()
        record = read_record(package_id)
        if not record:
            raise ValueError("Cannot retire unrecorded desktop package")
        verify_payload(record)
        if installed_version(record["package"]) != record["version"]:
            raise ValueError("Refusing retirement of replaced package")
        records = [record]
        if package_id == "dux-runtime":
            selection_preflight(record)
            retained = STATE / "retained"
            if retained.exists():
                records += [
                    json.loads(p.read_text()) for p in sorted(retained.glob("dotfiles-dux-*.json"))
                ]
            for old in records:
                verify_payload(old)
        atomic_json(
            STATE / "journal.json",
            {
                "operation": "retire",
                "candidate": record,
                "previous": record,
                "retained": records[1:],
                "phase": "prepared",
            },
        )
        if package_id == "dux-runtime":
            for name in LAUNCHERS:
                (Path("/usr/local/bin") / name).unlink()
            (RUNTIME / "current").unlink()
        apt(["remove", *[r["package"] for r in records]])
        record_path(package_id).unlink()
        for old in records[1:]:
            (STATE / "retained" / (old["package"] + ".json")).unlink()
        (STATE / "journal.json").unlink()
    return {"retired": [r["package"] for r in records], "application_state": "preserved"}


PPA_FILES = {
    "key": Path("/etc/apt/keyrings/dotfiles-cppiber-hyprland.gpg"),
    "source": Path("/etc/apt/sources.list.d/dotfiles-cppiber-hyprland.sources"),
    "pin": Path("/etc/apt/preferences.d/dotfiles-cppiber-hyprland"),
}


def require_ppa_versions(lock):
    """Refuse same-version source collisions, wrong suites and provider drift."""
    ppa = lock["ppa"]
    for name in ppa["install"]:
        selected = ppa["scope"][name]
        policy = run(["apt-cache", "policy", name])
        candidate = re.search(r"^\s*Candidate:\s*(\S+)", policy, re.M)
        if not candidate or candidate.group(1) != selected:
            raise ValueError("Exact Noble PPA candidate unavailable: " + name + "=" + selected)
        in_version = False
        sources = []
        for line in policy.splitlines():
            version = re.match(r"^\s*(?:\*\*\*\s+)?(\S+)\s+-?\d+\s*$", line)
            if version:
                in_version = version.group(1) == selected
                continue
            source = re.match(r"^\s+-?\d+\s+(\S+)(?:\s+(.*))?$", line)
            if in_version and source and source.group(1) != "/var/lib/dpkg/status":
                sources.append((source.group(1).rstrip("/"), source.group(2) or ""))
        if not sources or any(
            url != ppa["url"] or not metadata.startswith("noble/main ") for url, metadata in sources
        ):
            raise ValueError("Unreviewed source or suite for exact PPA version: " + name)


def ppa_configuration(lock):
    ppa = lock["ppa"]
    source = f"Types: deb\nURIs: {ppa['url']}\nSuites: noble\nComponents: main\nArchitectures: amd64\nSigned-By: {PPA_FILES['key']}\n"
    pins = f"Package: *\nPin: release o={ppa['origin']}\nPin-Priority: -1\n\n"
    # An origin allowlist alone permits unexpected new versions; pin exact versions.
    for name in sorted(ppa["install"]):
        version = ppa["scope"][name]
        pins += f"Package: {name}\nPin: version {version}\nPin-Priority: 700\n\n"
    return source, pins


def installed_package_payload(names):
    payload = {}
    for name in names:
        for value in run(["dpkg-query", "-L", name]).splitlines():
            path = Path(value)
            if not path.is_absolute():
                raise ValueError("Unexpected dpkg file-list path")
            if path.is_symlink():
                payload[str(path.relative_to("/"))] = {"link": os.readlink(path)}
            elif path.is_file():
                payload[str(path.relative_to("/"))] = {
                    "sha256": digest(path),
                    "mode": path.stat().st_mode & 0o777,
                }
    return payload


def hypridle_global_links():
    return list(Path("/etc/systemd/user").glob("*.wants/hypridle.service"))


def suppress_new_hypridle_enablement():
    for path in hypridle_global_links():
        if (
            not path.is_symlink()
            or path.resolve() != Path("/usr/lib/systemd/user/hypridle.service").resolve()
        ):
            raise ValueError("Unexpected global Hypridle enablement: " + str(path))
        path.unlink()


def write_apt_file(path, content):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def provision_ppa(context, lock, *, resume=False):
    import urllib.request

    noble(context)
    with mutation():
        if (STATE / "journal.json").exists():
            raise ValueError(
                "Recover the interrupted source-package transition before PPA provisioning"
            )
        previous = read_record("hyprland-ppa")
        pending_path = STATE / "ppa-pending.json"
        pending = json.loads(pending_path.read_text()) if pending_path.exists() else None
        if pending and not resume:
            raise ValueError("Interrupted PPA transaction; use recover --id hyprland-ppa")
        if resume and (not pending or pending["lock_sha256"] != lock_digest(lock)):
            raise ValueError("PPA recovery requires its exact recorded source lock")
        expected_files = pending["files"] if pending else previous["files"] if previous else {}
        for path in PPA_FILES.values():
            if path.exists() or path.is_symlink():
                allowed = {expected_files.get(str(path))}
                if pending:
                    allowed.add(pending.get("previous_files", {}).get(str(path)))
                if path.is_symlink() or digest(path) not in allowed:
                    raise ValueError("Foreign APT source/key/pin collision: " + str(path))
        if previous and not pending:
            verify_payload(previous)
        installed_versions = {}
        for name in lock["ppa"]["install"]:
            installed = installed_version(name)
            installed_versions[name] = installed
            if pending:
                allowed_versions = {
                    pending["versions"].get(name),
                    pending.get("previous_versions", previous["versions"] if previous else {}).get(
                        name
                    ),
                }
            else:
                allowed_versions = {None, previous["versions"].get(name) if previous else None}
            if installed not in allowed_versions:
                raise ValueError(
                    "Existing PPA-scope package requires explicit ownership handoff: " + name
                )
        for binary in ("hyprlock", "hypridle"):
            path = Path("/usr/bin") / binary
            if path.exists() and (not (previous or pending) or owner(path) != binary):
                raise ValueError("Existing /usr/bin/" + binary + " owner requires explicit handoff")
        if hypridle_global_links():
            raise ValueError(
                "Foreign globally enabled hypridle.service must be explicitly handed off before provisioning"
            )
        with tempfile.TemporaryDirectory(prefix="dotfiles-ppa-") as directory:
            directory = Path(directory)
            key = directory / "key.asc"
            with urllib.request.urlopen(lock["ppa"]["key_url"], timeout=60) as response:
                key.write_bytes(response.read())
            keyhome = directory / "gnupg"
            keyhome.mkdir(mode=0o700)
            info = run(
                ["gpg", "--batch", "--homedir", keyhome, "--show-keys", "--with-colons", key]
            )
            fingerprints = [
                line.split(":")[9] for line in info.splitlines() if line.startswith("fpr:")
            ]
            if (
                not fingerprints
                or fingerprints[0] != lock["ppa"]["fingerprint"]
                or sum(line.startswith("pub:") for line in info.splitlines()) != 1
            ):
                raise ValueError("PPA primary signing key fingerprint mismatch")
            binary_key = directory / "key.gpg"
            run(["gpg", "--batch", "--homedir", keyhome, "--dearmor", "--output", binary_key, key])
            source, pins = ppa_configuration(lock)
            for path in PPA_FILES.values():
                path.parent.mkdir(parents=True, exist_ok=True)
            candidate_files = {
                str(PPA_FILES["key"]): digest(binary_key),
                str(PPA_FILES["source"]): hashlib.sha256(source.encode()).hexdigest(),
                str(PPA_FILES["pin"]): hashlib.sha256(pins.encode()).hexdigest(),
            }
            previous_files = (
                pending.get("previous_files", {})
                if pending
                else {str(p): digest(p) for p in PPA_FILES.values() if p.exists()}
            )
            pending = {
                "lock": lock,
                "lock_sha256": lock_digest(lock),
                "files": candidate_files,
                "previous_files": previous_files,
                "versions": {n: lock["ppa"]["scope"][n] for n in lock["ppa"]["install"]},
                "previous_versions": (
                    pending.get("previous_versions", previous["versions"] if previous else {})
                    if pending
                    else installed_versions
                ),
            }
            atomic_json(pending_path, pending)
            write_apt_file(PPA_FILES["key"], binary_key.read_bytes())
            write_apt_file(PPA_FILES["source"], source.encode())
            write_apt_file(PPA_FILES["pin"], pins.encode())
            # Signed Noble metadata and exact native dependency constraints are
            # authoritative. An unavailable version/ABI fails, not a suite fallback.
            run(["apt-get", "update"])
            require_ppa_versions(lock)
            specifications = [
                name + "=" + lock["ppa"]["scope"][name] for name in lock["ppa"]["install"]
            ]
            run(["apt-get", "--simulate", "--no-install-recommends", "install", *specifications])
            try:
                apt(["install", *(["--reinstall"] if resume else []), *specifications])
            finally:
                # dh_installsystemduser enables globally, but the dotfiles user
                # service controller is the sole activation owner.
                suppress_new_hypridle_enablement()
        versions = {name: installed_version(name) for name in lock["ppa"]["install"]}
        for name, version in versions.items():
            if version != lock["ppa"]["scope"][name]:
                raise ValueError("PPA installed source version mismatch: " + name)
        for binary in ("hyprlock", "hypridle"):
            inspect_elf(Path("/usr/bin") / binary)
            if lock["ppa"]["scope"][binary].split("-", 1)[0] not in run(
                [Path("/usr/bin") / binary, "--version"]
            ):
                raise ValueError("PPA executable upstream version mismatch: " + binary)
        record = {
            "id": "hyprland-ppa",
            "lock_sha256": lock_digest(lock),
            "source_lock": lock,
            "versions": versions,
            "files": {str(path): digest(path) for path in PPA_FILES.values()},
            "binaries": {
                str(Path("/usr/bin") / name): digest(Path("/usr/bin") / name)
                for name in ("hyprlock", "hypridle")
            },
            "payload": installed_package_payload(versions),
        }
        atomic_json(record_path("hyprland-ppa"), record)
        pending_path.unlink()
        return record


def check_ppa(lock):
    record = read_record("hyprland-ppa")
    if not record or record["lock_sha256"] != lock_digest(lock):
        raise ValueError("Missing/stale PPA ownership")
    verify_payload(record)
    if hypridle_global_links():
        raise ValueError(
            "Foreign global hypridle.service enablement conflicts with the explicit service owner"
        )
    for path, expected in record["files"].items():
        if Path(path).is_symlink() or not Path(path).is_file() or digest(path) != expected:
            raise ValueError("PPA configuration changed: " + path)
    for name, version in record["versions"].items():
        if installed_version(name) != version:
            raise ValueError("PPA package version changed: " + name)
    for path, expected in record["binaries"].items():
        if not Path(path).is_file() or Path(path).is_symlink() or digest(path) != expected:
            raise ValueError("PPA executable replaced: " + path)
    return record


def retire_ppa():
    record = read_record("hyprland-ppa")
    if not record:
        raise ValueError("PPA is not owned")
    verify_payload(record)
    if (STATE / "ppa-pending.json").exists():
        raise ValueError("Recover the pending PPA transaction before retirement")
    for name, version in record["versions"].items():
        if installed_version(name) != version:
            raise ValueError("PPA package was replaced: " + name)
    for path, expected in record["binaries"].items():
        if digest(path) != expected:
            raise ValueError("PPA executable replaced: " + path)
    for path, expected in record["files"].items():
        if digest(path) != expected:
            raise ValueError("PPA file replaced")
    # Shared ABI libraries may now have other consumers. Never auto-remove them.
    atomic_json(
        STATE / "ppa-pending.json",
        {
            "lock": record["source_lock"],
            "lock_sha256": record["lock_sha256"],
            "files": record["files"],
            "previous_files": record["files"],
            "versions": record["versions"],
            "previous_versions": record["versions"],
        },
    )
    apt(["remove", "hyprlock", "hypridle"])
    for path in record["files"]:
        Path(path).unlink()
    record_path("hyprland-ppa").unlink()
    (STATE / "ppa-pending.json").unlink()
    return {"retired": ["hyprlock", "hypridle"], "shared_libraries": "retained; no autoremove"}
