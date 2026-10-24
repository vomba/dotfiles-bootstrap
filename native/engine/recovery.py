"""Ownership-bound package/link and service selection recovery.

Recovery never resets application databases, credentials or user state. Native
APT versions must remain obtainable from the selected signed repositories;
Homebrew recovery requires a retained exact bottle and is not guessed from an
unversioned formula. Recipe-specific retained artifacts remain recipe-owned.
"""

from __future__ import annotations

import os
from pathlib import Path
import uuid

from safety import Error, atomic_json, command, fingerprint, load, no_symlink_parents, run
import providers
import services


def recover_services(context, identifiers=()):
    state = Path(context["paths"]["state"])
    journal_path = state / "service-journal.json"
    if not journal_path.exists():
        raise Error("no recorded service transition to recover")
    journal = load(journal_path)
    if journal.get("home") != context["home"]:
        raise Error("service journal belongs to another home")
    selected = set(identifiers) or {
        key
        for key, value in journal["services"].items()
        if value["status"] in ("pending", "committed")
    }
    if not selected or selected - journal["services"].keys():
        raise Error("no matching recoverable service transition")
    records_path = state / "services.json"
    records = (
        load(records_path) if records_path.exists() else {"home": context["home"], "services": {}}
    )
    if records.get("home") != context["home"]:
        raise Error("service record belongs to another home")
    prepared = []
    for identifier in selected:
        transition = journal["services"][identifier]
        if transition["status"] not in ("pending", "committed"):
            raise Error(f"service transition already recovered: {identifier}")
        service = transition["selection"]
        target = services.descriptor(context, service)
        if fingerprint(target) != transition["descriptor"]:
            raise Error(f"service recovery preserves changed descriptor: {target}")
        observed = services.observe(context, service)
        before, after = transition["before"], transition.get("after")
        if after is not None and observed not in (before, after):
            raise Error(f"service changed outside recorded transition: {identifier}")
        if after is None:
            # An interrupted command may have either side of its one transition.
            expected = dict(before)
            action = transition["action"]
            if service["manager"].startswith("systemd-"):
                if action in ("start", "restart"):
                    expected["ActiveState"] = "active"
                elif action == "stop":
                    expected["ActiveState"] = "inactive"
                elif action == "enable":
                    expected["UnitFileState"] = "enabled"
                elif action == "disable":
                    expected["UnitFileState"] = "disabled"
            else:
                if action in ("start", "restart"):
                    expected["loaded"] = True
                elif action in ("stop", "disable"):
                    expected["loaded"] = False
                if action in ("enable", "disable"):
                    expected["disabled"] = action == "disable"
            if observed not in (before, expected):
                raise Error(f"service recovery cannot attribute current state: {identifier}")
        executable = (
            services.system_command(context, service)
            if service["manager"].startswith("systemd-")
            else command(context, "launchctl", "/bin/launchctl", mutating=True)
        )
        prepared.append((identifier, transition, target, executable))
    results = {}
    for identifier, transition, target, executable in prepared:
        service, before = transition["selection"], transition["before"]
        if service["manager"].startswith("systemd-"):
            current = services.observe(context, service)
            enabled = before.get("UnitFileState") in ("enabled", "enabled-runtime")
            if current.get("UnitFileState") != before.get("UnitFileState") and before.get(
                "UnitFileState"
            ) not in ("static", "indirect", "generated", "transient"):
                runtime = (
                    ["--runtime"]
                    if "enabled-runtime"
                    in (before.get("UnitFileState"), current.get("UnitFileState"))
                    else []
                )
                run(
                    [*executable, *runtime, "enable" if enabled else "disable", service["name"]],
                    context,
                )
            if (current.get("ActiveState") == "active") != (before.get("ActiveState") == "active"):
                run(
                    [
                        *executable,
                        "start" if before.get("ActiveState") == "active" else "stop",
                        service["name"],
                    ],
                    context,
                )
        else:
            domain = f"gui/{os.getuid()}"
            label = f"{domain}/{service['name']}"
            run([executable, "disable" if before.get("disabled") else "enable", label], context)
            observed = services.observe(context, service)
            if before.get("loaded"):
                if not observed.get("loaded"):
                    run([executable, "bootstrap", domain, target], context)
                run([executable, "kickstart", label], context)
            elif observed.get("loaded"):
                run([executable, "bootout", label], context)
        observed = services.observe(context, service)
        if service["manager"].startswith("systemd-"):
            if (observed.get("ActiveState") == "active") != (before.get("ActiveState") == "active"):
                raise Error("service recovery active-selection readback failed")
            if (observed.get("UnitFileState") in ("enabled", "enabled-runtime")) != (
                before.get("UnitFileState") in ("enabled", "enabled-runtime")
            ):
                raise Error("service recovery enable-selection readback failed")
        elif observed != before:
            raise Error("launchd recovery selection readback failed")
        previous = transition["previous_record"]
        if previous is None:
            records["services"].pop(identifier, None)
        else:
            records["services"][identifier] = previous
        transition["status"] = "recovered"
        transition["recovered_observation"] = observed
        atomic_json(records_path, records)
        atomic_json(journal_path, journal)
        results[identifier] = {
            "selection": observed,
            "scope": "enabled/active selection only; process/session data is not transactional",
        }
    return results


def recover_links(context, records, selected_prefixes):
    journal_path = Path(context["paths"]["state"]) / "links-journal.json"
    if not journal_path.exists():
        return []
    journal = load(journal_path)
    if journal.get("home") != context["home"]:
        raise Error("link journal belongs to another home")
    selected = []
    for name, transition in journal.get("links", {}).items():
        after_target = Path(transition["after"]["target"])
        if not any(after_target.is_relative_to(prefix) for prefix in selected_prefixes):
            continue
        if transition.get("status") not in ("pending", "applied"):
            continue
        target = Path(name)
        no_symlink_parents(target, Path(context["home"]))
        observed = fingerprint(target)
        if observed not in (transition["before"], transition["after"]):
            raise Error(f"link recovery preserves foreign replacement: {target}")
        selected.append((target, transition))
    for target, transition in selected:
        before = transition["before"]
        if before is None:
            if fingerprint(target) is not None:
                target.unlink()
            records.setdefault("links", {}).pop(str(target), None)
        else:
            if before.get("kind") != "symlink":
                raise Error("artifact link journal cannot restore an arbitrary file")
            temporary = target.with_name(target.name + ".recover-" + uuid.uuid4().hex)
            temporary.symlink_to(before["target"])
            os.replace(temporary, target)
            records.setdefault("links", {})[str(target)] = before
        transition["status"] = "recovered"
        temporary = target.with_name(target.name + ".dotfiles-candidate")
        if temporary.is_symlink() and fingerprint(temporary) == transition["after"]:
            temporary.unlink()
    atomic_json(journal_path, journal)
    return [str(target) for target, _ in selected]


def recover_packages(context, context_path, identifiers):
    if not identifiers:
        raise Error("package recovery requires explicit --id")
    state = Path(context["paths"]["state"])
    path = state / "packages.json"
    records = (
        load(path) if path.exists() else {"home": context["home"], "packages": {}, "links": {}}
    )
    if records.get("home") != context["home"]:
        raise Error("package record belongs to another home")
    records["_state_dir"] = context["paths"]["state"]
    selected = {p["id"]: p for p in context["packages"]}
    output = {}
    for identifier in identifiers:
        if identifier in records.get("retained_shared_runtime", {}):
            output[identifier] = {
                "status": "retained-shared-runtime",
                "restored": False,
                "reason": "retirement did not uninstall/stop this shared runtime or remove its data; nothing to recover",
            }
            continue
        retirement = records.get("retired", {}).get(identifier)
        package_journal_path = state / "package-journal.json"
        package_journal = load(package_journal_path) if package_journal_path.exists() else {}
        if (
            package_journal.get("home") == context["home"]
            and package_journal.get("id") == identifier
            and package_journal.get("action") == "retire"
            and package_journal.get("status") == "pending"
        ):
            retirement = package_journal["before"]
        if retirement and retirement["entry"]["selection"]["provider"] in (
            "artifact",
            "mise",
            "apt",
        ):
            entry = retirement["entry"]
            package = entry["selection"]
            if package["provider"] == "apt":
                version = entry["observed"]["installed"]
                providers.mutation_guard(context, package, context_path)
                if identifier in selected and selected[identifier] != package:
                    providers.mutation_guard(context, selected[identifier], context_path)
                providers.apt_transaction(
                    context,
                    [
                        "install",
                        "--yes",
                        "--allow-downgrades",
                        "--no-install-recommends",
                        "--",
                        package["name"] + "=" + version,
                    ],
                )
            observed = providers.package_status(context, package, context_path)
            if observed != entry["observed"]:
                raise Error(
                    f"retained package no longer matches its recorded payload: {identifier}"
                )
            for name, expected in {
                **retirement.get("links", {}),
                **retirement.get("shims", {}),
            }.items():
                target = Path(name)
                no_symlink_parents(target, Path(context["home"]))
                if fingerprint(target) not in (None, expected):
                    raise Error(f"retired package recovery preserves foreign replacement: {target}")
            for name, expected in retirement.get("links", {}).items():
                providers.owned_link(Path(name), expected["target"], records)
            for name, expected in retirement.get("shims", {}).items():
                target = Path(name)
                if fingerprint(target) is None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + ".recover-" + uuid.uuid4().hex)
                    temporary.symlink_to(expected["target"])
                    os.replace(temporary, target)
                records.setdefault("shims", {})[target.name] = expected
            records["packages"][identifier] = entry
            records.get("retired", {}).pop(identifier, None)
            atomic_json(path, records)
            if package_journal.get("id") == identifier:
                package_journal["status"] = "recovered"
                atomic_json(package_journal_path, package_journal)
            output[identifier] = {"restored_owned_exposure": True, "observed": observed}
            continue
        current = records["packages"].get(identifier)
        package = current["selection"] if current else selected.get(identifier)
        if package is None:
            raise Error(f"unknown/unowned package recovery: {identifier}")
        if package["provider"] == "recipe":
            # Recipes explicitly advertise recover via their action metadata.
            if "recover" not in package.get("privileged_actions", []) and not package.get(
                "recoverable"
            ):
                raise Error(
                    f"recipe recovery is its documented provision/retire transaction; no generic rollback is claimed: {identifier}"
                )
            output[identifier] = providers.recipe(context, package, "recover", context_path)
            continue
        history = records.get("history", {}).get(identifier, [])
        if not history:
            prefixes = (
                [providers.prefix(context, package)] if package["provider"] == "artifact" else []
            )
            recovered = recover_links(context, records, prefixes)
            if not recovered:
                raise Error(
                    f"no retained package selection or interrupted owned links: {identifier}"
                )
            output[identifier] = {"recovered_links": recovered, "retained_payload": "not deleted"}
            atomic_json(path, records)
            continue
        previous = history[-1]
        previous_package = previous["selection"]
        observed = providers.package_status(context, package, context_path)
        if current and observed != current["observed"]:
            raise Error(f"package recovery preserves changed installation: {identifier}")
        provider = previous_package["provider"]
        if provider == "artifact":
            providers.install_artifact(context, previous_package, records)
        elif provider == "apt":
            version = previous["observed"]["installed"]
            providers.mutation_guard(context, package, context_path)
            if previous_package != package:
                providers.mutation_guard(context, previous_package, context_path)
            providers.apt_transaction(
                context,
                [
                    "install",
                    "--yes",
                    "--allow-downgrades",
                    "--no-install-recommends",
                    "--",
                    previous_package["name"] + "=" + version,
                ],
            )
        elif provider == "mise":
            # Retained versioned installations are verified; recovery never
            # fabricates a download/pin for a missing old runtime.
            restored = providers.package_status(context, previous_package, context_path)
            for name, executable in restored["executables"].items():
                if previous_package["tool"] not in ("cidr", "terraform", "helmfile"):
                    providers.owned_link(Path(context["paths"]["bin"]) / name, executable, records)
        else:
            raise Error(
                "Homebrew rollback is not transactional without a retained exact bottle; preserve current installation and restore reviewed provider/source selection explicitly"
            )
        restored = providers.package_status(context, previous_package, context_path)
        if restored != previous["observed"]:
            raise Error(f"recovered package readback differs from retained selection: {identifier}")
        records.setdefault("recovery_retained", {}).setdefault(identifier, []).append(current)
        records["packages"][identifier] = history.pop()
        atomic_json(path, records)
        output[identifier] = {"selection": previous_package, "observed": restored}
    return output
