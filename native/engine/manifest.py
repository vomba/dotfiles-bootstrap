"""Manifest schema v1 and origin-preserving public/private composition.

Feature selectors are dotfiles-relative boolean paths; a selector list means AND.
An explicit {\"any\": [selectors]} means OR. Dependencies explicitly required by an
enabled package are selected independently of their own feature selector.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
import platform as host_platform
import re
import sys
import urllib.parse

from safety import Error, digest, inside, load, native, no_symlink_parents, relative

PLATFORMS = ("linux-x64", "macos-arm64")
PROVIDERS = ("apt", "brew", "cask", "artifact", "mise", "recipe")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+@/-]*$")


def merge(left, right):
    result = copy.deepcopy(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def platform():
    pair = (host_platform.system(), host_platform.machine())
    if pair == ("Linux", "x86_64"):
        return "linux-x64"
    if pair == ("Darwin", "arm64"):
        return "macos-arm64"
    raise Error(f"unsupported platform: {pair}; require Ubuntu 24.04 x86_64 or macOS arm64")


def feature(options, selector):
    if not isinstance(selector, str) or not selector or selector.startswith("dotfiles."):
        raise Error(f"invalid dotfiles-relative feature selector: {selector!r}")
    value = options
    for part in selector.split("."):
        if not isinstance(value, dict) or part not in value:
            raise Error(f"unknown feature selector: {selector}")
        value = value[part]
    if not isinstance(value, bool):
        raise Error(f"feature selector is not boolean: {selector}")
    return value


def enabled(options, selector):
    if selector is None:
        return True
    if isinstance(selector, dict):
        if set(selector) != {"any"} or not isinstance(selector["any"], list) or not selector["any"]:
            raise Error("feature expression must be a nonempty any list")
        return any([feature(options, item) for item in selector["any"]])
    if isinstance(selector, list):
        if not selector:
            raise Error("empty AND feature selector")
        return all([feature(options, item) for item in selector])
    return feature(options, selector)


def set_feature(options, name, value):
    # Only declared boolean leaves can be changed; typos never create options.
    feature(options, name)
    parts = name.split(".")
    target = options
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def validate(fragment, origin):
    if not isinstance(fragment, dict) or fragment.get("schema_version") != 1:
        raise Error(f"unsupported manifest schema: {origin}")
    for field in ("defaults", "profiles", "tasks"):
        if field in fragment and not isinstance(fragment[field], dict):
            raise Error(f"{field} must be an object: {origin}")
    for key in fragment.get("profiles", {}):
        if key not in PLATFORMS:
            raise Error(f"unknown profile platform: {key}")
    for path in fragment.get("overrides", []):
        relative(path)
    for section in ("packages", "services"):
        records = fragment.get(section, [])
        if not isinstance(records, list):
            raise Error(f"{section} must be a list: {origin}")
        for record in records:
            if (
                not isinstance(record, dict)
                or not IDENTIFIER.fullmatch(record.get("id", ""))
                or "/" in record["id"]
            ):
                raise Error(f"invalid {section} record id: {record!r}")
            platforms = record.get("platforms", list(PLATFORMS))
            if (
                not isinstance(platforms, list)
                or not platforms
                or any(p not in PLATFORMS for p in platforms)
            ):
                raise Error(f"invalid platforms for {record['id']}")
            if "when" in record and not isinstance(record["when"], (str, list, dict)):
                raise Error(f"invalid feature selector: {record['id']}")
            if section == "packages":
                provider = record.get("provider")
                if provider not in PROVIDERS:
                    raise Error(f"unknown package provider: {provider}")
                for requirement in record.get("requires", []):
                    if not isinstance(requirement, str):
                        raise Error("requires must contain package ids")
                requirements = record.get("requires_services", [])
                if not isinstance(requirements, list) or any(
                    not isinstance(value, str) or not IDENTIFIER.fullmatch(value) or "/" in value
                    for value in requirements
                ):
                    raise Error("requires_services must contain explicit service ids")
                inactive = record.get("mutation_requires_inactive_services", [])
                if (
                    not isinstance(inactive, list)
                    or any(
                        not isinstance(value, str)
                        or not IDENTIFIER.fullmatch(value)
                        or "/" in value
                        for value in inactive
                    )
                    or len(set(inactive)) != len(inactive)
                ):
                    raise Error(
                        "mutation_requires_inactive_services must contain distinct explicit service ids"
                    )
                if inactive and (provider != "apt" or platforms != ["linux-x64"]):
                    raise Error(
                        "inactive-system-unit mutation guards require a Linux-only APT package"
                    )
                if not isinstance(record.get("retain_shared_runtime", False), bool):
                    raise Error("retain_shared_runtime must be boolean")
                if record.get("retain_shared_runtime") and provider not in ("apt", "brew", "cask"):
                    raise Error(
                        "shared runtime retention supports native APT/Homebrew backends only"
                    )
                check = record.get("safety_check")
                if check is not None:
                    if (
                        provider != "cask"
                        or not record.get("retain_shared_runtime")
                        or not isinstance(check, dict)
                        or set(check) != {"path", "action"}
                        or check["action"] != "update-check"
                    ):
                        raise Error(
                            "safety_check is a retained cask's unprivileged repository update-check"
                        )
                    inside(origin, check["path"], existing=True)
                if provider in ("apt", "brew", "cask"):
                    if not IDENTIFIER.fullmatch(record.get("name", "")) or record[
                        "name"
                    ].startswith("-"):
                        raise Error(f"invalid native package name: {record['id']}")
                elif provider == "recipe":
                    recipe = record.get("path", record.get("recipe"))
                    inside(origin, recipe, existing=True)
                    if not isinstance(record.get("args", []), list) or any(
                        not isinstance(a, str) for a in record.get("args", [])
                    ):
                        raise Error("recipe args must be strings")
                    if any(
                        argument.split("=", 1)[0] in ("--context", "--output", "--id")
                        for argument in record.get("args", [])
                    ):
                        raise Error("recipe args cannot override the engine context/output/id")
                    actions = record.get("privileged_actions", [])
                    if not isinstance(actions, list) or any(
                        action not in ("provision", "retire", "recover") for action in actions
                    ):
                        raise Error(
                            "only explicit install/retire/recover recipe actions may be privileged"
                        )
                    if not isinstance(record.get("build_before_provision", False), bool):
                        raise Error("build_before_provision must be boolean")
                elif provider == "mise":
                    if not isinstance(record.get("tool"), str) or not isinstance(
                        record.get("version"), str
                    ):
                        raise Error(f"mise tool and exact version required: {record['id']}")
                    if record["version"] in ("latest", "stable", "lts", "nightly"):
                        raise Error(
                            "mise versions must be selected by committed lock, not floating"
                        )
                elif provider == "artifact":
                    if not isinstance(record.get("version"), str) or not record["version"]:
                        raise Error(f"artifact version required: {record['id']}")
                    relative(record["version"])
                    variants = record.get(
                        "sources", record.get("artifacts", record.get("platform_artifacts", {}))
                    )
                    if not variants and "url" in record:
                        variants = {platform: record for platform in platforms}
                    if not isinstance(variants, dict):
                        raise Error(f"artifact requires platform source records: {record['id']}")
                    for platform in platforms:
                        source = variants.get(platform)
                        if not isinstance(source, dict):
                            raise Error(f"artifact source missing for {platform}: {record['id']}")
                        url = urllib.parse.urlsplit(source.get("url", ""))
                        if url.scheme != "https" or not url.netloc or url.username or url.password:
                            raise Error("artifact URL must be credential-free HTTPS")
                        if not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", "")):
                            raise Error("artifact requires an exact SHA-256")
                        for field in ("bin", "app"):
                            value = source.get(field)
                            if isinstance(value, str):
                                relative(value)
                            elif isinstance(value, dict):
                                for command_name, path in value.items():
                                    relative(command_name)
                                    relative(path)
                            elif value is not None:
                                raise Error(f"invalid artifact {field}")
                if record.get("policy", "latest-stable") not in (
                    "latest-stable",
                    "distro-security",
                    "project",
                    "license-pinned",
                ):
                    raise Error(f"unknown package policy: {record['id']}")
                if record.get("policy", "latest-stable") != "latest-stable" and not record.get(
                    "reason"
                ):
                    raise Error(f"nonlatest policy requires reason: {record['id']}")
            else:
                if record.get("manager") not in ("systemd-user", "systemd-system", "launchd"):
                    raise Error(f"unknown service manager: {record['id']}")
                if record["manager"] == "systemd-system":
                    if (
                        record.get("existing_unit") is not True
                        or platforms != ["linux-x64"]
                        or not record.get("unit_package")
                        or not record.get("package_id")
                        or any(key in record for key in ("source", "target"))
                    ):
                        raise Error(
                            "systemd-system requires existing_unit, Linux-only platform, unit_package and package_id; APT owns its descriptor"
                        )
                    for key in ("unit_package", "package_id"):
                        if (
                            not isinstance(record[key], str)
                            or not IDENTIFIER.fullmatch(record[key])
                            or "/" in record[key]
                        ):
                            raise Error(f"invalid existing unit {key}")
                elif record.get("existing_unit") or not (
                    record.get("target") or record.get("source")
                ):
                    raise Error("authored user services require a descriptor, not existing_unit")
                for key in ("source", "target"):
                    if key in record:
                        relative(record[key])
                if not record.get("name") or not IDENTIFIER.fullmatch(record["name"]):
                    raise Error(f"invalid service name: {record['id']}")
                conflicts = record.get("conflicts", [])
                if not isinstance(conflicts, list) or any(
                    not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or "/" in name
                    for name in conflicts
                ):
                    raise Error("service conflicts must be explicit same-manager unit/label names")
    return fragment


def compose(base, root, home, selected_platform=None, flags=(), context_file=None):
    if Path(home).is_symlink():
        raise Error("home cannot be a symlink")
    base, root, home = Path(base).resolve(), Path(root).resolve(), Path(home).resolve()
    if any("/nix/" in str(path) for path in (base, root, home)):
        raise Error("source/home paths cannot depend on the Nix runtime")
    active_platform = selected_platform or platform()
    if active_platform not in PLATFORMS:
        raise Error(f"unknown platform: {active_platform}")
    if home.is_symlink() or "/nix/" in str(home):
        raise Error("unsafe home directory")
    data = {}
    fragments = []
    packages, services, artifacts, tasks, overrides = [], [], {}, {}, []
    origins = [base] if root == base else [base, root]
    for origin in origins:
        for path in sorted((origin / "native/manifests").glob("*.json")):
            fragment = validate(load(path), origin)
            fragments.append({"path": str(path), "origin": str(origin)})
            data = merge(data, fragment.get("defaults", {}))
            data = merge(data, fragment.get("profiles", {}).get(active_platform, {}))
            for section, destination in (("packages", packages), ("services", services)):
                for record in fragment.get(section, []):
                    record = copy.deepcopy(record)
                    record["_origin"] = str(origin)
                    destination.append(record)
            source_artifacts = fragment.get("artifacts", {})
            if isinstance(source_artifacts, list):
                source_artifacts = {item["id"]: item for item in source_artifacts}
            if not isinstance(source_artifacts, dict):
                raise Error("artifacts must be named records")
            for key, record in source_artifacts.items():
                if key in artifacts:
                    raise Error(f"duplicate artifact id: {key}")
                if not isinstance(record, dict):
                    raise Error(f"artifact must be a metadata object: {key}")
                if record.get("source_manifest"):
                    inside(origin, record["source_manifest"], existing=True)
                if record.get("recipe"):
                    inside(origin, record["recipe"], existing=True)
                artifacts[key] = copy.deepcopy(record)
                artifacts[key]["_origin"] = str(origin)
            for key, task in fragment.get("tasks", {}).items():
                if key in tasks and tasks[key] != task:
                    raise Error(f"duplicate task: {key}")
                tasks[key] = task
            if origin != base:
                overrides.extend(fragment.get("overrides", []))
    if context_file:
        override = load(context_file)
        if not isinstance(override, dict):
            raise Error("fixture context must be an object")
        data = merge(data, override)
    options = data.setdefault("dotfiles", {})
    for flag in flags:
        if "=" not in flag or flag.rsplit("=", 1)[1] not in ("true", "false"):
            raise Error(f"feature override must be NAME=true|false: {flag}")
        name, value = flag.rsplit("=", 1)
        set_feature(options, name, value == "true")
    active = {}
    declared_platforms = {}
    for record in packages:
        platforms = set(record.get("platforms", PLATFORMS))
        overlap = declared_platforms.get(record["id"], set()) & platforms
        if overlap:
            raise Error(f"duplicate conflicting package id: {record['id']} on {sorted(overlap)}")
        declared_platforms.setdefault(record["id"], set()).update(platforms)
        # Validate selector even on the other platform, so mistakes cannot hide.
        enabled(options, record.get("when"))
        if active_platform not in record.get("platforms", PLATFORMS):
            continue
        if record["id"] in active:
            raise Error(f"duplicate conflicting package id: {record['id']}")
        active[record["id"]] = record
    selected = set()
    visiting = set()

    def select(identifier):
        if identifier in visiting:
            raise Error(f"package dependency cycle: {identifier}")
        if identifier in selected:
            return
        if identifier not in active:
            raise Error(f"missing active-platform package dependency: {identifier}")
        visiting.add(identifier)
        for requirement in active[identifier].get("requires", []):
            select(requirement)
        visiting.remove(identifier)
        selected.add(identifier)
        ordered.append(active[identifier])

    ordered = []
    for identifier, record in active.items():
        if enabled(options, record.get("when")):
            select(identifier)
    active_services = []
    seen_services = set()
    for record in services:
        enabled(options, record.get("when"))
        if active_platform in record.get("platforms", PLATFORMS):
            if record["id"] in seen_services:
                raise Error(f"duplicate service id: {record['id']}")
            seen_services.add(record["id"])
            if enabled(options, record.get("when")):
                active_services.append(record)
    service_ids = {record["id"] for record in active_services}
    for package in ordered:
        if set(package.get("requires_services", [])) - service_ids:
            raise Error(f"missing active-platform service dependency: {package['id']}")
        for identifier in package.get("mutation_requires_inactive_services", []):
            service = next((item for item in active_services if item["id"] == identifier), None)
            if (
                service is None
                or service.get("manager") != "systemd-system"
                or not service.get("existing_unit")
                or service.get("package_id") != package["id"]
                or service.get("unit_package") != package.get("name")
                or service["_origin"] != package["_origin"]
            ):
                raise Error(
                    f"runtime mutation guard requires same-origin selected package/unit: {identifier}"
                )
    for service in active_services:
        if service["manager"] == "systemd-system":
            package = next(
                (record for record in ordered if record["id"] == service["package_id"]), None
            )
            if (
                package is None
                or package.get("provider") != "apt"
                or package.get("name") != service["unit_package"]
            ):
                raise Error(f"existing unit requires selected APT package: {service['id']}")
    paths = {
        "config": str(home / ".config"),
        "data": str(home / ".local/share/dotfiles"),
        "state": str(home / ".local/state/dotfiles"),
        "cache": str(home / ".cache/dotfiles"),
        "bin": str(home / ".local/bin"),
    }
    paths.update(data.get("paths", {}))
    for key in ("config", "data", "state", "cache", "bin"):
        path = Path(paths[key])
        if not path.is_absolute() or ".." in path.parts or not path.resolve().is_relative_to(home):
            raise Error(f"managed {key} path must be within selected home")
    paths.setdefault("runtime", str(Path(paths["data"]) / "runtime"))
    paths.setdefault("completions", str(Path(paths["data"]) / "completions"))
    paths.setdefault("omz", str(Path(paths["runtime"]) / "oh-my-zsh/current"))
    for key in ("runtime", "completions"):
        path = Path(paths[key])
        if not path.is_absolute() or ".." in path.parts or not path.resolve().is_relative_to(home):
            raise Error(f"managed {key} path must be within selected home")
    pins = load(base / "native/engine/pins.json")
    executables = {"python3": native(sys.executable)}
    for identifier in ("mise", "chezmoi"):
        pin = pins[identifier]
        executables[identifier] = str(
            Path(paths["runtime"])
            / "bootstrap"
            / identifier
            / pin["version"]
            / pin["platforms"][active_platform]["bin"]
        )
    for record in ordered:
        provider = record["provider"]
        mappings = record.get("executables", record.get("binaries", {}))
        if isinstance(mappings, list):
            mappings = {name: f"bin/{name}" for name in mappings}
        for name, value in mappings.items():
            if not Path(value).is_absolute():
                relative(value)
            if Path(value).is_absolute():
                executable = value
            elif provider == "apt":
                executable = str(Path("/usr") / value) if "/" in value else f"/usr/bin/{value}"
            elif provider in ("brew", "cask"):
                executable = str(Path("/opt/homebrew/opt") / record["name"] / value)
            elif provider == "mise":
                tool = record["tool"].split(":")[-1].split("/")[-1]
                executable = str(
                    Path(paths["data"]) / "mise/installs" / tool / record["version"] / value
                )
            else:
                runtime_id = record.get("runtime_id", record["id"])
                relative(runtime_id)
                relative(record.get("version", "current"))
                executable = str(
                    Path(paths["runtime"]) / runtime_id / record.get("version", "current") / value
                )
            native(executable, exists=False)
            executables[name] = executable
    installed_path = Path(paths["state"]) / "packages.json"
    no_symlink_parents(installed_path, home)
    if installed_path.exists():
        if installed_path.is_symlink():
            raise Error("symlink package ownership record")
        installed = load(installed_path)
        if installed.get("home") != str(home):
            raise Error("package ownership belongs to another home")
        for package in ordered:
            entry = installed.get("packages", {}).get(package["id"], {})
            selection = entry.get("selection", {})
            if (
                package["provider"] != "mise"
                or selection.get("version") != package.get("version")
                or selection.get("tool") != package.get("tool")
            ):
                continue
            observed = entry.get("observed", {})
            for name, executable in observed.get("executables", {}).items():
                native(executable)
                if name not in package.get("executables", {}) or not Path(
                    executable
                ).resolve().is_relative_to(Path(paths["data"]) / "mise/installs"):
                    raise Error(f"unsafe recorded mise executable: {name}")
                if digest(executable) != observed.get("installed_files", {}).get(name):
                    raise Error(f"recorded mise executable changed: {name}")
                executables[name] = executable
    executables.update(data.get("executables", {}))
    for path in executables.values():
        native(path, exists=False)
    data.update(
        {
            "schema_version": 1,
            "root": str(root),
            "base": str(base),
            "home": str(home),
            "platform": active_platform,
            "dotfiles": options,
            "paths": paths,
            "executables": executables,
            "packages": ordered,
            "services": active_services,
            "artifacts": artifacts,
            "tasks": tasks,
            "_fragments": fragments,
            "_overrides": sorted(set(overrides)),
        }
    )
    data.setdefault("identity", {})
    data.setdefault("selectors", {})
    data["identity"].setdefault("username", home.name)
    data["identity"].setdefault("home", str(home))
    return data
