#!/usr/bin/env python3
"""Portable dotfiles engine. Run ./bootstrap --help for the operational contract.

Configuration, packages and services are separate explicit operations. Local
source/context overrides are solely for isolated verification. Public source is
resolved by the private shell entry from bootstrap.lock.json, never a discovered
checkout. This module does not invoke Nix or decrypt credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tarfile
import uuid


def verify_cached_source():
    """Compare all cached bytes before importing any repository Python module."""
    base = Path(__file__).resolve().parents[2]
    archive = base / ".bootstrap-source.tar"
    if not archive.exists():
        return
    expected = set()
    try:
        with tarfile.open(archive, "r:") as stream:
            for member in stream.getmembers():
                name = member.name.removeprefix("./").rstrip("/")
                if not name:
                    continue
                parts = Path(name).parts
                if Path(name).is_absolute() or ".." in parts:
                    raise ValueError("source archive contains an unsafe path")
                path = base / name
                if member.isdir():
                    if path.is_symlink() or not path.is_dir():
                        raise ValueError(f"source directory changed: {name}")
                    continue
                expected.add(name)
                if member.issym():
                    if (
                        not path.is_symlink()
                        or os.readlink(path) != member.linkname
                        or not path.resolve().is_relative_to(base)
                    ):
                        raise ValueError(f"source symlink changed/escaped: {name}")
                elif member.isfile():
                    if path.is_symlink() or not path.is_file():
                        raise ValueError(f"source file changed type: {name}")
                    with path.open("rb") as actual, stream.extractfile(member) as committed:
                        if (
                            hashlib.file_digest(actual, "sha256").digest()
                            != hashlib.file_digest(committed, "sha256").digest()
                        ):
                            raise ValueError(f"immutable source file changed: {name}")
                else:
                    raise ValueError("source archive contains unsupported member")
        actual = {
            path.relative_to(base).as_posix()
            for path in base.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        actual.discard(".bootstrap-source.tar")
        actual.discard(".bootstrap-receipt.json")
        if actual != expected:
            raise ValueError("immutable source cache contains missing/foreign files")
    except (OSError, ValueError, tarfile.TarError) as error:
        print(f"dotfiles: {error}", file=sys.stderr)
        raise SystemExit(2)


verify_cached_source()
sys.dont_write_bytecode = True

# -I prevents caller/project imports. Only this immutable engine directory is added.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import configuration
from manifest import PLATFORMS, compose, platform
import providers
from safety import Error, atomic_json, fingerprint, load, locked, native, no_symlink_parents, run
import services
import updates

MUTATIONS = {"bootstrap", "apply", "provision", "update", "retire", "recover"}


def parser():
    result = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    result.add_argument(
        "action",
        nargs="?",
        default="plan",
        choices=(
            "bootstrap",
            "plan",
            "render",
            "apply",
            "provision",
            "updates",
            "update",
            "verify",
            "services",
            "retire",
            "recover",
            "doctor",
            "desktop",
            "work",
            "source",
            "lifecycle",
            "native-host",
        ),
    )
    result.add_argument("service_action", nargs="?", metavar="OPERATION")
    result.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    result.add_argument("--base", type=Path, default=Path(__file__).resolve().parents[2])
    result.add_argument("--home", type=Path, default=Path.home())
    result.add_argument("--platform", choices=PLATFORMS)
    result.add_argument(
        "--set", dest="flags", action="append", default=[], metavar="FEATURE=true|false"
    )
    result.add_argument(
        "--bootstrap-source", type=Path, help="explicit mutable public source, verification only"
    )
    result.add_argument(
        "--verification-only",
        action="store_true",
        help="allow local source/fixture context against isolated home",
    )
    result.add_argument(
        "--fixture-context", type=Path, help="verification-only isolated executable/context data"
    )
    result.add_argument(
        "--yes", action="store_true", help="explicitly authorize the selected mutating operation"
    )
    result.add_argument("--output", type=Path, help="absent/empty render staging directory")
    result.add_argument(
        "--id", action="append", default=[], help="select package/service ids (repeatable)"
    )
    result.add_argument(
        "--refresh",
        action="store_true",
        help="inspect public latest-stable metadata without changing pins",
    )
    result.add_argument("--candidate", type=Path, help="reviewed update bundle JSON; stages only")
    result.add_argument(
        "--promote",
        metavar="CANDIDATE_ID",
        help="promote an already validated candidate's manifest/lock bytes",
    )
    result.add_argument(
        "--packages",
        action="store_true",
        help="operate on explicitly named package ownership/recovery, not configuration",
    )
    result.add_argument(
        "--state-dir",
        type=Path,
        help="recover only: explicit home-contained state directory when desired manifests are broken",
    )
    result.add_argument("--require-private-site", action="store_true", help=argparse.SUPPRESS)
    result.add_argument(
        "--verification-tools", action="store_true", help="bootstrap internal verification tools"
    )
    result.add_argument(
        "--checks",
        action="store_true",
        help="verify repository source checks instead of installed state",
    )
    result.add_argument(
        "--files-from", type=Path, help="JSON source-file union for repository checks"
    )
    result.add_argument(
        "--no-tests",
        action="store_true",
        help="omit behavior suites from explicit repository checks",
    )
    result.add_argument(
        "--evidence", type=Path, help="explicit lifecycle ownership and acceptance evidence"
    )
    result.add_argument("--family", choices=("mise", "desktop", "all"))
    result.add_argument("--message", help="signed source publication commit message")
    result.add_argument("--branch", help="explicit public source publication branch")
    result.add_argument(
        "--candidate-output", type=Path, help="managed/versioned desktop source-lock output"
    )
    for name in ("qt-version", "rust-version", "wayland-version", "protocols-version"):
        result.add_argument("--" + name)
    result.add_argument("--ack-desktop-consent", action="store_true")
    result.add_argument("--ack-root-equivalent", action="store_true")
    result.add_argument("--uid", type=int)
    result.add_argument("--username")
    return result


ROUTES = {
    "desktop": ("status", "inspect", "acquire", "candidate", "recover"),
    "work": ("docker-status", "docker-start", "docker-access"),
    "source": ("status", "prepare", "check", "publish", "recover"),
    "lifecycle": ("inspect", "shell", "handoff", "rescue", "recover", "retire-nix"),
    "native-host": ("status", "provision", "clt-request"),
    "services": services.ACTIONS,
}


def validate_arguments(arguments):
    action = arguments.action
    operation = arguments.service_action
    if action in ROUTES:
        operation = operation or ROUTES[action][0]
        if operation not in ROUTES[action]:
            raise Error(
                f"invalid {action} operation: {operation}; expected {', '.join(ROUTES[action])}"
            )
    elif operation is not None:
        raise Error(f"{action} does not accept an operation")
    arguments.service_action = operation or "status"
    permitted = {
        "verification_tools": action == "bootstrap",
        "checks": action == "verify",
        "files_from": action == "verify" and arguments.checks,
        "no_tests": action == "verify" and arguments.checks,
        "evidence": action == "lifecycle",
        "family": action == "source" and operation == "prepare",
        "message": action == "source" and operation == "publish",
        "branch": action == "source" and operation == "publish",
        "candidate_output": action == "desktop" and operation == "acquire",
        "qt_version": (action == "desktop" and operation == "acquire")
        or (action == "source" and operation == "prepare"),
        "rust_version": (action == "desktop" and operation == "acquire")
        or (action == "source" and operation == "prepare"),
        "wayland_version": (action == "desktop" and operation == "acquire")
        or (action == "source" and operation == "prepare"),
        "protocols_version": (action == "desktop" and operation == "acquire")
        or (action == "source" and operation == "prepare"),
        "ack_desktop_consent": action == "work" and operation == "docker-start",
        "ack_root_equivalent": action == "work" and operation == "docker-access",
        "uid": action == "work" and operation == "docker-access",
        "username": action == "work" and operation == "docker-access",
        "candidate": action in ("update", "recover", "desktop"),
        "promote": action == "update",
        "packages": action in ("retire", "recover"),
        "refresh": action == "updates",
        "output": action in ("render", "apply")
        or (action == "desktop" and operation == "candidate"),
    }
    for name, allowed in permitted.items():
        value = getattr(arguments, name)
        if value is not None and value is not False and not allowed:
            raise Error(
                f"--{name.replace('_', '-')} is not valid for {action} {operation or ''}".rstrip()
            )
    if action in ("source", "lifecycle", "native-host", "work") and arguments.id:
        raise Error(f"{action} does not accept --id")
    if action == "desktop" and len(arguments.id) > 1:
        raise Error("desktop selects exactly one --id")
    if action == "verify" and arguments.checks and arguments.id:
        raise Error("repository checks select source files, not package --id")
    if action == "recover" and arguments.packages and arguments.candidate:
        raise Error("choose package recovery or source candidate recovery, not both")
    if action == "update" and bool(arguments.candidate) == bool(arguments.promote):
        raise Error("update requires exactly one of --candidate BUNDLE or --promote ID")
    if action == "desktop" and operation == "candidate" and not arguments.candidate:
        raise Error("desktop candidate requires a complete --candidate source lock")
    if (
        action == "source"
        and arguments.family == "mise"
        and any(
            getattr(arguments, name) is not None
            for name in ("qt_version", "rust_version", "wayland_version", "protocols_version")
        )
    ):
        raise Error("desktop toolchain overrides require --family desktop or all")
    return (
        action in MUTATIONS
        or (action == "services" and operation != "status")
        or (action in ROUTES and operation not in ("status", "inspect", "check", "docker-status"))
    )


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def directory_metadata(name):
    return name in ("java_home", "ghidra-home") or name.endswith("_home")


def isolated(arguments):
    mutation = validate_arguments(arguments)
    if arguments.bootstrap_source and not arguments.verification_only:
        raise Error("--bootstrap-source requires --verification-only")
    if arguments.fixture_context and not arguments.verification_only:
        raise Error(
            "--fixture-context is verification-only; production never inherits executable overrides"
        )
    if (
        arguments.base.resolve() != Path(__file__).resolve().parents[2]
        and not arguments.verification_only
    ):
        raise Error(
            "--base cannot bypass immutable source resolution; use explicit --bootstrap-source --verification-only"
        )
    if arguments.verification_only:
        selected_home = arguments.home.resolve()
        real_home = Path.home().resolve()
        if selected_home == real_home or real_home.is_relative_to(selected_home):
            raise Error("verification requires an explicit disposable --home, not real HOME")
        if arguments.home.is_symlink():
            raise Error("verification home cannot be a symlink")
    # Validation and authorization precede any staging, provider or hook call.
    if (
        arguments.platform
        and mutation
        and not arguments.verification_only
        and arguments.platform != platform()
    ):
        raise Error("cannot mutate a different platform; render/plan it instead")
    # An isolated HOME is not authority to run real systemctl/sudo. The service
    # controller separately requires explicit home-contained fixture executables.
    if mutation and not arguments.yes:
        raise Error(
            f"{arguments.action} requires --yes; plan/render/updates/doctor are read-only with respect to live configuration"
        )
    return mutation


def context_file(context):
    directory = Path(context["paths"]["cache"]) / "contexts"
    directory.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix="context-", suffix=".json", dir=directory)
    os.close(fd)
    atomic_json(filename, context)
    return Path(filename)


def require_stopped_services(context):
    path = Path(context["paths"]["state"]) / "services.json"
    if not path.exists():
        return
    records = load(path)
    for identifier, record in records.get("services", {}).items():
        if record["selection"].get("existing_unit"):
            continue  # Shared distro daemon selection is not package/config ownership.
        observed = services.observe(context, record["selection"])
        if (
            observed.get("loaded")
            or observed.get("ActiveState") == "active"
            or observed.get("UnitFileState") in ("enabled", "enabled-runtime")
        ):
            raise Error(
                f"stop and disable owned service explicitly before retiring configuration/packages: {identifier}"
            )


def retire_packages(context, path, identifiers):
    if not identifiers:
        raise Error("package retirement requires explicit --id; no purge/autoremove is supported")
    record_path = Path(context["paths"]["state"]) / "packages.json"
    if not record_path.exists():
        raise Error("no owned package records")
    record = load(record_path)
    if record.get("home") != context["home"]:
        raise Error("package ownership belongs to another home")
    retained_shared = {}
    retiring = []
    for identifier in identifiers:
        entry = record.get("packages", {}).get(identifier)
        if entry is None:
            raise Error(f"refusing to remove unowned package: {identifier}")
        if providers.shared_runtime(entry["selection"], entry, context):
            retained_shared[identifier] = {
                "status": "retained-shared-runtime",
                "reason": "shared backend/package, activation and all application/user data preserved; no uninstall/stop/cleanup performed",
            }
        else:
            retiring.append(identifier)
    if retiring:
        require_stopped_services(context)
    for identifier in retiring:
        if identifier not in record.get("packages", {}):
            raise Error(f"refusing to remove unowned package: {identifier}")
        entry = record["packages"][identifier]
        if entry.get("preexisting"):
            raise Error(f"package predates engine ownership; preserved: {identifier}")
        package = entry["selection"]
        observed = providers.package_status(context, package, path)
        if observed != entry["observed"]:
            raise Error(f"package changed since provision; preserved: {identifier}")
        providers.mutation_guard(context, package, path)
        if package["provider"] == "apt":
            providers.apt_removal_plan(context, package)
        if package["provider"] in ("artifact", "mise"):
            prefix = (
                providers.prefix(context, package)
                if package["provider"] == "artifact"
                else Path(observed["prefix"])
            )
            for name, expected in record.get("links", {}).items():
                if (
                    Path(expected["target"]).is_relative_to(prefix)
                    and fingerprint(name) != expected
                ):
                    raise Error(f"foreign replacement blocks package retirement: {name}")
    for identifier in retiring:
        entry = record["packages"][identifier]
        package = entry["selection"]
        provider = package["provider"]
        retained = {"entry": entry, "links": {}, "shims": {}}
        if provider in ("artifact", "mise"):
            prefix = (
                providers.prefix(context, package)
                if provider == "artifact"
                else Path(entry["observed"]["prefix"])
            )
            retained["links"] = {
                name: expected
                for name, expected in record.get("links", {}).items()
                if Path(expected["target"]).is_relative_to(prefix)
            }
            if provider == "mise":
                shims = Path(context["paths"]["data"]).parent / "mise-shims"
                for name in package.get("executables", {}):
                    expected = record.get("shims", {}).get(name)
                    if expected:
                        if fingerprint(shims / name) != expected:
                            raise Error(f"foreign shim replacement blocks retirement: {name}")
                        retained["shims"][str(shims / name)] = expected
        journal_path = Path(context["paths"]["state"]) / "package-journal.json"
        transition = {
            "home": context["home"],
            "id": identifier,
            "action": "retire",
            "status": "pending",
            "before": retained,
        }
        atomic_json(journal_path, transition)
        if provider == "recipe":
            providers.recipe(context, package, "retire", path)
        elif provider in ("artifact", "mise"):
            # Retire owned exposure, retaining immutable payload for rescue and
            # explicit project version requirements. Never prune user runtimes.
            for name in retained["links"]:
                Path(name).unlink()
                del record["links"][name]
            for name in retained["shims"]:
                Path(name).unlink()
                record["shims"].pop(Path(name).name, None)
        elif provider == "apt":
            providers.apt_transaction(
                context, ["remove", "--yes", "--no-auto-remove", "--", package["name"]]
            )
        elif provider in ("brew", "cask"):
            run(
                [
                    providers.command(context, "brew", "/opt/homebrew/bin/brew", mutating=True),
                    "uninstall",
                    "--cask" if provider == "cask" else "--formula",
                    "--",
                    package["name"],
                ],
                context,
                capture=False,
            )
        record.setdefault("retired", {})[identifier] = retained
        del record["packages"][identifier]
        atomic_json(record_path, record)
        transition["status"] = "committed"
        atomic_json(journal_path, transition)
    if retained_shared:
        record.setdefault("retained_shared_runtime", {}).update(retained_shared)
        atomic_json(record_path, record)
    return {
        "retired_packages": retiring,
        "retained_shared_runtime": retained_shared,
        "versioned_payloads": "retained for owned artifact/mise recovery; shared native runtimes are never uninstalled; other APT/Homebrew removals are provider operations, not a broad purge",
    }


def doctor(context, arguments):
    result = {
        "platform": context["platform"],
        "source": {"base": context["base"], "verification_only": arguments.verification_only},
        "prerequisites": {},
        "manual_prerequisites": [],
        "mutations_performed": False,
    }
    lock_path = Path(context["root"]) / "bootstrap.lock.json"
    if lock_path.exists():
        lock = load(lock_path)
        result["source"]["publication"] = lock.get("status", "unknown")
        if lock.get("status") != "published":
            result["source"]["blocker"] = (
                "No published immutable native source pin; local verification requires explicit override."
            )
    for name, executable in context["executables"].items():
        try:
            if directory_metadata(name):
                if not Path(executable).is_dir() or "/nix/" in str(Path(executable).resolve()):
                    raise Error("directory prerequisite unavailable/non-native")
            else:
                native(executable)
            result["prerequisites"][name] = {"path": executable, "available": True}
        except Error as exc:
            result["prerequisites"][name] = {
                "path": executable,
                "available": False,
                "error": str(exc),
            }
    if context["platform"] == "macos-arm64":
        try:
            selected = run(["/usr/bin/xcode-select", "-p"], context).strip()
            result["prerequisites"]["apple-command-line-tools"] = {
                "available": bool(selected),
                "path": selected,
            }
        except (Error, OSError) as error:
            result["prerequisites"]["apple-command-line-tools"] = {
                "available": False,
                "error": str(error),
            }
            result["manual_prerequisites"].append(
                "Install Apple Command Line Tools and accept required Apple license/OS consent explicitly before native builds; doctor never installs them."
            )
        result["manual_prerequisites"].append(
            "Accessibility/TCC consent and app/login-session acceptance require explicit OS/user action; not bypassed or inferred."
        )
    else:
        result["manual_prerequisites"].append(
            "GPU/session/portal/audio/lock/suspend/hardware acceptance must run on the actual Ubuntu 24.04 device."
        )
    result["manual_prerequisites"].append(
        "Secret identities, keyrings, provider authentication and signing remain machine-local and are never read by doctor."
    )
    return result


def extended_action(context, path, arguments):
    action, operation = arguments.action, arguments.service_action
    if action == "source":
        import source_workflow

        options = {
            name: getattr(arguments, name)
            for name in (
                "family",
                "qt_version",
                "rust_version",
                "wayland_version",
                "protocols_version",
                "message",
                "branch",
            )
            if getattr(arguments, name) is not None
        }
        if operation != "status":
            options["yes"] = arguments.yes
        return source_workflow.run(context, operation, options)
    if action == "native-host":
        import native_host

        return native_host.dispatch(context, operation, authorize=arguments.yes)
    if action == "lifecycle":
        result = configuration.site(context, "lifecycle", path)
        if result is None:
            raise Error("lifecycle requires the private site adapter")
        return result
    if action == "work":
        recipe = Path(context["root"]) / "native/recipes/docker.py"
        if context["root"] == context["base"] or not recipe.is_file() or recipe.is_symlink():
            raise Error("work Docker operations require the private declared recipe")
        argv = [
            native(sys.executable),
            "-I",
            "-B",
            recipe,
            operation.removeprefix("docker-"),
            "--context",
            path,
        ]
        for name in ("yes", "ack_desktop_consent", "ack_root_equivalent"):
            if getattr(arguments, name):
                argv.append("--" + name.replace("_", "-"))
        for name in ("uid", "username"):
            if getattr(arguments, name) is not None:
                argv.extend(["--" + name, str(getattr(arguments, name))])
        return json.loads(run(argv, context))
    if action == "desktop":
        identifier = arguments.id[0] if arguments.id else "niri"
        if identifier not in (
            "niri",
            "xwayland-satellite",
            "dux-runtime",
            "hyprland-ppa",
            "nordic-theme",
            "meslolgs-nf",
            "jetbrains-mono-nf",
            "nordzy-cursors",
        ):
            raise Error(f"unknown desktop package: {identifier}")
        if operation in ("acquire", "candidate") and identifier not in (
            "niri",
            "xwayland-satellite",
            "dux-runtime",
        ):
            raise Error(
                "coherent desktop source acquisition/candidate requires a source-built desktop id"
            )
        if operation == "candidate" and not arguments.candidate:
            raise Error("desktop candidate requires a complete --candidate source lock")
        candidate_output = arguments.candidate_output
        if operation == "acquire" and candidate_output is None:
            candidate_output = (
                Path(context["paths"]["state"])
                / "source-update/candidates"
                / ("sources-" + uuid.uuid4().hex + ".json")
            )
        for target in (candidate_output, arguments.output):
            if target is None:
                continue
            target = target.absolute()
            # No operation may mutate an authenticated immutable source tree.
            if (
                Path(context["base"]) / ".bootstrap-source.tar"
            ).exists() and target.resolve().is_relative_to(Path(context["base"])):
                raise Error(
                    "desktop output cannot mutate the immutable source cache; use source prepare"
                )
            if target.is_symlink():
                raise Error("desktop output cannot be a foreign symlink")
        if candidate_output is not None:
            permitted = (Path(context["root"]), Path(context["paths"]["state"]) / "source-update")
            if not any(candidate_output.absolute().is_relative_to(root) for root in permitted):
                raise Error(
                    "desktop source lock output must be in the owning repository or managed source-update workspace"
                )
            no_symlink_parents(
                candidate_output.absolute(),
                next(
                    root for root in permitted if candidate_output.absolute().is_relative_to(root)
                ),
            )
        argv = [
            native(sys.executable),
            "-I",
            "-B",
            Path(context["base"]) / "native/recipes/desktop/desktop.py",
            operation,
            "--context",
            path,
            "--id",
            identifier,
        ]
        for name, value in (
            ("output", arguments.output),
            ("candidate", arguments.candidate),
            ("candidate-output", candidate_output),
            ("qt-version", arguments.qt_version),
            ("rust-version", arguments.rust_version),
            ("wayland-version", arguments.wayland_version),
            ("protocols-version", arguments.protocols_version),
        ):
            if value is not None:
                argv.extend(["--" + name, str(value)])
        return json.loads(run(argv, context))
    raise Error(f"unsupported root action: {action}")


def repository_verification(context, arguments):
    verifier = load_module(
        Path(context["base"]) / "native/verification/verify.py", "dotfiles_verifier"
    )
    files = load(arguments.files_from) if arguments.files_from else None
    if files is not None and (
        not isinstance(files, list) or any(not isinstance(name, str) for name in files)
    ):
        raise Error("--files-from requires a JSON array of relative source filenames")
    result = {
        "private" if context["root"] != context["base"] else "public": verifier.check(
            context, files, not arguments.no_tests
        )
    }
    if context["root"] != context["base"]:
        public = copy.deepcopy(context)
        public["root"] = context["base"]
        result["public"] = verifier.check(public, None, not arguments.no_tests)
    failed = [
        f"{scope}:{stage['name']}: {stage['detail']}"
        for scope, stages in result.items()
        for stage in stages
        if stage["status"] == "failed"
    ]
    if failed:
        raise Error("repository verification failed:\\n" + "\\n".join(failed))
    return result


def execute(arguments):
    mutation = isolated(arguments)
    if mutation and not arguments.verification_only and platform() == "linux-x64":
        release = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                release[key] = value.strip("\"'")
        if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
            raise Error(
                "native Linux mutations require Ubuntu 24.04 LTS; plan/render remain available"
            )
    base = (
        arguments.bootstrap_source.resolve()
        if arguments.bootstrap_source
        else arguments.base.resolve()
    )
    if arguments.action == "recover":
        selected_home = arguments.home.resolve()
        state = (
            arguments.state_dir.resolve()
            if arguments.state_dir
            else selected_home / ".local/state/dotfiles"
        )
        if not state.is_relative_to(selected_home):
            raise Error("recovery state must be within selected home")
        no_symlink_parents(state / "context.json", selected_home)
        active = state / "context.json"
        journal_path = state / "journal.json"
        pending_context = None
        if not arguments.packages and not arguments.candidate and journal_path.exists():
            pending_context = load(journal_path).get("context")
        if pending_context is not None or active.exists():
            context = pending_context if pending_context is not None else load(active)
            if context.get("home") != str(selected_home) or context.get("root") != str(
                arguments.root.resolve()
            ):
                raise Error("recovery context belongs to another checkout/home")
        else:
            data = selected_home / ".local/share/dotfiles"
            context = {
                "schema_version": 1,
                "root": str(arguments.root.resolve()),
                "base": str(base),
                "home": str(selected_home),
                "platform": arguments.platform or platform(),
                "dotfiles": {},
                "identity": {},
                "selectors": {},
                "paths": {
                    "state": str(state),
                    "cache": str(selected_home / ".cache/dotfiles"),
                    "data": str(data),
                    "config": str(selected_home / ".config"),
                    "bin": str(selected_home / ".local/bin"),
                    "runtime": str(data / "runtime"),
                    "completions": str(data / "completions"),
                },
                "executables": {"python3": native(sys.executable)},
                "packages": [],
                "services": [],
            }
        context["paths"]["state"] = str(state)
        # Recovery executes the currently authenticated source, never Python
        # modules chosen by a mutable historical context's base path.
        context["base"] = str(base)
    else:
        if arguments.state_dir:
            raise Error("--state-dir is only accepted for narrow recovery")
        context = compose(
            base,
            arguments.root,
            arguments.home,
            arguments.platform,
            arguments.flags,
            arguments.fixture_context,
        )
    context["_operation"] = {
        "action": arguments.action,
        "service_action": arguments.service_action,
        "ids": arguments.id,
        "operation": arguments.service_action,
        "evidence": str(arguments.evidence.absolute()) if arguments.evidence else None,
        "authorize": arguments.yes,
    }
    context["_verification_only"] = arguments.verification_only
    context["_fixture_context"] = bool(arguments.fixture_context)
    context["_require_site"] = arguments.require_private_site or context.get("_require_site", False)
    if arguments.action != "recover" and context["root"] != context["base"]:
        with tempfile.NamedTemporaryFile(
            prefix="dotfiles-site-context-", suffix=".json", dir="/tmp"
        ) as temporary:
            atomic_json(temporary.name, context)
            configured = configuration.site(context, "context", temporary.name)
            if configured is not None:
                context = configured
    home = Path(context["home"])
    for name in ("state", "data", "cache", "config", "bin", "runtime", "completions"):
        no_symlink_parents(Path(context["paths"][name]) / ".boundary", home)
    action = arguments.action
    if action == "plan":
        return {
            "context": context,
            "operations": {
                "apply": "configuration only; no package install, decryption or service activation",
                "provision": [package["id"] for package in context["packages"]],
                "services": [service["id"] for service in context["services"]],
            },
            "live_mutations_performed": False,
        }
    if action == "doctor":
        return doctor(context, arguments)
    # Render also takes the same operation lock to prevent simultaneous staged
    # source/data generation against an in-flight configuration transaction.
    with locked(context["paths"]["state"]):
        if action == "bootstrap":
            result = {"prerequisites": providers.prerequisites(context)}
            if arguments.verification_tools:
                verifier = load_module(
                    Path(context["base"]) / "native/verification/verify.py", "dotfiles_verifier"
                )
                host = verifier.host_context(context)
                host["packages"] = verifier.host_prerequisites(host)
                record_path = Path(context["paths"]["state"]) / "packages.json"
                records = (
                    load(record_path)
                    if record_path.exists()
                    else {"schema_version": 1, "home": context["home"], "packages": {}, "links": {}}
                )
                host_path = context_file(host)
                try:
                    result["verification_prerequisites"] = providers.provision(
                        host, host_path, records, [p["id"] for p in host["packages"]]
                    )
                finally:
                    host_path.unlink(missing_ok=True)
                result["verification_tools"] = verifier.provision(host)
            return result
        if action == "render":
            staged = configuration.stage(context, arguments.output)
            return {
                "stage": str(staged),
                "render": load(staged / "render.json"),
                "live_mutations_performed": False,
            }
        if action == "apply":
            state_path = Path(context["paths"]["state"]) / "packages.json"
            installed = load(state_path) if state_path.exists() else {"packages": {}}
            for package in context["packages"]:
                if package["provider"] == "mise":
                    recorded = (
                        installed.get("packages", {}).get(package["id"], {}).get("observed", {})
                    )
                    expected = providers.mise_metadata(context, package)
                    if recorded.get("provenance", {}).get("selection") != expected["selection"]:
                        raise Error(
                            f"provision the selected locked native runtime before config apply: {package['id']}"
                        )
            for name, executable in context["executables"].items():
                if directory_metadata(name):
                    # Metadata is not an executable. Its feature owner validates
                    # existence only when that feature is actually enabled.
                    native(executable, exists=False)
                else:
                    native(executable)
            staged = configuration.stage(context, arguments.output)
            return configuration.apply(context, staged)
        path = context_file(context)
        try:
            if action in ("desktop", "work", "source", "lifecycle", "native-host"):
                return extended_action(context, path, arguments)
            if action == "verify" and arguments.checks:
                return repository_verification(context, arguments)
            if action == "provision":
                record_path = Path(context["paths"]["state"]) / "packages.json"
                records = (
                    load(record_path)
                    if record_path.exists()
                    else {"schema_version": 1, "home": context["home"], "packages": {}, "links": {}}
                )
                if records.get("home") != context["home"]:
                    raise Error("package ownership record belongs to another home")
                result = providers.provision(context, path, records, arguments.id)
                result["private"] = configuration.site(context, "provision", path)
                return result
            if action == "updates":
                return updates.report(context, path, refresh=arguments.refresh)
            if action == "update":
                return (
                    updates.stage_candidate(context, arguments.candidate)
                    if arguments.candidate
                    else updates.promote(context, arguments.promote)
                )
            if action == "verify":
                results = {"configuration": configuration.verify(context), "packages": {}}
                installed_path = Path(context["paths"]["state"]) / "packages.json"
                installed = load(installed_path) if installed_path.exists() else {"packages": {}}
                for package in context["packages"]:
                    if arguments.id and package["id"] not in arguments.id:
                        continue
                    observed = providers.package_status(context, package, path)
                    recorded = installed.get("packages", {}).get(package["id"])
                    if recorded is None or recorded.get("observed") != observed:
                        raise Error(
                            f"package lacks matching verified ownership/provenance: {package['id']}"
                        )
                    results["packages"][package["id"]] = observed
                results["services"] = services.control(
                    context,
                    "status",
                    arguments.id if arguments.id and not results["packages"] else (),
                )
                results["private"] = configuration.site(context, "verify", path)
                return results
            if action == "services":
                result = services.control(context, arguments.service_action, arguments.id)
                if arguments.service_action != "status":
                    configuration.site(context, "services", path)
                return result
            if action == "retire":
                if arguments.packages:
                    return retire_packages(context, path, arguments.id)
                require_stopped_services(context)
                configuration.collisions(context, {}, configuration.read_record(context))
                return configuration.apply(context, retire=True)
            if action == "recover":
                if arguments.candidate:
                    return updates.recover_candidate(context, arguments.candidate.name)
                if arguments.packages:
                    from recovery import recover_packages

                    require_stopped_services(context)
                    return recover_packages(context, path, arguments.id)
                result = configuration.recover(context)
                return result
        finally:
            path.unlink(missing_ok=True)
    raise Error(f"unhandled action: {action}")


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        result = execute(arguments)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (Error, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f"dotfiles: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
