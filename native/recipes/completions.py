#!/usr/bin/env python3
"""Build native Zsh assets explicitly; never source generators during startup.

API: plan(context), build(context, output), provision(context), check(context),
retire(context). CLI ACTION --context FILE [--output DIRECTORY] [--id PACKAGE_ID].
The output is a dedicated, collision-checked tree containing package-native/, generated/,
supplemental/, init.zsh and ownership.json. The shell owner prepends the three
fpath directories in that order and sources init.zsh AFTER its sole compinit.

Optional context.completion_providers records have id, version, assets (absolute
Zsh completion directories/files), tier (package-native/supplemental), and commands.
Otherwise installed package prefixes and native package receipts are inspected.
Explicit records are useful for isolated fixtures and exceptional tool recipes;
source paths must be installed assets, never build/research directories. Every
copied/generated asset records provider, version, source and content SHA-256.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import io
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tarfile
import urllib.request


POLICY = Path(__file__).resolve().parents[1] / "manifests/completions.json"
TIERS = ("package-native", "generated", "supplemental")
RECEIPT = "ownership.json"
SOURCES = Path(__file__).resolve().parents[1] / "completions/sources.json"


class CompletionError(RuntimeError):
    pass


def enabled(context, condition):
    if isinstance(condition, list):
        return all(enabled(context, item) for item in condition)
    value = context.get("dotfiles", {})
    for key in condition.split("."):
        if not isinstance(value, dict) or key not in value:
            raise CompletionError(f"missing feature flag: {condition}")
        value = value[key]
    if not isinstance(value, bool):
        raise CompletionError(f"feature flag is not boolean: {condition}")
    return value


def policy():
    return json.loads(POLICY.read_text())["completion_policy"]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def executable(context, command):
    path = Path(context.get("executables", {}).get(command, ""))
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise CompletionError(f"missing trusted installed executable: {command}")
    if "/nix/" in str(path) or "/nix/" in str(path.resolve()):
        raise CompletionError(f"non-native executable: {command}")
    return path.resolve()


def packages(context):
    items = context.get("packages", [])
    return list(items.values()) if isinstance(items, dict) else items


def package_for(context, command):
    matches = [
        p
        for p in packages(context)
        if p.get("id") == command
        or p.get("tool") == command
        or command in p.get("commands", [])
        or command in p.get("executables", {})
    ]
    return matches[0] if matches else {}


def clean_environment(root):
    env = {
        "LC_ALL": "C",
        "LANG": "C",
        "PATH": "/usr/bin:/bin",
        "MISE_OFFLINE": "1",
        "MISE_AUTO_INSTALL": "0",
        "MISE_NOT_FOUND_AUTO_INSTALL": "0",
        "MISE_NOT_FOUND_SYSTEM_FALLBACK": "0",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "HOMEBREW_NO_ANALYTICS": "1",
    }
    for key, relative in (
        ("HOME", "home"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_RUNTIME_DIR", "runtime"),
        ("TMPDIR", "tmp"),
    ):
        directory = root / relative
        directory.mkdir(mode=0o700)
        env[key] = str(directory)
    env["KUBECONFIG"] = str(root / "absent-kubeconfig")
    env["MISE_GLOBAL_CONFIG_FILE"] = str(root / "absent-mise.toml")
    env["MISE_SYSTEM_CONFIG_DIR"] = str(root / "absent-system")
    env["MISE_CEILING_PATHS"] = str(root)
    return env


def run_isolated(argv):
    with tempfile.TemporaryDirectory(prefix="dotfiles-completion-env-") as temporary:
        root = Path(temporary)
        result = subprocess.run(
            [str(a) for a in argv],
            cwd=root,
            env=clean_environment(root),
            capture_output=True,
            timeout=60,
            check=False,
        )
    if result.returncode:
        # Neither generated output nor stderr is echoed: generators should not
        # disclose inherited information even when an upstream tool misbehaves.
        raise CompletionError(f"completion command failed ({result.returncode}): {argv[0]}")
    return result.stdout


def version_for(context, command):
    for provider in context.get("completion_providers", []):
        if command in provider.get("commands", []) or provider["id"] == command:
            if provider.get("version"):
                return str(provider["version"])
    package = package_for(context, command)
    selected = package.get("installed_version") or package.get("version")
    if selected and selected not in ("latest", "latest-stable", "system"):
        return str(selected)
    output = run_isolated([executable(context, command), "--version"])
    text = output.decode("utf-8", errors="strict").strip()
    if not text or len(text) > 4096:
        raise CompletionError(f"cannot associate completion with installed version: {command}")
    return text


def discover_providers(context):
    explicit = context.get("completion_providers")
    if explicit is not None:
        return explicit
    providers = []
    seen = set()
    for command, raw in sorted(context.get("executables", {}).items()):
        path = Path(raw)
        if not path.is_absolute() or not path.is_file():
            continue
        if package_for(context, command).get("provider") in ("apt", "brew"):
            continue
        prefixes = (path.resolve().parent, path.resolve().parent.parent)
        directories = [
            prefix / relative
            for prefix in prefixes
            for relative in (
                "share/zsh/site-functions",
                "share/zsh/vendor-completions",
                "completions",
            )
        ]
        for directory in directories:
            # Shared native package directories are enumerated by owner below.
            if directory.is_dir() and str(directory.parent.parent) not in ("/usr", "/usr/local"):
                key = str(directory)
                if key not in seen:
                    seen.add(key)
                    owner = package_for(context, command).get("id", command)
                    providers.append(
                        {
                            "id": owner,
                            "version": version_for(context, command),
                            "assets": [key],
                            "commands": [command],
                        }
                    )
    for package in packages(context):
        provider = package.get("provider")
        name = package.get("name", package.get("id"))
        if provider == "apt":
            query = Path("/usr/bin/dpkg-query")
            if not query.is_file():
                raise CompletionError("dpkg-query unavailable for native completion discovery")
            version = run_isolated([query, "-W", "-f=${Version}", name]).decode().strip()
            listing = run_isolated([query, "-L", name]).decode().splitlines()
            assets = [
                p
                for p in listing
                if "/zsh/" in p and Path(p).is_file() and Path(p).name.startswith("_")
            ]
            if assets:
                providers.append(
                    {
                        "id": package["id"],
                        "version": version,
                        "assets": assets,
                        "tier": "supplemental" if name == "zsh-completions" else "package-native",
                    }
                )
        elif provider == "brew":
            brew = executable(context, "brew")
            prefix = Path(run_isolated([brew, "--prefix", name]).decode().strip())
            assets = [
                str(prefix / relative)
                for relative in ("share/zsh/site-functions", "share/zsh/vendor-completions")
                if (prefix / relative).is_dir()
            ]
            if assets:
                info = json.loads(run_isolated([brew, "info", "--json=v2", name]))
                installed = info["formulae"][0]["installed"]
                if len(installed) != 1:
                    raise CompletionError(f"ambiguous installed completion provider: {name}")
                providers.append(
                    {
                        "id": package["id"],
                        "version": installed[0]["version"],
                        "assets": assets,
                        "tier": "supplemental" if name == "zsh-completions" else "package-native",
                    }
                )
    # Ask trusted Zsh for compiled-in fpath, never the user's startup files.
    zsh = executable(context, "zsh")
    directories = run_isolated([zsh, "-df", "-c", "print -rl -- $fpath"]).decode().splitlines()
    wanted = {
        r["command"]
        for r in policy()["required"]
        if enabled(context, r["when"]) and r["command"] != "terraform"
    }
    supplied = set()
    for provider in providers:
        for raw in provider["assets"]:
            source = Path(raw)
            files = source.iterdir() if source.is_dir() else [source]
            supplied.update(p.name[1:] for p in files if p.name.startswith("_") and p.is_file())
    wanted -= supplied
    assets = []
    for command in sorted(wanted):
        for directory in directories:
            candidate = Path(directory) / ("_" + command)
            if candidate.is_file() and "/nix/" not in str(candidate.resolve()):
                assets.append(str(candidate))
                break
    omz = context.get("paths", {}).get("omz")
    if omz and enabled(context, "dev.mise.enable"):
        asset = Path(omz) / "plugins/terraform/_terraform"
        version = package_for(context, "oh-my-zsh").get("version")
        if not asset.is_file() or not version:
            raise CompletionError("installed pinned OMZ terraform completion unavailable")
        providers.append(
            {
                "id": "oh-my-zsh",
                "version": str(version),
                "assets": [str(asset)],
                "commands": ["terraform"],
            }
        )
    if assets:
        providers.append(
            {
                "id": "zsh",
                "version": version_for(context, "zsh"),
                "assets": assets,
                "commands": sorted(wanted),
            }
        )
    return providers


def plan(context):
    active = enabled(context, "shell.zsh.enable")
    selected = policy()
    return {
        "enabled": active,
        "destination": context["paths"]["completions"],
        "precedence": list(TIERS),
        "generators": [
            g["command"] for g in selected["generators"] if active and enabled(context, g["when"])
        ],
        "startup_generation": False,
    }


def _option(context, path, default):
    value = context.get("dotfiles", {})
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def initialization(context):
    lines = ["# Generated by explicit dotfiles completion build; source after compinit."]
    if enabled(context, "dev.kubernetes.enable"):
        crossplane = executable(context, "crossplane")
        lines += [
            "autoload -U +X bashcompinit",
            "bashcompinit",
            "complete -C " + shlex.quote(str(crossplane)) + " crossplane",
        ]
    if enabled(context, "apps.yazi.enable") and _option(
        context, "apps.yazi.enableZshIntegration", True
    ):
        wrapper = _option(context, "apps.yazi.shellWrapperName", "yy")
        lines.append("compdef " + shlex.quote(wrapper + "=yazi"))
    if enabled(context, "shell.git.enable") and _option(
        context, "shell.git.lazygitZshIntegration", True
    ):
        wrapper = _option(context, "shell.git.lazygitWrapperName", "lazygit")
        lines.append("compdef " + shlex.quote(wrapper + "=lazygit"))
    return ("\n".join(lines) + "\n").encode()


def supplemental(context, add):
    source = json.loads(SOURCES.read_text())["zsh-completions"]
    cache = Path(context["paths"]["cache"]) / "completion-sources"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / ("zsh-completions-" + source["version"] + ".tar.gz")
    if archive.exists():
        data = archive.read_bytes()
    else:
        with urllib.request.urlopen(source["url"], timeout=60) as response:
            data = response.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024 or digest(data) != source["sha256"]:
        raise CompletionError("zsh-completions source checksum mismatch")
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temporary:
            temporary.write(data)
            temporary_path = Path(temporary.name)
        temporary_path.replace(archive)
    owner = {
        "provider": "zsh-completions",
        "version": source["version"],
        "source": source["url"],
        "kind": "package",
    }
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
        count = 0
        for member in bundle.getmembers():
            if member.name == source["license"]:
                add("LICENSE.zsh-completions", bundle.extractfile(member).read(), owner)
            elif (
                member.isfile()
                and member.name.startswith(source["source_directory"] + "/")
                and Path(member.name).name.startswith("_")
            ):
                name = Path(member.name).name
                if name[1:] not in policy()["excluded_commands"]:
                    add("supplemental/" + name, bundle.extractfile(member).read(), owner)
                    count += 1
        if not count:
            raise CompletionError("zsh-completions archive has no completion assets")


def _populate(context, root):
    selected = policy()
    records = {}
    providers = discover_providers(context)
    for tier in TIERS:
        (root / tier).mkdir()

    def add(relative, data, owner):
        if not data.strip() or b"\x00" in data:
            raise CompletionError(f"empty or invalid completion: {relative}")
        previous = records.get(relative)
        record = dict(owner, sha256=digest(data))
        if previous:
            if previous != record:
                raise CompletionError(f"completion asset ownership collision: {relative}")
            return
        (root / relative).write_bytes(data)
        records[relative] = record

    for provider in providers:
        tier = provider.get("tier", "package-native")
        if tier not in ("package-native", "supplemental") or not provider.get("version"):
            raise CompletionError(f"invalid completion provider: {provider.get('id')}")
        for raw in provider.get("assets", []):
            source = Path(raw)
            if not source.is_absolute() or not source.exists() or "/nix/" in str(source.resolve()):
                raise CompletionError(f"missing or non-native completion asset: {source}")
            files = sorted(source.iterdir()) if source.is_dir() else [source]
            for asset in files:
                if not asset.name.startswith("_") or not asset.is_file():
                    continue
                if asset.suffix in (".ps1", ".fish", ".bash", ".nu", ".ts", ".elv"):
                    continue
                if asset.name[1:] in selected["excluded_commands"]:
                    continue
                if not re.fullmatch(r"_[A-Za-z0-9_.+-]+", asset.name):
                    raise CompletionError(f"invalid completion asset name: {asset.name}")
                data = asset.read_bytes()
                source_digest = digest(data)
                if asset.name == "_lazygit" and re.search(rb"_lazygit\s*\(\s*\)", data):
                    data += b'\nif [[ $funcstack[1] == _lazygit ]]; then\n  _lazygit "$@"\nfi\n'
                if asset.name == "_kubectl-oidc_login":
                    data = data.replace(b"kubelogin", b"kubectl-oidc_login")
                add(
                    f"{tier}/{asset.name}",
                    data,
                    {
                        "provider": provider["id"],
                        "version": str(provider["version"]),
                        "source": str(asset.resolve()),
                        "source_sha256": source_digest,
                        "kind": "package",
                    },
                )
    if not any(p.get("tier") == "supplemental" for p in providers):
        supplemental(context, add)
    for generator in selected["generators"]:
        command = generator["command"]
        if not enabled(context, generator["when"]):
            continue
        if f"package-native/_{command}" in records:
            continue
        binary = executable(context, command)
        data = run_isolated([binary, *generator["args"]])
        if "rename" in generator:
            old, new = generator["rename"]
            if old.encode() not in data:
                raise CompletionError(f"upstream completion rename anchor missing: {command}")
            data = data.replace(old.encode(), new.encode())
        if generator.get("autoload_dispatch"):
            data += b'\nif [[ $funcstack[1] == _lazygit ]]; then\n  _lazygit "$@"\nfi\n'
        add(
            f"generated/_{command}",
            data,
            {
                "provider": package_for(context, command).get("id", command),
                "version": version_for(context, command),
                "source": str(binary),
                "executable_sha256": digest(binary.read_bytes()),
                "command": command,
                "kind": "generated",
            },
        )
    for required in selected["required"]:
        command = required["command"]
        if (
            command == "openstack"
            and enabled(context, required["when"])
            and not any(f"{t}/_openstack" in records for t in TIERS)
        ):
            binary = executable(context, command)
            source = binary.parent.parent / "share/bash-completion/completions/openstack"
            if source.is_file():
                bash = source.read_bytes()
                registration = re.search(rb"(?m)^complete\s+.*?-F\s+([A-Za-z_][A-Za-z0-9_]*)", bash)
                if not registration:
                    raise CompletionError("OpenStack Bash completion registration missing")
                function = registration.group(1)
                bash = re.sub(rb"(?m)^complete\s+.*$", b"", bash)
                bash = bash.replace(function, b"_dotfiles_bash_openstack")
                data = (
                    b"#compdef openstack\nautoload -U +X bashcompinit\nbashcompinit\n"
                    + bash
                    + b'\n_bash_complete -F _dotfiles_bash_openstack "$@"\n'
                )
                add(
                    "generated/_openstack",
                    data,
                    {
                        "provider": package_for(context, command).get("id", command),
                        "version": version_for(context, command),
                        "source": str(source),
                        "source_sha256": digest(source.read_bytes()),
                        "command": command,
                        "kind": "bash-compat",
                    },
                )
        if enabled(context, required["when"]) and not any(
            f"{t}/_{command}" in records for t in TIERS
        ):
            raise CompletionError(f"native/supplemental completion required: {command}")
    add(
        "init.zsh",
        initialization(context),
        {
            "provider": "dotfiles-completions",
            "version": "1",
            "source": "recipe",
            "kind": "initialization",
        },
    )
    receipt = {
        "schema_version": 1,
        "owner": "dotfiles-completions",
        "assets": records,
        "precedence": list(TIERS),
        "providers": providers,
    }
    if enabled(context, "dev.kubernetes.enable"):
        binary = executable(context, "crossplane")
        receipt["protocols"] = {
            "crossplane": {
                "executable": str(binary),
                "version": version_for(context, "crossplane"),
                "sha256": digest(binary.read_bytes()),
            }
        }
    (root / RECEIPT).write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return receipt


def _validate(root):
    if root.is_symlink() or not root.is_dir() or not (root / RECEIPT).is_file():
        raise CompletionError(f"refusing unowned completion tree: {root}")
    receipt = json.loads((root / RECEIPT).read_text())
    if receipt.get("schema_version") != 1 or receipt.get("owner") != "dotfiles-completions":
        raise CompletionError(f"invalid completion ownership receipt: {root}")
    expected = set(receipt["assets"]) | {RECEIPT}
    actual = set()
    allowed_directories = set(TIERS)
    for relative in receipt["assets"]:
        allowed_directories.update(
            str(parent) for parent in Path(relative).parents if str(parent) != "."
        )
    for path in root.rglob("*"):
        if path.relative_to(root).parts[0] == "private":
            continue  # Reserved private-work owner; never inspect its content.
        if path.is_symlink():
            raise CompletionError(f"foreign symlink in completion tree: {path}")
        if path.is_dir() and path.relative_to(root).as_posix() not in allowed_directories:
            raise CompletionError(f"foreign completion directory: {path}")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != expected:
        raise CompletionError(f"foreign or missing completion assets: {root}")
    for relative, record in receipt["assets"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise CompletionError("invalid completion receipt path")
        if digest((root / relative).read_bytes()) != record["sha256"]:
            raise CompletionError(f"modified completion asset: {relative}")
    return receipt


@contextlib.contextmanager
def _lock(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (destination.parent / ("." + destination.name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def build(context, output):
    destination = Path(output)
    if not destination.is_absolute():
        destination = destination.absolute()
    with _lock(destination):
        if destination.exists() or destination.is_symlink():
            private_only = (
                not destination.is_symlink()
                and destination.is_dir()
                and {p.name for p in destination.iterdir()} == {"private"}
                and (destination / "private").is_dir()
                and not (destination / "private").is_symlink()
            )
            if not private_only:
                _validate(destination)
        with tempfile.TemporaryDirectory(
            prefix=".completions-stage-", dir=destination.parent
        ) as temporary:
            stage = Path(temporary) / "tree"
            stage.mkdir()
            if enabled(context, "shell.zsh.enable"):
                receipt = _populate(context, stage)
            else:
                receipt = {
                    "schema_version": 1,
                    "owner": "dotfiles-completions",
                    "assets": {},
                    "precedence": list(TIERS),
                    "providers": [],
                }
                (stage / RECEIPT).write_text(json.dumps(receipt, indent=2) + "\n")
            private = destination / "private"
            if private.exists() or private.is_symlink():
                if private.is_symlink() or not private.is_dir():
                    raise CompletionError("private completion namespace must be a directory")
                shutil.copytree(private, stage / "private", symlinks=True)
            backup = Path(temporary) / "previous"
            if destination.exists():
                destination.rename(backup)
            try:
                stage.rename(destination)
            except BaseException:
                if backup.exists():
                    backup.rename(destination)
                raise
    return receipt


def provision(context):
    return build(context, Path(context["paths"]["completions"]))


def check(context):
    receipt = _validate(Path(context["paths"]["completions"]))
    for relative, record in receipt["assets"].items():
        source = Path(record["source"])
        if record["kind"] == "generated":
            binary = executable(context, record["command"])
            if (
                version_for(context, record["command"]) != record["version"]
                or digest(binary.read_bytes()) != record["executable_sha256"]
            ):
                raise CompletionError(f"stale generated completion: {relative}")
        elif record["kind"] in ("package", "bash-compat") and source.is_absolute():
            if not source.is_file():
                raise CompletionError(f"missing completion provider source: {source}")
            if digest(source.read_bytes()) != record.get("source_sha256", record["sha256"]):
                raise CompletionError(f"stale package completion: {relative}")
            if (
                record["kind"] == "bash-compat"
                and version_for(context, record["command"]) != record["version"]
            ):
                raise CompletionError(f"stale Bash-compatible completion: {relative}")
    for provider in receipt.get("providers", []):
        current = next(
            (p for p in context.get("completion_providers", []) if p["id"] == provider["id"]), None
        )
        selected = (
            current.get("version")
            if current
            else package_for(context, provider["id"]).get("version")
        )
        if (
            selected
            and selected not in ("latest", "latest-stable", "system")
            and str(selected) != provider["version"]
        ):
            raise CompletionError(f"stale completion provider version: {provider['id']}")
    for command, protocol in receipt.get("protocols", {}).items():
        binary = executable(context, command)
        if (
            str(binary) != protocol["executable"]
            or version_for(context, command) != protocol["version"]
            or digest(binary.read_bytes()) != protocol["sha256"]
        ):
            raise CompletionError(f"stale completion protocol: {command}")
    return receipt


def retire(context):
    destination = Path(context["paths"]["completions"])
    with _lock(destination):
        if not destination.exists() and not destination.is_symlink():
            return {"retired": False}
        if (
            not destination.is_symlink()
            and destination.is_dir()
            and {p.name for p in destination.iterdir()} == {"private"}
            and (destination / "private").is_dir()
            and not (destination / "private").is_symlink()
        ):
            return {"retired": False}
        receipt = _validate(destination)
        for relative in receipt["assets"]:
            (destination / relative).unlink()
        (destination / RECEIPT).unlink()
        for tier in TIERS:
            directory = destination / tier
            if directory.exists():
                directory.rmdir()
        if not any(destination.iterdir()):
            destination.rmdir()
    return {"retired": True}


def report(action, result, directory):
    statuses = {
        "plan": "planned",
        "build": "built",
        "provision": "provisioned",
        "check": "verified",
        "retire": "retired",
    }
    artifacts = []
    if action in ("build", "provision", "check"):
        for relative in [RECEIPT, *sorted(result["assets"])]:
            path = (Path(directory) / relative).resolve()
            artifacts.append({"path": str(path), "sha256": digest(path.read_bytes())})
    return {
        "id": "shell-completions",
        "status": statuses[action],
        "artifacts": artifacts,
        "executables": {},
        "details": result,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "build", "provision", "check", "retire"))
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--id", default="shell-completions")
    args = parser.parse_args(argv)
    try:
        context = json.loads(args.context.read_text())
        if args.id != "shell-completions":
            raise CompletionError(f"unsupported recipe id: {args.id}")
        if args.action == "build":
            output = args.output or Path(context["paths"]["cache"]) / "completions"
            result = build(context, output)
        else:
            output = Path(context["paths"]["completions"])
            result = globals()[args.action](context)
        print(json.dumps(report(args.action, result, output), sort_keys=True))
    except (CompletionError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"completions: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
