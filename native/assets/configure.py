#!/usr/bin/env python3
"""Explicit native configuration operations; no shell-startup hooks.

CLI: ACTION --context FILE [--output DIRECTORY] --id RECIPE_ID
plan/check are read-only; build stages a Bat cache or verified extension payloads;
provision creates caches, reconciles a CLOSED Firefox profile, or changes only
Btop's color_theme key. retire preserves browser data and restores owned fields.
Ordinary chezmoi render/apply never invokes this module. All executables are taken
from the engine's absolute trusted context, never caller PATH or a project config.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ASSETS = Path(__file__).resolve().parent
MAX_SEARCH = 16 * 1024 * 1024
DISCLAIMER = (
    "By modifying this file, I agree that I am doing so "
    "only within Firefox itself, using official, user-driven search "
    "engine selection processes, and in a way which does not circumvent "
    "user consent. I acknowledge that any attempt to change this file "
    "from outside of Firefox is a malicious act, and will be responded "
    "to accordingly."
)


def enabled(context, key):
    value = context["dotfiles"]
    for part in key.split("."):
        value = value[part]
    return bool(value)


def trusted(context, name):
    executable = Path(context["executables"][name])
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise ValueError(f"native executable is unavailable: {name}")
    if "/nix/" in str(executable):
        raise ValueError(f"native executable uses the retired package store: {name}")
    return str(executable)


def atomic(path, data):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"refusing mutable-file symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".dotfiles-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def firefox_paths(context):
    home = Path(context["home"])
    if context["platform"] == "macos-arm64":
        root = home / "Library/Application Support/Firefox"
        relative = "Profiles/default"
    elif context["platform"] == "linux-x64":
        root = home / ".mozilla/firefox"
        relative = "default"
    else:
        raise ValueError("unsupported Firefox platform")
    profile = root / relative
    cursor = home
    for component in profile.relative_to(home).parts:
        cursor /= component
        if cursor.is_symlink():
            raise ValueError(f"foreign Firefox profile directory symlink: {cursor}")
    return root, profile, relative


def ini_data(path):
    parser = configparser.RawConfigParser(strict=True)
    parser.optionxform = str
    if path.is_symlink():
        raise ValueError(f"refusing mutable-file symlink: {path}")
    if path.exists():
        parser.read_string(path.read_text())
    return parser


def patch_ini(path, changes):
    """Change selected INI fields without rewriting unrelated sections/comments."""
    original = path.read_text() if path.exists() else ""
    lines = original.splitlines(keepends=True)
    for section, fields in changes.items():
        start = next((i for i, line in enumerate(lines) if line.strip() == f"[{section}]"), None)
        if start is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.extend(["\n", f"[{section}]\n"])
            start = len(lines) - 1
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        for key, value in fields.items():
            matches = [
                i
                for i in range(start + 1, end)
                if re.match(r"^\s*" + re.escape(key) + r"\s*=", lines[i])
            ]
            if len(matches) > 1:
                raise ValueError(f"duplicate Firefox INI field: {section}.{key}")
            if matches:
                index = matches[0]
                if value is None:
                    del lines[index]
                    end -= 1
                else:
                    lines[index] = f"{key}={value}\n"
            elif value is not None:
                lines.insert(end, f"{key}={value}\n")
                end += 1
    result = "".join(lines).encode()
    if result != original.encode():
        atomic(path, result)


def reconcile_profile(context, dry_run=False):
    root, profile, relative = firefox_paths(context)
    parser = ini_data(root / "profiles.ini")
    candidates = [
        section
        for section in parser.sections()
        if section.startswith("Profile") and parser.get(section, "Name", fallback="") == "default"
    ]
    if len(candidates) > 1:
        raise ValueError("multiple Firefox profiles named default")
    if candidates:
        section = candidates[0]
        if (
            parser.get(section, "Path", fallback="") != relative
            or parser.get(section, "IsRelative", fallback="1") != "1"
        ):
            raise ValueError(
                "foreign Firefox default profile has a different path; preserve it and resolve selection explicitly"
            )
    else:
        # Never alias an existing foreign name/path into our managed record.
        if any(parser.get(s, "Path", fallback="") == relative for s in parser.sections()):
            raise ValueError("Firefox managed profile path is already owned under another name")
        number = 0
        while parser.has_section(f"Profile{number}"):
            number += 1
        section = f"Profile{number}"
    changes = {
        s: {"Default": None}
        for s in parser.sections()
        if s.startswith("Profile") and s != section and parser.has_option(s, "Default")
    }
    changes[section] = {"Name": "default", "IsRelative": "1", "Path": relative, "Default": "1"}
    changes["General"] = {"StartWithLastProfile": "1", "Version": "2"}
    for s in parser.sections():
        if s.startswith("Install"):
            changes[s] = {"Default": relative}
    installs = ini_data(root / "installs.ini")
    if not dry_run:
        profile.mkdir(parents=True, exist_ok=True)
        patch_ini(root / "profiles.ini", changes)
        if installs.sections():
            patch_ini(
                root / "installs.ini", {s: {"Default": relative} for s in installs.sections()}
            )
    return profile


def decode_search(payload):
    if payload[:8] != b"mozLz40\0" or len(payload) < 12:
        raise ValueError("invalid Firefox search container")
    size = struct.unpack_from("<I", payload, 8)[0]
    if size > MAX_SEARCH:
        raise ValueError("Firefox search container exceeds limit")
    src = memoryview(payload)[12:]
    pos = 0
    out = bytearray()

    def length(value):
        nonlocal pos
        if value == 15:
            while True:
                if pos >= len(src):
                    raise ValueError("truncated Firefox search block")
                extra = src[pos]
                pos += 1
                value += extra
                if extra != 255:
                    break
        return value

    while pos < len(src):
        token = src[pos]
        pos += 1
        count = length(token >> 4)
        if pos + count > len(src) or len(out) + count > size:
            raise ValueError("invalid Firefox search literal")
        out.extend(src[pos : pos + count])
        pos += count
        if pos == len(src):
            break
        if pos + 2 > len(src):
            raise ValueError("truncated Firefox search offset")
        offset = src[pos] | src[pos + 1] << 8
        pos += 2
        count = length(token & 15) + 4
        if not offset or offset > len(out) or len(out) + count > size:
            raise ValueError("invalid Firefox search match")
        for _ in range(count):
            out.append(out[-offset])
    if len(out) != size:
        raise ValueError("Firefox search size mismatch")
    return json.loads(out)


def encode_search(value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    size = len(data)
    if size > MAX_SEARCH:
        raise ValueError("Firefox search configuration exceeds limit")
    block = bytearray([min(size, 15) << 4])
    if size >= 15:
        count = size - 15
        while count >= 255:
            block.append(255)
            count -= 255
        block.append(count)
    block.extend(data)
    return b"mozLz40\0" + struct.pack("<I", size) + block


def search_default_hash(profile, value):
    """Match Firefox's verification salt: profile directory leaf, not full path."""
    salt = profile.name + value + DISCLAIMER
    return base64.b64encode(hashlib.sha256(salt.encode()).digest()).decode()


def _search_engines(current):
    if (
        not isinstance(current, dict)
        or type(current.get("version")) is not int
        or current["version"] not in {12, 14}
        or not isinstance(current.get("engines"), list)
        or not isinstance(current.get("metaData", {}), dict)
    ):
        raise ValueError("unsupported Firefox search schema; refusing destructive replacement")
    engines = {}
    for item in current["engines"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or item["id"] in engines
            or not isinstance(item.get("_metaData", {}), dict)
        ):
            raise ValueError("invalid Firefox search engine record; refusing replacement")
        engines[item["id"]] = item
    return engines


def desired_search(profile, previous=None):
    desired = json.loads((ASSETS / "firefox/search.json").read_text())
    current = previous if previous is not None else {"version": 12, "engines": [], "metaData": {}}
    _search_engines(current)
    result = copy.deepcopy(current)
    engines = {item["id"]: item for item in result["engines"]}
    classification = "_isAppProvided" if result["version"] == 12 else "_isConfigEngine"
    for definition in desired["engines"]:
        identifier = definition["id"]
        engine = engines.setdefault(identifier, {"id": identifier})
        engine.update(
            {
                "_name": identifier,
                "_loadPath": "[dotfiles]/" + identifier,
                "_definedAliases": [definition["alias"]],
            }
        )
        # Base/OpenSearch serialization omits the schema-14 config flag.
        engine.pop("_isAppProvided", None)
        engine.pop("_isConfigEngine", None)
        if result["version"] == 12:
            engine[classification] = False
        urls = engine.get("_urls")
        url = urls[0] if isinstance(urls, list) and urls and isinstance(urls[0], dict) else {}
        old_params = url.get("params", [])
        params = []
        for definition_param in definition["params"]:
            param = (
                next(
                    (
                        item.copy()
                        for item in old_params
                        if isinstance(item, dict) and item.get("name") == definition_param["name"]
                    ),
                    {},
                )
                if isinstance(old_params, list)
                else {}
            )
            # Firefox ignores persisted parameters with truthy config-only
            # keys. Remove these keys when repairing authored query values.
            param.pop("purpose", None)
            param.pop("mozparam", None)
            param.update(definition_param)
            params.append(param)
        url.update(
            {
                "template": definition["template"],
                "method": "GET",
                "type": "text/html",
                "rels": [],
                "params": params,
            }
        )
        engine["_urls"] = [url]
        if "icon" in definition:
            icons = engine.get("_iconMapObj")
            if not isinstance(icons, dict):
                icons = {}
            icons["16"] = definition["icon"]
            engine["_iconMapObj"] = icons
            engine["_updateInterval"] = 86400000
    for identifier in desired["hidden"] + desired["order"]:
        # Never reclassify an existing, unrelated engine.
        engines.setdefault(identifier, {"id": identifier, classification: True, "_metaData": {}})
    for identifier in desired["hidden"]:
        engines[identifier].setdefault("_metaData", {})["hidden"] = True

    def saved_order(identifier):
        number = engines[identifier].get("_metaData", {}).get("order")
        return number if type(number) is int and number > 0 else float("inf")

    # The physical engine array is not Firefox's UI order. Keep the saved
    # relative order of engines outside the authored prefix when repairing.
    order = desired["order"] + sorted(
        (name for name in engines if name not in desired["order"]), key=saved_order
    )
    for number, identifier in enumerate(order, 1):
        engines[identifier].setdefault("_metaData", {})["order"] = number
    result["engines"] = [engines[identifier] for identifier in order]
    metadata = result.setdefault("metaData", {})
    default = desired["default"]
    if metadata.get("defaultEngineId") == "" and metadata.get("appDefaultEngineId") == default:
        default = ""
    metadata.update(
        {
            "defaultEngineId": default,
            "defaultEngineIdHash": search_default_hash(profile, default),
            "useSavedOrder": True,
        }
    )
    return result


def search_matches(profile, current):
    """Compare only authored search policy, allowing Firefox's serialization."""
    engines = _search_engines(current)
    desired = json.loads((ASSETS / "firefox/search.json").read_text())
    for definition in desired["engines"]:
        engine = engines.get(definition["id"])
        if engine is None or any(
            engine.get(key) != value
            for key, value in (
                ("_name", definition["id"]),
                ("_loadPath", "[dotfiles]/" + definition["id"]),
                ("_definedAliases", [definition["alias"]]),
            )
        ):
            return False
        if any(
            engine.get(key, False) is not False for key in ("_isAppProvided", "_isConfigEngine")
        ):
            return False
        urls = engine.get("_urls")
        if not isinstance(urls, list) or len(urls) != 1 or not isinstance(urls[0], dict):
            return False
        url = urls[0]
        if (
            url.get("template") != definition["template"]
            or url.get("method", "GET") != "GET"
            or url.get("type", "text/html") != "text/html"
            or url.get("rels", []) != []
        ):
            return False
        params = url.get("params")
        if (
            not isinstance(params, list)
            or len(params) != len(definition["params"])
            or any(
                not isinstance(param, dict)
                or "purpose" in param
                or "mozparam" in param
                or any(param.get(key) != value for key, value in expected.items())
                for param, expected in zip(params, definition["params"])
            )
        ):
            return False
        if "icon" in definition and (
            not isinstance(engine.get("_iconMapObj"), dict)
            or engine["_iconMapObj"].get("16") != definition["icon"]
            or engine.get("_updateInterval") != 86400000
        ):
            return False
    for identifier in desired["hidden"]:
        if engines.get(identifier, {}).get("_metaData", {}).get("hidden") is not True:
            return False
    orders = []
    for identifier in desired["order"]:
        number = engines.get(identifier, {}).get("_metaData", {}).get("order")
        if type(number) is not int or number <= 0 or (orders and number <= orders[-1]):
            return False
        orders.append(number)
    for identifier, engine in engines.items():
        number = engine.get("_metaData", {}).get("order")
        if identifier not in desired["order"]:
            # Firefox coerces string/float/bool order values; do not certify a
            # prefix that such a value can precede in its sorted engine array.
            if number is not None and type(number) is not int:
                return False
            if type(number) is int and 0 < number <= orders[-1]:
                return False
    metadata = current.get("metaData", {})
    default = metadata.get("defaultEngineId")
    if default != desired["default"] and not (
        default == "" and metadata.get("appDefaultEngineId") == desired["default"]
    ):
        return False
    return metadata.get("useSavedOrder") is True and metadata.get(
        "defaultEngineIdHash"
    ) == search_default_hash(profile, default)


@contextlib.contextmanager
def closed_profile(profile):
    """Hold the same POSIX profile lock family while editing browser-owned data."""
    import fcntl

    profile.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        for name in ("parent.lock", ".parentlock"):
            target = profile / name
            if target.is_symlink():
                raise ValueError("Firefox profile is locked; close Firefox before provisioning")
            descriptor = os.open(target, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            stack.callback(os.close, descriptor)
            try:
                fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ValueError(
                    "Firefox profile is in use; close Firefox before provisioning"
                ) from error
        link = profile / "lock"
        if link.is_symlink():
            raise ValueError(
                "Firefox profile lock exists; close Firefox and resolve stale locks explicitly"
            )
        yield


def extensions(context):
    records = context["dotfiles"]["apps"]["firefox"].get("extensions", [])
    seen = set()
    for record in records:
        identifier = record["id"]
        if identifier in seen or any(c in identifier for c in "/\\\0") or not identifier:
            raise ValueError("invalid or duplicate Firefox extension identifier")
        if not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) or not record["url"].startswith(
            "https://"
        ):
            raise ValueError("Firefox extension needs pinned HTTPS payload and SHA256")
        seen.add(identifier)
    return records


def acquire_extension(record):
    with urllib.request.urlopen(record["url"], timeout=60) as stream:
        payload = stream.read(64 * 1024 * 1024 + 1)
    if len(payload) > 64 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != record["sha256"]:
        raise ValueError("Firefox extension digest mismatch: " + record["id"])
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        info = archive.getinfo("manifest.json")
        if info.file_size > 1024 * 1024:
            raise ValueError("Firefox extension manifest exceeds limit")
        manifest = json.loads(archive.read(info))
    gecko = manifest.get("browser_specific_settings", manifest.get("applications", {})).get(
        "gecko", {}
    )
    if gecko.get("id") != record["id"] or manifest.get("version") != record["version"]:
        raise ValueError("Firefox extension identity/version mismatch")
    return payload


def extension_state(context):
    state = Path(context["paths"]["state"]) / "firefox-extensions.json"
    if state.is_symlink():
        raise ValueError("foreign Firefox ownership record")
    value = (
        json.loads(state.read_text()) if state.exists() else {"schema_version": 1, "extensions": {}}
    )
    if value.get("schema_version") != 1 or not isinstance(value.get("extensions"), dict):
        raise ValueError("invalid Firefox extension ownership record")
    for identifier, digest in value["extensions"].items():
        if (
            not identifier
            or any(c in identifier for c in "/\\\0")
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("invalid Firefox extension ownership entry")
    return state, value["extensions"]


def prepare_extensions(context, profile, retire=False):
    state, previous = extension_state(context)
    desired = {} if retire else {record["id"]: record for record in extensions(context)}
    directory = profile / "extensions"
    if directory.is_symlink():
        raise ValueError("foreign Firefox extension directory")
    # Preflight every destination before acquisition or changes.
    for identifier in set(previous) | set(desired):
        target = directory / (identifier + ".xpi")
        if target.is_symlink():
            raise ValueError("foreign Firefox extension symlink: " + identifier)
        if target.exists():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest not in {previous.get(identifier), desired.get(identifier, {}).get("sha256")}:
                raise ValueError("foreign/locally updated Firefox extension: " + identifier)
    payloads = {}
    for identifier, record in desired.items():
        target = directory / (identifier + ".xpi")
        if (
            not target.exists()
            or hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]
        ):
            payloads[identifier] = acquire_extension(record)
    return state, previous, desired, payloads


def reconcile_extensions(context, profile, retire=False, prepared=None):
    state, previous, desired, payloads = (
        prepared if prepared is not None else prepare_extensions(context, profile, retire)
    )
    directory = profile / "extensions"
    for identifier, payload in payloads.items():
        atomic(directory / (identifier + ".xpi"), payload)
    for identifier in previous.keys() - desired.keys():
        (directory / (identifier + ".xpi")).unlink(missing_ok=True)
    atomic(
        state,
        json.dumps(
            {
                "schema_version": 1,
                "extensions": {key: value["sha256"] for key, value in desired.items()},
            },
            sort_keys=True,
        ).encode(),
    )


def bat_cache(context, output=None):
    executable = trusted(context, "bat")
    if output is None:
        config = Path(context["paths"]["config"]) / "bat"
        cache = Path(context["paths"]["cache"]).parent / "bat"
        if not (config / "themes/nord-contrast.tmTheme").is_file():
            raise ValueError(
                "apply the authored Bat Nord theme before explicitly provisioning its cache"
            )
    else:
        config = output / "config/bat"
        cache = output / "cache/bat"
        (config / "themes").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            ASSETS / "nord/Nord-contrast.tmTheme", config / "themes/nord-contrast.tmTheme"
        )
    cache.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": context["home"],
        "PATH": str(Path(executable).parent) + os.pathsep + "/usr/bin:/bin",
        "BAT_CONFIG_DIR": str(config),
        "BAT_CACHE_PATH": str(cache),
        "LC_ALL": "C",
    }
    subprocess.run([executable, "cache", "--build"], env=env, check=True, stdout=sys.stderr)
    if not (cache / "themes.bin").is_file():
        raise ValueError("Bat cache build did not produce themes.bin")
    verify_bat_theme(context, config, cache)
    return cache


def verify_bat_theme(context, config, cache):
    executable = trusted(context, "bat")
    env = {
        "HOME": context["home"],
        "PATH": str(Path(executable).parent) + os.pathsep + "/usr/bin:/bin",
        "BAT_CONFIG_DIR": str(config),
        "BAT_CACHE_PATH": str(cache),
        "LC_ALL": "C",
    }
    result = subprocess.run(
        [executable, "--list-themes", "--color=never"],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    if "nord-contrast" not in {line.strip() for line in result.stdout.splitlines()}:
        raise ValueError("native Bat cannot discover nord-contrast in its generated cache")


def btop_settings(action, context):
    """Reconcile one Btop setting; Btop owns and rewrites the rest of this file."""
    target = Path(context["paths"]["config"]) / "btop/btop.conf"
    state = Path(context["paths"]["state"]) / "btop-theme.json"
    managed = 'color_theme = "nord-contrast"'
    if action == "plan":
        return {"id": "btop-nord-settings", "target": str(target), "managed_key": "color_theme"}
    if action not in {"provision", "check", "retire"}:
        raise ValueError("Btop settings support plan/provision/check/retire, not build")
    if target.is_symlink() or state.is_symlink():
        raise ValueError("Btop legacy/foreign symlink requires an explicit ownership handoff")
    original = target.read_text() if target.exists() else ""
    lines = original.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if re.match(r"^\s*color_theme\s*=", line)]
    if len(matches) > 1:
        raise ValueError("duplicate Btop color_theme settings")
    index = matches[0] if matches else None
    current = lines[index] if index is not None else None
    recorded = json.loads(state.read_text()) if state.exists() else None
    if recorded is not None and (recorded.get("schema_version") != 1 or "previous" not in recorded):
        raise ValueError("invalid Btop setting ownership record")
    if action == "check":
        if current is None or current.strip() != managed or recorded is None:
            raise ValueError("Btop Nord setting has not been provisioned")
        return {"target": str(target), "managed_key": "color_theme"}
    if action == "retire" and recorded is None:
        return {"target": str(target), "owned_fields": 0}
    if (
        recorded is not None
        and current != recorded["previous"]
        and (current is None or current.strip() != managed)
    ):
        raise ValueError("Btop color_theme changed outside dotfiles; preserving the user setting")
    if action == "provision":
        if not (enabled(context, "shell.enable") and enabled(context, "theme.nord.enable")):
            raise ValueError("Btop Nord setting is disabled")
        if recorded is None:
            recorded = {"schema_version": 1, "previous": current}
        replacement = managed + "\n"
    else:
        replacement = recorded["previous"]
        if replacement is not None and not isinstance(replacement, str):
            raise ValueError("invalid previous Btop setting")
    if index is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if replacement is not None:
            lines.append(replacement)
    elif replacement is None:
        del lines[index]
    else:
        lines[index] = replacement
    result = "".join(lines)
    # Record the previous field before changing it; recovery never needs a
    # whole-file snapshot that could overwrite unrelated application settings.
    if action == "provision":
        atomic(state, json.dumps(recorded, sort_keys=True).encode())
    if result != original:
        atomic(target, result.encode())
    if action == "retire":
        state.unlink()
    return {"target": str(target), "action": action}


def operate(action, context, identifier, output=None):
    if identifier == "btop-nord-settings":
        return btop_settings(action, context)
    if identifier == "bat-nord-cache":
        if action == "retire":
            # The aggregate cache can include user themes/syntaxes, so retire
            # only authored inputs through chezmoi and keep application data.
            return {
                "preserved_application_cache": str(Path(context["paths"]["cache"]).parent / "bat")
            }
        if not (enabled(context, "shell.enable") and enabled(context, "theme.nord.enable")):
            raise ValueError("Bat Nord cache is disabled")
        if action == "plan":
            return {
                "id": identifier,
                "command": [context["executables"]["bat"], "cache", "--build"],
                "phase": "explicit-provision",
            }
        if action in {"build", "provision"}:
            if action == "build" and output is None:
                raise ValueError("build requires --output")
            return {"cache": str(bat_cache(context, output if action == "build" else None))}
        if action == "check":
            cache = Path(context["paths"]["cache"]).parent / "bat/themes.bin"
            if not cache.is_file():
                raise ValueError("Bat theme cache has not been provisioned")
            verify_bat_theme(context, Path(context["paths"]["config"]) / "bat", cache.parent)
            return {"cache": str(cache)}
        raise ValueError("Bat caches are application state and are not destructively retired")
    if identifier != "firefox-profile":
        raise ValueError("unknown configuration recipe ID")
    _, profile, _ = firefox_paths(context)
    if action == "plan":
        return {
            "id": identifier,
            "profile": str(profile),
            "extensions": [
                {"id": item["id"], "version": item["version"]} for item in extensions(context)
            ],
            "requires": "Firefox closed; explicit provision",
        }
    if action == "build":
        if output is None:
            raise ValueError("build requires --output")
        for record in extensions(context):
            atomic(output / (record["id"] + ".xpi"), acquire_extension(record))
        atomic(output / "search.json.mozlz4", encode_search(desired_search(profile)))
        return {"output": str(output)}
    if action == "check":
        parser = ini_data(firefox_paths(context)[0] / "profiles.ini")
        if not any(
            parser.get(s, "Path", fallback="") == firefox_paths(context)[2]
            and parser.get(s, "Default", fallback="") == "1"
            for s in parser.sections()
            if s.startswith("Profile")
        ):
            raise ValueError("Firefox default profile has not been provisioned")
        search = profile / "search.json.mozlz4"
        if not search.is_file() or not search_matches(profile, decode_search(search.read_bytes())):
            raise ValueError("Firefox managed search settings differ")
        for record in extensions(context):
            target = profile / "extensions" / (record["id"] + ".xpi")
            if (
                not target.is_file()
                or hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]
            ):
                raise ValueError("Firefox extension differs: " + record["id"])
        return {"profile": str(profile), "verified": True}
    if action not in {"provision", "retire"}:
        raise ValueError("unsupported Firefox operation")
    if action == "provision" and not enabled(context, "apps.firefox.enable"):
        raise ValueError("Firefox profile is disabled")
    if action == "retire" and not extension_state(context)[0].exists():
        return {"profile": str(profile), "action": action, "owned_extensions": 0}
    with closed_profile(profile):
        if action == "provision":
            search = profile / "search.json.mozlz4"
            if search.is_symlink():
                raise ValueError(
                    "Firefox search symlink requires the explicit legacy ownership handoff"
                )
            previous = decode_search(search.read_bytes()) if search.exists() else None
            matches = previous is not None and search_matches(profile, previous)
            desired = None if matches else desired_search(profile, previous)
            # Validate every profile/schema and acquire all payloads before
            # replacing any browser-owned configuration or extension leaf.
            reconcile_profile(context, dry_run=True)
            prepared = prepare_extensions(context, profile)
            reconcile_profile(context)
            reconcile_extensions(context, profile, prepared=prepared)
            if not matches:
                atomic(search, encode_search(desired))
        else:
            reconcile_extensions(context, profile, retire=True)
    return {"profile": str(profile), "action": action}


def cli_result(action, context, identifier, details, output):
    """Emit verifiable regular-file evidence for the engine's recipe receipt."""
    candidates = []
    if action in {"provision", "check"}:
        if identifier == "bat-nord-cache":
            candidates = [Path(context["paths"]["cache"]).parent / "bat/themes.bin"]
        elif identifier == "btop-nord-settings":
            candidates = [Path(context["paths"]["state"]) / "btop-theme.json"]
        else:
            _, profile, _ = firefox_paths(context)
            candidates = [extension_state(context)[0]]
            candidates.extend(
                profile / "extensions" / (record["id"] + ".xpi") for record in extensions(context)
            )
    elif action == "build" and output is not None:
        candidates = [
            path for path in output.rglob("*") if path.is_file() and not path.is_symlink()
        ]
    artifacts = []
    for path in candidates:
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ValueError(
                f"configuration recipe did not produce a regular receipt artifact: {path}"
            )
        artifacts.append(
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    statuses = {
        "plan": "planned",
        "build": "built",
        "provision": "provisioned",
        "check": "verified",
        "retire": "retired",
    }
    return {
        "id": identifier,
        "status": statuses[action],
        "artifacts": artifacts,
        "executables": {},
        "details": details,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "build", "provision", "check", "retire"])
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--id", choices=["bat-nord-cache", "firefox-profile", "btop-nord-settings"], required=True
    )
    args = parser.parse_args()
    try:
        context = json.loads(args.context.read_text())
        result = operate(args.action, context, args.id, args.output)
        result = cli_result(args.action, context, args.id, result, args.output)
    except (
        ValueError,
        OSError,
        KeyError,
        configparser.Error,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
    ) as error:
        parser.exit(1, f"configuration: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
