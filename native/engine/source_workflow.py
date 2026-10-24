"""Explicit public desired-state updates, operated only from the private entry.

`run(context, operation, options)` supports status/prepare/check/publish/recover.
Prepare creates an owned, disposable Git worktree from bootstrap.lock.json,
resolves declared mise backends and the complete desktop source family, and
retains the real mise/Aube lock graph. Check stages a conventional engine
candidate; it does not promote it. Publish alone signs and fast-forward pushes
an exact reviewed tree, reads back its commit/archive from the public remote,
and stages/promotes only the private bootstrap.lock.json source-pin candidate.
Its journal retains the original source roots across pin-selected cache changes.
No live package/runtime selection, private source copy, or SDK publication occurs.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import uuid

from manifest import PLATFORMS, compose
from providers import mise_metadata
from safety import (
    Error,
    atomic_bytes,
    atomic_json,
    digest,
    inside,
    load,
    locked,
    native,
    no_symlink_parents,
)
import updates

ACTIONS = ("status", "prepare", "check", "publish", "recover")
VERSION = re.compile(r"[0-9][0-9A-Za-z.+_-]{0,95}")
DESKTOP = "native/recipes/desktop/sources.json"
OPTIONS = {
    "status": set(),
    "prepare": {
        "family",
        "qt_version",
        "rust_version",
        "wayland_version",
        "protocols_version",
        "yes",
    },
    "check": {"yes"},
    "publish": {"yes", "message", "branch"},
    "recover": {"yes"},
}


def _environment(context, directory):
    # No ambient Git credentials, URL rewrites, shell hooks, project config or
    # environment-sourced author identity. Signing is selected explicitly below.
    return {
        "HOME": str(directory),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
    }


def _git(context, directory, *arguments, binary=False, settings=()):
    argv = [
        native("/usr/bin/git"),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "commit.gpgSign=true",
        "-c",
        "core.fileMode=true",
        "-c",
        "protocol.file.allow=" + ("always" if context.get("_verification_only") else "never"),
    ]
    for name, value in settings:
        argv.extend(("-c", name + "=" + value))
    argv.extend(("-C", str(directory), *map(str, arguments)))
    environment = _environment(context, directory)
    if settings:
        environment["HOME"] = context["home"]
        if os.environ.get("SSH_AUTH_SOCK"):
            environment["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    result = subprocess.run(argv, env=environment, capture_output=True, check=False)
    if result.returncode:
        raise Error(
            "source Git operation failed: " + result.stderr.decode(errors="replace").strip()
        )
    return result.stdout if binary else result.stdout.decode().strip()


def _paths(context):
    home = Path(context["home"]).resolve()
    state = Path(context["paths"]["state"]) / "source-update"
    no_symlink_parents(state / "active.json", home)
    if state.is_symlink():
        raise Error("source workspace state is a foreign symlink")
    root = Path(context["root"]).resolve()
    if root == Path(context["base"]).resolve():
        raise Error("source updates require the private entry and its bootstrap.lock.json")
    pin = root / "bootstrap.lock.json"
    no_symlink_parents(pin, root)
    return state, pin


def _pin(context, path):
    value = load(path)
    if value.get("schema_version") != 1 or value.get("status") != "published":
        raise Error("source preparation requires a reviewed published bootstrap.lock.json pin")
    if not re.fullmatch(r"[0-9a-f]{40}", value.get("revision", "")) or not re.fullmatch(
        r"[0-9a-f]{64}", value.get("sha256", "")
    ):
        raise Error("source pin requires an exact commit and git-archive SHA256")
    parsed = urllib.parse.urlsplit(value.get("repository", ""))
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not (
            parsed.scheme == "https"
            and parsed.netloc
            or context.get("_verification_only")
            and parsed.scheme == "file"
        )
    ):
        raise Error("public source requires a credential-free HTTPS Git repository")
    return value


def _archive(context, directory, revision):
    return _git(context, directory, "archive", "--format=tar", revision, binary=True)


def _fetch(context, directory, pin):
    directory.mkdir(parents=True)
    _git(context, directory, "init", "--quiet")
    _git(context, directory, "fetch", "--quiet", "--no-tags", pin["repository"], pin["revision"])
    if _git(context, directory, "rev-parse", "FETCH_HEAD") != pin["revision"]:
        raise Error("public source remote returned a different revision")
    archive = _archive(context, directory, "FETCH_HEAD")
    if hashlib.sha256(archive).hexdigest() != pin["sha256"]:
        raise Error("public source archive differs from the private immutable pin")
    _git(context, directory, "checkout", "--quiet", "--detach", pin["revision"])


def _tracked(context, tree):
    names = _git(context, tree, "ls-files", "-z").split("\0")
    names += _git(context, tree, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    result = {}
    for name in sorted(set(filter(None, names))):
        path = inside(tree, name, existing=True)
        if path.is_symlink():
            result[name] = {"link": os.readlink(path)}
        elif path.is_file():
            result[name] = {"sha256": digest(path), "executable": bool(path.stat().st_mode & 0o111)}
        else:
            raise Error("public tracked source has an unsupported file type")
    return result


def _changed(context, tree, revision):
    tracked = _git(context, tree, "diff", "--name-only", "-z", revision, "--").split("\0")
    extra = _git(context, tree, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    # Include ignored files too: a .gitignore must never hide publication input.
    ignored = _git(
        context, tree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
    ).split("\0")
    return sorted(set(filter(None, tracked + extra + ignored)))


def _allowed(name):
    if name in (
        "apps/mise/mise.toml",
        "apps/mise/mise.lock",
        "native/manifests/base.json",
        "native/manifests/desktop.json",
        DESKTOP,
    ):
        return True
    return bool(
        re.fullmatch(
            r"apps/mise/\.mise/locks/[A-Za-z0-9_.-]+/[A-Za-z0-9_.+-]+/(aube-lock\.yaml|package\.json)",
            name,
        )
    )


def _snapshot(context, tree, revision):
    result = {}
    for name in _changed(context, tree, revision):
        if not _allowed(name):
            raise Error("public update preserves foreign/unapproved file: " + name)
        path = inside(tree, name, existing=True)
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o111:
            raise Error("update content must be nonexecutable regular desired-state data: " + name)
        result[name] = digest(path)
    return result


def _receipt(state):
    active = load(state / "active.json")
    identifier = active.get("id", "")
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise Error("invalid managed source workspace identifier")
    directory = state / identifier
    if directory.is_symlink() or not directory.is_dir():
        raise Error("managed source generation was replaced")
    receipt = load(directory / "receipt.json")
    if receipt.get("id") != identifier:
        raise Error("managed source receipt identity mismatch")
    return directory, receipt


def _assert_unchanged(context, directory, receipt):
    tree = directory / "tree"
    if tree.is_symlink() or tree.resolve().parent != directory.resolve():
        raise Error("managed source worktree was replaced")
    if (tree / ".git").is_symlink() or not (tree / ".git").is_dir():
        raise Error("managed source Git directory was replaced")
    head = _git(context, tree, "rev-parse", "HEAD")
    if (
        head not in (receipt["pin"]["revision"], receipt.get("commit"))
        and receipt["status"] != "committing"
    ):
        raise Error("managed source HEAD has foreign commits")
    if _snapshot(context, tree, receipt["pin"]["revision"]) != receipt["files"]:
        raise Error("managed source has foreign edits since preparation/check")
    # Detect changes to unchanged tracked files as well, including executable bits.
    current = _tracked(context, tree)
    expected = receipt["tracked"]
    if current != expected:
        raise Error("managed source tracked bytes or file modes changed")
    return tree


def _mise_command(context, directory, arguments):
    empty = directory / "empty-system"
    empty.mkdir(exist_ok=True)
    (empty / "config.toml").write_text("")
    env = _environment(context, directory)
    env.update(
        {
            "MISE_CONFIG_DIR": str(directory),
            "MISE_GLOBAL_CONFIG_FILE": str(directory / "config.toml"),
            "MISE_SYSTEM_CONFIG_DIR": str(empty),
            "MISE_SYSTEM_CONFIG_FILE": str(empty / "config.toml"),
            "MISE_TRUSTED_CONFIG_PATHS": str(directory / "config.toml"),
            "MISE_CEILING_PATHS": "/",
            "MISE_NO_ENV": "1",
            "MISE_NO_HOOKS": "1",
            "MISE_SAFE": "1",
            "MISE_LOCKED": "0",
            "MISE_AUTO_INSTALL": "0",
            "MISE_PRERELEASES": "0",
            "MISE_DATA_DIR": str(Path(context["paths"]["data"]) / "mise"),
            "MISE_CACHE_DIR": str(Path(context["paths"]["cache"]) / "mise"),
        }
    )
    result = subprocess.run(
        [native(context["executables"]["mise"]), *arguments],
        cwd="/",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise Error("mise source metadata/lock acquisition failed: " + result.stderr.strip())
    return result.stdout.strip()


def _acquire_mise(context, tree):
    path = tree / "native/manifests/base.json"
    manifest = load(path)
    packages = [p for p in manifest["packages"] if p["provider"] == "mise"]
    config_path = tree / "apps/mise/mise.toml"
    text = config_path.read_text()
    config = tomllib.loads(text)
    by_tool = {p["tool"]: p for p in packages}
    if set(by_tool) != set(config["tools"]):
        raise Error("every canonical mise tool must have one public package policy")
    details = []
    with tempfile.TemporaryDirectory(
        prefix="dotfiles-source-mise-", dir=Path("/tmp").resolve()
    ) as temporary:
        scratch = Path(temporary)
        shutil.copyfile(config_path, scratch / "config.toml")
        shutil.copyfile(config_path.with_suffix(".lock"), scratch / "mise.lock")
        sidecars = config_path.parent / ".mise/locks"
        if sidecars.exists():
            shutil.copytree(sidecars, scratch / ".mise/locks", symlinks=False)
        for name, selection in config["tools"].items():
            package = by_tool[name]
            variants = [item for item in packages if item["tool"] == name]
            old = selection["version"] if isinstance(selection, dict) else selection
            policy = package.get("policy")
            if any(item["version"] != old or item.get("policy") != policy for item in variants):
                raise Error(
                    "canonical mise policy/version mismatch across platform variants: " + name
                )
            if policy != "latest-stable":
                if not package.get("reason"):
                    raise Error("mise policy exception requires an explicit reason: " + name)
                details.append(
                    {
                        "tool": name,
                        "selected": old,
                        "policy": policy,
                        "reason": package["reason"],
                        "changed": False,
                    }
                )
                continue
            backend = config["tool_alias"].get(name)
            if not backend:
                raise Error("mise source resolution requires a declared canonical backend")
            latest = _mise_command(context, scratch, ["latest", backend])
            if not VERSION.fullmatch(latest) or re.search(
                r"(?:alpha|beta|rc|nightly|dev|snapshot)", latest, re.I
            ):
                raise Error("mise did not return an exact stable release for " + name)
            old_parts = tuple(int(part) for part in re.findall(r"\d+", old))
            new_parts = tuple(int(part) for part in re.findall(r"\d+", latest))
            if new_parts < old_parts:
                raise Error("source update refuses a provider downgrade: " + name)
            pattern = re.compile(
                r"^("
                + re.escape(name)
                + r"\s*=\s*(?:\{\s*version\s*=\s*)?\")"
                + re.escape(old)
                + r"(\")",
                re.M,
            )
            text, count = pattern.subn(lambda m: m[1] + latest + m[2], text)
            if count != 1:
                raise Error("canonical mise request cannot be narrowly rewritten: " + name)
            for variant in variants:
                variant["version"] = latest
            details.append(
                {
                    "tool": name,
                    "selected": old,
                    "candidate": latest,
                    "backend": backend,
                    "policy": policy,
                    "changed": latest != old,
                }
            )
        (scratch / "config.toml").write_text(text)
        _mise_command(context, scratch, ["lock", "--global", "--platform", "linux-x64,macos-arm64"])
        # Keep native dependency graphs verbatim; never synthesize Aube checksums.
        atomic_bytes(config_path, text.encode(), mode=0o644)
        atomic_bytes(
            config_path.with_suffix(".lock"), (scratch / "mise.lock").read_bytes(), mode=0o644
        )
        generated = scratch / ".mise/locks"
        if generated.exists():
            for item in generated.rglob("*"):
                if item.is_symlink():
                    raise Error("generated mise graph contains a symlink")
                if item.is_file():
                    target = config_path.parent / item.relative_to(scratch)
                    if not _allowed(target.relative_to(tree).as_posix()):
                        raise Error("unapproved generated mise sidecar")
                    atomic_bytes(target, item.read_bytes(), mode=0o644)
    for package in packages:
        verification = package.get("verification", {})
        if verification.get("backend") == "go":
            verification["module_version"] = "v" + package["version"].removeprefix("v")
            verification["toolchain"] = "go@" + by_tool["go"]["version"]
    atomic_json(path, manifest)
    return details


def _desktop_modules(tree):
    # Execute only the *pinned installed base's* reviewed recipe, never freshly
    # fetched update code. Its data target is passed separately by the caller.
    directory = Path(tree) / "native/recipes/desktop"
    original = list(sys.path)
    names = (
        "desktop",
        "desktop_build",
        "desktop_updates",
        "desktop_lifecycle",
        "desktop_appearance",
    )
    saved = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    try:
        sys.path.insert(0, str(directory))
        import desktop
        import desktop_updates

        return desktop, desktop_updates
    finally:
        sys.path[:] = original
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


def _acquire_desktop(context, tree, options):
    desktop, producer = _desktop_modules(context["base"])
    current = load(tree / DESKTOP)
    candidate, details = producer.acquire(
        current,
        Path(context["paths"]["cache"]) / "desktop-sources",
        **{
            name: options.get(name)
            for name in ("qt_version", "rust_version", "wayland_version", "protocols_version")
        },
    )
    desktop.validate_lock(candidate)
    atomic_json(tree / DESKTOP, candidate)
    descriptor = tree / "native/manifests/desktop.json"
    manifest = load(descriptor)
    manifest["artifacts"]["noble-desktop"]["qt_standard_support_until"] = candidate["runtime"][
        "qt_standard_support_until"
    ]
    atomic_json(descriptor, manifest)
    return details


def _validate_graphs(context, tree):
    # Public-only and private compositions both matter. Private inputs are read,
    # never copied into the public Git repository or publication staging index.
    for root in (tree, Path(context["root"])):
        for platform in PLATFORMS:
            candidate = compose(tree, root, context["home"], platform)
            updates.validate_local_locks(candidate)
            for package in candidate["packages"]:
                if package["provider"] == "mise":
                    mise_metadata(candidate, package)
    if (tree / DESKTOP).is_file():
        desktop, _ = _desktop_modules(context["base"])
        desktop.validate_lock(load(tree / DESKTOP))


def _prepare(context, state, pin_path, options):
    pin = _pin(context, pin_path)
    if (state / "active.json").exists():
        previous, receipt = _receipt(state)
        _assert_unchanged(context, previous, receipt)
        if receipt["status"] in ("committing", "committed", "pushing", "published", "pinning"):
            raise Error("recover/finish the pending publication before preparing another update")
    identifier = uuid.uuid4().hex
    directory = state / identifier
    directory.mkdir()
    receipt = {
        "schema_version": 1,
        "id": identifier,
        "status": "preparing",
        "pin": pin,
        "private_root": context["root"],
        "private_pin_before": digest(pin_path),
    }
    atomic_json(directory / "receipt.json", receipt)
    try:
        tree = directory / "tree"
        _fetch(context, tree, pin)
        _tracked(context, tree)
        if not (tree / "native/engine/__main__.py").is_file():
            raise Error("pinned source lacks the native engine")
        details = {}
        family = options.get("family", "all")
        if family in ("mise", "all"):
            details["mise"] = _acquire_mise(context, tree)
        if family in ("desktop", "all"):
            details["desktop"] = _acquire_desktop(context, tree, options)
        _validate_graphs(context, tree)
        receipt.update(
            status="prepared",
            files=_snapshot(context, tree, pin["revision"]),
            tracked=_tracked(context, tree),
            acquisition=details,
            desktop_candidate=str(tree / DESKTOP) if family in ("desktop", "all") else None,
        )
        atomic_json(directory / "receipt.json", receipt)
        atomic_json(state / "active.json", {"id": identifier})
    except BaseException:
        receipt["status"] = "failed-preparation"
        atomic_json(directory / "receipt.json", receipt)
        raise
    return _result(directory, receipt)


def _result(directory, receipt):
    return {
        "status": receipt["status"],
        "id": receipt["id"],
        "worktree": str(directory / "tree"),
        "files": receipt.get("files", {}),
        "desktop_candidate": receipt.get("desktop_candidate"),
        "acquisition": receipt.get("acquisition", {}),
        "commit": receipt.get("commit"),
        "pin_candidate": receipt.get("pin_candidate"),
        "live_runtime_changed": False,
        "qualification": "source graphs/compositions/downloads only; native build, UI and host acceptance remain explicit",
    }


def _check(context, directory, receipt):
    if receipt["status"] not in ("prepared", "checked"):
        raise Error("source check requires a prepared candidate")
    tree = _assert_unchanged(context, directory, receipt)
    _validate_graphs(context, tree)
    if receipt["files"]:
        bundle = {
            "schema_version": 1,
            "files": [
                {"repository": "public", "path": name, "source": "tree/" + name, "sha256": checksum}
                for name, checksum in receipt["files"].items()
            ],
        }
        atomic_json(directory / "bundle.json", bundle)
        staged = updates.stage_candidate({**context, "base": str(tree)}, directory / "bundle.json")
        receipt["validation_candidate"] = staged["id"]
    receipt["status"] = "checked"
    receipt["private_inputs"] = _private_inputs(context)
    atomic_json(directory / "receipt.json", receipt)
    return _result(directory, receipt)


def _private_inputs(context):
    root = Path(context["root"])
    if (root / "native").is_symlink():
        raise Error("private verification input directory is a foreign symlink")
    result = {}
    for path in sorted((root / "native").rglob("*")):
        if path.is_symlink():
            raise Error("private verification input is a foreign symlink")
        if path.is_file() and "__pycache__" not in path.parts:
            result[path.relative_to(root).as_posix()] = digest(path)
    for pattern in (
        "apps/mise/mise.toml",
        "apps/mise/mise.lock",
        "apps/*/package.json",
        "apps/*/package-lock.json",
        "apps/*/pnpm-lock.yaml",
        "apps/*/source-lock.json",
    ):
        for candidate in sorted(root.glob(pattern)):
            name = candidate.relative_to(root).as_posix()
            path = inside(root, name)
            if path.is_file():
                result[name] = digest(path)
    return result


def _signing(context):
    # Read operator Git identity only during separately confirmed publication.
    env = {"HOME": context["home"], "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    settings = []
    for key in (
        "user.name",
        "user.email",
        "user.signingkey",
        "gpg.format",
        "gpg.program",
        "gpg.ssh.program",
        "gpg.ssh.allowedSignersFile",
        "credential.helper",
    ):
        result = subprocess.run(
            ["/usr/bin/git", "-C", context["root"], "config", "--get", key],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise Error("cannot read operator signing configuration")
        value = result.stdout.strip()
        if value:
            if "\n" in value or "\0" in value:
                raise Error("invalid operator signing configuration")
            if key.endswith("program"):
                native(value)
            settings.append((key, value))
    selected = dict(settings)
    if not all(selected.get(key) for key in ("user.name", "user.email", "user.signingkey")):
        raise Error("publication requires configured operator identity and signing key")
    if selected.get("gpg.format", "openpgp") not in ("ssh", "openpgp"):
        raise Error("publication only supports configured SSH or OpenPGP commit signing")
    if selected.get("gpg.format") == "ssh" and not selected.get("gpg.ssh.allowedSignersFile"):
        raise Error("SSH publication requires an allowedSignersFile for signed-commit verification")
    # Only confirmed signing/publication commands receive the operator HOME and
    # SSH agent socket; acquisition never discovers credentials or Git helpers.
    return settings


def _remote_tip(context, tree, repository, branch):
    output = _git(context, tree, "ls-remote", "--exit-code", repository, "refs/heads/" + branch)
    rows = [line.split() for line in output.splitlines()]
    if len(rows) != 1 or rows[0][1] != "refs/heads/" + branch:
        raise Error("publication branch is missing or ambiguous")
    return rows[0][0]


def _readback(context, directory, receipt):
    readback = directory / ("readback-" + uuid.uuid4().hex)
    pin = {
        "schema_version": 1,
        "status": "published",
        "repository": receipt["pin"]["repository"],
        "revision": receipt["commit"],
        "sha256": receipt["archive_sha256"],
    }
    _fetch(context, readback, pin)
    if _tracked(context, readback) != receipt["tracked"]:
        raise Error("published public source tree differs from the reviewed candidate")
    return pin


def _select_pin(context, directory, receipt, pin_path):
    if receipt["private_root"] != context["root"]:
        raise Error("publication receipt belongs to another private entry")
    if _private_inputs(context) != receipt["private_inputs"]:
        raise Error("private composition changed after source check; preserve pin and check again")
    pin = _readback(context, directory, receipt)
    selected = directory / "bootstrap.lock.json"
    atomic_json(selected, pin)
    if load(pin_path) == pin:
        identifier = receipt.get("pin_candidate", "")
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise Error("published source pin has no recorded candidate")
        candidate_directory = Path(context["paths"]["state"]) / "candidates" / identifier
        candidate_path = candidate_directory / "candidate.json"
        staged_pin = candidate_directory / "private/bootstrap.lock.json"
        for path in (candidate_path, staged_pin):
            no_symlink_parents(path, Path(context["home"]).resolve())
        candidate = load(candidate_path)
        expected = [
            {
                "repository": "private",
                "path": "bootstrap.lock.json",
                "sha256": digest(selected),
                "before": receipt["private_pin_before"],
            }
        ]
        # The new pin selects a different immutable engine cache on restart.
        # Compare against the roots journaled before staging, not that new cache.
        roots = receipt.get("pin_roots")
        if (
            not isinstance(roots, dict)
            or set(roots) != {"public", "private"}
            or roots["private"] != context["root"]
            or candidate.get("roots") != roots
            or candidate.get("id") != identifier
            or candidate.get("files") != expected
            or candidate.get("status") not in ("promoting", "promoted")
        ):
            raise Error("published source pin candidate receipt changed")
        if (
            staged_pin.is_symlink()
            or not staged_pin.is_file()
            or digest(staged_pin) != digest(selected)
            or digest(pin_path) != digest(selected)
        ):
            raise Error("published source pin candidate bytes changed")
        if candidate["status"] == "promoting":
            candidate["status"] = "promoted"
            atomic_json(candidate_path, candidate)
        receipt["status"] = "complete"
        atomic_json(directory / "receipt.json", receipt)
        return _result(directory, receipt)
    if digest(pin_path) != receipt["private_pin_before"]:
        raise Error("publication preserves a foreign private source-pin edit")
    atomic_json(
        directory / "pin-bundle.json",
        {
            "schema_version": 1,
            "files": [
                {
                    "repository": "private",
                    "path": "bootstrap.lock.json",
                    "source": "bootstrap.lock.json",
                    "sha256": digest(selected),
                }
            ],
        },
    )
    receipt["status"] = "pinning"
    atomic_json(directory / "receipt.json", receipt)
    if receipt.get("pin_candidate"):
        candidate_path = (
            Path(context["paths"]["state"])
            / "candidates"
            / receipt["pin_candidate"]
            / "candidate.json"
        )
        candidate = load(candidate_path)
        if candidate.get("status") == "promoting":
            updates.recover_candidate(context, receipt["pin_candidate"])
            receipt.pop("pin_candidate")
        elif candidate.get("status") in ("failed", "recovered"):
            receipt.pop("pin_candidate")
    if not receipt.get("pin_candidate"):
        receipt["pin_roots"] = {"public": context["base"], "private": context["root"]}
        atomic_json(directory / "receipt.json", receipt)
        candidate = updates.stage_candidate(context, directory / "pin-bundle.json")
        receipt["pin_candidate"] = candidate["id"]
        atomic_json(directory / "receipt.json", receipt)
    updates.promote(context, receipt["pin_candidate"])
    if load(pin_path) != pin:
        raise Error("published private pin readback failed")
    receipt["status"] = "complete"
    atomic_json(directory / "receipt.json", receipt)
    return _result(directory, receipt)


def _publish(context, directory, receipt, pin_path, options):
    if not options.get("yes"):
        raise Error("source publish requires explicit operator --yes confirmation")
    if not receipt["files"]:
        raise Error("source candidate has no update content to publish")
    tree = _assert_unchanged(context, directory, receipt)
    if receipt["status"] not in ("checked", "committed", "pushing", "published", "pinning"):
        raise Error("source publish requires a successfully checked candidate")
    if (
        _private_inputs(context) != receipt["private_inputs"]
        or digest(pin_path) != receipt["private_pin_before"]
    ):
        raise Error("source/private inputs changed since check")
    branch = options.get("branch", receipt.get("branch", "main"))
    if receipt.get("branch") and receipt["branch"] != branch:
        raise Error("publication cannot change its recorded destination branch")
    signing = _signing(context)
    if not receipt.get("commit"):
        if (
            _remote_tip(context, tree, receipt["pin"]["repository"], branch)
            != receipt["pin"]["revision"]
        ):
            raise Error(
                "public branch advanced; prepare from a reviewed new source pin, never force push"
            )
        receipt.update(status="committing", branch=branch)
        atomic_json(directory / "receipt.json", receipt)
        _git(context, tree, "add", "--", *receipt["files"])
        staged = set(
            filter(
                None,
                _git(
                    context,
                    tree,
                    "diff",
                    "--cached",
                    "--name-only",
                    "-z",
                    receipt["pin"]["revision"],
                    "--",
                ).split("\0"),
            )
        )
        if staged != set(receipt["files"]):
            raise Error("publication index contains foreign staged content")
        for name, checksum in receipt["files"].items():
            if (
                hashlib.sha256(_git(context, tree, "show", ":" + name, binary=True)).hexdigest()
                != checksum
            ):
                raise Error("publication index differs from checked desired-state bytes")
        _git(
            context,
            tree,
            "commit",
            "-S",
            "-m",
            options.get("message", "Update reviewed native desired-state locks"),
            settings=signing,
        )
        receipt["commit"] = _git(context, tree, "rev-parse", "HEAD")
        receipt["archive_sha256"] = hashlib.sha256(
            _archive(context, tree, receipt["commit"])
        ).hexdigest()
        receipt["status"] = "committed"
        atomic_json(directory / "receipt.json", receipt)
    _assert_unchanged(context, directory, receipt)
    _git(context, tree, "verify-commit", receipt["commit"], settings=signing)
    if _git(context, tree, "rev-parse", receipt["commit"] + "^") != receipt["pin"]["revision"]:
        raise Error("publication commit is not the single reviewed update on its immutable base")
    tip = _remote_tip(context, tree, receipt["pin"]["repository"], branch)
    if tip not in (receipt["pin"]["revision"], receipt["commit"]):
        raise Error("public branch advanced; refusing non-fast-forward publication")
    if tip != receipt["commit"]:
        receipt["status"] = "pushing"
        atomic_json(directory / "receipt.json", receipt)
        _git(
            context,
            tree,
            "push",
            "--porcelain",
            receipt["pin"]["repository"],
            receipt["commit"] + ":refs/heads/" + branch,
            settings=signing,
        )
    if _remote_tip(context, tree, receipt["pin"]["repository"], branch) != receipt["commit"]:
        raise Error("published branch readback differs from the signed update")
    receipt["status"] = "published"
    atomic_json(directory / "receipt.json", receipt)
    return _select_pin(context, directory, receipt, pin_path)


def run(context, operation, options=None):
    """Action-specific, JSON-safe API. No arbitrary command/file-update gateway."""
    options = dict(options or {})
    if operation not in ACTIONS or set(options) - OPTIONS.get(operation, set()):
        raise Error("unsupported source operation/options")
    if "yes" in options and not isinstance(options["yes"], bool):
        raise Error("source confirmation must be boolean")
    if options.get("family", "all") not in ("mise", "desktop", "all"):
        raise Error("source family must be mise, desktop, or all")
    for key in ("qt_version", "rust_version", "wayland_version", "protocols_version"):
        if key in options and (
            not isinstance(options[key], str)
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", options[key])
        ):
            raise Error("invalid exact desktop source version")
    if options.get("family") == "mise" and any(key.endswith("_version") for key in options):
        raise Error("desktop version options cannot accompany mise-only preparation")
    if "branch" in options and (
        not isinstance(options["branch"], str)
        or len(options["branch"]) > 255
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}(?:/[A-Za-z0-9][A-Za-z0-9_-]{0,63})*",
            options["branch"],
        )
    ):
        raise Error("publication branch requires safe nonempty ref components")
    if "message" in options and (
        not isinstance(options["message"], str)
        or not options["message"].strip()
        or len(options["message"]) > 500
        or "\0" in options["message"]
    ):
        raise Error("invalid source commit message")
    state, pin_path = _paths(context)
    if operation == "status":
        if not (state / "active.json").exists():
            return {"status": "absent", "pin": load(pin_path), "live_runtime_changed": False}
        directory, receipt = _receipt(state)
        _assert_unchanged(context, directory, receipt)
        return _result(directory, receipt)
    with locked(state / ".lock"):
        if operation == "prepare":
            return _prepare(context, state, pin_path, options)
        directory, receipt = _receipt(state)
        if operation == "check":
            return _check(context, directory, receipt)
        if operation == "publish":
            return _publish(context, directory, receipt, pin_path, options)
        # Recovery never creates a commit or pushes. It may only finish an
        # already published/read-back private pin under renewed confirmation.
        tree = _assert_unchanged(context, directory, receipt)
        if receipt["status"] == "committing":
            head = _git(context, tree, "rev-parse", "HEAD")
            if head == receipt["pin"]["revision"]:
                receipt["status"] = "checked"
                atomic_json(directory / "receipt.json", receipt)
                return _result(directory, receipt)
            if not options.get("yes"):
                raise Error("recovering a signed source commit requires recover --yes")
            if _git(context, tree, "rev-parse", head + "^") != receipt["pin"]["revision"]:
                raise Error("interrupted source commit has a foreign parent")
            _git(context, tree, "verify-commit", head, settings=_signing(context))
            receipt.update(
                commit=head,
                status="committed",
                archive_sha256=hashlib.sha256(_archive(context, tree, head)).hexdigest(),
            )
            atomic_json(directory / "receipt.json", receipt)
        if (
            receipt.get("commit")
            and _remote_tip(context, tree, receipt["pin"]["repository"], receipt["branch"])
            == receipt["commit"]
        ):
            if not options.get("yes"):
                raise Error("finishing a published source pin requires recover --yes")
            _git(context, tree, "verify-commit", receipt["commit"], settings=_signing(context))
            return _select_pin(context, directory, receipt, pin_path)
        return {
            **_result(directory, receipt),
            "next": "check or publish --yes; recovery never pushes",
        }
