"""Read-only safety checks for ordinary Homebrew desktop packages.

The engine calls preflight(context, package, info, owned=None) before an
explicit brew operation and check(context, package, info) after readback. info
is brew info --json=v2; owned is the previous observed.macos dictionary.
Homebrew alone installs/registers/removes apps, formulae and individual fonts.
This module never installs, launches, registers, updates, grants OS consent or
writes application data. Retirement compares the returned immutable payload
identity before ordinary non-zap uninstall. Generic recovery refuses to invent
historical Homebrew artifacts; a reviewed current reinstall uses provision.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import stat
import struct

from safety import Error, digest, native, no_symlink_parents, relative

ARM64 = 0x0100000C


def roots(context):
    if context.get("platform") != "macos-arm64":
        raise Error("macOS GUI verification requires macos-arm64")
    fixture = context.get("_macos_fixture_root")
    if fixture is not None:
        home, fixture = Path(context["home"]).resolve(), Path(fixture)
        if (
            not context.get("_fixture_context")
            or not context.get("_verification_only")
            or not fixture.is_absolute()
            or not fixture.resolve().is_relative_to(home)
        ):
            raise Error("macOS fixture roots require isolated verification context")
        no_symlink_parents(fixture / "placeholder", home)
        return fixture / "Applications", fixture / "opt/homebrew", home / "Library/Fonts"
    return Path("/Applications"), Path("/opt/homebrew"), Path(context["home"]) / "Library/Fonts"


def provider_record(package, info):
    key = "casks" if package["provider"] == "cask" else "formulae"
    records = info.get(key, [])
    if len(records) != 1 or records[0].get("token", records[0].get("name")) != package["name"]:
        raise Error("Homebrew metadata does not identify the selected package")
    return records[0]


def installed_versions(package, record):
    installed = record.get("installed")
    if package["provider"] == "cask":
        return [installed] if isinstance(installed, str) and installed else []
    return [
        item["version"]
        for item in installed or []
        if isinstance(item, dict) and item.get("version")
    ]


def macho_arm64(path):
    """Inspect bounded Mach-O executable slices without launching or Rosetta."""
    with Path(path).open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size

        def executable_slice(offset, length):
            if offset < 0 or length < 32 or offset + length > size:
                return False
            stream.seek(offset)
            header = stream.read(32)
            if header[:4] not in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
                return False
            endian = "<" if header[0] == 0xCF else ">"
            _, cpu, _, filetype, commands, command_bytes, _, _ = struct.unpack(
                endian + "8I", header
            )
            return (
                cpu == ARM64
                and filetype == 2
                and command_bytes <= length - 32
                and commands <= command_bytes // 8
            )

        header = stream.read(8)
        if len(header) != 8:
            raise Error(f"truncated native executable: {path}")
        magic = header[:4]
        if magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
            if executable_slice(0, size):
                return
        elif magic in (
            b"\xca\xfe\xba\xbe",
            b"\xbe\xba\xfe\xca",
            b"\xca\xfe\xba\xbf",
            b"\xbf\xba\xfe\xca",
        ):
            endian = ">" if magic[0] == 0xCA else "<"
            count = struct.unpack(endian + "I", header[4:])[0]
            wide = magic in (b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca")
            width = 32 if wide else 20
            if count > 64 or 8 + count * width > size:
                raise Error(f"invalid Mach-O slice table: {path}")
            entries = stream.read(count * width)
            for index in range(count):
                entry = entries[index * width : (index + 1) * width]
                cpu = struct.unpack_from(endian + "I", entry)[0]
                offset, length = struct.unpack_from(endian + ("QQ" if wide else "II"), entry, 8)
                if (
                    cpu == ARM64
                    and offset >= 8 + count * width
                    and executable_slice(offset, length)
                ):
                    return
    raise Error(f"native arm64 Mach-O slice required (no Rosetta): {path}")


def payload_identity(root):
    """Hash only the installed app payload, never preferences, sessions or caches."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise Error(f"native app must be a real directory, not Nix Apps/foreign link: {root}")
    entries = []
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        name = path.relative_to(root).as_posix()
        if stat.S_ISLNK(mode):
            if not path.resolve().is_relative_to(root.resolve()):
                raise Error(f"app payload symlink escapes bundle: {path}")
            entries.append([name, "symlink", os.readlink(path)])
        elif stat.S_ISREG(mode):
            entries.append([name, "file", stat.S_IMODE(mode), digest(path)])
        elif stat.S_ISDIR(mode):
            entries.append([name, "directory", stat.S_IMODE(mode)])
        else:
            raise Error(f"unsupported app payload entry: {path}")
    import json

    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def font_families(path):
    """Read OpenType name IDs 1/16; a filename alone is not font identity."""
    data = Path(path).read_bytes()
    try:
        if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
            raise ValueError("not an sfnt font")
        count = struct.unpack_from(">H", data, 4)[0]
        for index in range(count):
            tag, _, offset, size = struct.unpack_from(">4sIII", data, 12 + index * 16)
            if tag != b"name":
                continue
            table = data[offset : offset + size]
            _, names, strings = struct.unpack_from(">HHH", table)
            families = set()
            for item in range(names):
                platform, _, _, name, length, start = struct.unpack_from(
                    ">HHHHHH", table, 6 + item * 12
                )
                if name not in (1, 16):
                    continue
                raw = table[strings + start : strings + start + length]
                if len(raw) != length:
                    raise ValueError("truncated font name")
                families.add(raw.decode("utf-16-be" if platform in (0, 3) else "mac_roman"))
            if families:
                return sorted(families)
        raise ValueError("missing font family name")
    except (ValueError, UnicodeError, struct.error) as exc:
        raise Error(f"invalid installed font {path}: {exc}") from exc


def destinations(context, package, info):
    apps, brew, fonts = roots(context)
    spec = package["macos"]
    record = provider_record(package, info)
    if "app" in spec:
        name = spec["app"]["name"]
        relative(name)
        if (
            "/" in name
            or not name.endswith(".app")
            or not any(item.get("app", [None])[0] == name for item in record.get("artifacts", []))
        ):
            raise Error("Homebrew app staging metadata disagrees with desired bundle")
        return [apps / name]
    if "font" in spec:
        names = []
        for artifact in record.get("artifacts", []):
            if "font" in artifact:
                name = artifact["font"][0]
                relative(name)
                if "/" in name or not name.endswith(".ttf"):
                    raise Error("unsafe Homebrew font artifact")
                names.append(name)
        required = spec["font"]["required_faces"]
        if len(names) != len(set(names)) or not set(required).issubset(names):
            raise Error("Homebrew font staging metadata lacks required family faces")
        return [fonts / name for name in sorted(names)]
    if spec.get("formula"):
        return [brew / "opt" / package["name"]]
    raise Error("unknown macOS desktop verification shape")


def preflight(context, package, info, owned=None):
    paths = destinations(context, package, info)
    for path in paths:
        if "font" in package["macos"]:
            no_symlink_parents(path, Path(context["home"]))
        elif "app" in package["macos"]:
            no_symlink_parents(path, path.parent)
    if owned is not None:
        if check(context, package, info) != owned:
            raise Error("foreign replacement or changed native payload; preserved")
        return
    record = provider_record(package, info)
    if installed_versions(package, record):
        # Engine preserves this verified installation as preexisting, with no
        # install/upgrade and no subsequent owned retirement authorization.
        check(context, package, info)
        return
    for path in paths:
        if path.exists() or path.is_symlink():
            raise Error(f"foreign native destination collision; preserved: {path}")


def check(context, package, info):
    paths = destinations(context, package, info)
    record = provider_record(package, info)
    versions = installed_versions(package, record)
    if not versions:
        raise Error(f"Homebrew package not installed: {package['name']}")
    spec = package["macos"]
    if "app" in spec:
        app = paths[0]
        no_symlink_parents(app, app.parent)
        identity = payload_identity(app)
        try:
            with (app / "Contents/Info.plist").open("rb") as stream:
                plist = plistlib.load(stream)
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            raise Error(f"invalid native app metadata: {app}") from exc
        desired = spec["app"]
        if plist.get("CFBundleIdentifier") != desired["bundle_id"]:
            raise Error(f"foreign app bundle identity: {app}")
        version = str(plist.get("CFBundleShortVersionString", ""))
        if version not in {item.split(",", 1)[0] for item in versions}:
            raise Error(
                f"app version differs from installed Homebrew receipt: {app}; bundle={version!r}, receipt={versions!r}; preserve self-updated/foreign payload for explicit reconciliation"
            )
        relative(desired["binary"])
        executable = app / desired["binary"]
        if not executable.resolve().is_relative_to(app.resolve()):
            raise Error("app command escapes native bundle")
        native(executable)
        macho_arm64(executable)
        if plist.get("CFBundleExecutable") != executable.name:
            raise Error("app main executable differs from desired native command")
        for command, declared in package.get("executables", {}).items():
            try:
                relative_command = Path(declared).relative_to(
                    Path("/Applications") / desired["name"]
                )
            except ValueError as exc:
                raise Error(f"app command is not in the declared native bundle: {command}") from exc
            binary = app / relative_command
            if not binary.resolve().is_relative_to(app.resolve()):
                raise Error(f"app command escapes native bundle: {command}")
            native(binary)
            macho_arm64(binary)
        return {
            "kind": "app",
            "path": str(app),
            "bundle_id": desired["bundle_id"],
            "version": version,
            "executable": str(executable),
            "architecture": "arm64",
            "payload_sha256": identity,
            "registration": "Homebrew app artifact in /Applications; LaunchServices/login-session and OS consent acceptance remain unverified",
        }
    if "font" in spec:
        fonts = []
        for path in paths:
            no_symlink_parents(path, Path(context["home"]))
            if path.is_symlink() or not path.is_file():
                raise Error(f"missing or foreign font destination: {path}")
            families = font_families(path)
            if (
                path.name in spec["font"]["required_faces"]
                and spec["font"]["family"] not in families
            ):
                raise Error(f"wrong font family at {path}")
            fonts.append({"path": str(path), "sha256": digest(path), "families": families})
        return {
            "kind": "font",
            "versions": versions,
            "family": spec["font"]["family"],
            "files": fonts,
        }
    prefix = paths[0]
    brew = roots(context)[1]
    if not prefix.is_dir() or not prefix.resolve().is_relative_to(
        (brew / "Cellar" / package["name"]).resolve()
    ):
        raise Error("formula opt prefix is missing or outside its native Cellar owner")
    selected_version = prefix.resolve().name
    if selected_version not in versions:
        raise Error("formula opt prefix differs from installed Homebrew receipt")
    executables = {}
    for command, value in package.get("executables", {}).items():
        relative(value)
        path = prefix / value
        native(path)
        if not path.resolve().is_relative_to(prefix.resolve()):
            raise Error("formula executable escapes selected keg")
        macho_arm64(path)
        executables[command] = {"path": str(path), "sha256": digest(path)}
    if not executables:
        raise Error("native formula verification requires a declared command")
    return {
        "kind": "formula",
        "version": selected_version,
        "prefix": str(prefix.resolve()),
        "architecture": "arm64",
        "executables": executables,
    }
