"""Explicit native service selection; distro unit payloads remain APT-owned.

Existing system units are observed without adoption when already healthy.
Only explicit services actions may change activation, and their prior selection
is journalled for narrow recovery. Configuration/package retirement never stops
or disables these shared OS daemons.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import re

import shlex
import sys
from configuration import read_record
from safety import Error, atomic_json, fingerprint, load, no_symlink_parents, run, command

ACTIONS = ("status", "enable", "disable", "start", "stop", "restart", "reload", "recover")


def descriptor(context, service):
    if service["manager"] == "systemd-system":
        return Path(observe(context, service)["FragmentPath"])
    name = service.get("target", service.get("source"))
    if not name:
        raise Error(f"service requires an authored descriptor: {service['id']}")
    path = Path(context["home"]) / name
    if name.startswith(".config/"):
        path = Path(context["paths"]["config"]) / name[len(".config/") :]
    no_symlink_parents(path, Path(context["home"]))
    record = read_record(context)["files"]
    key = path.relative_to(context["home"]).as_posix()
    if key not in record or fingerprint(path) != record[key]:
        raise Error(
            f"service descriptor is not an unchanged owned file: {path}; apply config first"
        )
    content = path.read_bytes()
    if b"/nix/" in content:
        raise Error(f"service retains Nix dependency: {service['id']}")
    if service["manager"] == "launchd":
        data = plistlib.loads(content)
        if data.get("Label") != service["name"]:
            raise Error("launchd label does not match manifest")
    return path


def validate_platform(context, service):
    manager = service["manager"]
    if context["platform"] not in ("linux-x64", "macos-arm64"):
        raise Error(f"unsupported service platform: {context['platform']}")
    if (
        manager == "systemd-system"
        and not context.get("_verification_only")
        and sys.platform != "linux"
    ):
        raise Error("systemd-system service control is native Linux only")
    if context["platform"] == "linux-x64" and manager not in ("systemd-user", "systemd-system"):
        raise Error("launchd service selected on Linux")
    if context["platform"] == "macos-arm64" and manager != "launchd":
        raise Error("systemd service selected on macOS")
    if manager == "systemd-system" and not service.get("existing_unit"):
        raise Error("system services must explicitly reference an existing distro unit")


def existing_unit(context, service, observed):
    """Bind the loaded unit to its installed APT payload, not a filename claim."""
    package = next(
        (item for item in context["packages"] if item["id"] == service.get("package_id")), None
    )
    if package is None:
        records_path = Path(context["paths"]["state"]) / "packages.json"
        if records_path.exists():
            records = load(records_path)
            if records.get("home") != context["home"]:
                raise Error("existing unit package record belongs to another home")
            package = (
                records.get("packages", {}).get(service.get("package_id"), {}).get("selection")
            )
    if (
        package is None
        or package.get("provider") != "apt"
        or package.get("name") != service.get("unit_package")
    ):
        raise Error(f"existing unit requires its declared APT package: {service['id']}")
    path = Path(observed.get("FragmentPath", ""))
    fixture = context.get("_verification_only") and context.get("_fixture_context")
    if fixture:
        if not path.is_absolute() or not path.resolve().is_relative_to(
            Path(context["home"]).resolve()
        ):
            raise Error("fixture system unit must be under disposable HOME")
        no_symlink_parents(path, Path(context["home"]))
        # Read-only queries also must not reach the host's package database.
        for name, default in (
            ("systemctl", "/usr/bin/systemctl"),
            ("dpkg-query", "/usr/bin/dpkg-query"),
        ):
            command(context, name, default, mutating=True)
    elif path.parent not in (Path("/usr/lib/systemd/system"), Path("/lib/systemd/system")):
        raise Error(f"foreign system unit origin: {path}")
    if (
        observed.get("LoadState") != "loaded"
        or observed.get("Id") != service["name"]
        or path.name != service["name"]
        or not path.is_file()
        or path.is_symlink()
        or observed.get("DropInPaths")
        or observed.get("SourcePath")
    ):
        raise Error(f"foreign/replaced system unit or drop-in: {service['name']}")
    if not fixture and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022):
        raise Error(f"unsafe system unit ownership/mode: {path}")
    query = command(context, "dpkg-query", "/usr/bin/dpkg-query")
    name = service["unit_package"]
    status = run([query, "--show", "--showformat=${Status}\\t${Version}", name], context).strip()
    if not status.startswith("install ok installed\t"):
        raise Error(f"existing unit package is not installed: {name}")
    candidates = {str(path).lstrip("/")}
    if not fixture:
        candidates.update(
            {f"usr/lib/systemd/system/{path.name}", f"lib/systemd/system/{path.name}"}
        )
    owned_paths = {
        line.lstrip("/") for line in run([query, "--listfiles", name], context).splitlines()
    }
    if not candidates.intersection(owned_paths):
        raise Error(f"foreign system unit package owner: {path}")
    sums = Path(run([query, "--control-path", name, "md5sums"], context).strip())
    if fixture:
        if not sums.resolve().is_relative_to(Path(context["home"]).resolve()):
            raise Error("fixture package metadata must be under disposable HOME")
        no_symlink_parents(sums, Path(context["home"]))
    elif sums != Path("/var/lib/dpkg/info") / f"{name}.md5sums":
        raise Error("unexpected APT package checksum metadata")
    if sums.is_symlink():
        raise Error("symlink package checksum metadata")
    checksums = dict(
        line.split(None, 1)[::-1] for line in sums.read_text().splitlines() if line.strip()
    )
    checksum = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
    if not any(checksums.get(candidate) == checksum for candidate in candidates):
        raise Error(f"foreign/replaced distro unit payload: {path}")
    return {
        "path": str(path),
        "package": name,
        "version": status.split("\t", 1)[1],
        "fingerprint": fingerprint(path),
    }


def system_command(context, service):
    executable = command(context, "systemctl", "/usr/bin/systemctl", mutating=True)
    argv = [executable, "--user" if service["manager"] == "systemd-user" else "--system"]
    if service["manager"] == "systemd-system":
        # Validate sudo even when the test runner happens to be root. A fake
        # systemctl alone must never make a real privilege helper permissible.
        if context.get("_verification_only") or os.geteuid() != 0:
            sudo = command(context, "sudo", "/usr/bin/sudo", mutating=True)
            argv = [sudo, "--", *argv]
    return argv


def require_services(context, package):
    """Read-only prerequisite check used immediately before package mutation."""
    desired = {item["id"]: item for item in context["services"]}
    for identifier in package.get("requires_services", []):
        if identifier not in desired:
            raise Error(f"package requires missing selected service: {identifier}")
        service = desired[identifier]
        observed = observe(context, service)
        healthy = observed.get("ActiveState") == "active" and observed.get("UnitFileState") in (
            "enabled",
            "enabled-runtime",
            "static",
        )
        if not healthy:
            entry = shlex.quote(str(Path(context["root"]) / "bootstrap"))
            raise Error(
                f"{package['id']} requires {identifier}; no activation was performed. "
                f"Explicit service intent: {entry} services enable --id {identifier} --yes; "
                f"{entry} services start --id {identifier} --yes; "
                f"then resume {entry} provision --id {package['id']} --yes"
            )


def conflict_preflight(context, service):
    for conflict in service.get("conflicts", []):
        observed = observe(context, {**service, "name": conflict})
        if (
            observed.get("loaded")
            or observed.get("ActiveState") == "active"
            or observed.get("UnitFileState") in ("enabled", "enabled-runtime")
        ):
            raise Error(
                f"legacy/foreign service conflict {conflict}; explicit ownership-aware handoff is required, never disable by name"
            )
        if service["manager"] == "systemd-user":
            # A globally enabled user unit may not yet be loaded in this user's
            # manager. It remains a conflict, not a unit we may disable by name.
            executable = command(context, "systemctl", "/usr/bin/systemctl")
            text = run(
                [executable, "--global", "list-unit-files", conflict, "--no-legend", "--no-pager"],
                context,
            )
            if any(
                row.split()[:2] in ([conflict, "enabled"], [conflict, "enabled-runtime"])
                for row in text.splitlines()
            ):
                raise Error(
                    f"global legacy/foreign service conflict {conflict}; recorded ownership-aware handoff required"
                )


def observe(context, service):
    validate_platform(context, service)
    if service["manager"] in ("systemd-user", "systemd-system"):
        existing = service["manager"] == "systemd-system"
        if existing and context.get("_verification_only"):
            command(context, "systemctl", "/usr/bin/systemctl", mutating=True)
        properties = "ActiveState,UnitFileState,LoadState"
        if existing:
            properties += ",Id,FragmentPath,DropInPaths,SourcePath"
        output = run(
            [
                command(context, "systemctl", "/usr/bin/systemctl"),
                "--system" if existing else "--user",
                "show",
                service["name"],
                "--property=" + properties,
            ],
            context,
        )
        observed = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if existing:
            observed["origin"] = existing_unit(context, service, observed)
        return observed
    domain = f"gui/{os.getuid()}"
    executable = command(context, "launchctl", "/bin/launchctl")
    disabled_text = run([executable, "print-disabled", domain], context)
    match = re.search(
        r'"' + re.escape(service["name"]) + r'"\s*=>\s*(true|false|enabled|disabled)',
        disabled_text,
    )
    disabled = bool(match and match.group(1) in ("true", "disabled"))
    try:
        run([executable, "print", f"{domain}/{service['name']}"], context)
    except Error as exc:
        if "Could not find service" in str(exc) or "not find service" in str(exc):
            return {"loaded": False, "disabled": disabled}
        raise
    return {"loaded": True, "disabled": disabled}


def control(context, action, identifiers=()):
    if action not in ACTIONS:
        raise Error(f"unknown service action: {action}")
    if action == "recover":
        from recovery import recover_services

        return recover_services(context, identifiers)
    path = Path(context["paths"]["state"]) / "services.json"
    records = load(path) if path.exists() else {"home": context["home"], "services": {}}
    if records.get("home") != context["home"]:
        raise Error("service ownership belongs to another home")
    desired = {record["id"]: record for record in context["services"]}
    # Disabled feature services remain controllable only when previously recorded.
    available = {key: item["selection"] for key, item in records["services"].items()}
    available.update(desired)
    selected = set(identifiers) if identifiers else set(desired)
    if selected - available.keys():
        raise Error(f"unknown or unowned services: {sorted(selected - available.keys())}")
    output = {}
    for identifier in sorted(selected):
        service = available[identifier]
        manager = service["manager"]
        validate_platform(context, service)
        if action == "status":
            output[identifier] = observe(context, service)
            continue
        if action in ("start", "enable", "restart", "reload") and identifier not in desired:
            raise Error(f"cannot activate disabled-feature service: {identifier}")
        if action in ("stop", "disable") and identifier not in records["services"]:
            raise Error(f"refusing to deactivate unowned service: {identifier}")
        target = descriptor(context, service)
        if action in ("start", "enable", "restart", "reload"):
            conflict_preflight(context, service)
        before = observe(context, service)
        existing = manager == "systemd-system"
        previous = records["services"].get(identifier)
        if existing and previous and before["origin"] != previous["observed"]["origin"]:
            raise Error(f"service unit replaced since recorded transition: {identifier}")
        if existing and action in ("stop", "disable") and previous:
            prior = previous.get("prior_state", previous["observed"])
            if (action == "stop" and prior.get("ActiveState") == "active") or (
                action == "disable" and prior.get("UnitFileState") in ("enabled", "enabled-runtime")
            ):
                raise Error(
                    f"shared OS service predates engine activation; preserved: {identifier}"
                )
        journal_path = Path(context["paths"]["state"]) / "service-journal.json"
        journal = (
            load(journal_path)
            if journal_path.exists()
            else {"home": context["home"], "services": {}}
        )
        if journal.get("home") != context["home"]:
            raise Error("service journal belongs to another home")
        if journal["services"].get(identifier, {}).get("status") == "pending":
            raise Error(f"unfinished service transition: {identifier}; services recover --id first")
        if existing and (
            (
                action == "enable"
                and before.get("UnitFileState") in ("enabled", "enabled-runtime", "static")
            )
            or (action == "start" and before.get("ActiveState") == "active")
        ):
            output[identifier] = {**before, "preserved_existing": True}
            continue
        if identifier not in records["services"]:
            active = before.get("ActiveState") == "active" or before.get("loaded")
            enabled = before.get("UnitFileState") in ("enabled", "enabled-runtime")
            if (active or enabled) and not existing:
                raise Error(
                    f"service already active/enabled without engine ownership: {identifier}"
                )
            if existing and action in ("restart", "reload"):
                raise Error(
                    f"refusing to restart/reload an unowned shared OS service: {identifier}"
                )
        executable = (
            system_command(context, service)
            if manager.startswith("systemd-")
            else [command(context, "launchctl", "/bin/launchctl", mutating=True)]
        )
        transition = {
            "selection": service,
            "descriptor": fingerprint(target),
            "before": before,
            "previous_record": records["services"].get(identifier),
            "action": action,
            "status": "pending",
        }
        journal["services"][identifier] = transition
        atomic_json(journal_path, journal)
        if manager.startswith("systemd-"):
            if manager == "systemd-user" and action in ("enable", "start", "restart", "reload"):
                run([*executable, "daemon-reload"], context)
            run([*executable, action, service["name"]], context)
        # launchd's executable is one element, unlike privileged systemd argv.
        else:
            executable = executable[0]
            domain = f"gui/{os.getuid()}"
            label = f"{domain}/{service['name']}"
            if action == "enable":
                run([executable, "enable", label], context)
            elif action == "disable":
                if before.get("loaded"):
                    run([executable, "bootout", label], context)
                run([executable, "disable", label], context)
            elif action == "start":
                if not before.get("loaded"):
                    run([executable, "bootstrap", domain, target], context)
                run([executable, "kickstart", label], context)
            elif action == "stop":
                if before.get("loaded"):
                    run([executable, "bootout", label], context)
            elif action == "restart":
                if not before.get("loaded"):
                    run([executable, "bootstrap", domain, target], context)
                run([executable, "kickstart", "-k", label], context)
            else:
                raise Error("launchd has no generic reload; use explicit restart")
        observed = observe(context, service)
        if manager.startswith("systemd-"):
            if action in ("start", "restart") and observed.get("ActiveState") != "active":
                raise Error(f"service did not become active: {identifier}")
            if action == "stop" and observed.get("ActiveState") not in ("inactive", "failed"):
                raise Error(f"service did not stop: {identifier}")
            if action == "enable" and observed.get("UnitFileState") not in (
                "enabled",
                "enabled-runtime",
                "static",
            ):
                raise Error(f"service did not enable: {identifier}")
            if action == "disable" and observed.get("UnitFileState") in (
                "enabled",
                "enabled-runtime",
            ):
                raise Error(f"service remained enabled: {identifier}")
        elif action in ("start", "restart") and not observed.get("loaded"):
            raise Error(f"launchd did not load: {identifier}")
        if manager == "launchd":
            if action == "enable" and observed.get("disabled"):
                raise Error(f"launchd label remained disabled: {identifier}")
            if action == "disable" and not observed.get("disabled"):
                raise Error(f"launchd label did not disable: {identifier}")
        transition["after"] = observed
        transition["status"] = "committed"
        atomic_json(journal_path, journal)
        records["services"][identifier] = {
            "selection": service,
            "observed": observed,
            "descriptor": fingerprint(target),
            "last_action": action,
            "prior_state": previous.get("prior_state", before) if previous else before,
        }
        atomic_json(path, records)
        output[identifier] = observed
    return output
