"""Portable engine primitives. No caller PATH, shell interpolation or Nix fallback."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile


class Error(RuntimeError):
    pass


def load(path):
    if Path(path).is_symlink():
        raise Error(f"refusing symlink JSON source/record: {path}")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Error(f"duplicate JSON key: {key} in {path}")
            result[key] = value
        return result

    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=pairs)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise Error(f"invalid relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise Error(f"unsafe relative path: {value!r}")
    return path


def inside(root, value, *, existing=False):
    root = Path(root).resolve()
    path = root.joinpath(*relative(value).parts)
    if not path.resolve().is_relative_to(root):
        raise Error(f"path escapes owning repository: {value}")
    if existing and not path.exists():
        raise Error(f"missing source: {path}")
    return path


def no_symlink_parents(path, boundary):
    path, boundary = Path(path), Path(boundary)
    if not path.is_absolute() or not path.is_relative_to(boundary):
        raise Error(f"destination escapes managed root: {path}")
    current = boundary
    if current.is_symlink():
        raise Error(f"symlink managed root: {current}")
    for component in path.relative_to(boundary).parts[:-1]:
        current /= component
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise Error(f"unsafe destination ancestor: {current}")


def atomic_bytes(path, data, mode=0o600, *, replace_symlink=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() and not replace_symlink:
        raise Error(f"refusing symlink record: {path}")
    fd, temporary = tempfile.mkstemp(prefix=".dotfiles-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


@contextlib.contextmanager
def locked(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / "operation.lock"
    if path.is_symlink():
        raise Error(f"unsafe lock: {path}")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Error("another dotfiles operation holds the lock") from exc
        yield
    finally:
        os.close(fd)


def native(path, *, exists=True):
    path = Path(path)
    if not path.is_absolute() or "/nix/" in str(path) or "/nix/" in str(path.resolve()):
        raise Error(f"non-native executable path: {path}")
    if exists and (not path.is_file() or not os.access(path, os.X_OK)):
        raise Error(f"native prerequisite unavailable: {path}; run explicit provision/bootstrap")
    return str(path)


def command(context, name, default, *, mutating=False):
    """Native provider commands; only explicit fixtures may substitute them."""
    value = default
    if context.get("_fixture_context"):
        value = context["executables"].get(name, default)
    if mutating and context.get("_verification_only"):
        if (
            not context.get("_fixture_context")
            or value == default
            or not Path(value).resolve().is_relative_to(Path(context["home"]).resolve())
        ):
            raise Error(
                f"verification refuses live {name} mutation; supply an isolated fixture executable"
            )
    return native(value)


def environment(context):
    home = context["home"]
    paths = context["paths"]
    result = {
        "HOME": home,
        "USER": context.get("identity", {}).get("username", Path(home).name),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "XDG_CONFIG_HOME": paths["config"],
        "XDG_DATA_HOME": str(Path(paths["data"]).parent),
        "XDG_CACHE_HOME": str(Path(paths["cache"]).parent),
        "XDG_STATE_HOME": str(Path(paths["state"]).parent),
        "MISE_DATA_DIR": str(Path(paths["data"]) / "mise"),
        "MISE_CACHE_DIR": str(Path(paths["cache"]) / "mise"),
        "MISE_CONFIG_DIR": str(Path(paths["data"]) / "mise-config"),
        "MISE_GLOBAL_CONFIG_FILE": str(Path(context["base"]) / "apps/mise/mise.toml"),
        "MISE_ENV_FILE": "",
        "MISE_AUTO_INSTALL": "0",
        "MISE_LOCKED": "1",
        "MISE_NO_ENV": "1",
        "MISE_NO_HOOKS": "1",
        "MISE_SAFE": "1",
        "MISE_CEILING_PATHS": "/",
        "MISE_SYSTEM_CONFIG_DIR": str(Path(paths["data"]) / "mise-empty-system"),
        "MISE_SYSTEM_CONFIG_FILE": "/dev/null",
        "MISE_TRUSTED_CONFIG_PATHS": str(Path(context["base"]) / "apps/mise/mise.toml"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if context.get("platform") == "linux-x64":
        runtime = Path("/run/user") / str(os.getuid())
        if runtime.is_dir():
            result["XDG_RUNTIME_DIR"] = str(runtime)
            result["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    return result


def run(argv, context, *, cwd=None, capture=True, input=None, extra_env=None):
    argv = [str(item) for item in argv]
    native(argv[0])
    env = environment(context)
    env.update(extra_env or {})
    result = subprocess.run(
        argv,
        cwd=cwd or context["base"],
        env=env,
        input=input,
        text=True,
        capture_output=capture,
        stdout=None if capture else sys.stderr,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or "").strip() if capture else ""
        raise Error(
            f"command failed ({result.returncode}): {argv[0]} {' '.join(argv[1:3])}: {detail}"
        )
    return result.stdout if capture else ""


def download(url, sha256, cache):
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(c not in "0123456789abcdef" for c in sha256)
    ):
        raise Error("artifact requires an exact lowercase SHA-256")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise Error("artifact requires credential-free HTTPS")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / sha256
    if target.is_symlink():
        raise Error(f"unsafe download cache: {target}")
    if target.exists():
        if digest(target) != sha256:
            raise Error(f"corrupt cached artifact: {target}")
        return target
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=cache)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "dotfiles-native/1"})
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            os.fdopen(fd, "wb") as stream,
        ):
            if urllib.parse.urlsplit(response.url).scheme != "https":
                raise Error("download redirected away from HTTPS")
            shutil.copyfileobj(response, stream)
        if digest(temporary) != sha256:
            raise Error(f"artifact SHA-256 mismatch: {url}")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def extract(archive, output, kind="tar", strip_components=0):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if not isinstance(strip_components, int) or strip_components < 0:
        raise Error("strip_components must be a nonnegative integer")
    if kind in ("raw", "binary", "file", "appimage"):
        raise Error("raw artifacts require an explicit destination filename")

    def destination(name):
        parts = relative(name.rstrip("/")).parts
        parts = parts[strip_components:]
        return None if not parts else output.joinpath(*parts)

    if kind == "zip":
        with zipfile.ZipFile(archive) as source:
            symlinks = []
            for member in source.infolist():
                path = destination(member.filename)
                if path is None:
                    continue
                no_symlink_parents(path, output)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    target = source.read(member).decode("utf-8")
                    if Path(target).is_absolute() or not (
                        path.parent / target
                    ).resolve().is_relative_to(output.resolve()):
                        raise Error(f"escaping zip symlink: {member.filename}")
                    symlinks.append((path, target))
                elif member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as src, path.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                    path.chmod(0o755 if mode & 0o111 else 0o644)
            for path, target in symlinks:
                no_symlink_parents(path, output)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(target)
    elif kind in ("tar", "tar.gz", "tar.xz", "tgz", "txz", "tar.bz2"):
        with tarfile.open(archive, "r:*") as source:
            members = []
            for member in source.getmembers():
                name = member.name.removeprefix("./").rstrip("/")
                if not name:
                    continue
                path = destination(name)
                if path is None:
                    continue
                if member.isdev() or member.isfifo() or member.islnk():
                    raise Error(f"unsafe archive member: {member.name}")
                if member.issym():
                    if Path(member.linkname).is_absolute() or not (
                        path.parent / member.linkname
                    ).resolve().is_relative_to(output.resolve()):
                        raise Error(f"escaping archive symlink: {member.name}")
                elif not (member.isfile() or member.isdir()):
                    raise Error(f"unsupported archive member: {member.name}")
                members.append((member, path))
            for member, path in members:
                no_symlink_parents(path, output)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.issym():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.symlink_to(member.linkname)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as src, path.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                    path.chmod(0o755 if member.mode & 0o111 else 0o644)
    else:
        raise Error(f"unsupported archive format: {kind}")


def fingerprint(path):
    path = Path(path)
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if path.is_file():
        return {"kind": "file", "sha256": digest(path), "mode": stat.S_IMODE(path.stat().st_mode)}
    if path.exists():
        raise Error(f"managed leaf is not a regular file or symlink: {path}")
    return None


def inventory(root):
    root = Path(root)
    return {
        path.relative_to(root).as_posix(): fingerprint(path)
        for path in sorted(root.rglob("*"))
        if path.is_symlink() or path.is_file()
    }


def check_inventory(root, expected):
    actual = inventory(root)
    if actual != expected:
        raise Error(f"managed artifact content changed: {root}")
