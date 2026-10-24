"""Noble-only source/artifact assembly. No host installation or service operations.

build(context, lock, package_id, output) returns a receipt path. Every output has
its own digest-named workspace; source locks and native package payloads are
retained alongside the deb for audit/recovery. Caller supplies real maintainer
metadata in identity.package_maintainer (or identity.name + identity.email).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

PRODUCTION_FEATURES = frozenset(
    "USE_JEMALLOC CRASH_HANDLER SOCKETS WAYLAND WAYLAND_WLR_LAYERSHELL WAYLAND_SESSION_LOCK WAYLAND_TOPLEVEL_MANAGEMENT HYPRLAND HYPRLAND_IPC HYPRLAND_GLOBAL_SHORTCUTS HYPRLAND_FOCUS_GRAB HYPRLAND_SURFACE_EXTENSIONS SCREENCOPY SCREENCOPY_ICC SCREENCOPY_WLR SCREENCOPY_HYPRLAND_TOPLEVEL X11 I3 I3_IPC SERVICE_STATUS_NOTIFIER SERVICE_PIPEWIRE SERVICE_MPRIS SERVICE_PAM SERVICE_POLKIT SERVICE_GREETD SERVICE_UPOWER SERVICE_NOTIFICATIONS BLUETOOTH NETWORK".split()
)
SDK_EXCLUSION_TEMPLATES = (
    "plugins/sqldrivers/libqsqlibase.so",
    "plugins/sqldrivers/libqsqlmimer.so",
    "plugins/sqldrivers/libqsqlmysql.so",
    "plugins/sqldrivers/libqsqloci.so",
    "plugins/sqldrivers/libqsqlodbc.so",
    "plugins/sqldrivers/libqsqlpsql.so",
    "include/QtQuick3DSpatialAudio",
    "lib/libQt6Quick3DSpatialAudio.prl",
    "lib/libQt6Quick3DSpatialAudio.so",
    "lib/libQt6Quick3DSpatialAudio.so.6",
    "lib/libQt6Quick3DSpatialAudio.so.{qt_version}",
    "lib/cmake/Qt6Quick3DSpatialAudioPrivate",
    "lib/cmake/Qt6Qml/QmlPlugins/Qt6quick3dspatialaudioAdditionalTargetInfo.cmake",
    "lib/cmake/Qt6Qml/QmlPlugins/Qt6quick3dspatialaudioConfig.cmake",
    "lib/cmake/Qt6Qml/QmlPlugins/Qt6quick3dspatialaudioConfigVersion.cmake",
    "lib/cmake/Qt6Qml/QmlPlugins/Qt6quick3dspatialaudioConfigVersionImpl.cmake",
    "lib/cmake/Qt6Qml/QmlPlugins/Qt6quick3dspatialaudioTargets-relwithdebinfo.cmake",
    "lib/cmake/Qt6Qml/QmlPlugins/Qt6quick3dspatialaudioTargets.cmake",
    "lib/cmake/Qt6Qml/QmlPlugins/Qt6quick3dspatialaudioTargetsPrecheck.cmake",
    "metatypes/qt6quick3dspatialaudioprivate_metatypes.json",
    "mkspecs/modules/qt_lib_quick3dspatialaudio_private.pri",
    "modules/Quick3DSpatialAudioPrivate.json",
    "qml/QtQuick3D/SpatialAudio",
)
BUILD_DEPENDENCIES = "build-essential ca-certificates curl git pkg-config clang-18 clang-tools-18 libclang-18-dev libudev-dev libinput-dev libseat-dev libxkbcommon-dev libgbm-dev libegl1-mesa-dev libwayland-dev libpango1.0-dev libpipewire-0.3-dev libdisplay-info-dev libxcb-composite0-dev libxcb-xfixes0-dev libxcb-render0-dev libxcb-shape0-dev libxcb-icccm4-dev libxcb1-dev libxcb-cursor-dev libxcb-res0-dev libxcb-randr0-dev libpixman-1-dev meson ninja-build python3 cmake libdrm-dev libvulkan-dev libpam0g-dev libpolkit-agent-1-dev libglib2.0-dev libjemalloc-dev libunwind-dev libffi-dev libexpat1-dev libxml2-dev zlib1g-dev spirv-tools libcli11-dev 7zip patchelf dpkg-dev file xz-utils".split()
NIRI_ASSETS = [
    ("target/release/niri", "usr/bin/niri", 0o755),
    ("resources/niri-session", "usr/bin/niri-session", 0o755),
    ("resources/niri.desktop", "usr/share/wayland-sessions/niri.desktop", 0o644),
    ("resources/niri-portals.conf", "usr/share/xdg-desktop-portal/niri-portals.conf", 0o644),
    ("resources/niri.service", "usr/lib/systemd/user/niri.service", 0o644),
    ("resources/niri-shutdown.target", "usr/lib/systemd/user/niri-shutdown.target", 0o644),
    ("resources/default-config.kdl", "usr/share/doc/niri/default-config.kdl", 0o644),
    ("LICENSE", "usr/share/doc/niri/copyright", 0o644),
]
SATELLITE_ASSETS = [
    ("target/release/xwayland-satellite", "usr/bin/xwayland-satellite", 0o755),
    ("LICENSE", "usr/share/doc/xwayland-satellite/copyright", 0o644),
    ("README.md", "usr/share/doc/xwayland-satellite/README.md", 0o644),
    (
        "resources/xwayland-satellite.service",
        "usr/share/doc/xwayland-satellite/examples/xwayland-satellite.service",
        0o644,
    ),
    ("xwayland-satellite.man", "usr/share/man/man1/xwayland-satellite.1", 0o644),
]
DLOPEN = {
    "niri": "libegl1 libegl-mesa0 libgles2 libgl1-mesa-dri libwayland-server0 libwayland-client0 libwayland-cursor0 dbus-user-session systemd bash procps".split(),
    "xwayland-satellite": ["xwayland (>= 2:23.1)", "libwayland-client0", "libwayland-cursor0"],
    "dux-runtime": "libegl1 libegl-mesa0 libgles2 libgl1-mesa-dri libglx-mesa0 libvulkan1 mesa-vulkan-drivers libxkbcommon0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 libx11-xcb1 libxcb1 libfontconfig1 libfreetype6 libdbus-1-3 libglib2.0-0t64 libpcre2-16-0 libzstd1 zlib1g libdrm2 libgbm1 libpipewire-0.3-0t64 libpam0g libpolkit-agent-1-0 libjemalloc2 libunwind8 libpulse0 libasound2t64 libssl3t64 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-libav".split(),
}
DLOPEN["dux-runtime"] += (
    "libgtk-3-0t64 libcups2t64 libxkbcommon-x11-0 libgssapi-krb5-2 libva2 libva-drm2 libva-x11-2 mesa-va-drivers libvdpau1 mesa-vdpau-drivers".split()
)
BUILD_DEPENDENCIES = list(dict.fromkeys([*BUILD_DEPENDENCIES, *DLOPEN["dux-runtime"]]))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def lock_digest(lock):
    return hashlib.sha256(
        json.dumps(lock, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o644)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def environment():
    # No user/project compiler, loader, Cargo or Qt configuration is inherited.
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "SOURCE_DATE_EPOCH": "1776384000",
        "CARGO_BUILD_JOBS": "2",
        "CMAKE_BUILD_PARALLEL_LEVEL": "2",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "/usr/bin/false",
    }


def run(argv, *, cwd=None, env=None):
    return subprocess.run(
        [str(a) for a in argv],
        cwd=cwd,
        env=env or environment(),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def noble(context):
    if context.get("platform") != "linux-x64":
        raise ValueError("Desktop recipes require linux-x64")
    release = dict(
        line.split("=", 1)
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line
    )
    if (
        release.get("ID", "").strip('"') != "ubuntu"
        or release.get("VERSION_ID", "").strip('"') != "24.04"
        or run(["dpkg", "--print-architecture"]).strip() != "amd64"
    ):
        raise ValueError(
            "Build/provision/check require Ubuntu 24.04 amd64, not a mixed-suite builder"
        )
    if run(["getconf", "GNU_LIBC_VERSION"]).strip() != "glibc 2.39":
        raise ValueError(
            "Noble native glibc ABI is required; do not build against another Ubuntu release"
        )
    for line in run(["apt-cache", "policy"]).splitlines():
        if "release " in line and re.search(
            r"(^|,)o=Ubuntu(,|$)", line.strip().removeprefix("release ")
        ):
            codename = re.search(r"(?:^|,)n=([^,]+)", line.strip().removeprefix("release "))
            if codename and codename.group(1) != "noble":
                raise ValueError("A non-Noble Ubuntu APT suite is configured: " + codename.group(1))


def maintainer(context):
    identity = context.get("identity", {})
    value = identity.get("package_maintainer")
    if not value and identity.get("name") and identity.get("email"):
        value = f"{identity['name']} <{identity['email']}>"
    if not isinstance(value, str) or not re.fullmatch(r"[^<>\r\n]+ <[^<>\s@]+@[^<>\s@]+>", value):
        raise ValueError("Declare identity.package_maintainer or real identity.name/email")
    return value


def fetch(record, directory):
    if not re.fullmatch("[0-9a-f]{64}", record.get("sha256", "")) or not record.get(
        "url", ""
    ).startswith("https://"):
        raise ValueError("HTTPS URL and exact SHA256 required")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / record["sha256"]
    if path.is_symlink():
        raise ValueError("Artifact cache entry is a foreign symlink: " + str(path))
    if path.exists():
        if digest(path) != record["sha256"]:
            raise ValueError(f"Corrupt cached artifact: {path}")
        return path
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=directory)
    try:
        with (
            os.fdopen(fd, "wb") as stream,
            urllib.request.urlopen(record["url"], timeout=180) as response,
        ):
            shutil.copyfileobj(response, stream)
        if digest(temporary) != record["sha256"]:
            raise ValueError(f"Artifact hash mismatch: {record['url']}")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def untar(archive, destination):
    """Reject traversal, special files and link escapes on Python 3.12 (Noble)."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise ValueError("Expected one source root")
    return children[0]


def checkout(record, destination):
    run(
        [
            "git",
            "-c",
            "protocol.file.allow=never",
            "clone",
            "--depth",
            "1",
            "--branch",
            record["tag"],
            record["repository"],
            destination,
        ]
    )
    if run(["git", "rev-parse", "HEAD"], cwd=destination).strip() != record["commit"]:
        raise ValueError("Tag/commit mismatch")
    if digest(Path(destination) / "Cargo.lock") != record["cargo_lock_sha256"]:
        raise ValueError("Cargo.lock mismatch")
    return Path(destination)


def elfs(root):
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) == b"\x7fELF":
                    yield path


def inspect_elf(path, *, runtime=False):
    header = run(["readelf", "-h", path])
    if "Advanced Micro Devices X86-64" not in header:
        raise ValueError(f"Wrong ELF architecture: {path}")
    dynamic = run(["readelf", "-d", path])
    program = run(["readelf", "-l", path])
    interpreter = re.search(r"Requesting program interpreter: ([^]]+)]", program)
    if interpreter and interpreter.group(1) != "/lib64/ld-linux-x86-64.so.2":
        raise ValueError(f"Unapproved ELF interpreter: {path}")
    if "/nix/" in dynamic + program or "/tmp/" in dynamic or "/work/" in dynamic:
        raise ValueError(f"Foreign/research loader reference: {path}")
    if runtime and "(NEEDED)" in dynamic and "(RUNPATH)" not in dynamic:
        raise ValueError(f"Missing private RUNPATH: {path}")
    linked = run(["ldd", path]) if "(NEEDED)" in dynamic else "static"
    if "not found" in linked or "/nix/" in linked:
        raise ValueError(f"Unresolved/foreign ELF dependencies: {path}\n{linked}")
    if runtime:
        direct = set(re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic))
        head, separator, tail = str(Path(path).resolve()).partition("/opt/dotfiles/dux/")
        runtime_root = Path(head + separator + tail.split("/", 1)[0])
        private_icu = {
            match.group(1)
            for library in (runtime_root / "qt/lib").glob("libicu*.so.*")
            if (match := re.match(r"(libicu[^.]+\.so\.[0-9]+)(?:\.|$)", library.name))
        }
        for line in linked.splitlines():
            soname = line.split("=>", 1)[0].strip()
            # Entry points validate their whole loaded closure. Shared plugins
            # bind their own imports; native GTK's transitive host dependencies
            # are not private SDK imports (the entry's closure is checked above).
            if not interpreter and soname not in direct:
                continue
            if soname.startswith("libicu") and not private_icu:
                raise ValueError("Private runtime ICU payload is missing")
            if "=>" in line and (
                soname.startswith(("libQt6", "libwayland-"))
                or soname in private_icu
                or soname == "libbz2.so.1"
            ):
                resolved = line.split("=>", 1)[1].strip().split(" ", 1)[0]
                if not separator or not Path(resolved).resolve().is_relative_to(runtime_root):
                    raise ValueError(f"Private library escaped its own runtime: {line}")
    return {"dynamic": dynamic, "linkage": linked}


def patch_runtime(prefix):
    qtlib = prefix / "qt/lib"
    for path in elfs(prefix):
        if "(NEEDED)" not in run(["readelf", "-d", path]):
            continue
        relative = os.path.relpath(qtlib, path.parent)
        private = "$ORIGIN/" + relative if relative != "." else "$ORIGIN"
        old = run(["patchelf", "--print-rpath", path]).strip()
        keep = [part for part in old.split(":") if part.startswith("$ORIGIN") and part != private]
        run(["patchelf", "--set-rpath", ":".join([private, "$ORIGIN", *keep]), path])


def copy_assets(source, stage, assets):
    for relative, target, mode in assets:
        origin = source / relative
        if not origin.is_file():
            raise ValueError(f"Missing upstream resource: {relative}")
        destination = stage / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, destination)
        destination.chmod(mode)


def legal_files(source, destination):
    count = 0
    for path in sorted(source.rglob("*")):
        if (
            path.is_file()
            and not path.is_symlink()
            and (
                re.search(
                    r"(^|[._ -])(licen[cs]e|copying|copyright|notice)([._ -]|$)", path.name, re.I
                )
                or path.name == "qt_attribution.json"
                or "LICENSES" in path.parts
            )
        ):
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            count += 1
    if not count:
        raise ValueError(f"No upstream license/notices found: {source}")


def normalize_debian_payload(stage):
    """Native package payloads must remain readable after root ownership."""
    for path in [stage, *stage.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        if mode & 0o6000:
            raise ValueError("Privileged payload modes are forbidden: " + str(path))
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o755 if mode & 0o111 else 0o644)


def make_deb(stage, source, name, version, owner, package_id, output):
    binaries = list(elfs(stage))
    if not binaries:
        raise ValueError("Empty native package")
    debian = source / "debian"
    debian.mkdir(exist_ok=True)
    (debian / "control").write_text(
        f"Source: {name}\nSection: x11\nPriority: optional\nMaintainer: {owner}\nStandards-Version: 4.6.2\n\nPackage: {name}\nArchitecture: amd64\nDescription: Pinned native Noble desktop integration\n"
    )
    args = ["dpkg-shlibdeps", "-O"]
    if package_id == "dux-runtime":
        prefix = next((stage / "opt/dotfiles/dux").iterdir())
        args += ["-l" + str(prefix / "qt/lib")]
        # Qt/ICU/Wayland are in this same private package, not external APT deps.
        excluded = set()
        for library in elfs(prefix / "qt/lib"):
            soname = re.search(r"\(SONAME\).*\[(.+?)\]", run(["readelf", "-d", library]))
            if soname:
                excluded.add(soname.group(1))
        local = source / "debian/shlibs.local"
        local.write_text(
            "".join(
                f"{soname.split('.so.')[0]} {soname.split('.so.', 1)[1]} {name}\n"
                for soname in sorted(excluded)
                if ".so." in soname
            )
        )
    args += ["-e" + str(p) for p in binaries if "(NEEDED)" in run(["readelf", "-d", p])]
    raw = run(args, cwd=source).strip()
    if not raw.startswith("shlibs:Depends="):
        raise ValueError("dpkg-shlibdeps did not produce dependencies")
    dependencies = [
        d.strip()
        for d in raw.removeprefix("shlibs:Depends=").split(",")
        if d.strip() and not d.strip().startswith(name)
    ]
    dependencies += DLOPEN[package_id]
    control = stage / "DEBIAN"
    control.mkdir(exist_ok=True)
    (control / "control").write_text(
        f"Package: {name}\nVersion: {version}\nArchitecture: amd64\nMaintainer: {owner}\nDepends: {', '.join(dict.fromkeys(dependencies))}\nDescription: Exact upstream desktop runtime built for Ubuntu Noble\n"
    )
    package = output / f"{name}_{version}_amd64.deb"
    normalize_debian_payload(stage)
    run(["dpkg-deb", "--root-owner-group", "--build", stage, package])
    # Round-trip native metadata without unpacking/installing into the host.
    fields = run(["dpkg-deb", "-f", package, "Package", "Version", "Architecture"])
    if name not in fields or version not in fields or "amd64" not in fields:
        raise ValueError("Native package metadata mismatch")
    return package


def payload_manifest(stage):
    result = {}
    for path in sorted(stage.rglob("*")):
        relative = str(path.relative_to(stage))
        if relative == "DEBIAN" or relative.startswith("DEBIAN/"):
            continue
        if path.is_symlink():
            result[relative] = {"link": os.readlink(path)}
        elif path.is_file():
            if path.stat().st_mode & 0o6000:
                raise ValueError("Privileged mode is forbidden in desktop payload: " + str(path))
            result[relative] = {"sha256": digest(path), "mode": path.stat().st_mode & 0o777}
    return result


def rust_package(context, lock, package_id, work):
    downloads = Path(context["paths"]["cache"]) / "desktop-sources"
    rust_archive = fetch(lock["rust"], downloads)
    rust = untar(rust_archive, work / "rust-source")
    toolchain = work / "rust"
    run(
        [
            "sh",
            rust / "install.sh",
            "--prefix=" + str(toolchain),
            "--components=rustc,cargo,rust-std-x86_64-unknown-linux-gnu",
            "--disable-ldconfig",
        ]
    )
    env = environment()
    env.update(
        PATH=str(toolchain / "bin") + ":" + env["PATH"],
        HOME=str(work / "home"),
        CARGO_HOME=str(work / "cargo"),
        RUSTUP_HOME=str(work / "rustup"),
    )
    Path(env["HOME"]).mkdir()
    if lock["rust"]["version"] not in run([toolchain / "bin/rustc", "--version"], env=env):
        raise ValueError("Rust toolchain version mismatch")
    source = checkout(lock[package_id], work / "source")
    if package_id == "niri":
        import tomllib

        cargo = tomllib.loads((source / "Cargo.toml").read_text())
        if set(cargo["features"]["default"]) != set(lock["niri"]["features"]):
            raise ValueError("Upstream Niri default feature contract changed")
    args = [toolchain / "bin/cargo", "build", "--release", "--locked", "--bin", package_id]
    if package_id == "xwayland-satellite":
        args += ["--features", "systemd"]
    run(args, cwd=source, env=env)
    if digest(source / "Cargo.lock") != lock[package_id]["cargo_lock_sha256"]:
        raise ValueError("Build modified locked Cargo dependencies")
    stage = work / "stage"
    stage.mkdir()
    copy_assets(source, stage, NIRI_ASSETS if package_id == "niri" else SATELLITE_ASSETS)
    binary = stage / "usr/bin" / package_id
    inspect_elf(binary)
    version = lock[package_id]["version"]
    reported = run([binary, "--version" if package_id == "niri" else "-version"], env=env)
    if version not in reported:
        raise ValueError("Built source version mismatch")
    if package_id == "niri":
        run(
            [binary, "validate", "--config", stage / "usr/share/doc/niri/default-config.kdl"],
            env=env,
        )
    package = make_deb(
        stage, source, package_id, version + "-0noble1", maintainer(context), package_id, work
    )
    return package, stage, package_id, version + "-0noble1"


def sdk_exclusions(qt_version):
    return [path.format(qt_version=qt_version) for path in SDK_EXCLUSION_TEMPLATES]


def select_sdk_runtime(qt, runtime):
    """Deploy the Dux/QS closure, not unrelated SDK database/3D adapters."""
    exclusions = runtime["deployment_exclusions"]
    if exclusions != sdk_exclusions(runtime["qt_version"]):
        raise ValueError("SDK deployment exclusions differ from the reviewed application closure")
    required = (
        "plugins/sqldrivers/libqsqlite.so",
        "plugins/multimedia/libffmpegmediaplugin.so",
        "qml/QtMultimedia/libquickmultimediaplugin.so",
    )
    for relative in required:
        if not (qt / relative).is_file():
            raise ValueError("Required SQLite/Multimedia/QML SDK resource absent: " + relative)
    for relative in exclusions:
        path = qt / relative
        if not path.exists() and not path.is_symlink():
            raise ValueError("SDK layout changed at declared deployment exclusion: " + relative)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def private_bzip2(source, qt, runtime, work, env):
    """Unmodified upstream library API, with the official SDK's RHEL SONAME."""
    version = re.search(
        r'#define\s+BZ_VERSION\s+"([^"]+)"', (source / "bzlib_private.h").read_text()
    )
    if not version or version.group(1).split(",", 1)[0] != runtime["bzip2_version"]:
        raise ValueError("Private bzip2 source version mismatch")
    if list((qt / "lib").glob("libbz2.so*")):
        raise ValueError("SDK unexpectedly bundles bzip2; review the coupled source contract")
    builddir = work / "bzip2-build"
    builddir.mkdir()
    objects = []
    for name in (
        "blocksort",
        "huffman",
        "crctable",
        "randtable",
        "compress",
        "decompress",
        "bzlib",
    ):
        obj = builddir / (name + ".o")
        run(
            [
                "gcc",
                "-fpic",
                "-fPIC",
                "-Wall",
                "-Winline",
                "-O2",
                "-g",
                "-D_FILE_OFFSET_BITS=64",
                "-c",
                source / (name + ".c"),
                "-o",
                obj,
            ],
            env=env,
        )
        objects.append(obj)
    library = qt / "lib" / ("libbz2.so." + runtime["bzip2_version"])
    run(
        ["gcc", "-shared", "-Wl,-soname," + runtime["bzip2_soname"], "-o", library, *objects],
        env=env,
    )
    (qt / "lib" / runtime["bzip2_soname"]).symlink_to(library.name)
    probe_private_bzip2(library, runtime["bzip2_version"], env)


def probe_private_bzip2(library, version, env=None):
    # Packaging rewrites this ELF's RUNPATH later. Never keep the mutable image
    # mapped in the builder: changing a loaded DSO can crash interpreter teardown.
    script = (
        "import ctypes,sys;"
        "api=ctypes.CDLL(sys.argv[1]);"
        "api.BZ2_bzlibVersion.argtypes=[];"
        "api.BZ2_bzlibVersion.restype=ctypes.c_char_p;"
        "print(api.BZ2_bzlibVersion().decode())"
    )
    reported = run(["/usr/bin/python3", "-I", "-c", script, library], env=env)
    if reported.split(",", 1)[0] != version:
        raise ValueError("Compiled private bzip2 API version mismatch")


def runtime_package(context, lock, work):
    runtime = lock["runtime"]
    downloads = Path(context["paths"]["cache"]) / "desktop-sources"
    # Resolve required legal inputs before spending time on native compilation.
    for name, record in runtime["licenses"].items():
        try:
            fetch(record, downloads)
        except OSError as error:
            raise ValueError("Unable to acquire declared runtime license: " + name) from error
    runtime_id = (
        "dms-" + runtime["version"] + "-qt-" + runtime["qt_version"] + "-" + lock_digest(lock)[:12]
    )
    logical = Path("/opt/dotfiles/dux") / runtime_id
    stage = work / "stage"
    prefix = stage / logical.relative_to("/")
    qt = prefix / "qt"
    qt.mkdir(parents=True)
    for record in runtime["qt_archives"]:
        archive = fetch(record, downloads)
        target = qt / "lib" if record["module"] == "icu" else qt
        target.mkdir(exist_ok=True)
        # 7z listing is checked before extraction, including symlink targets after.
        listing = run(["/usr/bin/7z", "l", "-slt", archive])
        for line in listing.splitlines():
            if line.startswith("Path = "):
                entry = line[7:]
                if entry == str(archive):
                    continue
                if Path(entry).is_absolute() or ".." in Path(entry).parts:
                    raise ValueError("Unsafe Qt archive member")
        run(["/usr/bin/7z", "x", "-y", "-o" + str(target), archive])
    for link in qt.rglob("*"):
        if link.is_symlink() and not link.resolve().is_relative_to(qt.resolve()):
            raise ValueError("Qt SDK contains an escaping symlink")
    legal = prefix / "share/licenses"
    # Capture actual notices before excluding any unreferenced SDK bindings.
    legal_files(qt, legal / "qt-sdk")
    select_sdk_runtime(qt, runtime)
    (qt / "bin/qt.conf").write_text("[Paths]\nPrefix=..\n")
    (qt / "libexec/qt.conf").write_text("[Paths]\nPrefix=..\n")
    patch_runtime(prefix)
    if run([qt / "bin/qmake", "-query", "QT_VERSION"]).strip() != runtime["qt_version"]:
        raise ValueError("Qt artifact version mismatch")
    if not list(qt.rglob("qquickitem_p.h")) or not list(qt.rglob("qwaylandwindow_p.h")):
        raise ValueError("Required Qt private headers absent")
    sources = {}
    for name, record in runtime["sources"].items():
        archive = fetch(record, downloads)
        if name != "dms":
            sources[name] = untar(archive, work / (name + "-source"))
    env = environment()
    env.update(
        PATH=str(qt / "bin") + ":" + env["PATH"],
        PKG_CONFIG_PATH=os.pathsep.join((str(qt / "lib/pkgconfig"), str(qt / "share/pkgconfig"))),
        CMAKE_PREFIX_PATH=str(qt),
        CC="clang-18",
        CXX="clang++-18",
        HOME=str(work / "home"),
    )
    Path(env["HOME"]).mkdir()
    for name, options in (
        ("wayland", ["-Ddocumentation=false", "-Dtests=false"]),
        ("protocols", ["-Dtests=false"]),
    ):
        builddir = work / (name + "-build")
        # Build-time private prefix is staged, never the system prefix.
        run(
            [
                "meson",
                "setup",
                builddir,
                sources[name],
                "--prefix=" + str(qt),
                "--libdir=lib",
                "--buildtype=release",
                *options,
            ],
            env=env,
        )
        run(["meson", "compile", "-C", builddir, "-j", "2"], env=env)
        run(["meson", "install", "-C", builddir], env=env)
    versions = run(
        ["pkg-config", "--modversion", "wayland-client", "wayland-protocols"], env=env
    ).splitlines()
    if versions != [runtime["wayland_version"], runtime["protocols_version"]]:
        raise ValueError("Private Wayland/protocols source version mismatch")
    private_bzip2(sources["bzip2"], qt, runtime, work, env)
    source = sources["quickshell"]
    cmake_text = (source / "CMakeLists.txt").read_text()
    if f'VERSION "{runtime["quickshell_version"]}"' not in cmake_text:
        raise ValueError("Quickshell source version mismatch")
    if {
        m.group(1) for m in re.finditer(r"boption\((\w+)\s+\"[^\"]+\" ON", cmake_text)
    } != PRODUCTION_FEATURES:
        raise ValueError("Upstream production feature set changed")
    builddir = work / "qs-build"
    options = ["-D" + feature + "=ON" for feature in sorted(PRODUCTION_FEATURES)]
    options += [
        "-DVENDOR_CPPTRACE=ON",
        "-DCMAKE_CXX_FLAGS=-pthread",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_INSTALL_PREFIX=" + str(logical),
        "-DCMAKE_INSTALL_RPATH=" + str(logical / "qt/lib"),
        "-DINSTALL_QML_PREFIX=lib/qt6/qml",
        "-DDISTRIBUTOR=" + maintainer(context),
        "-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
    ]
    options += [
        "-DFETCHCONTENT_SOURCE_DIR_" + name.upper() + "=" + str(sources[name])
        for name in ("cpptrace", "libdwarf", "zstd")
    ]
    run(["cmake", "-S", source, "-B", builddir, "-GNinja", *options], env=env)
    cache = (builddir / "CMakeCache.txt").read_text()
    for feature in PRODUCTION_FEATURES:
        if not re.search(r"^" + feature + r":BOOL=ON$", cache, re.M):
            raise ValueError("Production feature disabled: " + feature)
    run(["cmake", "--build", builddir, "--parallel", "2"], env=env)
    run(["cmake", "--install", builddir], env=dict(env, DESTDIR=str(stage)))
    # Upstream qs symlink names the final absolute prefix; preserve for installed use.
    bin_dir = prefix / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "qt.conf").write_text("[Paths]\nPrefix=../qt\n")
    dms_archive = fetch(runtime["sources"]["dms"], downloads)
    with gzip.open(dms_archive, "rb") as stream, (bin_dir / "dms").open("wb") as target:
        shutil.copyfileobj(stream, target)
    (bin_dir / "dms").chmod(0o755)
    patch_runtime(prefix)
    for executable, argument, expected in (
        (bin_dir / "quickshell", "--version", runtime["quickshell_version"]),
        (bin_dir / "dms", "version", runtime["version"]),
    ):
        if expected not in run([executable, argument], env=env):
            raise ValueError("Runtime executable version mismatch")
    for path in elfs(prefix):
        inspect_elf(path, runtime=True)
    for name, source_tree in sources.items():
        legal_files(source_tree, legal / name)
    for name, record in runtime["licenses"].items():
        shutil.copy2(fetch(record, downloads), legal / (name + ".txt"))
    # Build metadata must describe the installed prefix, never the workspace.
    for metadata in qt.rglob("*"):
        if (
            metadata.is_file()
            and not metadata.is_symlink()
            and metadata.suffix in (".pc", ".cmake", ".prl", ".la")
        ):
            original = metadata.read_text()
            relocated = original.replace(str(qt), str(logical / "qt"))
            if relocated != original:
                metadata.write_text(relocated)
    atomic_json(prefix / "share/source-lock.json", lock)
    (prefix / "share/LOCAL-ARTIFACT-POLICY.txt").write_text(
        "LOCAL OPERATOR USE ONLY; NOT AUTHORIZED FOR REDISTRIBUTION.\nQt/ICU SDK binaries are acquired directly from the official repository for local assembly. Actual embedded notices are retained under share/licenses before application-specific deployment selection; exact source/artifact URLs, digests and exclusions are in share/source-lock.json.\nGeneric license texts are not a corresponding-source offer. Corresponding-source verification is tracked explicitly in the source lock; no commercial license, LTS patch entitlement or redistribution compliance is inferred. Review all applicable obligations before any binary redistribution.\n"
    )
    name = "dotfiles-dux-" + lock_digest(lock)[:12]
    version = runtime["version"] + "-0noble1"
    package = make_deb(stage, source, name, version, maintainer(context), "dux-runtime", work)
    return package, stage, name, version


def build(context, lock, package_id, output):
    noble(context)
    maintainer(context)
    work = Path(output).resolve() / (package_id + "-" + lock_digest(lock)[:16])
    if work.exists():
        receipt = work / "receipt.json"
        if receipt.exists():
            record = json.loads(receipt.read_text())
            if digest(record["deb"]) != record["deb_sha256"]:
                raise ValueError("Existing built package was modified")
            if record.get("maintainer") != maintainer(context):
                raise ValueError("Cached native package maintainer differs from declared caller")
            return receipt
        raise ValueError(
            f"Incomplete workspace retained for diagnosis: {work}; choose a fresh output directory"
        )
    work.mkdir(parents=True)
    if package_id in ("niri", "xwayland-satellite"):
        package, stage, name, version = rust_package(context, lock, package_id, work)
    elif package_id == "dux-runtime":
        package, stage, name, version = runtime_package(context, lock, work)
    else:
        raise ValueError("No source build for " + package_id)
    record = {
        "schema_version": 1,
        "id": package_id,
        "lock_sha256": lock_digest(lock),
        "source_lock": lock,
        "maintainer": maintainer(context),
        "package": name,
        "version": version,
        "deb": str(package),
        "deb_sha256": digest(package),
        "payload": payload_manifest(stage),
    }
    atomic_json(work / "source-lock.json", lock)
    atomic_json(work / "receipt.json", record)
    return work / "receipt.json"
