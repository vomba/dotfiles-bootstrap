"""Acquire concrete, reviewable desktop source locks from primary publications.

No install, service operation, credential discovery, Git mutation or publication.
Application releases come from GitHub's stable release endpoint; source tags are
resolved to exact commits and Cargo.lock bytes. Qt modules come from ONE official
SDK Updates.xml family, with every archive checked against its published SHA1 and
also locked by SHA256. Rust is checked against its official SHA256 publication.
Cpptrace/libdwarf/zstd pins are read from the selected upstream build definitions,
not independently floated. Native PPA versions are inspected over official HTTPS;
only native APT may authenticate/install them with the separately pinned key.

The caller validates the complete returned lock and atomically writes it to a
MANAGED REPOSITORY source-candidate JSON (not a temporary research path). Building
and qualifying that lock is a separate `candidate` action. Acquisition never
promotes it, edits the selected sources.json, signs/pushes Git, or redistributes
SDK binaries. Cached downloads are content-addressed local operator artifacts.
"""

from __future__ import annotations

import copy
import datetime
import gzip
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from desktop_build import PRODUCTION_FEATURES, digest, run, sdk_exclusions

GITHUB = "https://api.github.com/repos/"
QT_SUPPORT = "https://doc.qt.io/qt-6/qt-releases.html"
QT_REPOSITORY = "https://download.qt.io/online/qtsdkrepository/linux_x64/desktop/"
REPOSITORIES = {
    "niri": "niri-wm/niri",
    "xwayland-satellite": "Supreeeme/xwayland-satellite",
    "quickshell": "quickshell-mirror/quickshell",
    "dux": "duxlabs-dev/dux-shell",
    "hyprlock": "hyprwm/hyprlock",
    "hypridle": "hyprwm/hypridle",
}


def read_url(url):
    if not url.startswith("https://"):
        raise ValueError("Only explicit HTTPS source publications are accepted")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "dotfiles-desktop-source-candidate",
            "Accept": "application/vnd.github+json" if url.startswith(GITHUB) else "*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        content = response.read(8 * 1024 * 1024 + 1)
    if len(content) > 8 * 1024 * 1024:
        raise ValueError("Unexpectedly large source metadata: " + url)
    return content


def release(repository):
    value = json.loads(read_url(GITHUB + repository + "/releases/latest"))
    version = value["tag_name"].removeprefix("v")
    if (
        value.get("draft")
        or value.get("prerelease")
        or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", version)
    ):
        raise ValueError("Upstream did not publish a numeric stable release: " + repository)
    return value


def tag_commit(repository, tag):
    if not re.fullmatch(r"v?[0-9]+(?:\.[0-9]+){1,2}", tag):
        raise ValueError("A stable exact source tag is required")
    result = run(
        [
            "git",
            "ls-remote",
            "https://github.com/" + repository + ".git",
            "refs/tags/" + tag,
            "refs/tags/" + tag + "^{}",
        ]
    )
    refs = dict((line.split()[1], line.split()[0]) for line in result.splitlines())
    commit = refs.get("refs/tags/" + tag + "^{}", refs.get("refs/tags/" + tag))
    if not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Stable source tag did not resolve uniquely: " + repository)
    return commit


def download(url, cache, *, expected_sha256=None, expected_sha1=None):
    """Compute actual byte digests; check publisher digests where available."""
    if not url.startswith("https://"):
        raise ValueError("Only official HTTPS artifact URLs are accepted")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".acquire-", dir=cache)
    sha256, sha1 = hashlib.sha256(), hashlib.sha1()
    try:
        with os.fdopen(fd, "wb") as target, urllib.request.urlopen(url, timeout=180) as response:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
                sha256.update(chunk)
                sha1.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        if expected_sha256 and sha256.hexdigest() != expected_sha256:
            raise ValueError("Official SHA256 mismatch: " + url)
        if expected_sha1 and sha1.hexdigest() != expected_sha1:
            raise ValueError("Official Qt SDK SHA1 mismatch: " + url)
        path = cache / sha256.hexdigest()
        if path.is_symlink():
            raise ValueError("Artifact acquisition cache contains a foreign symlink")
        if path.exists():
            if path.is_symlink() or digest(path) != sha256.hexdigest():
                raise ValueError("Foreign/corrupt content-addressed acquisition cache")
        else:
            os.replace(temporary, path)
        return {"url": url, "sha256": sha256.hexdigest()}
    finally:
        Path(temporary).unlink(missing_ok=True)


class SupportTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attributes):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, value):
        if self.cell is not None:
            self.cell.append(value)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def qt_support(html, version):
    table = SupportTable()
    table.feed(html)
    family = ".".join(version.split(".")[:2])
    for row in table.rows:
        if len(row) < 3 or not re.fullmatch(r"Qt\s+" + re.escape(family) + r"(?:\s+LTS)?", row[0]):
            continue
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", row[2])
        if not date_match:
            raise ValueError("Qt support schedule has no current horizon for " + version)
        latest = re.search(r"\d+\.\d+\.\d+", row[1])
        if "commercial only" in row[1].lower() and latest and latest.group() == version:
            raise ValueError(
                "Selected Qt patch is marked commercial-only; no entitlement is assumed"
            )
        vendor = datetime.date.fromisoformat(date_match.group())
        minor = int(family.split(".")[1])
        # Since 6.8, every fourth minor is five-year commercial LTS. Bound this
        # self-maintained public SDK to the original one-year qualification
        # window; never mistake the five-year vendor date for free patch access.
        horizon = vendor.replace(year=vendor.year - 4) if minor >= 8 and minor % 4 == 0 else vendor
        if horizon < datetime.date.today():
            raise ValueError(
                "Selected public Qt baseline exceeded its non-commercial qualification horizon"
            )
        return {
            "horizon": str(horizon),
            "vendor_until": str(vendor),
            "reference": QT_SUPPORT,
            "commercial_entitlement_assumed": False,
            "latest": latest.group() if latest else None,
            "commercial_only": "commercial only" in row[1].lower(),
        }
    raise ValueError("Selected Qt minor is absent from the official supported-version table")


def qt_archive_records(xml, version):
    compact = version.replace(".", "")
    base = QT_REPOSITORY + "qt6_" + compact + "/qt6_" + compact + "/"
    packages = {item.findtext("Name"): item for item in ET.fromstring(xml).findall("PackageUpdate")}
    modules = (
        "qtbase",
        "qtdeclarative",
        "qtwayland",
        "qtsvg",
        "qttranslations",
        "icu",
        "qtshadertools",
        "qtmultimedia",
        "qtimageformats",
        "qt5compat",
    )
    addons = {"qtshadertools", "qtmultimedia", "qtimageformats", "qt5compat"}
    records, builds = [], set()
    for module in modules:
        name = (
            "qt.qt6."
            + compact
            + "."
            + ("addons." + module + "." if module in addons else "")
            + "linux_gcc_64"
        )
        package = packages.get(name)
        if package is None:
            raise ValueError("Official SDK module unavailable for selected Qt family: " + module)
        build = package.findtext("Version", "")
        if not build.startswith(version + "-"):
            raise ValueError("Official Qt SDK family version mismatch")
        builds.add(build)
        archives = [
            item.strip() for item in package.findtext("DownloadableArchives", "").split(",")
        ]
        matches = [
            item for item in archives if item.startswith(module + "-") and item.endswith(".7z")
        ]
        if len(matches) != 1:
            raise ValueError("Official Qt SDK archive layout changed: " + module)
        records.append({"module": module, "url": base + name + "/" + build + matches[0]})
    if len(builds) != 1:
        raise ValueError("Qt SDK modules do not share one coherent published build")
    return records


def versions(value):
    fields = tuple(int(part) for part in value.split("."))
    return fields + (0,) * (3 - len(fields))


def quoted_variable(text, name):
    match = re.search(r"set\(" + name + r'\s+"([^"\n]+)"', text)
    if not match:
        raise ValueError("Upstream dependency declaration changed: " + name)
    return match.group(1)


def acquire(
    current,
    cache,
    *,
    qt_version=None,
    rust_version=None,
    wayland_version=None,
    protocols_version=None,
):
    candidate = copy.deepcopy(current)
    runtime = candidate["runtime"]
    releases = {name: release(repository) for name, repository in REPOSITORIES.items()}
    for name in ("niri", "xwayland-satellite"):
        tag = releases[name]["tag_name"]
        commit = tag_commit(REPOSITORIES[name], tag)
        record = candidate[name]
        record.update(version=tag.removeprefix("v"), tag=tag, commit=commit)
        raw = "https://raw.githubusercontent.com/" + REPOSITORIES[name] + "/" + commit + "/"
        record["cargo_lock_sha256"] = hashlib.sha256(read_url(raw + "Cargo.lock")).hexdigest()
        import tomllib

        cargo = tomllib.loads(read_url(raw + "Cargo.toml").decode())
        if name == "niri" and set(cargo["features"]["default"]) != set(record["features"]):
            raise ValueError("New Niri default feature set requires a reviewed recipe change")
        if name == "xwayland-satellite" and "systemd" not in cargo.get("features", {}):
            raise ValueError("New satellite source no longer exposes its required systemd feature")
        minimum = (
            cargo.get("workspace", {})
            .get("package", {})
            .get("rust-version", cargo.get("package", {}).get("rust-version"))
        )
        if minimum and versions(rust_version or candidate["rust"]["version"]) < versions(minimum):
            raise ValueError(
                "Selected Rust is below the new source MSRV; acquire with --rust-version"
            )
    rust = rust_version or candidate["rust"]["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", rust):
        raise ValueError("An official stable Rust release is required")
    rust_url = "https://static.rust-lang.org/dist/rust-" + rust + "-x86_64-unknown-linux-gnu.tar.xz"
    published = read_url(rust_url + ".sha256").decode().split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", published):
        raise ValueError("Official Rust SHA256 publication is malformed")
    candidate["rust"] = {"version": rust, **download(rust_url, cache, expected_sha256=published)}

    qs_tag = releases["quickshell"]["tag_name"]
    qs_commit = tag_commit(REPOSITORIES["quickshell"], qs_tag)
    qs_raw = (
        "https://raw.githubusercontent.com/" + REPOSITORIES["quickshell"] + "/" + qs_commit + "/"
    )
    cmake = read_url(qs_raw + "CMakeLists.txt").decode()
    if {
        match.group(1) for match in re.finditer(r'boption\((\w+)\s+"[^\"]+" ON', cmake)
    } != PRODUCTION_FEATURES:
        raise ValueError("New Quickshell production features require a reviewed source contract")
    runtime["quickshell_version"] = qs_tag.removeprefix("v")
    runtime["sources"]["quickshell"] = {
        **download(
            "https://codeload.github.com/" + REPOSITORIES["quickshell"] + "/tar.gz/" + qs_commit,
            cache,
        ),
        "commit": qs_commit,
    }
    qt = qt_version or runtime["qt_version"]
    if not re.fullmatch(r"6\.[0-9]+\.[0-9]+", qt) or versions(qt) < versions("6.10.0"):
        raise ValueError("Use an official stable Qt 6 SDK >=6.10 for the Dux lock/scaling contract")
    if versions(qt) < versions(quoted_variable(cmake, "QT_MIN_VERSION")):
        raise ValueError("Selected Qt is below the new Quickshell minimum")
    schedule_html = read_url(QT_SUPPORT).decode()
    schedule = qt_support(schedule_html, qt)
    if qt_version is None and schedule["latest"]:
        qt = schedule["latest"]
        schedule = qt_support(schedule_html, qt)
    compact = qt.replace(".", "")
    metadata = QT_REPOSITORY + "qt6_" + compact + "/qt6_" + compact + "/Updates.xml"
    archives = qt_archive_records(read_url(metadata), qt)
    for archive in archives:
        sha1 = read_url(archive["url"] + ".sha1").decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha1):
            raise ValueError("Official Qt archive SHA1 publication is malformed")
        archive.update(download(archive["url"], cache, expected_sha1=sha1), sha1=sha1)
    runtime.update(
        qt_version=qt,
        qt_archives=archives,
        deployment_exclusions=sdk_exclusions(qt),
        qt_standard_support_until=schedule["horizon"],
        qt_vendor_support_until=schedule["vendor_until"],
    )
    runtime["support_policy"] = (
        "Self-maintained public SDK: qualify within the non-commercial one-year window. Vendor LTS dates do not grant free LTS patch access or commercial entitlement."
    )
    runtime["qt_corresponding_source_verified"] = False

    crash = read_url(qs_raw + "src/crash/CMakeLists.txt").decode()
    cpp_tag = re.search(r"GIT_TAG\s+(v[0-9]+(?:\.[0-9]+){2})", crash)
    if not cpp_tag:
        raise ValueError("Upstream cpptrace vendoring declaration changed")
    cpp = cpp_tag.group(1)
    runtime["sources"]["cpptrace"] = download(
        "https://codeload.github.com/jeremy-rifkin/cpptrace/tar.gz/refs/tags/" + cpp, cache
    )
    options = read_url(
        "https://raw.githubusercontent.com/jeremy-rifkin/cpptrace/"
        + cpp
        + "/cmake/OptionVariables.cmake"
    ).decode()
    dwarf_repository = quoted_variable(options, "CPPTRACE_LIBDWARF_REPO")
    if dwarf_repository != "https://github.com/jeremy-rifkin/libdwarf-lite.git":
        raise ValueError("Upstream libdwarf source trust boundary changed")
    dwarf = quoted_variable(options, "CPPTRACE_LIBDWARF_TAG")
    if not re.fullmatch(r"[0-9a-f]{40}", dwarf):
        dwarf = tag_commit("jeremy-rifkin/libdwarf-lite", dwarf)
    runtime["sources"]["libdwarf"] = download(
        "https://codeload.github.com/jeremy-rifkin/libdwarf-lite/tar.gz/" + dwarf, cache
    )
    zstd = quoted_variable(options, "CPPTRACE_ZSTD_URL")
    if not re.fullmatch(
        r"https://github.com/facebook/zstd/releases/download/v[0-9.]+/zstd-[0-9.]+\.tar\.gz", zstd
    ):
        raise ValueError("Upstream zstd artifact trust boundary changed")
    runtime["sources"]["zstd"] = download(zstd, cache)
    for name, version in (
        ("wayland", wayland_version or runtime["wayland_version"]),
        ("protocols", protocols_version or runtime["protocols_version"]),
    ):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", version):
            raise ValueError("Stable private Wayland/protocols releases are required")
        project = "wayland" if name == "wayland" else "wayland-protocols"
        url = (
            "https://gitlab.freedesktop.org/wayland/"
            + project
            + "/-/releases/"
            + version
            + "/downloads/"
            + project
            + "-"
            + version
            + ".tar.xz"
        )
        runtime[name + "_version"] = version
        runtime["sources"][name] = download(url, cache)
    wayland_cmake = read_url(qs_raw + "src/wayland/CMakeLists.txt").decode()
    protocol_minimum = re.search(r"wayland-protocols>=([0-9.]+)", wayland_cmake)
    if not protocol_minimum or versions(runtime["protocols_version"]) < versions(
        protocol_minimum.group(1)
    ):
        raise ValueError(
            "New Quickshell needs a newer private XML release; acquire with --protocols-version"
        )
    for name, record in runtime["licenses"].items():
        download(record["url"], cache, expected_sha256=record["sha256"])
    bzip2 = runtime["sources"]["bzip2"]
    download(bzip2["url"], cache, expected_sha256=bzip2["sha256"])

    dms_release = releases["dux"]
    runtime["version"] = dms_release["tag_name"].removeprefix("v")
    asset = next(
        (item for item in dms_release.get("assets", []) if item["name"] == "dms-cli-amd64.gz"), None
    )
    if asset is None:
        raise ValueError("Official Dux amd64 artifact layout changed")
    published_digest = asset.get("digest")
    expected = (
        published_digest.removeprefix("sha256:")
        if published_digest and published_digest.startswith("sha256:")
        else None
    )
    dms_url = (
        "https://github.com/"
        + REPOSITORIES["dux"]
        + "/releases/download/"
        + dms_release["tag_name"]
        + "/dms-cli-amd64.gz"
    )
    runtime["sources"]["dms"] = download(dms_url, cache, expected_sha256=expected)
    runtime["sources"]["dux-source"] = download(
        "https://codeload.github.com/"
        + REPOSITORIES["dux"]
        + "/tar.gz/refs/tags/"
        + dms_release["tag_name"],
        cache,
    )

    packages = gzip.decompress(
        read_url(candidate["ppa"]["url"] + "/dists/noble/main/binary-amd64/Packages.gz")
    ).decode()
    records = {}
    for block in packages.split("\n\n"):
        fields = {
            line.split(": ", 1)[0]: line.split(": ", 1)[1]
            for line in block.splitlines()
            if ": " in line and not line.startswith(" ")
        }
        if "Package" in fields:
            records[fields["Package"]] = fields
    for name in candidate["ppa"]["scope"]:
        if name not in records:
            raise ValueError("Reviewed Noble PPA scope disappeared: " + name)
        candidate["ppa"]["scope"][name] = records[name]["Version"]
    for name in ("hyprlock", "hypridle"):
        selected = candidate["ppa"]["scope"][name].split(":")[-1].split("-", 1)[0]
        if versions(selected) != versions(releases[name]["tag_name"].removeprefix("v")):
            raise ValueError(
                "Noble PPA lags upstream stable "
                + name
                + "; preserve the working runtime and report the provider gap"
            )
        for dependency in re.split(r",|\|", records[name].get("Depends", "")):
            package = dependency.strip().split(" ", 1)[0]
            if (
                package.startswith(("libhypr", "libsdbus"))
                and package not in candidate["ppa"]["scope"]
            ):
                raise ValueError("New Noble PPA ABI scope requires review: " + package)
    details = {
        "upstream": {
            name: {"tag": value["tag_name"], "url": value["html_url"]}
            for name, value in releases.items()
        },
        "qt_metadata": metadata,
        "support": schedule,
        "ppa_authentication": "HTTPS metadata inspection only; native APT must authenticate the Noble signature with the pinned key before installation",
        "publication": "source lock only; local SDK/artifact cache must not be redistributed",
        "rust_policy": "explicit build-time compatibility pin, refreshed only with --rust-version",
    }
    return candidate, details
