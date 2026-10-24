"""Pinned appearance assets, with receipt-committed, recoverable link transitions.

The durable pending journal records both exact receipts and link selections before
mutation. Until the candidate receipt is published, recovery restores the previous
selection (or absence on first install); after publication it completes the
candidate. Neither recovery path removes source generations or foreign leaves.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import tarfile

from desktop_build import (
    atomic_json,
    digest,
    environment,
    fetch,
    legal_files,
    lock_digest,
    payload_manifest,
    run,
    untar,
)
from desktop_lifecycle import atomic_link


def assemble(lock, package_id, output):
    work = Path(output).resolve() / (package_id + "-" + lock_digest(lock)[:16])
    receipt = work / "receipt.json"
    if receipt.exists():
        record = json.loads(receipt.read_text())
        verify(record)
        return receipt
    if work.exists():
        raise ValueError("Incomplete appearance build: " + str(work))
    work.mkdir(parents=True)
    stage = work / "stage"
    stage.mkdir()
    if package_id == "meslolgs-nf":
        source = untar(fetch(lock[package_id], work / "downloads"), work / "source")
        fonts = stage / "fonts"
        fonts.mkdir()
        for name in ("Regular", "Bold", "Italic", "Bold Italic"):
            shutil.copy2(source / ("MesloLGS NF " + name + ".ttf"), fonts)
        shutil.copy2(source / "MesloLGS NF License.txt", fonts)
        links = ["fonts/" + p.name for p in sorted(fonts.iterdir())]
    elif package_id == "jetbrains-mono-nf":
        archive = fetch(lock[package_id], work / "downloads")
        fonts = stage / "fonts"
        fonts.mkdir()
        with tarfile.open(archive) as stream:
            stream.extractall(fonts, filter="data")
        if not list(fonts.glob("JetBrainsMonoNerdFont-*.ttf")) or not (fonts / "OFL.txt").is_file():
            raise ValueError("Incomplete JetBrains Mono Nerd Font release")
        links = ["fonts/" + p.name for p in sorted(fonts.iterdir()) if p.suffix == ".ttf"]
    elif package_id == "nordzy-cursors":
        source = untar(fetch(lock[package_id], work / "downloads"), work / "source")
        target = stage / "icons/Nordzy-cursors"
        target.parent.mkdir(parents=True)
        shutil.copytree(source / "xcursors/Nordzy-cursors", target, symlinks=True)
        if not (target / "index.theme").is_file() or not (target / "cursors/left_ptr").exists():
            raise ValueError("Incomplete Nordzy XCursor resources")
        legal_files(source, target / "licenses")
        links = ["icons/Nordzy-cursors"]
    elif package_id == "nordic-theme":
        links = []
        for variant, record in lock[package_id]["variants"].items():
            source = untar(fetch(record, work / "downloads"), work / (variant + "-source"))
            target = stage / "themes" / variant
            target.mkdir(parents=True)
            for directory in (
                "gtk-3.0",
                "gtk-4.0",
                "gnome-shell",
                "cinnamon",
                "metacity-1",
                "xfwm4",
                "assets",
            ):
                origin = source / directory
                if origin.is_dir():
                    shutil.copytree(origin, target / directory, symlinks=True)
            if (
                not (target / "gtk-3.0/gtk.css").is_file()
                or not (target / "gtk-4.0/gtk.css").is_file()
            ):
                raise ValueError("Upstream Nordic GTK resources absent: " + variant)
            if (source / "index.theme").is_file():
                shutil.copy2(source / "index.theme", target)
            legal_files(source, target / "licenses")
            links.append("themes/" + variant)
            if variant == "Nordic":
                for directory, destination in (
                    ("extras/wallpapers", "wallpapers/Nordic"),
                    ("kde/folders", "icons"),
                ):
                    origin = source / directory
                    if not origin.is_dir():
                        raise ValueError("Missing Nordic upstream resource: " + directory)
                    shutil.copytree(origin, stage / destination, symlinks=True)
                links.append("wallpapers/Nordic")
                links += ["icons/" + p.name for p in sorted((stage / "icons").iterdir())]
        # Old packaged Nordic intentionally excluded broken upstream symlinks.
        # Keep that exact rule, and never carry an escaping live link.
        for path in stage.rglob("*"):
            if path.is_symlink():
                if not path.exists():
                    path.unlink()
                elif not path.resolve().is_relative_to(stage.resolve()):
                    raise ValueError("Escaping Nordic resource symlink")
    else:
        raise ValueError("Unknown appearance package")
    record = {
        "id": package_id,
        "version": lock[package_id]["version"],
        "stage": str(stage),
        "lock_sha256": lock_digest(lock),
        "payload": payload_manifest(stage),
        "links": links,
    }
    atomic_json(receipt, record)
    return receipt


def verify(record):
    if payload_manifest(Path(record["stage"])) != record["payload"]:
        raise ValueError("Appearance artifact modified")


def destinations(context, record):
    home = Path(context["home"])
    for relative in record["links"]:
        path = Path(relative)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] not in ("fonts", "themes", "icons", "wallpapers")
        ):
            raise ValueError("Appearance destination escapes its declared asset scope")
        if context["platform"] == "macos-arm64" and path.parts[0] == "fonts":
            target = home / "Library/Fonts" / path.name
        else:
            target = home / ".local/share" / path
        yield relative, target


def _links(context, record):
    return (
        {
            str(target): str(Path(record["stage"]) / relative)
            for relative, target in destinations(context, record)
        }
        if record is not None
        else {}
    )


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_generation(stage):
    directories = [stage]
    for path in stage.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            directories.append(path)
        elif path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _sync_directory(path)


def _publish_record(path, record):
    atomic_json(path, record)
    _sync_directory(path.parent)


def _read_record(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Foreign appearance state replacement: " + str(path))
    return json.loads(path.read_text()) if path.exists() else None


def _discard_pending(path, transaction):
    if _read_record(path) != transaction:
        raise ValueError("Foreign appearance transaction replacement")
    path.unlink()
    _sync_directory(path.parent)


def _transition_links(context, previous, candidate):
    before, after = _links(context, previous), _links(context, candidate)
    return [
        {"path": path, "previous": before.get(path), "candidate": after.get(path)}
        for path in dict.fromkeys([*after, *before])
    ]


def _validate_links(links):
    # Preflight every leaf before writing any leaf. Only exact journal evidence,
    # never a runtime path prefix, admits an interrupted transition's links.
    for link in links:
        path = Path(link["path"])
        if path.exists() or path.is_symlink():
            if not path.is_symlink() or os.readlink(path) not in (
                link["previous"],
                link["candidate"],
            ):
                raise ValueError("Foreign appearance replacement: " + str(path))


def _recover(context, package_id, receipt, pending):
    transaction = _read_record(pending)
    current = _read_record(receipt)
    if transaction is None:
        if current is not None:
            verify(current)
            for path, target in _links(context, current).items():
                if not Path(path).is_symlink() or os.readlink(path) != target:
                    raise ValueError("Foreign appearance replacement: " + path)
        return current if current is not None else {"recovered": package_id, "selection": None}
    previous, candidate = transaction["previous"], transaction["candidate"]
    if (
        transaction.get("policy") != "receipt-commit-v1"
        or transaction.get("home") != context["home"]
        or transaction.get("platform") != context["platform"]
        or transaction.get("runtime") != context["paths"]["runtime"]
        or candidate["id"] != package_id
        or (previous is not None and previous["id"] != package_id)
        or transaction["links"] != _transition_links(context, previous, candidate)
    ):
        raise ValueError("Appearance transaction does not match its recorded scope")
    if current != previous and current != candidate:
        raise ValueError("Foreign appearance receipt replacement")
    for record in (previous, candidate):
        if record is not None:
            verify(record)
    links = transaction["links"]
    _validate_links(links)
    selection = "candidate" if current == candidate else "previous"
    for link in links:
        path, target = Path(link["path"]), link[selection]
        # Recheck immediately before mutation as well as the all-leaf preflight.
        _validate_links([link])
        if target is None:
            if path.is_symlink():
                path.unlink()
        elif not path.is_symlink() or os.readlink(path) != target:
            atomic_link(target, path)
        if path.parent.exists():
            _sync_directory(path.parent)
    # The receipt is the commit point, so rollback never rewrites it. Flush its
    # directory before discarding evidence, including a kill just after replace.
    _sync_directory(receipt.parent)
    if _read_record(receipt) != current:
        raise ValueError("Foreign appearance receipt replacement")
    _discard_pending(pending, transaction)
    return current if current is not None else {"recovered": package_id, "selection": None}


def reconcile(context, lock, package_id, action, output):
    state = Path(context["paths"]["state"]) / "desktop-appearance"
    state.mkdir(parents=True, exist_ok=True)
    _sync_directory(state.parent)
    with (state / "operation.lock").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = state / (package_id + ".json")
        pending = state / (package_id + "-pending.json")
        if action == "recover":
            return _recover(context, package_id, receipt, pending)
        if pending.exists() or pending.is_symlink():
            if action != "provision":
                raise ValueError("Pending appearance transition requires recovery")
            _recover(context, package_id, receipt, pending)
        previous = _read_record(receipt)
        if action in ("check", "retire"):
            if previous is None:
                raise ValueError("Appearance package is not owned")
            verify(previous)
            for relative, target in destinations(context, previous):
                if not target.is_symlink() or os.readlink(target) != str(
                    Path(previous["stage"]) / relative
                ):
                    raise ValueError("Foreign appearance replacement: " + str(target))
            if action == "check":
                if previous["lock_sha256"] != lock_digest(lock):
                    raise ValueError("Stale appearance selection")
                return previous
            owned = list(
                {old["stage"]: old for old in [previous, *previous.get("retained", [])]}.values()
            )
            boundary = (Path(context["paths"]["runtime"]) / package_id).resolve()
            for old in owned:
                root_path = Path(old["stage"])
                if (
                    root_path.is_symlink()
                    or root_path.resolve().parent != boundary
                    or len(root_path.name) != 16
                    or any(c not in "0123456789abcdef" for c in root_path.name)
                ):
                    raise ValueError("Appearance retirement escapes its recorded runtime scope")
                verify(old)
            for _, target in destinations(context, previous):
                target.unlink()
            for old in owned:
                shutil.rmtree(old["stage"])
            receipt.unlink()
            return {"retired": package_id}
        if action != "provision":
            raise ValueError("Unsupported appearance action")
        candidate = json.loads(assemble(lock, package_id, output).read_text())
        if previous:
            verify(previous)
            for relative, target in destinations(context, previous):
                if not target.is_symlink() or os.readlink(target) != str(
                    Path(previous["stage"]) / relative
                ):
                    raise ValueError("Foreign appearance replacement: " + str(target))
        prior_links = _links(context, previous)
        for _, target in destinations(context, candidate):
            if target.exists() or target.is_symlink():
                if (
                    str(target) not in prior_links
                    or not target.is_symlink()
                    or os.readlink(target) != prior_links[str(target)]
                ):
                    raise ValueError("Foreign appearance collision: " + str(target))
        root = Path(context["paths"]["runtime"]) / package_id / candidate["lock_sha256"][:16]
        if root.is_symlink():
            raise ValueError("Appearance runtime root is a foreign symlink")
        if root.exists():
            if payload_manifest(root) != candidate["payload"]:
                raise ValueError("Foreign appearance runtime")
        else:
            root.parent.mkdir(parents=True, exist_ok=True)
            temporary = root.with_name("." + root.name + "-" + str(os.getpid()))
            try:
                shutil.copytree(candidate["stage"], temporary, symlinks=True)
                _sync_generation(temporary)
                os.replace(temporary, root)
                _sync_directory(root.parent)
                _sync_directory(root.parent.parent)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        candidate["stage"] = str(root)
        retained = (
            {old["stage"]: old for old in previous.get("retained", []) if old["stage"] != str(root)}
            if previous
            else {}
        )
        if previous and previous["stage"] != str(root):
            retained[previous["stage"]] = {
                "stage": previous["stage"],
                "payload": previous["payload"],
            }
        candidate["retained"] = list(retained.values())
        links = _transition_links(context, previous, candidate)
        transaction = {
            "policy": "receipt-commit-v1",
            "home": context["home"],
            "platform": context["platform"],
            "runtime": context["paths"]["runtime"],
            "previous": previous,
            "candidate": candidate,
            "links": links,
        }
        _publish_record(pending, transaction)
        _validate_links(links)
        for link in links:
            target = Path(link["path"])
            old = link["previous"]
            if (target.exists() or target.is_symlink()) and (
                old is None or not target.is_symlink() or os.readlink(target) != old
            ):
                raise ValueError("Appearance target changed during transition: " + str(target))
            if link["candidate"] is not None:
                atomic_link(link["candidate"], target)
            else:
                target.unlink()
            _sync_directory(target.parent)
        if _read_record(receipt) != previous:
            raise ValueError("Foreign appearance receipt replacement")
        _publish_record(receipt, candidate)
        _discard_pending(pending, transaction)
        if (
            package_id in ("meslolgs-nf", "jetbrains-mono-nf")
            and context["platform"] == "linux-x64"
        ):
            run(
                ["/usr/bin/fc-cache", "-f", str(Path(context["home"]) / ".local/share/fonts")],
                env=dict(environment(), HOME=context["home"]),
            )
        return candidate
