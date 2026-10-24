"""Immutable Git source acquisition for bootstrap/cache and disposable tests.

Production requires a published commit plus SHA-256 of `git archive --format=tar
REVISION`. Mutable source overrides are verification-only and never inferred.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.parse

from safety import Error, atomic_json, digest, extract, inventory, load, locked, native


def resolve(lock_path, cache, *, override=None, verification_only=False):
    if override is not None:
        if not verification_only:
            raise Error("--bootstrap-source is verification-only")
        source = Path(override).resolve()
        if not (source / "native/engine/__main__.py").is_file():
            raise Error("local source does not contain native engine")
        return source
    lock = load(lock_path)
    if lock.get("schema_version") != 1 or lock.get("status") != "published":
        raise Error(
            "native public source is unpublished; publish/pin separately or use an explicit verification-only override"
        )
    revision, checksum, repository = (
        lock.get("revision", ""),
        lock.get("sha256", ""),
        lock.get("repository", ""),
    )
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise Error("source requires immutable commit and git-archive SHA-256")
    scheme = urllib.parse.urlsplit(repository).scheme
    if scheme != "https" and not (verification_only and scheme == "file"):
        raise Error("public source repository must use HTTPS")
    cache = Path(cache)
    destination = cache / revision
    with locked(cache / ".source-lock"):
        if destination.exists():
            archive = destination / ".bootstrap-source.tar"
            if archive.is_symlink() or not archive.is_file() or digest(archive) != checksum:
                raise Error("immutable source archive cache integrity failed")
            receipt_path = destination / ".bootstrap-receipt.json"
            if not receipt_path.exists():
                staged = Path(tempfile.mkdtemp(prefix=".readback-", dir=cache))
                try:
                    extract(archive, staged)
                    actual = inventory(destination)
                    actual.pop(".bootstrap-source.tar", None)
                    if actual != inventory(staged):
                        raise Error("immutable source cache integrity failed")
                    atomic_json(receipt_path, {"pin": lock, "files": inventory(destination)})
                finally:
                    shutil.rmtree(staged)
            record = load(receipt_path)
            expected = record["files"]
            actual = inventory(destination)
            actual.pop(".bootstrap-receipt.json", None)
            if record.get("pin") != lock or actual != expected:
                raise Error("immutable source cache integrity failed")
            return destination
        git = native("/usr/bin/git")
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(cache),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }
        stage = Path(tempfile.mkdtemp(prefix=".source-", dir=cache))

        def command(*arguments, binary=False):
            result = subprocess.run(
                [git, "-c", "core.hooksPath=/dev/null", *map(str, arguments)],
                env=environment,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                raise Error(f"source acquisition failed: {result.stderr.decode(errors='replace')}")
            return result.stdout if binary else result.stdout.decode().strip()

        try:
            repository_path = stage / "repository"
            command("init", "--quiet", repository_path)
            command(
                "-C",
                repository_path,
                "-c",
                "protocol.file.allow=" + ("always" if verification_only else "never"),
                "fetch",
                "--quiet",
                "--depth=1",
                repository,
                revision,
            )
            if command("-C", repository_path, "rev-parse", "FETCH_HEAD") != revision:
                raise Error("fetched source revision mismatch")
            archive = stage / "source.tar"
            archive.write_bytes(
                command("-C", repository_path, "archive", "--format=tar", "FETCH_HEAD", binary=True)
            )
            if digest(archive) != checksum:
                raise Error("source archive SHA-256 mismatch")
            tree = stage / "tree"
            extract(archive, tree)
            shutil.move(archive, tree / ".bootstrap-source.tar")
            atomic_json(tree / ".bootstrap-receipt.json", {"pin": lock, "files": inventory(tree)})
            os.replace(tree, destination)
        finally:
            shutil.rmtree(stage)
    return destination
