#!/usr/bin/env python3
"""Current Linux GUI delivery, with no authored private configuration or profiles.

ACTION --context JSON --id PACKAGE_ID [--output DIRECTORY], ACTION is
plan/build/provision/check/retire. Build acquires immutable publisher bytes into
an isolated verified stage; provision alone installs them. Kitty/Tabiew use
versioned user runtimes; Slack/Capacities use native Noble .debs. Slack's local
repack removes exactly the reviewed vendor cron script that otherwise adds an
unscoped APT repository. Capacities uses its official .deb, including its Noble
AppArmor integration, instead of an AppImage/NixGL wrapper. No sandbox bypasses.

Chromium uses Canonical's asserted stable-channel snap, with SHA256+SHA3 identity,
explicit local-revision refresh and a per-app automatic-refresh hold. snapd's
shared OS socket is an explicit repository service dependency, never activated
here. Only the owned Chromium snap is disabled on retirement: snap remove would
delete live data and give its automatic snapshot an expiry. No profile contents
are read, copied, inventoried or deleted by this recipe. The publisher's launcher
imports the old profile on first launch; original paths are preserved. Actual
Noble desktop/profile/keyring/sandbox qualification is a separate physical gate.

Required dependency packages must be selected AND installed. Trusted provider
paths are fixed native commands; only an explicitly marked disposable fixture
context can replace them. Receipts bind source, version, staged bytes and owned
outputs. Foreign collisions and changed owned outputs fail before mutation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "native/engine"))
from safety import (
    Error,
    atomic_bytes,
    atomic_json,
    command,
    digest,
    download,
    environment,
    extract,
    fingerprint,
    inventory,
    load,
    locked,
    native,
    no_symlink_parents,
    relative,
    run,
)

IDS = frozenset(("gui-kitty", "gui-tabiew", "gui-slack", "gui-capacities", "gui-chromium"))
TOOLS = {
    "dpkg-query": "/usr/bin/dpkg-query",
    "dpkg-deb": "/usr/bin/dpkg-deb",
    "apt-get": "/usr/bin/apt-get",
    "snap": "/usr/bin/snap",
    "unsquashfs": "/usr/bin/unsquashfs",
    "systemctl": "/usr/bin/systemctl",
    "ps": "/usr/bin/ps",
    "ldd": "/usr/bin/ldd",
}


def tool(ctx, name, mutating=False):
    return command(ctx, name, TOOLS[name], mutating=mutating)


def call(ctx, name, *args, mutating=False):
    return run([tool(ctx, name, mutating), *args], ctx)


def probe(ctx, name, *args, extra_env=None):
    env = environment(ctx)
    env.update(extra_env or {})
    result = subprocess.run(
        [tool(ctx, name), *map(str, args)],
        env=env,
        cwd=ctx["base"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def selected(ctx, package_id):
    if ctx["platform"] != "linux-x64" or package_id not in IDS:
        raise Error("unsupported Linux GUI selection")
    packages = ctx.get("packages", [])
    packages = list(packages.values()) if isinstance(packages, dict) else packages
    package = next((p for p in packages if p["id"] == package_id), None)
    if package is None:
        raise Error("GUI package is not enabled/selected: " + package_id)
    source = load(BASE / "native/gui/linux-sources.json")[package_id]
    # Context artifact records are resolved from their owning source, not a caller
    # supplied URL. Fixtures use isolated copied source trees, never overrides.
    if source["version"] != package.get("version"):
        raise Error("GUI package/source versions must update together")
    relative(source["version"])
    return package, source


def dependencies(ctx, package):
    records = ctx.get("packages", [])
    records = {p["id"]: p for p in records} if isinstance(records, list) else records
    for name in package.get("requires", []):
        dependency = records.get(name)
        if dependency is None:
            raise Error("missing selected dependency: " + name)
        if dependency["provider"] == "apt":
            code, text, _ = probe(
                ctx, "dpkg-query", "-W", "-f=${db:Status-Status}", dependency["name"]
            )
            if code or text.strip() != "installed":
                raise Error("missing installed dependency: " + name)
        else:
            for path in dependency.get("executables", {}).values():
                if Path(path).is_absolute():
                    native(path)


def locations(ctx, package, output=None, source=None):
    package_id, version = package["id"], package["version"]
    state = Path(ctx["paths"]["state"]) / "gui-linux"
    if source is None:
        source = load(BASE / "native/gui/linux-sources.json")[package_id]
    stage_identity = hashlib.sha256(
        (json.dumps(source, sort_keys=True) + digest(__file__)).encode()
    ).hexdigest()
    stage = (
        (Path(output) if output else Path(ctx["paths"]["cache"]) / "gui-linux")
        / package_id
        / version
        / stage_identity
    )
    runtime = Path(ctx["paths"]["runtime"]) / package_id / version
    for path in (state, runtime):
        no_symlink_parents(path, Path(ctx["home"]))
        if path.is_symlink():
            raise Error("symlink GUI runtime/state: " + str(path))
    if not stage.is_absolute() or stage.is_symlink():
        raise Error("stage must be an ordinary absolute directory")
    return state, stage, runtime


def receipt_path(state, package):
    return state / (package["id"] + ".json")


def state_record(ctx, path, value):
    atomic_json(path, value)
    if os.geteuid() == 0 and not ctx.get("_fixture_context"):
        owner = Path(ctx["home"]).stat()
        os.chown(path, owner.st_uid, owner.st_gid)
        os.chown(Path(path).parent, owner.st_uid, owner.st_gid)


def executable_paths(package, runtime):
    return {
        name: str(Path(path) if Path(path).is_absolute() else runtime / path)
        for name, path in package.get("executables", {}).items()
    }


def artifacts(paths):
    return [{"path": str(Path(p).absolute()), "sha256": digest(p)} for p in paths]


def result(package, action, files=(), executables=None, **details):
    return {
        "id": package["id"],
        "status": {
            "plan": "planned",
            "build": "built",
            "provision": "provisioned",
            "check": "verified",
            "retire": "retired",
        }[action],
        "artifacts": artifacts(files),
        "executables": executables or {},
        "details": details,
    }


def built_artifacts(package, source, stage):
    paths = [stage / "stage.json"]
    if source["format"] == "deb":
        paths.append(stage / "package.deb")
    elif source["format"] == "snap":
        paths.extend((stage / "chromium.snap", stage / "chromium.assert"))
    else:
        paths.extend(stage / "payload" / relative(path) for path in package["executables"].values())
    return paths


def installed_artifacts(path, record):
    if record["source"]["format"] == "snap":
        payload = [Path(p) for p in record["owned"] if p.endswith(".snap")]
    else:
        payload = [Path(p).resolve(strict=True) for p in record["executables"].values()]
    return [path, *payload]


def verify_tree(path, record, *, stage=False):
    if path.is_symlink() or not path.is_dir():
        raise Error("missing/foreign GUI tree: " + str(path))
    actual = inventory(path)
    if stage:
        actual.pop("stage.json", None)
    if actual != record["files"]:
        raise Error("GUI tree modified: " + str(path))


def ar_sections(path):
    """Read only ordinary Debian ar members, never execute package scripts."""
    with Path(path).open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise Error("publisher artifact is not a Debian ar archive")
        seen = set()
        while header := stream.read(60):
            if len(header) != 60 or header[58:] != b"`\n":
                raise Error("invalid Debian ar member")
            name = header[:16].decode("ascii").strip().removesuffix("/")
            if name in seen or "/" in name:
                raise Error("duplicate/unsafe Debian ar member")
            seen.add(name)
            length = int(header[48:58])
            data = stream.read(length)
            if len(data) != length:
                raise Error("truncated Debian ar member")
            if length % 2:
                stream.read(1)
            yield name, data


def unpack_deb(path, destination, source):
    found = set()
    for name, data in ar_sections(path):
        if not name.startswith(("data.tar", "control.tar")):
            continue
        section = name.split(".", 1)[0]
        if section in found:
            raise Error("duplicate Debian archive section")
        found.add(section)
        root = destination / ("DEBIAN" if section == "control" else "payload")
        with tempfile.NamedTemporaryFile(dir=destination, suffix=".tar") as stream:
            stream.write(data)
            stream.flush()
            extract(stream.name, root)
    if found != {"data", "control"}:
        raise Error("missing Debian package sections")
    control = (destination / "DEBIAN/control").read_text()
    fields = dict(re.findall(r"^([A-Za-z-]+): (.*)$", control, re.MULTILINE))
    if (fields.get("Package"), fields.get("Version"), fields.get("Architecture")) != (
        source["package"],
        source["version"],
        "amd64",
    ):
        raise Error("publisher .deb identity mismatch")
    if "remove_cron" in source:
        cron = source["remove_cron"]
        target = destination / "payload" / relative(cron["path"])
        if digest(target) != cron["sha256"]:
            raise Error("Slack repository-enabling cron script changed; review required")
        target.unlink()
        # Preserve the vendor's actual root-owned sandbox helper in the native
        # repack. The generic safe archive extractor intentionally drops suid.
        sandbox = destination / "payload/usr/lib/slack/chrome-sandbox"
        if not sandbox.is_file() or sandbox.is_symlink():
            raise Error("official Slack sandbox helper missing")
        sandbox.chmod(0o4755)
        control = re.sub(
            r"^Version: .*$", "Version: " + source["installed_version"], control, flags=re.MULTILINE
        )
        (destination / "DEBIAN/control").write_text(control)
        # The upstream package has no maintainer scripts. Do not acquire new
        # native install hooks silently on an update.
        allowed = {"control", "md5sums", "conffiles"}
        if any(p.name not in allowed for p in (destination / "DEBIAN").iterdir()):
            raise Error("unreviewed Slack maintainer script")
        for name in ("md5sums", "conffiles"):
            path = destination / "DEBIAN" / name
            if path.exists():
                path.write_text(
                    "\n".join(
                        line
                        for line in path.read_text().splitlines()
                        if not line.rstrip().endswith(cron["path"])
                    )
                    + "\n"
                )
    return destination / "payload"


def verify_deb_payload(ctx, staged):
    for name, expected in staged["details"]["payload"].items():
        target = host_path(ctx, "/" + name)
        actual = fingerprint(target)
        # Upstream postinst chooses the setuid sandbox only without user
        # namespaces. All other content and permissions must match.
        if name.endswith("/chrome-sandbox") and actual and actual.get("kind") == "file":
            if actual.get("mode") not in (0o755, 0o4755):
                raise Error("invalid native sandbox permissions")
            expected = {**expected, "mode": actual["mode"]}
        if actual != expected:
            raise Error("native package payload readback failed: " + str(target))


def linkage(ctx, root):
    """Inspect every actual ELF; don't assume bundle format or transitive ABI."""
    inspected = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        # Match the application's private library scope when inspecting an
        # extension on its own. This environment belongs only to ldd; launchers
        # and sessions never receive a global LD_LIBRARY_PATH.
        private_libs = [root / "lib", root / "usr/lib/slack", root / "opt/Capacities"]
        code, out, err = probe(
            ctx,
            "ldd",
            str(path),
            extra_env={"LD_LIBRARY_PATH": ":".join(str(p) for p in private_libs if p.is_dir())},
        )
        text = out + err
        if "not found" in text or "/nix/" in text:
            raise Error("missing/non-native ELF dependency: " + str(path) + ": " + text)
        if code and not any(
            marker in text for marker in ("not a dynamic executable", "statically linked")
        ):
            raise Error("ELF dependency inspection failed: " + str(path) + ": " + text)
        inspected += 1
    if not inspected:
        raise Error("publisher payload contains no Linux ELF executable")
    return inspected


def snap_runtime_metadata(ctx, path):
    text = call(ctx, "unsquashfs", "-cat", str(path), "meta/snap.yaml")
    fields = dict(re.findall(r"^([a-z-]+):\s*['\"]?([^\n'\"]+)['\"]?\s*$", text, re.MULTILINE))
    providers = re.findall(
        r"^\s+default-provider:\s*['\"]?([a-z0-9-]+)(?::[^\s'\"]+)?", text, re.MULTILINE
    )
    dependencies_ = sorted(set(providers + ([fields["base"]] if fields.get("base") else [])))
    if not dependencies_ or fields.get("confinement") != "strict":
        raise Error("Chromium must declare strict confinement and actual base/content dependencies")
    return fields, dependencies_


def build(ctx, package, source, stage):
    dependencies(ctx, package)
    if stage.exists():
        record = load(stage / "stage.json")
        if (
            record.get("source") != source
            or record.get("id") != package["id"]
            or record.get("recipe_sha256") != digest(__file__)
        ):
            raise Error("stage source/selection mismatch")
        verify_tree(stage, record, stage=True)
        return record
    stage.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".gui-stage-", dir=stage.parent))
    try:
        archive = None
        if source["format"] != "snap":
            archive = download(
                source["url"], source["sha256"], Path(ctx["paths"]["cache"]) / "downloads"
            )
        if source.get("publisher_sha512"):
            with archive.open("rb") as stream:
                actual = base64.b64encode(hashlib.file_digest(stream, "sha512").digest()).decode()
            if actual != source["publisher_sha512"]:
                raise Error("publisher SHA512 disagrees with reviewed artifact")
        details = {}
        if source["format"] == "deb":
            payload = unpack_deb(archive, work, source)
            details["elf_inspected"] = linkage(ctx, payload)
            if "remove_cron" in source:
                shutil.move(str(work / "DEBIAN"), payload / "DEBIAN")
                call(
                    ctx,
                    "dpkg-deb",
                    "--root-owner-group",
                    "--build",
                    str(payload),
                    str(work / "package.deb"),
                )
                shutil.rmtree(payload / "DEBIAN")
            else:
                shutil.copyfile(archive, work / "package.deb")
                shutil.rmtree(work / "DEBIAN")
            details["payload"] = inventory(payload)
        elif source["format"] == "snap":
            basename = work / "chromium"
            call(
                ctx,
                "snap",
                "download",
                source["name"],
                "--revision=" + str(source["revision"]),
                "--basename=" + str(basename),
            )
            snap = work / "chromium.snap"
            with snap.open("rb") as stream:
                sha3 = hashlib.file_digest(stream, "sha3_384").hexdigest()
            if (
                digest(snap) != source["sha256"]
                or sha3 != source["sha3_384"]
                or snap.stat().st_size != source["size"]
            ):
                raise Error("Snap store download disagrees with reviewed revision identity")
            assertion = work / "chromium.assert"
            if not assertion.is_file() or assertion.is_symlink():
                raise Error("snap download did not return its full assertion chain")
            assertion_text = assertion.read_text()
            for line in (
                "snap-id: " + source["snap_id"],
                "snap-revision: " + str(source["revision"]),
            ):
                if line not in assertion_text.splitlines():
                    raise Error("Snap assertion does not bind selected identity")
            fields, deps = snap_runtime_metadata(ctx, snap)
            if fields.get("name") != source["name"] or fields.get("version") != source["version"]:
                raise Error("Chromium snap metadata/version mismatch")
            details["snap_dependencies"] = deps
        else:
            payload = work / "payload"
            payload.mkdir()
            if source["format"] == "raw":
                target = payload / "bin/tw"
                target.parent.mkdir()
                shutil.copyfile(archive, target)
                target.chmod(0o755)
                license_ = source["license_source"]
                license_path = download(
                    license_["url"], license_["sha256"], Path(ctx["paths"]["cache"]) / "downloads"
                )
                shutil.copyfile(license_path, payload / "LICENSE")
            else:
                extract(archive, payload, source["format"])
            for name, target in source.get("runtime_aliases", {}).items():
                alias = payload / relative(name)
                destination = alias.parent / relative(target)
                if (
                    alias.exists()
                    or alias.is_symlink()
                    or not destination.is_file()
                    or destination.is_symlink()
                ):
                    raise Error(
                        "reviewed internal runtime alias no longer matches publisher payload"
                    )
                alias.symlink_to(target)
            for path in package["executables"].values():
                native(payload / relative(path))
            details["elf_inspected"] = linkage(ctx, payload)
        record = {
            "id": package["id"],
            "source": source,
            "version": package["version"],
            "recipe_sha256": digest(__file__),
            "files": inventory(work),
            "details": details,
        }
        atomic_json(work / "stage.json", record)
        os.replace(work, stage)
        return record
    finally:
        if work.exists():
            shutil.rmtree(work)


def closed(ctx, package_id):
    names = {
        "gui-slack": {"slack"},
        "gui-capacities": {"capacities", "Capacities"},
        "gui-chromium": {"chromium", "chromium-browse", "chrome", "chromium.launch"},
    }
    if package_id not in names:
        return
    output = call(ctx, "ps", "-u", str(Path(ctx["home"]).stat().st_uid), "-o", "comm=")
    if any(Path(line.strip()).name in names[package_id] for line in output.splitlines()):
        raise Error("close the application before install/update/retirement: " + package_id)


def assert_ownership(ctx, record, source, package):
    if (
        record.get("id") != package["id"]
        or record.get("version") != package["version"]
        or record.get("source") != source
    ):
        raise Error("installed GUI selection differs from receipt; provision a reviewed update")
    formats = {
        "gui-kitty": "tar.xz",
        "gui-tabiew": "raw",
        "gui-slack": "deb",
        "gui-capacities": "deb",
        "gui-chromium": "snap",
    }
    if source.get("format") != formats[package["id"]] or source.get("version") != record["version"]:
        raise Error("GUI receipt changed provider family/version")
    native_packages = {"gui-slack": "slack-desktop", "gui-capacities": "capacities"}
    if package["id"] in native_packages and source.get("package") != native_packages[package["id"]]:
        raise Error("GUI receipt changed native package identity")
    if package["id"] == "gui-chromium" and (source.get("name"), source.get("snap_id")) != (
        "chromium",
        "XKEcBqPM06H1Z7zGOdG5fbICuf8NWK5R",
    ):
        raise Error("GUI receipt changed Snap identity")
    if package["id"] in ("gui-kitty", "gui-tabiew"):
        allowed = {str(Path(ctx["paths"]["bin"]) / name) for name in package["executables"]}
        if package["id"] == "gui-kitty":
            allowed.update(
                str(Path(ctx["paths"]["data"]).parent / "applications" / name)
                for name in ("kitty.desktop", "kitty-open.desktop")
            )
        if set(record.get("owned", {})) - allowed:
            raise Error("GUI receipt claims a foreign launcher path")
        for runtime_record in (record, *record.get("retained_runtimes", [])):
            expected = (
                Path(ctx["paths"]["runtime"]) / package["id"] / relative(runtime_record["version"])
            )
            if runtime_record.get("runtime") != str(expected):
                raise Error("GUI receipt claims a foreign runtime path")
            no_symlink_parents(expected, Path(ctx["home"]))
    for path, expected in record.get("owned", {}).items():
        boundary = Path(ctx["home"]) if Path(path).is_relative_to(Path(ctx["home"])) else Path("/")
        no_symlink_parents(Path(path), boundary)
        if fingerprint(path) != expected:
            raise Error("foreign/modified GUI output: " + path)


def dpkg_version(ctx, source):
    code, out, _ = probe(
        ctx, "dpkg-query", "-W", "-f=${db:Status-Status}\t${Version}", source["package"]
    )
    if code or not out.startswith("installed\t"):
        return None
    return out.strip().split("\t", 1)[1]


def host_path(ctx, path):
    """Never redirect production /usr or /opt; fixtures use a disposable sysroot."""
    if ctx.get("_fixture_context"):
        root = Path(ctx["_fixture_system_root"])
        if not root.resolve().is_relative_to(Path(ctx["home"]).resolve()):
            raise Error("fixture system root must be inside disposable HOME")
        return root / str(path).lstrip("/")
    return Path(path)


def deb_targets(ctx, staged, source):
    return [host_path(ctx, "/" + name) for name in staged["details"]["payload"]] + [
        host_path(ctx, path) for path in source.get("script_owned_paths", [])
    ]


def check_snap(ctx, source, *, disabled=False):
    # JSON from snapd is the stable source of installed version/revision, hold,
    # confinement, tracking and interface state; human snap list columns vary.
    socket = Path("/run/snapd.socket")
    if ctx.get("_fixture_context"):
        socket = Path(ctx["home"]) / "snapd.socket"
    import http.client
    import socket as socket_module

    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
            self.sock.settimeout(30)
            self.sock.connect(str(socket))

    connection = UnixConnection("localhost")
    try:
        connection.request("GET", "/v2/snaps/" + source["name"])
        response = connection.getresponse()
        data = json.loads(response.read())
    finally:
        connection.close()
    if data.get("status-code") != 200:
        raise Error("owned Chromium snap is missing from snapd")
    snap = data["result"]
    if (snap.get("id"), snap.get("version"), str(snap.get("revision"))) != (
        source["snap_id"],
        source["version"],
        str(source["revision"]),
    ):
        raise Error("installed Chromium snap identity differs from owned selection")
    if snap.get("confinement") != "strict" or snap.get("devmode") or snap.get("dangerous"):
        raise Error("Chromium snap confinement/assertion validation failed")
    if snap.get("status") != ("installed" if disabled else "active"):
        raise Error("Chromium snap is not in its owned active/disabled state")
    if snap.get("hold") != "forever":
        raise Error("Chromium automatic refresh is not held by the declared updater")
    if snap.get("tracking-channel") != source["channel"]:
        raise Error("Chromium snap no longer tracks the declared stable channel")
    return snap


def snap_connections(ctx, source):
    connections = call(ctx, "snap", "connections", source["name"])
    for plug in source["interfaces"]:
        rows = [
            line.split()
            for line in connections.splitlines()
            if source["name"] + ":" + plug in line.split()
        ]
        if not rows or any(len(row) < 3 or row[2] == "-" for row in rows):
            raise Error("required Chromium interface disconnected: " + plug)


def snap_ready(ctx):
    code, _, _ = probe(ctx, "systemctl", "is-active", "--quiet", "snapd.socket")
    if code:
        raise Error(
            "snapd.socket inactive; run the explicit privileged bootstrap services action for snapd-socket, then resume provision"
        )


def kitty_registration(ctx, stage, runtime):
    files = {}
    executable = str(runtime / "bin/kitty")
    quoted = (
        '"'
        + executable.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        + '"'
    )
    for name in ("kitty.desktop", "kitty-open.desktop"):
        text = (stage / "payload/share/applications" / name).read_text()
        lines = []
        for line in text.splitlines():
            if line.startswith(("Exec=", "TryExec=")):
                key, value = line.split("=", 1)
                if value != "kitty" and not value.startswith("kitty "):
                    raise Error("unreviewed Kitty desktop launch command")
                line = (
                    key + "=" + (executable if key == "TryExec" else quoted) + value[len("kitty") :]
                )
            elif line.startswith("Icon="):
                line = "Icon=" + str(runtime / "share/icons/hicolor/256x256/apps/kitty.png")
            lines.append(line)
        files[Path(ctx["paths"]["data"]).parent / "applications" / name] = (
            "\n".join(lines) + "\n"
        ).encode()
    return files


def install(ctx, package, source, state, stage, runtime, staged):
    path = receipt_path(state, package)
    previous = load(path) if path.exists() else None
    if previous:
        assert_ownership(
            ctx, previous, previous["source"], {**package, "version": previous["version"]}
        )
    if previous and previous.get("status") != "retired":
        if previous.get("runtime"):
            verify_tree(Path(previous["runtime"]), previous)
        for retained in previous.get("retained_runtimes", []):
            verify_tree(Path(retained["runtime"]), retained)
    closed(ctx, package["id"])
    executables = executable_paths(package, runtime)
    owned = {}
    extra = dict(staged["details"])
    if source["format"] == "deb":
        current = dpkg_version(ctx, source)
        if current and (not previous or current != previous["installed_version"]):
            raise Error("foreign native package collision: " + source["package"])
        targets = deb_targets(ctx, staged, source)
        for target in targets:
            code, owners, _ = probe(ctx, "dpkg-query", "-S", str(target))
            if code == 0 and (
                not previous
                or any(
                    not line.startswith(source["package"] + ": ") for line in owners.splitlines()
                )
            ):
                raise Error("foreign dpkg owner collision: " + str(target))
        for target in targets:
            boundary = (
                Path(ctx["_fixture_system_root"]) if ctx.get("_fixture_context") else Path("/")
            )
            no_symlink_parents(target, boundary)
            actual = fingerprint(target)
            if actual is not None and (
                not previous or actual != previous.get("owned", {}).get(str(target))
            ):
                raise Error("foreign native package path collision: " + str(target))
        # Journal the intended transition before native mutation. A failed apt
        # operation leaves the journal visible and never publishes ownership.
        state_record(
            ctx,
            state / (package["id"] + ".pending.json"),
            {"source": source, "before": previous, "stage": str(stage)},
        )
        call(
            ctx,
            "apt-get",
            "install",
            "-y",
            "--reinstall",
            "--no-install-recommends",
            str(stage / "package.deb"),
            mutating=True,
        )
        if dpkg_version(ctx, source) != source["installed_version"]:
            raise Error("native .deb version readback failed")
        verify_deb_payload(ctx, staged)
        for target in targets:
            actual = fingerprint(target)
            if actual is not None:
                owned[str(target)] = actual
        for command_, declared in executables.items():
            executables[command_] = native(host_path(ctx, declared))
        extra["installed_version"] = source["installed_version"]
    elif source["format"] == "snap":
        snap_ready(ctx)
        code, listing, _ = probe(ctx, "snap", "list", source["name"])
        if code == 0 and not previous:
            raise Error("foreign Chromium snap collision; no implicit adoption")
        if previous:
            # Query against the previously owned version before refreshing.
            check_snap(ctx, previous["source"], disabled=previous.get("status") == "retired")
        state_record(
            ctx,
            state / (package["id"] + ".pending.json"),
            {"source": source, "before": previous, "stage": str(stage)},
        )
        call(ctx, "snap", "ack", str(stage / "chromium.assert"), mutating=True)
        if previous and previous.get("status") == "retired":
            call(ctx, "snap", "enable", source["name"], mutating=True)
        if previous:
            if previous["source"]["revision"] != source["revision"]:
                call(
                    ctx,
                    "snap",
                    "refresh",
                    source["name"],
                    "--revision=" + str(source["revision"]),
                    "--channel=" + source["channel"],
                    mutating=True,
                )
        else:
            call(ctx, "snap", "install", str(stage / "chromium.snap"), mutating=True)
        call(ctx, "snap", "switch", source["name"], "--channel=" + source["channel"], mutating=True)
        call(ctx, "snap", "refresh", "--hold=forever", source["name"], mutating=True)
        # Prevent Canonical's launcher selecting unencrypted password-store=basic.
        # No credential is read and the browser is not launched.
        for plug in ("password-manager-service", "chromium-config"):
            call(ctx, "snap", "connect", source["name"] + ":" + plug, mutating=True)
        check_snap(ctx, source)
        snap_connections(ctx, source)
        for dependency in staged["details"]["snap_dependencies"]:
            call(ctx, "snap", "list", dependency)
        executable = host_path(ctx, "/snap/bin/chromium")
        native(executable)
        executables = {"chromium": str(executable)}
        installed_snap = host_path(
            ctx, "/var/lib/snapd/snaps/chromium_" + str(source["revision"]) + ".snap"
        )
        if digest(installed_snap) != source["sha256"]:
            raise Error("installed Chromium snap bytes differ from reviewed artifact")
        owned[str(installed_snap)] = fingerprint(installed_snap)
        owned[str(executable)] = fingerprint(executable)
        extra["profile_transition"] = {
            "source": str(Path(ctx["home"]) / ".config/chromium"),
            "destination": str(Path(ctx["home"]) / "snap/chromium/common/chromium"),
            "owner": "publisher first-launch import; original profile preserved, no recipe access",
        }
        extra["physical_host_gate"] = source["physical_host_gate"]
    else:
        links = {Path(ctx["paths"]["bin"]) / name: target for name, target in executables.items()}
        registration = (
            kitty_registration(ctx, stage, runtime) if package["id"] == "gui-kitty" else {}
        )
        for target in (*links, *registration):
            no_symlink_parents(target, Path(ctx["home"]))
            actual = fingerprint(target)
            if actual is not None and (
                not previous or actual != previous.get("owned", {}).get(str(target))
            ):
                raise Error("foreign GUI launcher collision: " + str(target))
            if os.path.lexists(target.with_name(target.name + ".gui-candidate")):
                raise Error("foreign GUI link staging collision")
        if runtime.exists():
            existing = (
                previous
                if previous and previous.get("runtime") == str(runtime)
                else next(
                    (
                        r
                        for r in (previous or {}).get("retained_runtimes", [])
                        if r["runtime"] == str(runtime)
                    ),
                    None,
                )
            )
            if existing is None:
                raise Error("foreign user runtime collision: " + str(runtime))
            verify_tree(runtime, existing)
            if inventory(runtime) != inventory(stage / "payload"):
                raise Error(
                    "immutable runtime version differs from reviewed stage; select a new version before replacing it"
                )
        else:
            runtime.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=".gui-install-", dir=runtime.parent
            ) as temporary:
                payload = Path(temporary) / "payload"
                shutil.copytree(stage / "payload", payload, symlinks=True)
                if inventory(payload) != inventory(stage / "payload"):
                    raise Error("runtime staging readback mismatch")
                os.replace(payload, runtime)
        for target, destination in links.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".gui-candidate")
            if os.path.lexists(temporary):
                raise Error("foreign GUI link staging collision")
            temporary.symlink_to(destination)
            os.replace(temporary, target)
            owned[str(target)] = fingerprint(target)
        for target, data in registration.items():
            atomic_bytes(target, data, 0o644)
            owned[str(target)] = fingerprint(target)
        for executable in executables.values():
            native(executable)
    record = {
        "id": package["id"],
        "version": package["version"],
        "source": source,
        "status": "installed",
        "owned": owned,
        "executables": executables,
        "installed_version": source.get("installed_version", source["version"]),
        "details": extra,
    }
    if source["format"] not in ("deb", "snap"):
        record.update(runtime=str(runtime), files=inventory(runtime))
        retained = [
            r for r in (previous or {}).get("retained_runtimes", []) if r["runtime"] != str(runtime)
        ]
        if (
            previous
            and previous.get("runtime")
            and previous["runtime"] != str(runtime)
            and previous.get("status") != "retired"
        ):
            retained.append(
                {key: previous[key] for key in ("runtime", "files", "version", "source")}
            )
        record["retained_runtimes"] = retained
    state_record(ctx, path, record)
    (state / (package["id"] + ".pending.json")).unlink(missing_ok=True)
    return record


def check(ctx, package, source, state):
    path = receipt_path(state, package)
    record = load(path)
    assert_ownership(ctx, record, source, package)
    if record.get("status") != "installed":
        raise Error("GUI application is retired, not installed")
    if source["format"] == "deb":
        installed_version = dpkg_version(ctx, source)
        if installed_version != source["installed_version"]:
            raise Error("installed native package version mismatch")
    elif source["format"] == "snap":
        snap_ready(ctx)
        installed_version = check_snap(ctx, source)["version"]
        snap_connections(ctx, source)
    else:
        verify_tree(Path(record["runtime"]), record)
        for retained in record.get("retained_runtimes", []):
            verify_tree(Path(retained["runtime"]), retained)
        installed_version = source["version"]
    if record.get("installed_version") != installed_version:
        raise Error("GUI receipt version disagrees with verified installation")
    record["details"] = {**record["details"], "installed_version": installed_version}
    for value in record["executables"].values():
        native(value)
    return record


def retire(ctx, package, source, state):
    path = receipt_path(state, package)
    record = load(path)
    source = record["source"]
    assert_ownership(ctx, record, source, {**package, "version": record["version"]})
    if record.get("status") == "retired":
        if source["format"] == "snap":
            snap_ready(ctx)
            check_snap(ctx, source, disabled=True)
        elif source["format"] == "deb" and dpkg_version(ctx, source) is not None:
            raise Error("foreign package installed after owned retirement")
        return record
    closed(ctx, package["id"])
    if source["format"] == "snap":
        snap_ready(ctx)
        check_snap(ctx, source)
        call(ctx, "snap", "disable", source["name"], mutating=True)
        check_snap(ctx, source, disabled=True)
        # Keep exact bytes, interfaces, hold, profile data and shared dependencies.
    elif source["format"] == "deb":
        if dpkg_version(ctx, source) != source["installed_version"]:
            raise Error("foreign native package version; retirement refused")
        preview = call(
            ctx, "apt-get", "--simulate", "remove", "--no-auto-remove", source["package"]
        )
        transitions = re.findall(
            r"^(Remv|Purg|Inst|Conf) ([^ :\n]+)(?::[^ \n]+)?", preview, re.MULTILINE
        )
        if transitions != [("Remv", source["package"])]:
            raise Error("native retirement would mutate foreign packages; refusing APT transaction")
        call(ctx, "apt-get", "remove", "-y", "--no-auto-remove", source["package"], mutating=True)
        if dpkg_version(ctx, source) is not None:
            raise Error("native package removal readback failed")
        record["owned"] = {
            p: expected for p, expected in record["owned"].items() if fingerprint(p) is not None
        }
    else:
        verify_tree(Path(record["runtime"]), record)
        for retained in record.get("retained_runtimes", []):
            verify_tree(Path(retained["runtime"]), retained)
        for owned in record["owned"]:
            Path(owned).unlink()
        shutil.rmtree(record["runtime"])
        for retained in record.get("retained_runtimes", []):
            shutil.rmtree(retained["runtime"])
        record["retained_runtimes"] = []
        record["owned"] = {}
    record["status"] = "retired"
    state_record(ctx, path, record)
    return record


def execute(ctx, action, package_id, output=None):
    package, source = selected(ctx, package_id)
    state, stage, runtime = locations(ctx, package, output, source)
    if action == "plan":
        return result(
            package,
            action,
            executables=executable_paths(package, runtime),
            source=source,
            stage=str(stage),
            requires=package.get("requires", []),
            requires_services=package.get("requires_services", []),
            appstate="Never accessed or owned; native vendor lifecycle only",
        )
    if action == "check":
        dependencies(ctx, package)
        record = check(ctx, package, source, state)
        return result(
            package,
            action,
            installed_artifacts(receipt_path(state, package), record),
            record["executables"],
            **record["details"],
        )
    with locked(state):
        if os.geteuid() == 0 and not ctx.get("_fixture_context"):
            owner = Path(ctx["home"]).stat()
            for path in (state, state / "operation.lock"):
                os.chown(path, owner.st_uid, owner.st_gid)
        if action == "build":
            staged = build(ctx, package, source, stage)
            return result(
                package, action, built_artifacts(package, source, stage), **staged["details"]
            )
        if action == "provision":
            dependencies(ctx, package)
            staged = build(ctx, package, source, stage)
            record = install(ctx, package, source, state, stage, runtime, staged)
            record = check(ctx, package, source, state)
            return result(
                package,
                action,
                installed_artifacts(receipt_path(state, package), record),
                record["executables"],
                **record["details"],
            )
        if action == "retire":
            record = retire(ctx, package, source, state)
            return result(package, action, [receipt_path(state, package)], **record["details"])
    raise Error("unsupported Linux GUI action")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "build", "provision", "check", "retire"))
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--id", required=True, choices=sorted(IDS))
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                execute(load(args.context), args.action, args.id, args.output), sort_keys=True
            )
        )
    except (Error, OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print("Linux GUI: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
