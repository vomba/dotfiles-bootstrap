"""Read-only freshness reports and coherent, checksum-validated update bundles.

`update --candidate bundle.json` stages reviewed file bytes, validates both
platform compositions, all local lock descriptors and all pinned download hashes.
`update --promote CANDIDATE_ID` atomically publishes only those reviewed manifest/
lock files after checking the source has not changed. No package installation or
service activation is part of candidate promotion.

Freshness keeps desired, installed, provider-latest and project-upstream records
separate. ``provider_update`` uses the same typed, read-only metadata resolvers
as ``update``; it never installs a payload or proves its provenance. Comparisons
are conservative numeric release comparisons, not package-manager ordering.
"""

from __future__ import annotations

import json
import functools
import gzip
import html
import os
from pathlib import Path
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import uuid

from manifest import PLATFORMS, compose, validate
from providers import artifact_spec, mise_metadata, package_status
from safety import Error, atomic_bytes, atomic_json, digest, download, inside, load, relative, run


def public_json(url, headers=None, *, max_bytes=None):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise Error("upstream metadata requires HTTPS")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "dotfiles-native/1",
            "Accept": "application/json",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if urllib.parse.urlsplit(response.url).scheme != "https":
            raise Error("metadata redirected away from HTTPS")
        if max_bytes is None:
            return json.load(response)
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise Error("upstream metadata exceeds the reviewed response bound")
        return json.loads(payload)


def public_bytes(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise Error("public package metadata requires credential-free HTTPS")
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "dotfiles-native/1"}), timeout=30
    ) as response:
        if urllib.parse.urlsplit(response.url).scheme != "https":
            raise Error("package metadata redirected away from HTTPS")
        return response.read()


def compare_debian_versions(left, right):
    """Debian epoch/upstream/revision ordering for read-only repository reports."""

    def components(value):
        epoch, separator, rest = value.partition(":")
        if separator:
            if not epoch.isdigit():
                raise Error("invalid Debian package version epoch")
            value, number = rest, int(epoch)
        else:
            number = 0
        upstream, dash, revision = value.rpartition("-")
        return number, upstream if dash else value, revision if dash else "0"

    def part(a, b):
        def order(character):
            if character == "~":
                return -1
            if not character or character.isdigit():
                return 0
            return ord(character) if character.isalpha() else ord(character) + 256

        while a or b:
            while (a and not a[0].isdigit()) or (b and not b[0].isdigit()):
                x, y = order(a[:1]), order(b[:1])
                if x != y:
                    return (x > y) - (x < y)
                a, b = a[1:] if a else a, b[1:] if b else b
            a, b = a.lstrip("0"), b.lstrip("0")
            x, y = re.match(r"\d*", a).group(), re.match(r"\d*", b).group()
            if len(x) != len(y):
                return (len(x) > len(y)) - (len(x) < len(y))
            if x != y:
                return (x > y) - (x < y)
            a, b = a[len(x) :], b[len(y) :]
        return 0

    a, b = components(left), components(right)
    if a[0] != b[0]:
        return (a[0] > b[0]) - (a[0] < b[0])
    return part(a[1], b[1]) or part(a[2], b[2])


def upstream(record):
    spec = record.get("update", {})
    if not isinstance(spec, dict):
        raise Error("update metadata resolver must be a typed object")
    if not spec:
        declared = record.get("upstream", record.get("repository", ""))
        if isinstance(declared, str):
            parsed = urllib.parse.urlsplit(declared)
            parts = parsed.path.strip("/").split("/")
            if parsed.scheme == "https" and parsed.hostname == "github.com" and len(parts) >= 2:
                spec = {
                    "provider": "github",
                    "repository": "/".join(parts[:2]).removesuffix(".git"),
                    "tag_prefix": "v",
                }
    provider = spec.get("provider")
    if provider == "chromiumdash":
        url = "https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Linux&num=1"
        if spec.get("url") != url:
            raise Error("Chromium release metadata requires the reviewed Stable Linux endpoint")
        data = public_json(url, max_bytes=65536)
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise Error("ChromiumDash must return exactly one stable Linux release")
        release = data[0]
        version = release.get("version")
        if not isinstance(version, str) or not re.fullmatch(
            r"[0-9]{1,10}(?:\.[0-9]{1,10}){3}", version
        ):
            raise Error("ChromiumDash release lacks a bounded four-component version")
        if (
            release.get("channel", "Stable") != "Stable"
            or release.get("platform", "Linux") != "Linux"
        ):
            raise Error("ChromiumDash returned a non-stable or non-Linux release")
        return {
            "version": version,
            "url": url,
            "source": "official Chromium stable Linux project release",
            "integrity": "release metadata only; not Snap payload or assertion verification",
        }
    if provider == "github":
        repository = spec.get("repository", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise Error("invalid upstream repository")
        release = public_json(f"https://api.github.com/repos/{repository}/releases/latest")
        if release.get("draft") or release.get("prerelease"):
            raise Error("upstream did not return a stable release")
        tag = release["tag_name"]
        prefix = spec.get("tag_prefix", "v")
        return {
            "version": tag.removeprefix(prefix).removesuffix(spec.get("tag_suffix", "")),
            "tag": tag,
            "url": release["html_url"],
        }
    if provider == "pypi":
        package = spec["package"]
        data = public_json(
            "https://pypi.org/pypi/" + urllib.parse.quote(package, safe="") + "/json"
        )
        return {"version": data["info"]["version"], "url": data["info"]["package_url"]}
    if provider == "npm":
        package = spec["package"]
        data = public_json(
            "https://registry.npmjs.org/" + urllib.parse.quote(package, safe="") + "/latest"
        )
        return {"version": data["version"], "url": data["dist"]["tarball"]}
    if provider == "firefox-addon":
        slug = spec["slug"]
        data = public_json(
            "https://addons.mozilla.org/api/v5/addons/addon/"
            + urllib.parse.quote(slug, safe="")
            + "/"
        )
        release = data["current_version"]
        artifact = release["file"]
        return {"version": release["version"], "url": artifact["url"], "hash": artifact["hash"]}
    if provider == "electron-builder":
        url = spec["url"]
        if urllib.parse.urlsplit(url).scheme != "https":
            raise Error("electron-builder metadata requires HTTPS")
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "dotfiles-native/1"}), timeout=30
        ) as response:
            if urllib.parse.urlsplit(response.url).scheme != "https":
                raise Error("release metadata redirected away from HTTPS")
            text = response.read().decode("utf-8")
        match = re.search(
            r"""^version:\s*['"]?([0-9]+(?:\.[0-9]+)+(?:[-+][A-Za-z0-9.-]+)?)['"]?\s*$""",
            text,
            re.MULTILINE,
        )
        if not match:
            raise Error("official electron-builder metadata lacks a plain version scalar")
        return {
            "version": match.group(1),
            "url": url,
            "integrity": "metadata only; candidate asset SHA-256 must be reviewed independently",
        }
    if provider == "snap":
        name = spec["name"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise Error("invalid Snap package name")
        channel = spec.get("channel", "latest/stable")
        architecture = spec.get("architecture", "amd64")
        if not isinstance(channel, str) or not re.fullmatch(r"[a-z0-9][a-z0-9./-]{0,63}", channel):
            raise Error("invalid Snap channel")
        if architecture not in ("amd64", "arm64"):
            raise Error("unsupported Snap metadata architecture")
        data = public_json(
            "https://api.snapcraft.io/v2/snaps/info/" + name, {"Snap-Device-Series": "16"}
        )
        if not isinstance(data, dict) or not isinstance(data.get("channel-map"), list):
            raise Error("Snap store metadata lacks a channel map")
        matches = []
        for entry in data["channel-map"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("channel"), dict):
                raise Error("Snap store metadata contains a malformed channel entry")
            selected = entry.get("channel", {})
            full_name = selected.get("track", "latest") + "/" + selected.get("risk", "")
            if (
                selected.get("risk") == "stable"
                and selected.get("architecture") == architecture
                and channel in (selected.get("name"), full_name)
            ):
                matches.append(entry)
        if len(matches) != 1:
            raise Error("Snap store stable channel is missing or ambiguous")
        entry = matches[0]
        version, revision, asset = (
            entry.get("version"),
            entry.get("revision"),
            entry.get("download"),
        )
        if not isinstance(version, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,127}", version
        ):
            raise Error("Snap store metadata lacks a bounded package version")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, (int, str))
            or not re.fullmatch(r"[0-9]{1,12}", str(revision))
        ):
            raise Error("Snap store metadata lacks a valid revision")
        if not isinstance(asset, dict) or not isinstance(asset.get("url"), str):
            raise Error("Snap store metadata lacks an asset URL")
        parsed = urllib.parse.urlsplit(asset["url"])
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise Error("Snap store asset requires credential-free HTTPS")
        if not isinstance(asset.get("sha3-384"), str) or not re.fullmatch(
            r"[0-9a-fA-F]{96}", asset["sha3-384"]
        ):
            raise Error("Snap store metadata lacks an asset SHA3-384")
        return {
            "version": entry["version"],
            "revision": entry["revision"],
            "channel": channel,
            "url": entry["download"]["url"],
            "sha3-384": entry["download"]["sha3-384"],
            "integrity": "unsigned store metadata only; native snapd assertion/provenance verification remains required",
        }
    if provider == "apt-repository":
        url = spec["url"]
        payload = public_bytes(url)
        if payload.startswith(b"\x1f\x8b"):
            payload = gzip.decompress(payload)
        rows, current = [], {}
        for line in payload.decode("utf-8").splitlines() + [""]:
            if not line:
                if current.get("Package") == spec["package"] and current.get("Architecture") in (
                    spec.get("architecture", "amd64"),
                    "all",
                ):
                    rows.append(current)
                current = {}
            elif not line[0].isspace() and ":" in line:
                key, value = line.split(":", 1)
                current[key] = value.strip()
        if not rows:
            raise Error("APT repository lacks the selected package/architecture")
        selected = max(
            rows,
            key=functools.cmp_to_key(
                lambda a, b: compare_debian_versions(a["Version"], b["Version"])
            ),
        )
        return {
            "version": selected["Version"],
            "source": url,
            "url": urllib.parse.urljoin(spec.get("base_url", url), selected.get("Filename", "")),
            "sha256": selected.get("SHA256"),
            "integrity": "repository metadata only; signed index/package verification remains the native provider's responsibility",
        }
    if provider == "slack-macos":
        url = "https://slack.com/release-notes/mac"
        document = public_bytes(url).decode("utf-8")
        for heading in re.findall(r"<h2\b[^>]*>(.*?)</h2>", document, re.IGNORECASE | re.DOTALL):
            title = html.unescape(re.sub(r"<[^>]+>", " ", heading))
            match = re.search(r"\bSlack\s+([0-9]+\.[0-9]+\.[0-9]+)\b", title)
            if match:
                return {
                    "version": match.group(1),
                    "url": url,
                    "source": "official stable macOS release notes; asset integrity is separately pinned",
                }
        raise Error("official Slack macOS release notes lack a version heading")
    return {
        "status": "not-declared",
        "reason": "No reviewed upstream release resolver declared; provider freshness is not upstream freshness.",
    }


METADATA_ERRORS = (Error, OSError, ValueError, KeyError, TypeError, AttributeError)


def release_metadata(record, *, refresh, field="update"):
    """Resolve only declared typed metadata; never reinterpret desired as latest."""
    if not refresh:
        return {
            "status": "not-refreshed",
            "hint": "updates --refresh inspects public release metadata",
        }
    if field == "provider_update":
        if not record.get(field):
            return {
                "status": "not-declared",
                "reason": "No independent provider release resolver declared.",
            }
        record = {"update": record[field]}
    try:
        return upstream(record)
    except METADATA_ERRORS as exc:
        return {"status": "unavailable", "error": str(exc)}


def installed_release(observed):
    """Use readback, including the typed recipe envelope, never desired state."""
    if not isinstance(observed, dict):
        return None
    if observed.get("status") in ("unavailable", "unverified"):
        return None
    if isinstance(observed.get("installed"), str):
        return observed["installed"]
    checked = observed.get("check")
    if isinstance(checked, dict) and checked.get("status") == "verified":
        details = checked.get("details", {})
        if isinstance(details, dict):
            return details.get("installed_version")
    return None


def release_comparison(value, latest):
    """Only bounded numeric release versions have a trustworthy generic order."""
    target = latest.get("version") if isinstance(latest, dict) else None
    result = {"version": value, "latest": target, "status": "unknown"}
    if isinstance(latest, dict) and latest.get("status") not in (None, "available"):
        result["reason"] = latest.get("error", latest.get("reason", latest["status"]))
        return result

    def numeric(version):
        if not isinstance(version, str) or not re.fullmatch(
            r"v?[0-9]{1,10}(?:\.[0-9]{1,10}){0,7}", version
        ):
            return None
        parts = [int(part) for part in version.removeprefix("v").split(".")]
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return tuple(parts)

    left, right = numeric(value), numeric(target)
    if left is None or right is None:
        result["reason"] = "Missing or non-comparable release version; no freshness claim."
    else:
        result["status"] = "current" if left == right else "behind" if left < right else "ahead"
    return result


def freshness(row):
    return {
        "selected_vs_upstream": release_comparison(row["selected"], row["upstream"]),
        "provider_vs_upstream": release_comparison(
            row["provider_latest"].get("version"), row["upstream"]
        ),
        "installed_vs_provider": release_comparison(
            installed_release(row.get("installed")), row["provider_latest"]
        ),
    }


def report(context, context_path, *, refresh=False):
    result = {"packages": [], "artifacts": {}, "network_refreshed": refresh}
    for package in context["packages"]:
        row = {
            "id": package["id"],
            "provider": package["provider"],
            "selected": package.get("version"),
            "policy": package.get("policy", "latest-stable"),
            "reason": package.get("reason"),
        }
        try:
            row["installed"] = package_status(context, package, context_path)
        except (Error, OSError, ValueError) as exc:
            row["installed"] = {"status": "unverified", "error": str(exc)}
        try:
            if package["provider"] == "apt":
                row["provider_candidate"] = run(
                    ["/usr/bin/apt-cache", "policy", package["name"]], context
                )
            elif package["provider"] in ("brew", "cask"):
                row["provider_candidate"] = json.loads(
                    run(["/opt/homebrew/bin/brew", "info", "--json=v2", package["name"]], context)
                )
            elif package["provider"] == "mise":
                row["provider_candidate"] = {"locked": mise_metadata(context, package)}
            else:
                row["provider_candidate"] = {
                    "version": package.get("version"),
                    "source": "committed manifest",
                }
        except (Error, OSError, ValueError) as exc:
            row["provider_candidate"] = {"status": "unavailable", "error": str(exc)}
        selected_record = dict(package)
        if refresh and not selected_record.get("update") and package["provider"] == "mise":
            try:
                backend = mise_metadata(context, package)["selection"].get("backend", "")
                if backend.startswith("npm:"):
                    selected_record["update"] = {"provider": "npm", "package": backend[4:]}
            except METADATA_ERRORS as exc:
                row["upstream"] = {"status": "unavailable", "error": str(exc)}
        if "upstream" not in row:
            row["upstream"] = release_metadata(selected_record, refresh=refresh)
        row["provider_latest"] = release_metadata(package, refresh=refresh, field="provider_update")
        row["freshness"] = freshness(row)
        result["packages"].append(row)
    for name, artifact in context.get("artifacts", {}).items():
        if context.get("platform") and context["platform"] not in artifact.get(
            "platforms", PLATFORMS
        ):
            continue
        row = {
            "selected": artifact.get("version"),
            "policy": artifact.get("policy", "latest-stable"),
            "reason": artifact.get("reason"),
        }
        try:
            artifact = artifact_metadata(artifact)
            row.update(
                selected=artifact.get("version"),
                policy=artifact.get("policy", "latest-stable"),
                reason=artifact.get("reason"),
            )
            row["upstream"] = release_metadata(artifact, refresh=refresh)
            row["provider_latest"] = release_metadata(
                artifact, refresh=refresh, field="provider_update"
            )
        except METADATA_ERRORS as exc:
            row["source_error"] = str(exc)
            row["upstream"] = {"status": "unavailable", "error": str(exc)}
            row["provider_latest"] = {"status": "unavailable", "error": str(exc)}
        row["freshness"] = freshness(row)
        result["artifacts"][name] = row
    return result


def allowed(path):
    relative(path)
    if path in (
        "bootstrap.lock.json",
        "apps/mise/mise.toml",
        "apps/mise/mise.lock",
        "native/engine/pins.json",
    ):
        return True
    parts = Path(path).parts
    if (
        len(parts) == 3
        and parts[0] == "apps"
        and parts[2] in ("package.json", "package-lock.json", "pnpm-lock.yaml", "source-lock.json")
    ):
        return True
    if path.startswith("apps/mise/.mise/locks/") and Path(path).name in (
        "aube-lock.yaml",
        "package.json",
        "uv.lock",
        "pyproject.toml",
    ):
        return True
    return (path.startswith("native/") and path.endswith(".json")) or (
        path.startswith("native/recipes/") and path.endswith(".lock")
    )


def validate_local_locks(context):
    for package in context["packages"]:
        for item in package.get("lock_inputs", []):
            path = inside(package["_origin"], item["path"], existing=True)
            if digest(path) != item["sha256"]:
                raise Error(f"candidate application lock digest mismatch: {path}")
        descriptor = package.get("python_lock")
        if descriptor:
            for path_key, checksum_key in (
                ("path", "sha256"),
                ("requirements", "requirements_sha256"),
            ):
                if path_key in descriptor:
                    path = inside(package["_origin"], descriptor[path_key], existing=True)
                    if digest(path) != descriptor[checksum_key]:
                        raise Error(f"candidate local lock digest mismatch: {path}")


def artifact_metadata(artifact):
    if not artifact.get("source_manifest"):
        return artifact
    path = inside(artifact["_origin"], artifact["source_manifest"], existing=True)
    metadata = load(path)
    if artifact.get("key"):
        for part in artifact["key"].split("."):
            metadata = metadata[part]
    if not isinstance(metadata, dict):
        raise Error("source_manifest key must identify a source record")
    if (
        "version" in artifact
        and "version" in metadata
        and artifact["version"] != metadata["version"]
    ):
        raise Error("candidate source_manifest version disagrees with selected artifact")
    return {**metadata, **artifact}


def verify_download_records(value, cache):
    if isinstance(value, dict):
        if "url" in value and "sha256" in value:
            download(value["url"], value["sha256"], cache)
        for item in value.values():
            verify_download_records(item, cache)
    elif isinstance(value, list):
        for item in value:
            verify_download_records(item, cache)


def stage_candidate(context, bundle_path):
    bundle_path = Path(bundle_path).resolve()
    bundle = load(bundle_path)
    if (
        bundle.get("schema_version") != 1
        or not isinstance(bundle.get("files"), list)
        or not bundle["files"]
    ):
        raise Error("candidate bundle requires schema_version=1 and nonempty files")
    identifier = uuid.uuid4().hex
    destination = Path(context["paths"]["state"]) / "candidates" / identifier
    destination.mkdir(parents=True)
    roots = {"public": Path(context["base"]), "private": Path(context["root"])}
    desired_roots = dict(roots)
    single_origin = roots["public"] == roots["private"]
    source_pins = [
        item
        for item in bundle["files"]
        if item.get("repository") == "private" and item.get("path") == "bootstrap.lock.json"
    ]
    if len(source_pins) > 1:
        raise Error("duplicate public source pin candidate")
    if source_pins and single_origin:
        raise Error("a public source pin belongs to a separate private entry")
    if source_pins:
        from source import resolve

        item = source_pins[0]
        selected_lock = inside(bundle_path.parent, item["source"], existing=True)
        if digest(selected_lock) != item.get("sha256"):
            raise Error("public source candidate lock digest mismatch")
        desired_roots["public"] = resolve(
            selected_lock, Path(context["paths"]["cache"]) / "sources"
        )
    staged_roots = {}
    for name, origin in desired_roots.items():
        if single_origin and name == "private":
            staged_roots[name] = staged_roots["public"]
            continue
        staged = destination / name
        staged.mkdir()
        source_paths = {"native", "apps/mise"}
        app_inputs = [
            item["path"]
            for package in context["packages"]
            if Path(package["_origin"]) == origin
            for item in package.get("lock_inputs", [])
        ]
        app_inputs.extend(
            item["path"]
            for item in bundle["files"]
            if (item.get("repository") == name or single_origin) and allowed(item.get("path", ""))
        )
        for path in app_inputs:
            relative(path)
            parts = Path(path).parts
            if len(parts) >= 3 and parts[0] == "apps":
                source_paths.add(str(Path(*parts[:2])))
        for source_path in sorted(source_paths):
            source = inside(origin, source_path)
            if source.exists():
                shutil.copytree(source, staged / source_path, symlinks=False)
        staged_roots[name] = staged
    entries, seen = [], set()
    for item in bundle["files"]:
        repository, path = item.get("repository"), item.get("path", "")
        if repository not in roots or not allowed(path):
            raise Error(
                f"candidate may only replace reviewed manifest/lock files: {repository}:{path}"
            )
        if single_origin:
            repository = "public"
        if path == "bootstrap.lock.json" and repository != "private":
            raise Error("the private entry exclusively owns the public source pin")
        if repository == "public" and (Path(context["base"]) / ".bootstrap-source.tar").exists():
            raise Error(
                "immutable public manifests update through a published bootstrap.lock.json source pin, never mutable cache edits"
            )
        key = (repository, path)
        if key in seen:
            raise Error(f"duplicate candidate target: {key}")
        seen.add(key)
        source = inside(bundle_path.parent, item["source"], existing=True)
        if source.is_symlink() or digest(source) != item.get("sha256"):
            raise Error(f"candidate source digest mismatch: {source}")
        existing = inside(roots[repository], path)
        target = inside(staged_roots[repository], path)
        atomic_bytes(target, source.read_bytes())
        entries.append(
            {
                "repository": repository,
                "path": path,
                "sha256": item["sha256"],
                "before": digest(existing) if existing.is_file() else None,
            }
        )
    for platform in PLATFORMS:
        candidate = compose(
            staged_roots["public"], staged_roots["private"], context["home"], platform
        )
        validate_local_locks(candidate)
        for package in candidate["packages"]:
            if package["provider"] == "artifact":
                spec = artifact_spec(package, platform)
                download(spec["url"], spec["sha256"], Path(context["paths"]["cache"]) / "downloads")
            elif package["provider"] == "mise":
                metadata = mise_metadata(candidate, package)
                entry = metadata["selection"]
                spec = entry.get("platforms." + platform, entry.get("platforms", {}).get(platform))
                if spec and spec.get("url") and spec.get("checksum", "").startswith("sha256:"):
                    download(
                        spec["url"],
                        spec["checksum"].removeprefix("sha256:"),
                        Path(context["paths"]["cache"]) / "downloads",
                    )
                    for additional in spec.get("additional_artifacts", []):
                        download(
                            additional["url"],
                            additional["checksum"].removeprefix("sha256:"),
                            Path(context["paths"]["cache"]) / "downloads",
                        )
        for artifact in candidate["artifacts"].values():
            verify_download_records(
                artifact_metadata(artifact), Path(context["paths"]["cache"]) / "downloads"
            )
        # Firefox defaults carry selected extension assets outside package records.
        verify_download_records(
            candidate["dotfiles"].get("apps", {}).get("firefox", {}).get("extensions", []),
            Path(context["paths"]["cache"]) / "downloads",
        )
    receipt = {
        "schema_version": 1,
        "id": identifier,
        "status": "validated",
        "files": entries,
        "roots": {key: str(value) for key, value in roots.items()},
        "validation": "both platform compositions, local-lock/native-graph digests and declared direct artifact downloads; native graph replay happens on explicit provision, no runtime/UI qualification claimed",
    }
    atomic_json(destination / "candidate.json", receipt)
    return receipt


def promote(context, identifier):
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise Error("invalid candidate id")
    candidate = Path(context["paths"]["state"]) / "candidates" / identifier
    receipt = load(candidate / "candidate.json")
    if receipt.get("status") != "validated":
        raise Error("candidate is not promotable")
    roots = {"public": context["base"], "private": context["root"]}
    if receipt.get("roots") != roots:
        raise Error("candidate belongs to other source roots")
    for item in receipt["files"]:
        if not allowed(item["path"]):
            raise Error("unsafe candidate target")
        if (
            item["repository"] == "public"
            and (Path(context["base"]) / ".bootstrap-source.tar").exists()
        ):
            raise Error(
                "cannot promote files over immutable public source; promote its reviewed private source pin instead"
            )
        target = inside(roots[item["repository"]], item["path"])
        before = digest(target) if target.is_file() else None
        if target.is_symlink() or before != item["before"]:
            raise Error(f"candidate source changed since staging: {target}")
        if digest(candidate / item["repository"] / item["path"]) != item["sha256"]:
            raise Error("candidate staged bytes changed")
    # Retain exact before bytes and a durable promotion journal for narrow rollback.
    receipt["status"] = "promoting"
    atomic_json(candidate / "candidate.json", receipt)
    completed = []
    try:
        for item in receipt["files"]:
            target = inside(roots[item["repository"]], item["path"])
            backup = candidate / "before" / item["repository"] / item["path"]
            if item["before"] is not None:
                atomic_bytes(backup, target.read_bytes())
            atomic_bytes(target, (candidate / item["repository"] / item["path"]).read_bytes())
            if digest(target) != item["sha256"]:
                raise Error("candidate promotion readback mismatch")
            completed.append(item)
        receipt["status"] = "promoted"
        atomic_json(candidate / "candidate.json", receipt)
    except BaseException:
        for item in reversed(completed):
            target = inside(roots[item["repository"]], item["path"])
            if digest(target) != item["sha256"]:
                continue  # Preserve a concurrent foreign edit.
            if item["before"] is None:
                target.unlink()
            else:
                atomic_bytes(
                    target, (candidate / "before" / item["repository"] / item["path"]).read_bytes()
                )
        receipt["status"] = "failed"
        atomic_json(candidate / "candidate.json", receipt)
        raise
    return receipt


def recover_candidate(context, identifier):
    """Restore only recorded candidate manifest/lock bytes, never foreign edits."""
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise Error("invalid candidate id")
    candidate = Path(context["paths"]["state"]) / "candidates" / identifier
    receipt = load(candidate / "candidate.json")
    if receipt.get("status") not in ("promoting", "promoted"):
        raise Error("candidate has no pending/promoted transition to recover")
    roots = dict(receipt.get("roots", {}))
    if roots.get("private") != context["root"] or (
        any(item["repository"] == "public" for item in receipt["files"])
        and roots.get("public") != context["base"]
    ):
        raise Error("candidate belongs to other source roots")
    for item in receipt["files"]:
        if not allowed(item["path"]):
            raise Error("unsafe candidate recovery target")
        target = inside(roots[item["repository"]], item["path"])
        if target.is_symlink():
            raise Error(f"candidate recovery preserves foreign symlink: {target}")
        actual = digest(target) if target.is_file() else None
        if actual not in (item["before"], item["sha256"]):
            raise Error(f"candidate recovery preserves foreign source edit: {target}")
        backup = candidate / "before" / item["repository"] / item["path"]
        if (
            actual != item["before"]
            and item["before"] is not None
            and (not backup.is_file() or digest(backup) != item["before"])
        ):
            raise Error(f"candidate recovery backup failed integrity: {backup}")
    for item in receipt["files"]:
        target = inside(roots[item["repository"]], item["path"])
        if (digest(target) if target.is_file() else None) == item["before"]:
            continue
        if item["before"] is None:
            target.unlink(missing_ok=True)
        else:
            backup = candidate / "before" / item["repository"] / item["path"]
            atomic_bytes(target, backup.read_bytes())
        if (digest(target) if target.is_file() else None) != item["before"]:
            raise Error("candidate recovery source readback mismatch")
    receipt["status"] = "recovered"
    atomic_json(candidate / "candidate.json", receipt)
    return receipt
