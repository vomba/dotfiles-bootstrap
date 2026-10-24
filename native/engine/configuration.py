"""One chezmoi source/context, collision-aware file transactions, narrow recovery.

Chezmoi renders only into a disposable destination. Its hooks, encrypted leaves,
external downloads, remove/modify operations and source-root overrides are refused.
Live mutations use the rendered file inventory, never chezmoi's broad purge modes.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from safety import (
    Error,
    atomic_bytes,
    atomic_json,
    fingerprint,
    inside,
    inventory,
    load,
    native,
    no_symlink_parents,
    relative,
    run,
)

FORBIDDEN = ("run_", "run_once_", "run_onchange_", "modify_", "remove_", "encrypted_")


def site(context, action, context_path, source_dir=None, dry_run=False, transaction=None):
    module = Path(context["root"]) / "native/site.py"
    required = context.get("_require_site", False) or (
        context["root"] != context["base"]
        and (Path(context["root"]) / "native/manifests/ai.json").exists()
    )
    if context["root"] == context["base"]:
        return None
    if not module.is_file() or module.is_symlink():
        if required:
            raise Error(
                "required private native/site.py hook is missing or not a regular source file"
            )
        return None
    no_symlink_parents(module, Path(context["root"]))
    argv = [native(sys.executable), "-I", "-B", module, action, "--context", context_path]
    if source_dir is not None:
        argv.extend(["--source-dir", source_dir])
    if dry_run:
        argv.append("--dry-run")
    if transaction is not None:
        argv.extend(["--transaction", transaction])
    output = run(argv, context)
    try:
        return json.loads(output)
    except ValueError as exc:
        raise Error(f"private hook returned invalid JSON for {action}") from exc


def validate_source(root):
    root = Path(root)
    for path in root.rglob("*"):
        name = path.name
        if name.startswith(FORBIDDEN) or (
            name.startswith(".chezmoi") and name not in (".chezmoiignore", ".chezmoitemplates")
        ):
            raise Error(f"unsafe chezmoi hook/external/decryption source: {path}")
        if path.is_symlink():
            raise Error(f"source leaves must be authored files, not symlinks: {path}")
        if path.is_file() and b"/nix/store/" in path.read_bytes():
            raise Error(f"Nix runtime reference in authored configuration: {path}")


def copy_sources(origin, target, overrides, *, private=False):
    source = Path(origin) / "native/chezmoi"
    if not source.exists():
        return
    validate_source(source)
    for path in sorted(source.rglob("*")):
        relative_name = path.relative_to(source).as_posix()
        destination = target / relative_name
        if path.is_dir():
            if destination.exists() and not destination.is_dir():
                raise Error(f"source directory collision: {relative_name}")
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if destination.exists():
            if not private or relative_name not in overrides:
                raise Error(
                    f"unacknowledged public/private source collision: {relative_name}; declare overrides"
                )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def encoded(path):
    return Path(*(f"dot_{part[1:]}" if part.startswith(".") else part for part in path.parts))


def stage(context, output=None):
    cache = Path(context["paths"]["cache"]) / "stages"
    cache.mkdir(parents=True, exist_ok=True)
    if output:
        directory = Path(output).resolve()
        if (
            Path(output).is_symlink()
            or directory == Path(context["home"]).resolve()
            or Path(context["home"]).resolve().is_relative_to(directory)
        ):
            raise Error("render output cannot be a symlink, live home or home ancestor")
        if directory.exists() and any(directory.iterdir()):
            raise Error(f"render output must be absent or empty: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
    else:
        directory = Path(tempfile.mkdtemp(prefix="config-", dir=cache))
    source, rendered = directory / "source", directory / "home"
    source.mkdir()
    rendered.mkdir()
    context_path = directory / "context.json"
    atomic_json(context_path, context)
    copy_sources(context["base"], source, context.get("_overrides", []))
    if context["root"] != context["base"]:
        copy_sources(context["root"], source, context.get("_overrides", []), private=True)
    authored = inventory(source)
    site(context, "render", context_path, source)
    generated = inventory(source)
    for name, expected in authored.items():
        if generated.get(name) != expected and name not in context.get("_overrides", []):
            raise Error(
                f"generated source overwrote authored leaf without explicit override: {name}"
            )
    validate_source(source)
    config = Path(context["paths"]["config"])
    standard = Path(context["home"]) / ".config"
    if config != standard and (source / "dot_config").exists():
        destination = source / encoded(config.relative_to(context["home"]))
        if destination.exists():
            raise Error(f"custom XDG source collision: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        (source / "dot_config").rename(destination)
    settings = directory / "chezmoi.json"
    # Disable scripts even though source validation has already rejected them.
    atomic_json(
        settings,
        {
            "data": context,
            "sourceDir": str(source),
            "destDir": str(rendered),
            "color": False,
        },
    )
    argv = [
        native(context["executables"]["chezmoi"]),
        "--config",
        settings,
        "--source",
        source,
        "--destination",
        rendered,
        "--cache",
        directory / "chezmoi-cache",
        "--persistent-state",
        directory / "chezmoi-state.boltdb",
        "apply",
        "--force",
        "--exclude",
        "scripts",
    ]
    run(argv, context)
    files = inventory(rendered)
    for name, record in files.items():
        relative(name)
        path = rendered / name
        if record["kind"] == "file" and b"/nix/store/" in path.read_bytes():
            raise Error(f"rendered file retains Nix runtime dependency: {name}")
        if record["kind"] == "symlink" and "/nix/" in record["target"]:
            raise Error(f"rendered symlink retains Nix runtime dependency: {name}")
    atomic_json(
        directory / "render.json",
        {
            "schema_version": 1,
            "files": files,
            "source_files": inventory(source),
            "context": str(context_path),
        },
    )
    return directory


def record_path(context):
    return Path(context["paths"]["state"]) / "files.json"


def read_record(context):
    path = record_path(context)
    if not path.exists():
        return {"schema_version": 1, "home": context["home"], "files": {}, "generation": None}
    value = load(path)
    if value.get("home") != context["home"]:
        raise Error("ownership record belongs to another home")
    for name in value.get("files", {}):
        relative(name)
    return value


def collisions(context, desired, previous):
    home = Path(context["home"])
    for name in sorted(set(desired) | set(previous["files"])):
        target = home / name
        no_symlink_parents(target, home)
        observed = fingerprint(target)
        owned = previous["files"].get(name)
        legacy = context.get("legacy_home_files")
        legacy_owned = legacy is not None and observed == {
            "kind": "symlink",
            "target": str(Path(legacy) / name),
        }
        if observed is not None and (owned is None or observed != owned) and not legacy_owned:
            raise Error(f"foreign or modified managed leaf: {target}; preserved")


def write_leaf(target, source, record, boundary):
    no_symlink_parents(target, boundary)
    target.parent.mkdir(parents=True, exist_ok=True)
    if record["kind"] == "file":
        atomic_bytes(target, source.read_bytes(), record["mode"], replace_symlink=True)
    else:
        temporary = target.with_name(target.name + ".dotfiles-" + uuid.uuid4().hex)
        try:
            temporary.symlink_to(record["target"])
            os.replace(temporary, target)
        finally:
            if temporary.is_symlink():
                temporary.unlink()


def guard_service_descriptors(context, desired):
    path = Path(context["paths"]["state"]) / "services.json"
    if not path.exists():
        return
    from services import observe

    records = load(path)
    if records.get("home") != context["home"]:
        raise Error("service ownership belongs to another home")
    for identifier, entry in records.get("services", {}).items():
        service = entry["selection"]
        if service.get("existing_unit"):
            continue
        name = service.get("target", service.get("source"))
        target = Path(context["home"]) / name
        if name.startswith(".config/"):
            target = Path(context["paths"]["config"]) / name[len(".config/") :]
        key = target.relative_to(context["home"]).as_posix()
        if desired.get(key) == fingerprint(target):
            continue
        observed = observe(context, service)
        if (
            observed.get("loaded")
            or observed.get("ActiveState") == "active"
            or observed.get("UnitFileState") in ("enabled", "enabled-runtime")
        ):
            raise Error(
                f"stop and disable owned service explicitly before changing its descriptor: {identifier}"
            )


def apply(context, staged=None, *, retire=False):
    state = Path(context["paths"]["state"])
    state.mkdir(parents=True, exist_ok=True)
    journal_path = state / "journal.json"
    if journal_path.exists() and load(journal_path).get("status") == "pending":
        raise Error("unfinished file operation; use recover before another apply")
    previous = read_record(context)
    desired = {} if retire else load(Path(staged) / "render.json")["files"]
    collisions(context, desired, previous)
    guard_service_descriptors(context, desired)
    private_context = Path(staged) / "context.json" if not retire else state / "retire-context.json"
    if retire:
        atomic_json(private_context, context)
    private_plan = site(
        context,
        "retire" if retire else "apply",
        private_context,
        None if retire else Path(staged) / "source",
        dry_run=True,
    )
    home = Path(context["home"])
    home.mkdir(parents=True, exist_ok=True)
    generation = uuid.uuid4().hex
    backup = state / "generations" / generation
    backup.mkdir(parents=True)
    before = {}
    after = {}
    for name in sorted(set(desired) | set(previous["files"])):
        target = home / name
        observed = fingerprint(target)
        before[name] = observed
        after[name] = desired.get(name)
        if observed is not None:
            copied = backup / "before" / name
            copied.parent.mkdir(parents=True, exist_ok=True)
            if observed["kind"] == "symlink":
                copied.symlink_to(observed["target"])
            else:
                shutil.copy2(target, copied)
    journal = {
        "schema_version": 1,
        "status": "pending",
        "id": generation,
        "home": str(home),
        "backup": str(backup),
        "before": before,
        "after": after,
        "previous": previous,
        "private": private_plan,
        "context": context,
        "completed": [],
    }
    journal["previous_context"] = (
        load(state / "context.json") if (state / "context.json").exists() else None
    )
    atomic_json(journal_path, journal)
    try:
        for name, expected in before.items():
            target = home / name
            no_symlink_parents(target, home)
            if fingerprint(target) != expected:
                raise Error(f"managed destination changed during operation: {target}")
        if not retire:
            metadata = load(Path(staged) / "render.json")
            if (
                inventory(Path(staged) / "home") != desired
                or inventory(Path(staged) / "source") != metadata["source_files"]
            ):
                raise Error("staged source/render bytes changed before apply")
            run(
                [
                    native(context["executables"]["chezmoi"]),
                    "--config",
                    Path(staged) / "chezmoi.json",
                    "--source",
                    Path(staged) / "source",
                    "--destination",
                    home,
                    "--cache",
                    Path(staged) / "chezmoi-cache",
                    "--persistent-state",
                    state / "chezmoi-state.boltdb",
                    "apply",
                    "--force",
                    "--exclude",
                    "scripts",
                ],
                context,
            )
        for name in after:
            target = home / name
            no_symlink_parents(target, home)
            if after[name] is None and before[name] is not None:
                if fingerprint(target) != before[name]:
                    raise Error(f"retirement preserves modified leaf: {target}")
                target.unlink()
            journal["completed"].append(name)
            atomic_json(journal_path, journal)
        for name, expected in after.items():
            if fingerprint(home / name) != expected:
                raise Error(f"file readback mismatch: {name}")
        # Application reconcilers run only on explicit apply, not render.
        if private_plan is not None:
            site(
                context, "retire" if retire else "apply", private_context, transaction=journal_path
            )
        result = {
            "schema_version": 1,
            "home": str(home),
            "files": desired,
            "generation": generation,
        }
        atomic_json(record_path(context), result)
        if not retire:
            atomic_json(state / "context.json", context)
        journal["status"] = "committed"
        atomic_json(journal_path, journal)
        return result
    except BaseException:
        # A durable pending journal is deliberately retained for narrow recovery.
        raise


def recover(context):
    state = Path(context["paths"]["state"])
    journal_path = state / "journal.json"
    if not journal_path.exists():
        raise Error("no recorded operation to recover")
    journal = load(journal_path)
    if journal.get("home") != context["home"]:
        raise Error("journal belongs to another home")
    if journal.get("status") not in ("pending", "committed"):
        raise Error("journal already recovered")
    home = Path(context["home"])
    if len(journal["id"]) != 32 or any(c not in "0123456789abcdef" for c in journal["id"]):
        raise Error("unsafe recovery generation id")
    guard_service_descriptors(context, journal["before"])
    private_context = state / "recovery-context.json"
    atomic_json(private_context, context)
    if journal.get("private") is not None:
        site(context, "recover", private_context, dry_run=True, transaction=journal_path)
    backup = state / "generations" / journal["id"]
    if str(backup) != journal["backup"]:
        raise Error("unsafe recovery backup path")
    # All collision checks precede changes. Both pre/post bytes are acceptable
    # after an interrupted atomic rename; anything else belongs to the user.
    for name, before in journal["before"].items():
        relative(name)
        target = home / name
        no_symlink_parents(target, home)
        observed = fingerprint(target)
        if observed not in (before, journal["after"][name]):
            raise Error(f"recovery preserves foreign replacement: {target}")
        if before is not None and fingerprint(backup / "before" / name) != before:
            raise Error(f"recovery backup integrity failure: {name}")
    if journal.get("private") is not None:
        site(context, "recover", private_context, transaction=journal_path)
    for name, before in journal["before"].items():
        target = home / name
        if before is None:
            if fingerprint(target) is not None:
                target.unlink()
        else:
            write_leaf(target, backup / "before" / name, before, home)
    atomic_json(record_path(context), journal["previous"])
    if journal.get("previous_context") is not None:
        atomic_json(state / "context.json", journal["previous_context"])
    elif (state / "context.json").exists():
        (state / "context.json").unlink()
    journal["status"] = "recovered"
    atomic_json(journal_path, journal)
    return journal["previous"]


def verify(context):
    if not record_path(context).exists():
        raise Error("no applied configuration ownership record")
    record = read_record(context)
    for name, expected in record["files"].items():
        target = Path(context["home"]) / name
        no_symlink_parents(target, Path(context["home"]))
        if fingerprint(target) != expected:
            raise Error(f"managed file verification failed: {name}")
        if expected["kind"] == "file" and b"/nix/store/" in target.read_bytes():
            raise Error(f"managed file retains Nix runtime path: {name}")
    return {"verified_files": len(record["files"]), "generation": record["generation"]}
