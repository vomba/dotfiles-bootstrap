#!/usr/bin/env python3
"""Check supported overlay releases; change complete source groups only with --apply."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import urllib.request

EXIT_SUCCESS = 0
EXIT_UPDATES_AVAILABLE = 1
EXIT_ERROR = 2
HASH_PATTERN = re.compile(r"sha256-[A-Za-z0-9+/]{43}=")
VERSION_PATTERN = re.compile(r'\bversion\s*=\s*"([^"\n]+)";')
SYSTEMS = frozenset({
    "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
    "x86_64-apple-darwin", "aarch64-apple-darwin",
})
NORDIC_PINNED = frozenset({
    "Nordic", "Nordic-standard-buttons", "Nordic-darker",
    "Nordic-darker-standard-buttons", "Nordic-bluish-accent",
    "Nordic-bluish-accent-standard-buttons", "Nordic-Polar",
    "Nordic-Polar-standard-buttons",
})
GITHUB_URL = re.compile(
    r"https://github\.com/(?P<owner>[\w-]+)/(?P<repo>[\w-]+)/releases/download/"
    r"v\$\{version\}/(?P<artifact>[^\"\n]+)"
)
PLUGIN = re.compile(
    r'buildHelmPlugin\s+rec\s*\{\s*pluginName\s*=\s*"(?P<name>[^"\n]+)";'
    r'\s*version\s*=\s*"(?P<version>[^"\n]+)";'
    r'\s*src\s*=\s*super\.fetchurl\s*\{\s*url\s*=\s*"(?P<url>[^"\n]+)";'
    r'\s*hash\s*=\s*"(?P<hash>[^"\n]+)";\s*\};\s*\}', re.MULTILINE,
)
MAP_SOURCE = re.compile(
    r'\b(?P<name>cli-src|tui-src)\s*=\s*fetchurl\s*\{\s*url\s*=\s*"(?P<url>[^"\n]+)";'
    r'\s*sha256\s*=\s*\{(?P<hashes>.*?)\}\s*\.\$\{system\}\s*;', re.DOTALL,
)
MAP_HASH = re.compile(r'"(?P<system>[^"\n]+)"\s*=\s*"(?P<hash>[^"\n]+)"\s*;')
NORDIC_SOURCE = re.compile(r'\(fetchFromGitHub\s*\{(?P<body>[^{}]+)\}\)')
FIELD = re.compile(r'\b(?P<key>owner|repo|rev|hash|name)\s*=\s*"(?P<value>[^"\n]+)";')


class UpdateCheckError(RuntimeError):
    """An update cannot be determined or safely applied."""


def github_request(url):
    request = urllib.request.Request(url)
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    request.add_header("User-Agent", "check-updates.py")
    return request


def get_latest_github_release(owner, repo):
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        with urllib.request.urlopen(github_request(url), timeout=30) as response:
            document = json.loads(response.read().decode())
            tag = document.get("tag_name") if isinstance(document, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateCheckError(f"failed to fetch the latest release for {owner}/{repo}: {error}") from error
    version = tag.removeprefix("v") if isinstance(tag, str) else ""
    if not re.fullmatch(r"[0-9][A-Za-z0-9._+-]*", version):
        raise UpdateCheckError(f"invalid release tag for {owner}/{repo}: {tag!r}")
    return version


def prefetch_url(url):
    if not re.fullmatch(
        r"https://github\.com/[\w-]+/[\w-]+/releases/download/v[0-9][A-Za-z0-9._+-]*/[^\s]+", url
    ):
        raise UpdateCheckError(f"unsupported artifact URL: {url}")
    try:
        result = subprocess.run(
            ["nix", "store", "prefetch-file", "--json", url],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateCheckError(f"prefetch failed for {url}: {error}") from error
    if result.returncode:
        raise UpdateCheckError(f"prefetch failed for {url}: {result.stderr.strip() or result.returncode}")
    try:
        hash_value = json.loads(result.stdout)["hash"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise UpdateCheckError(f"invalid prefetch result for {url}: {error}") from error
    if not isinstance(hash_value, str) or not HASH_PATTERN.fullmatch(hash_value):
        raise UpdateCheckError(f"invalid sha256 hash for {url}")
    return hash_value


def single_version(content, name):
    matches = list(VERSION_PATTERN.finditer(content))
    if len(matches) != 1:
        raise UpdateCheckError(f"{name}: expected exactly one version declaration")
    return matches[0]


def validated_url(url, owner=None, repo=None):
    match = GITHUB_URL.fullmatch(url)
    if not match or (owner and (owner, repo) != (match["owner"], match["repo"])):
        raise UpdateCheckError(f"unsupported release URL: {url}")
    return match

def validate_versioned_urls(content, sources, filename):
    # This is a supported-source inventory, not a Nix parser: refuse an
    # additional URL using the same version instead of publishing stale hashes.
    declared = {source.start("url") for source in sources}
    bound = set()
    offset = 0
    for line in content.splitlines(keepends=True):
        if not line.lstrip().startswith("#"):
            for match in re.finditer(r'\burl\s*=\s*"(?P<url>[^"\n]+)"\s*;', line):
                if "${version}" in match["url"]:
                    bound.add(offset + match.start("url"))
        offset += len(line)
    if bound != declared:
        raise UpdateCheckError(f"{filename}: unsupported or missing version-bound release source")


def validate_openstack_urls(content, sources, filename):
    validate_versioned_urls(content, sources, filename)
    for source in sources:
        url = validated_url(source["url"], "gtema", "openstack")
        expected = f"openstack_{source['name'].split('-', 1)[0]}-${{system}}.tar.xz"
        if url["artifact"] != expected:
            raise UpdateCheckError(f"{filename}: wrong artifact for {source['name']}")


def validate_plugin_urls(content, sources, filename):
    validate_versioned_urls(content, sources, filename)
    for source in sources:
        url = validated_url(source["url"], "jkroepke", "helm-secrets")
        artifact = source["name"].removeprefix("helm-")
        if url["artifact"] != f"{artifact}-${{version}}.tgz":
            raise UpdateCheckError(f"{filename}: wrong artifact for {source['name']}")



def new_artifact_hash(url, current, version):
    if not HASH_PATTERN.fullmatch(current):
        raise UpdateCheckError(f"missing or malformed hash for {url}")
    actual_url = url.replace("${version}", version)
    print(f"Prefetching {actual_url}", flush=True)
    return prefetch_url(actual_url)


def with_replacements(content, replacements):
    updated = content
    boundary = len(content)
    for start, end, replacement in sorted(replacements, reverse=True):
        if start < 0 or end > boundary or start >= end:
            raise UpdateCheckError("overlapping or invalid edit locations")
        updated = updated[:start] + replacement + updated[end:]
        boundary = start
    return updated


def plan_openstack(content, filename):
    version = single_version(content, filename)
    matches = list(MAP_SOURCE.finditer(content))
    if {match["name"] for match in matches} != {"cli-src", "tui-src"} or len(matches) != 2:
        raise UpdateCheckError(f"{filename}: expected both CLI and TUI source maps")
    validate_openstack_urls(content, matches, filename)
    owner, repo = "gtema", "openstack"
    latest = get_latest_github_release(owner, repo)
    if latest == version[1]:
        print(f"[{filename}] Up to date ({latest})")
        return False, content
    print(f"[{filename}] UPDATE AVAILABLE: {version[1]} -> {latest} (https://github.com/{owner}/{repo})")
    replacements = [(version.start(1), version.end(1), latest)]
    for source in matches:
        url = source["url"]
        hashes = list(MAP_HASH.finditer(source["hashes"]))
        if {item["system"] for item in hashes} != SYSTEMS or len(hashes) != len(SYSTEMS):
            raise UpdateCheckError(f"{filename}: missing or duplicate platform in {source['name']}")
        # Reject text between map entries rather than silently retaining an unsupported entry.
        residue = MAP_HASH.sub("", source["hashes"])
        if residue.strip():
            raise UpdateCheckError(f"{filename}: unsupported hash-map entry in {source['name']}")
        for item in hashes:
            artifact_url = url.replace("${system}", item["system"])
            new_hash = new_artifact_hash(artifact_url, item["hash"], latest)
            offset = source.start("hashes")
            replacements.append((offset + item.start("hash"), offset + item.end("hash"), new_hash))
    return True, with_replacements(content, replacements)


def plan_plugins(content, filename):
    sources = list(PLUGIN.finditer(content))
    if len(sources) != 3 or {item["name"] for item in sources} != {
        "helm-secrets", "helm-secrets-getter", "helm-secrets-post-renderer"
    }:
        raise UpdateCheckError(f"{filename}: expected all three helm-secrets release artifacts")
    validate_plugin_urls(content, sources, filename)
    urls = [validated_url(item["url"]) for item in sources]
    owner, repo = "jkroepke", "helm-secrets"
    if len({item["version"] for item in sources}) != 1:
        raise UpdateCheckError(f"{filename}: plugin versions differ")
    latest = get_latest_github_release(owner, repo)
    current = sources[0]["version"]
    if latest == current:
        print(f"[{filename}] Plugin artifacts up to date ({current})")
        return False, content
    print(f"[{filename}] UPDATE AVAILABLE: {current} -> {latest} (https://github.com/{owner}/{repo})")
    replacements = []
    for source, url in zip(sources, urls):
        if "${version}" not in url["artifact"] or source["url"].count("${version}") != 2:
            raise UpdateCheckError(f"{filename}: unsupported versioned artifact for {source['name']}")
        replacements.append((source.start("version"), source.end("version"), latest))
        replacements.append((source.start("hash"), source.end("hash"),
                             new_artifact_hash(source["url"], source["hash"], latest)))
    return True, with_replacements(content, replacements)


def report_nordic(content, filename):
    srcs = re.search(r'\bsrcs\s*=\s*\[(?P<body>.*?)\];', content, re.DOTALL)
    if not srcs:
        raise UpdateCheckError(f"{filename}: missing source list")
    sources = list(NORDIC_SOURCE.finditer(srcs["body"]))
    if NORDIC_SOURCE.sub("", srcs["body"]).strip() or len(sources) != len(NORDIC_PINNED):
        raise UpdateCheckError(f"{filename}: unsupported or missing Nordic source")
    names = set()
    for source in sources:
        fields = {item["key"]: item["value"] for item in FIELD.finditer(source["body"])}
        if len(fields) != 5 or not HASH_PATTERN.fullmatch(fields.get("hash", "")):
            raise UpdateCheckError(f"{filename}: incomplete Nordic source record")
        if FIELD.sub("", source["body"]).strip() or fields["owner"] != "EliverLara":
            raise UpdateCheckError(f"{filename}: unsupported Nordic source record")
        names.add(fields["name"])
        print(f"[{filename}] {fields['name']}: pinned at {fields['rev']} (manual variant)")
    if names != NORDIC_PINNED:
        raise UpdateCheckError(f"{filename}: unexpected or duplicate Nordic variants")
    return False


def publish(file_path, original, replacement, original_stat):
    if original == replacement:
        raise UpdateCheckError(f"{file_path.name}: no validated changes to publish")
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{file_path.name}.", dir=file_path.parent)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(original_stat.st_mode))
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        current = file_path.lstat()
        if (current.st_ino, current.st_dev, current.st_mtime_ns, current.st_mode) != (
            original_stat.st_ino, original_stat.st_dev, original_stat.st_mtime_ns, original_stat.st_mode
        ) or file_path.read_bytes() != original:
            raise UpdateCheckError(f"{file_path.name}: file changed while preparing update")
        os.replace(temporary, file_path)
        temporary = None
    except OSError as error:
        raise UpdateCheckError(f"{file_path.name}: unable to publish update: {error}") from error
    finally:
        if temporary is not None:
            os.unlink(temporary)


def check_file(file_path, apply_updates=False):
    file_path = Path(file_path)
    original_stat = file_path.lstat()
    if not stat.S_ISREG(original_stat.st_mode):
        raise UpdateCheckError(f"{file_path.name}: not a regular file")
    original = file_path.read_bytes()
    try:
        content = original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UpdateCheckError(f"{file_path.name}: not UTF-8: {error}") from error
    filename = file_path.name
    if filename == "openstack-tui.nix":
        if apply_updates:
            available, planned = plan_openstack(content, filename)
        else:
            available = report_release_from_url(content, filename, "gtema", "openstack")
            planned = content
    elif filename == "helm.nix":
        if apply_updates:
            available, planned = plan_plugins(content, filename)
        else:
            available = report_release_from_url(content, filename, "jkroepke", "helm-secrets")
            planned = content
    elif filename == "nordic.nix":
        available, planned = report_nordic(content, filename), content
    elif "fetchFromGitHub" in content or "releases/download/" in content:
        raise UpdateCheckError(f"{filename}: unsupported source policy")
    else:
        return False, False
    changed = planned != content
    if changed and apply_updates:
        publish(file_path, original, planned.encode("utf-8"), original_stat)
        print(f"[{filename}] Applied complete source/hash update")
    return available, changed and apply_updates


def report_release_from_url(content, filename, owner, repo):
    # Checking never prefetches, drafts edits, or mutates a file.
    if filename == "openstack-tui.nix":
        sources = list(MAP_SOURCE.finditer(content))
        if len(sources) != 2 or {source["name"] for source in sources} != {"cli-src", "tui-src"}:
            raise UpdateCheckError(f"{filename}: unsupported artifact source shape")
        validate_openstack_urls(content, sources, filename)
        for source in sources:
            hashes = list(MAP_HASH.finditer(source["hashes"]))
            if len(hashes) != len(SYSTEMS) or {item["system"] for item in hashes} != SYSTEMS:
                raise UpdateCheckError(f"{filename}: incomplete platform hashes")
            if MAP_HASH.sub("", source["hashes"]).strip() or any(
                not HASH_PATTERN.fullmatch(item["hash"]) for item in hashes
            ):
                raise UpdateCheckError(f"{filename}: invalid platform hashes")
        current = single_version(content, filename)[1]
    else:
        sources = list(PLUGIN.finditer(content))
        if len(sources) != 3 or {item["name"] for item in sources} != {
            "helm-secrets", "helm-secrets-getter", "helm-secrets-post-renderer"
        } or len({item["version"] for item in sources}) != 1:
            raise UpdateCheckError(f"{filename}: unsupported plugin source shape")
        validate_plugin_urls(content, sources, filename)
        for source in sources:
            if not HASH_PATTERN.fullmatch(source["hash"]):
                raise UpdateCheckError(f"{filename}: invalid plugin hash")
        current = sources[0]["version"]
    latest = get_latest_github_release(owner, repo)
    if current == latest:
        print(f"[{filename}] Up to date ({latest})")
        return False
    print(f"[{filename}] UPDATE AVAILABLE: {current} -> {latest} (https://github.com/{owner}/{repo})")
    return True


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Check for updates in supported Nix overlays")
    parser.add_argument("--apply", action="store_true", help="Apply complete validated updates")
    parser.add_argument("--dry-run", action="store_true", help="Report availability without writes")
    parser.add_argument("--yes", action="store_true", help="Confirm --apply without a prompt")
    parser.add_argument("--apply-force-rev", action="store_true", help="Reserved; unsafe without an explicit tracked source/ref and matching hash")
    args = parser.parse_args(argv)
    if (args.apply and args.dry_run) or (args.yes and not args.apply):
        parser.error("--apply and --dry-run conflict; --yes requires --apply")
    if args.apply_force_rev:
        parser.error("--apply-force-rev is unsafe: no configured tracked revision/ref and source hash policy")
    return args


def main(argv=None):
    try:
        args = parse_args(argv)
    except SystemExit as error:
        return EXIT_ERROR if error.code else EXIT_SUCCESS
    overlay_dir = Path(__file__).resolve().parent.parent / "overlays"
    if not overlay_dir.is_dir():
        print(f"Error: {overlay_dir} directory not found", file=sys.stderr)
        return EXIT_ERROR
    if args.apply and not args.yes:
        try:
            answer = input("Apply updates to overlay files? [y/N] ")
        except EOFError:
            answer = ""
        if answer.lower() != "y":
            print("Aborted.")
            return EXIT_SUCCESS
    print(f"Checking for updates in {overlay_dir}...", flush=True)
    any_available = False
    applied = []
    try:
        for file_path in sorted(overlay_dir.glob("*.nix")):
            if file_path.name == "default.nix":
                continue
            available, changed = check_file(file_path, args.apply)
            any_available |= available
            if changed:
                applied.append(file_path.name)
    except (UpdateCheckError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        if applied:
            print(f"Partial update: already applied {', '.join(applied)}; remaining files not checked.", file=sys.stderr)
        return EXIT_ERROR
    if args.apply:
        print(f"Applied: {', '.join(applied) if applied else 'none'}")
    return EXIT_UPDATES_AVAILABLE if any_available else EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
