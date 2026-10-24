#!/usr/bin/env python3
"""Hash-locked Helm, OpenStack and tenv assemblies; no provider operations.

CLI: ACTION --context JSON [--output DIRECTORY] --id PACKAGE_ID.
plan reports immutable sources and destinations; build acquires/verifies in output
(or context cache) and generates local completions; provision installs that stage;
check validates recorded content/links without executing tools; retire removes only
unchanged recorded outputs. Build/provision are the only network-capable actions.
The engine supplies packages/artifacts and trusted absolute executables. Committed
metadata is the standalone fallback, never a runtime download/version fallback.
OpenStack uses only locked binary wheels, the scoped python312 package's trusted
python3.12 executable, and an isolated venv; global Python is independent. No
package source build or third-party install hooks run. requirements.lock and the
wheel lock must change together with their manifest digests on reviewed updates.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid

BASE = Path(__file__).resolve().parents[3]
IDS = {"helm", "openstackclient-full", "openstack-rust", "tenv"}
HELM_PLUGINS = ("diff", "secrets", "secrets-getter", "secrets-post-renderer")
TENV_COMMANDS = ("tenv", "tofu", "terragrunt", "terramate", "tf", "atmos")
HELM_HELPERS = frozenset(
    (
        "bash sh sops age curl kubectl gpg gpgconf jq yq sed grep awk find xargs "
        "base64 basename cat chmod cp cut date dirname env head ln mkdir mktemp mv "
        "readlink rm sort tail touch tr uname wc"
    ).split()
)


class ToolError(RuntimeError):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def exists(path):
    return Path(path).exists() or Path(path).is_symlink()


def native_path(value):
    path = Path(value)
    if not path.is_absolute() or "/nix/" in str(path) or "/nix/" in str(path.resolve()):
        raise ToolError("expected absolute native executable: " + str(path))
    return path


def executable(context, *names):
    for name in names:
        value = context.get("executables", {}).get(name)
        if value:
            path = native_path(value)
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ToolError("missing executable: " + str(path))
            return path
    raise ToolError("missing trusted executable: " + "/".join(names))


def metadata(context, package_id):
    declared = json.loads((BASE / "native/manifests/tools.json").read_text())
    raw = context.get("packages", [])
    packages = list(raw.values()) if isinstance(raw, dict) else raw
    package = next((p for p in packages if p.get("id") == package_id), None)
    if package is None:
        package = next(p for p in declared["packages"] if p["id"] == package_id)
    artifacts = dict(declared["artifacts"])
    artifacts.update(context.get("artifacts", {}))
    if context["platform"] not in package["platforms"]:
        raise ToolError("unsupported platform")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", package["version"]):
        raise ToolError("invalid runtime version")
    upstream_version = package["version"].split("-assembly.", 1)[0]
    primary_sources = {
        "helm": ("helm",),
        "openstack-rust": ("openstack-cli", "openstack-tui"),
        "tenv": ("tenv",),
        "openstackclient-full": ("openstack-python",),
    }
    if any(artifacts[name]["version"] != upstream_version for name in primary_sources[package_id]):
        raise ToolError("upstream and assembly versions must update coherently")
    if (
        package_id == "helm"
        and len(
            {
                artifacts[name]["version"]
                for name in ("helm-secrets", "helm-secrets-getter", "helm-secrets-post-renderer")
            }
        )
        != 1
    ):
        raise ToolError("helm-secrets plugin family must update together")
    return package, artifacts


def tree_inventory(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        name = str(path.relative_to(root))
        if path.is_symlink():
            result[name] = {"link": os.readlink(path)}
        elif path.is_file():
            result[name] = {"sha256": digest(path), "mode": stat.S_IMODE(path.stat().st_mode)}
        elif path.is_dir():
            result[name] = {"directory": True, "mode": stat.S_IMODE(path.stat().st_mode)}
        else:
            raise ToolError("unexpected special file: " + str(path))
    return result


def fetch(source, cache):
    sha = source.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ToolError("missing concrete SHA256")
    url = source["url"]
    if urllib.parse.urlparse(url).scheme != "https":
        raise ToolError("only HTTPS artifact sources are permitted")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / sha
    if exists(target):
        if target.is_symlink() or digest(target) != sha:
            raise ToolError("corrupt cached artifact: " + sha)
        return target
    temp = cache / (sha + "." + uuid.uuid4().hex + ".part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "dotfiles-native-tools/1"})
        with urllib.request.urlopen(request, timeout=120) as incoming, temp.open("xb") as outgoing:
            if urllib.parse.urlparse(incoming.url).scheme != "https":
                raise ToolError("insecure artifact redirect")
            shutil.copyfileobj(incoming, outgoing)
        if digest(temp) != sha:
            raise ToolError("artifact checksum mismatch: " + url)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def unpack(archive, destination):
    """Release archives may contain regular files/directories, never links/devices."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as source:
        seen = set()
        for member in source:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or member.name in seen:
                raise ToolError("unsafe/duplicate archive path: " + member.name)
            seen.add(member.name)
            path = destination.joinpath(*name.parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, path.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise ToolError("unsupported archive member: " + member.name)


def unique_file(root, name):
    paths = [p for p in Path(root).rglob(name) if p.is_file() and not p.is_symlink()]
    if len(paths) != 1:
        raise ToolError("expected exactly one artifact file: " + name)
    return paths[0]


def copy_binary(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(0o755)


def run(command, context, **kwargs):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(kwargs.pop("home", context["home"])),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    if "stdout" not in kwargs and not kwargs.get("capture_output"):
        kwargs["stdout"] = sys.stderr
    try:
        return subprocess.run([str(c) for c in command], env=env, check=True, **kwargs)
    except subprocess.CalledProcessError as error:
        raise ToolError(
            "local assembly command failed (exit %s): %s" % (error.returncode, command[0])
        ) from error


def shell_launcher(path, shell, command, environment=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "#!" + str(shell) + "\nset -eu\n"
    for key, value in (environment or {}).items():
        text += "export " + key + "=" + shlex.quote(str(value)) + "\n"
    text += "exec " + " ".join(shlex.quote(str(c)) for c in command) + ' "$@"\n'
    path.write_text(text)
    path.chmod(0o755)


def helper_environment(context, stage, final):
    helpers = stage / "helpers"
    helpers.mkdir()
    for name, value in context.get("executables", {}).items():
        if name in HELM_HELPERS:
            target = native_path(value)
            if target.is_file() and os.access(target, os.X_OK):
                (helpers / name).symlink_to(target)
    (helpers / "sh").unlink(missing_ok=True)
    (helpers / "sh").symlink_to("/bin/sh")
    env = {
        "PATH": str(final / "helpers") + ":/usr/bin:/bin",
        "HELM_PLUGINS": str(final / "plugins"),
        "HELM_SECRETS_HELM_PATH": str(final / "libexec/helm"),
        "HELM_SECRETS_VALS_PATH": str(final / "libexec/vals"),
    }
    for name in ("sops", "age", "curl", "kubectl"):
        env["HELM_SECRETS_" + name.upper() + "_PATH"] = str(executable(context, name))
    for name in ("gpg", "gpgconf", "jq", "yq", "bash"):
        executable(context, name)
    return env


def helm_assembly(context, stage, final, sources):
    copy_binary(unique_file(sources["helm"], "helm"), stage / "libexec/helm")
    copy_binary(unique_file(sources["helm-vals"], "vals"), stage / "libexec/vals")
    for name, artifact in zip(
        HELM_PLUGINS,
        ("helm-diff", "helm-secrets", "helm-secrets-getter", "helm-secrets-post-renderer"),
    ):
        plugin = unique_file(sources[artifact], "plugin.yaml").parent
        destination = stage / "plugins" / name
        shutil.copytree(plugin, destination)
        if name == "diff":
            manifest = destination / "plugin.yaml"
            lines = manifest.read_text().splitlines()
            # Disable upstream acquisition hooks; every binary is already hash locked.
            start = next(
                (
                    n
                    for n, line in enumerate(lines)
                    if line.startswith(("platformHooks:", "hooks:"))
                ),
                len(lines),
            )
            manifest.write_text("\n".join(lines[:start]) + "\n")
            for installer in ("install-binary.sh", "install-binary.ps1"):
                (destination / installer).unlink(missing_ok=True)
    environment = helper_environment(context, stage, final)
    shell_launcher(
        stage / "bin/helm", executable(context, "bash"), [final / "libexec/helm"], environment
    )
    completion(stage / "libexec/helm", stage, "helm", context, ["completion"])


def completion(binary, stage, name, context, arguments):
    formats = {
        "bash": stage / "share/bash-completion/completions" / name,
        "zsh": stage / "share/zsh/site-functions" / ("_" + name),
        "fish": stage / "share/fish/vendor_completions.d" / (name + ".fish"),
    }
    with tempfile.TemporaryDirectory(prefix="completion-", dir=stage) as home:
        for shell, path in formats.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w") as out:
                run([binary, *arguments, shell], context, stdout=out, home=home)
            if not path.stat().st_size:
                raise ToolError("empty local completion: " + name)


def python_lock(context, package):
    root = Path(package.get("_origin", BASE))
    spec = package["python_lock"]
    lock_path = root / spec["path"]
    requirements = root / spec["requirements"]
    if digest(lock_path) != spec["sha256"] or digest(requirements) != spec["requirements_sha256"]:
        raise ToolError("Python lock/requirements digest mismatch; update them coherently")
    lock = json.loads(lock_path.read_text())
    if lock.get("python") != "3.12" or not lock.get("distributions"):
        raise ToolError("unsupported Python lock")
    expected = []
    for name, record in sorted(lock["distributions"].items()):
        hashes = sorted({item["sha256"] for item in record["platforms"].values()})
        expected.append(
            name
            + "=="
            + record["version"]
            + " "
            + " ".join("--hash=sha256:" + value for value in hashes)
        )
    if requirements.read_text() != "\n".join(expected) + "\n":
        raise ToolError("Python requirements do not match complete wheel lock")
    normalized_roots = {re.sub(r"[-_.]+", "-", name).lower() for name in lock["roots"]}
    if not normalized_roots.issubset(lock["distributions"]):
        raise ToolError("missing OpenStack plugin in lock")
    client_version = lock["distributions"]["python-openstackclient"]["version"]
    if package["version"].split("-assembly.", 1)[0] != client_version:
        raise ToolError("Python client version and assembly lock differ")
    return lock, requirements


def python_assembly(context, package, stage, final, cache):
    lock, requirements = python_lock(context, package)
    python = executable(context, "python3.12")
    observed = run(
        [python, "-I", "-B", "-c", "import sys;print('%s.%s'%sys.version_info[:2])"],
        context,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if observed != lock["python"]:
        raise ToolError("OpenStack lock requires native Python " + lock["python"])
    wheelhouse = stage / "wheelhouse"
    wheelhouse.mkdir()
    for record in lock["distributions"].values():
        source = record["platforms"][context["platform"]]
        name = source["filename"]
        if Path(name).name != name or not name.endswith(".whl"):
            raise ToolError("only locked binary wheels permitted")
        shutil.copyfile(fetch(source, cache), wheelhouse / name)
    venv = stage / "venv"
    with tempfile.TemporaryDirectory(prefix="python-home-", dir=stage) as home:
        run([python, "-I", "-B", "-m", "venv", "--copies", venv], context, home=home)
        vpython = venv / "bin/python"
        run(
            [
                vpython,
                "-I",
                "-B",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                wheelhouse,
                "--only-binary=:all:",
                "--require-hashes",
                "--no-deps",
                "--no-compile",
                "-r",
                requirements,
            ],
            context,
            home=home,
        )
        run([vpython, "-I", "-B", "-m", "pip", "check"], context, home=home, stdout=subprocess.PIPE)
        comp = stage / "share/bash-completion/completions/openstack"
        comp.parent.mkdir(parents=True)
        with comp.open("w") as out:
            run(
                [vpython, "-I", "-B", venv / "bin/openstack", "complete"],
                context,
                home=home,
                stdout=out,
            )
    shutil.rmtree(wheelhouse)
    # Console script shebangs must name the installed prefix, not the build stage.
    for path in (venv / "bin").iterdir():
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if data.startswith(b"#!"):
                path.write_bytes(data.replace(str(venv).encode(), str(final / "venv").encode()))
    for name in package["commands"]:
        script = venv / "bin" / name
        if not script.is_file():
            raise ToolError("locked environment lacks command: " + name)
        shell_launcher(
            stage / "bin" / name,
            "/bin/sh",
            [final / "venv/bin/python", "-I", "-B", final / "venv/bin" / name],
        )
    # Activation is never needed; remove stage-bearing convenience scripts.
    for path in (venv / "bin").glob("activate*"):
        path.unlink()
    (venv / "bin/Activate.ps1").unlink(missing_ok=True)
    cfg = venv / "pyvenv.cfg"
    cfg.write_text(
        "\n".join(line for line in cfg.read_text().splitlines() if not line.startswith("command ="))
        + "\n"
    )
    shutil.copyfile(requirements, stage / "requirements.lock")


def assemble(context, package, artifacts, stage, final, cache):
    with tempfile.TemporaryDirectory(prefix="sources-", dir=stage.parent) as sources_dir:
        sources = {}
        for name in package.get("sources", []):
            source = artifacts[name]["platforms"][context["platform"]]
            destination = Path(sources_dir) / name
            unpack(fetch(source, cache), destination)
            sources[name] = destination
        (stage / "bin").mkdir()
        if package["id"] == "helm":
            helm_assembly(context, stage, final, sources)
        elif package["id"] == "openstackclient-full":
            if (
                artifacts["openstack-python"]["version"]
                != package["version"].split("-assembly.", 1)[0]
            ):
                raise ToolError("Python upstream pin and assembly lock differ")
            python_assembly(context, package, stage, final, cache)
        elif package["id"] == "openstack-rust":
            for artifact, name in (("openstack-cli", "osc"), ("openstack-tui", "ostui")):
                copy_binary(unique_file(sources[artifact], name), stage / "bin" / name)
                shutil.copytree(
                    unique_file(sources[artifact], name).parent, stage / "share" / artifact
                )
        elif package["id"] == "tenv":
            for name in TENV_COMMANDS:
                copy_binary(unique_file(sources["tenv"], name), stage / "bin" / name)
            # terraform is intentionally never copied: mise owns Terraform 1.5.7.
            completion(stage / "bin/tenv", stage, "tenv", context, ["completion"])
            for name in ("LICENSE", "README.md", "CHANGELOG.md"):
                source = unique_file(sources["tenv"], name)
                target = stage / "share/doc/tenv" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)


def layout(context, package):
    paths = context["paths"]
    if package["id"] not in IDS or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._+-]*", package["version"]
    ):
        raise ToolError("unsafe runtime identity")
    final = Path(paths["runtime"]) / package["id"] / package["version"]
    state = Path(paths["state"]) / "tools"
    return final, state, state / (package["id"] + ".json")


def fingerprint(context, package, artifacts):
    data = {
        "package": {k: v for k, v in package.items() if not k.startswith("_")},
        "sources": {
            n: {k: v for k, v in artifacts[n].items() if not k.startswith("_")}
            for n in package.get("sources", [])
        },
        "platform": context["platform"],
        "runtime": context["paths"]["runtime"],
        "recipe": digest(__file__),
    }
    needed = (
        HELM_HELPERS
        if package["id"] == "helm"
        else {"python3.12"}
        if package["id"] == "openstackclient-full"
        else set()
    )
    data["executables"] = {k: v for k, v in context.get("executables", {}).items() if k in needed}
    if package["id"] == "openstackclient-full":
        data["python_upstream"] = {
            k: v for k, v in artifacts["openstack-python"].items() if not k.startswith("_")
        }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def plan(context, package, artifacts):
    final, _, _ = layout(context, package)
    return {
        "id": package["id"],
        "version": package["version"],
        "runtime": str(final),
        "commands": package["commands"],
        "sources": {n: artifacts[n] for n in package.get("sources", [])},
        "python_lock": package.get("python_lock"),
        "fingerprint": fingerprint(context, package, artifacts),
    }


def build(context, package, artifacts, output):
    final, _, _ = layout(context, package)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / package["id"]
    identity = fingerprint(context, package, artifacts)
    if exists(target):
        marker = target / "stage.json"
        if target.is_symlink() or not marker.is_file():
            raise ToolError("foreign build destination")
        record = json.loads(marker.read_text())
        if record["fingerprint"] != identity or record["inventory"] != tree_inventory(
            target / "root"
        ):
            raise ToolError("stale or modified build stage; choose a new --output")
        return target
    with tempfile.TemporaryDirectory(prefix="." + package["id"] + "-", dir=output) as tmp:
        temp = Path(tmp)
        root = temp / "root"
        root.mkdir()
        assemble(
            context,
            package,
            artifacts,
            root,
            final,
            Path(context["paths"]["cache"]) / "tools/downloads",
        )
        atomic_json(
            temp / "stage.json",
            {
                "id": package["id"],
                "version": package["version"],
                "fingerprint": identity,
                "inventory": tree_inventory(root),
            },
        )
        os.replace(temp, target)
    return target


def verify_record(context, package, record, check_links=True):
    if not record.get("commands") or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", name) for name in record["commands"]
    ):
        raise ToolError("unsafe command ownership record")
    final, _, _ = layout(context, dict(package, version=record["version"]))
    if (
        record.get("owner") != "dotfiles-native-tools"
        or record.get("id") != package["id"]
        or record.get("runtime") != str(final)
    ):
        raise ToolError("invalid ownership record")
    if final.is_symlink() or not final.is_dir() or tree_inventory(final) != record["inventory"]:
        raise ToolError("modified or missing owned runtime")
    expected_links = {
        str(Path(context["paths"]["bin"]) / name): str(final / "bin" / name)
        for name in record["commands"]
    }
    if record["links"] != expected_links:
        raise ToolError("ownership links differ from managed destinations")
    if check_links:
        for name, target in record["links"].items():
            path = Path(name)
            if not path.is_symlink() or os.readlink(path) != target:
                raise ToolError("foreign or missing launcher: " + name)
    return final


@contextlib.contextmanager
def operation_lock(state):
    state.mkdir(parents=True, exist_ok=True)
    with (state / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def recover_install(context, package):
    """Roll back an interrupted promotion only if every touched leaf is owned."""
    _, state, record_path = layout(context, package)
    journal = state / (package["id"] + ".journal.json")
    if not journal.exists():
        return
    transaction = json.loads(journal.read_text())
    previous, candidate = transaction["previous"], transaction["candidate"]
    final = Path(candidate["runtime"])
    expected_final, _, _ = layout(context, dict(package, version=candidate["version"]))
    if final != expected_final or candidate.get("owner") != "dotfiles-native-tools":
        raise ToolError("invalid interrupted transaction")
    expected_links = {
        str(Path(context["paths"]["bin"]) / name): str(final / "bin" / name)
        for name in package["commands"]
    }
    if candidate["id"] != package["id"] or candidate["links"] != expected_links:
        raise ToolError("invalid interrupted command ownership")
    if record_path.exists() and json.loads(record_path.read_text()) == candidate:
        verify_record(context, package, candidate)
        journal.unlink()
        return
    if previous:
        verify_record(context, package, previous, check_links=False)
    if exists(final) and (final.is_symlink() or tree_inventory(final) != candidate["inventory"]):
        raise ToolError("interrupted runtime changed; refusing recovery")
    old_links = previous["links"] if previous else {}
    for name in candidate["links"].keys() | old_links.keys():
        path = Path(name)
        targets = {value for value in (candidate["links"].get(name), old_links.get(name)) if value}
        if exists(path) and (not path.is_symlink() or os.readlink(path) not in targets):
            raise ToolError("foreign replacement blocks transaction recovery: " + name)
    for name in candidate["links"].keys() | old_links.keys():
        path = Path(name)
        if name in old_links:
            temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
            temp.symlink_to(old_links[name])
            os.replace(temp, path)
        elif exists(path):
            path.unlink()
    if final.exists():
        shutil.rmtree(final)
    if previous:
        atomic_json(record_path, previous)
    else:
        record_path.unlink(missing_ok=True)
    journal.unlink()


def provision(context, package, artifacts, output):
    final, state, record_path = layout(context, package)
    stage = build(context, package, artifacts, output)
    staged = json.loads((stage / "stage.json").read_text())
    with operation_lock(state):
        recover_install(context, package)
        previous = json.loads(record_path.read_text()) if record_path.exists() else None
        if previous:
            verify_record(context, package, previous)
        links = {
            str(Path(context["paths"]["bin"]) / name): str(final / "bin" / name)
            for name in package["commands"]
        }
        for name in links:
            if exists(name) and (not previous or name not in previous["links"]):
                raise ToolError("foreign launcher collision: " + name)
        if exists(final):
            if not previous or previous["runtime"] != str(final):
                raise ToolError("foreign runtime collision: " + str(final))
            if (
                previous["fingerprint"] != staged["fingerprint"]
                or previous["inventory"] != staged["inventory"]
            ):
                raise ToolError("immutable runtime version changed; bump assembly version")
            return previous
        final.parent.mkdir(parents=True, exist_ok=True)
        pending = final.with_name("." + final.name + "." + uuid.uuid4().hex)
        shutil.copytree(stage / "root", pending, symlinks=True)
        if tree_inventory(pending) != staged["inventory"]:
            shutil.rmtree(pending)
            raise ToolError("staged runtime copy differs")
        retained = (
            []
            if not previous
            else previous.get("retained", [])
            + [{k: v for k, v in previous.items() if k != "retained"}]
        )
        record = dict(
            staged,
            owner="dotfiles-native-tools",
            runtime=str(final),
            links=links,
            commands=package["commands"],
            retained=retained,
        )
        journal = state / (package["id"] + ".journal.json")
        atomic_json(journal, {"previous": previous, "candidate": record})
        changed = []
        try:
            os.replace(pending, final)
            for name, target in links.items():
                path = Path(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
                temp.symlink_to(target)
                os.replace(temp, path)
                changed.append(name)
            if previous:
                for name in previous["links"].keys() - links.keys():
                    Path(name).unlink()
                    changed.append(name)
            atomic_json(record_path, record)
            journal.unlink()
        except BaseException:
            for name in reversed(changed):
                path = Path(name)
                if previous and name in previous["links"]:
                    temp = path.with_name("." + path.name + ".rollback")
                    temp.symlink_to(previous["links"][name])
                    os.replace(temp, path)
                elif path.is_symlink() and os.readlink(path) == links.get(name):
                    path.unlink()
            if final.is_dir() and tree_inventory(final) == staged["inventory"]:
                shutil.rmtree(final)
            if pending.exists():
                shutil.rmtree(pending)
            if previous:
                atomic_json(record_path, previous)
            else:
                record_path.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
            raise
        return record


def check(context, package, artifacts):
    _, _, path = layout(context, package)
    if path.with_name(package["id"] + ".journal.json").exists():
        raise ToolError("interrupted promotion; rerun provision for owned recovery")
    if not path.is_file():
        raise ToolError("package has no owned installation record")
    record = json.loads(path.read_text())
    verify_record(context, package, record)
    if record["fingerprint"] != fingerprint(context, package, artifacts):
        raise ToolError("installed assembly differs from selected metadata")
    return {"id": package["id"], "version": record["version"], "status": "verified"}


def retire(context, package):
    _, state, path = layout(context, package)
    with operation_lock(state):
        recover_install(context, package)
        if not path.exists():
            raise ToolError("refusing retirement without owned record")
        record = json.loads(path.read_text())
        final = verify_record(context, package, record)
        runtimes = [final]
        for retained in record.get("retained", []):
            runtimes.append(verify_record(context, package, retained, check_links=False))
        removed = []
        try:
            for name in record["links"]:
                Path(name).unlink()
                removed.append(name)
        except BaseException:
            for name in removed:
                Path(name).symlink_to(record["links"][name])
            raise
        for runtime in runtimes:
            shutil.rmtree(runtime)
        path.unlink()
    return {"id": package["id"], "status": "retired"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "build", "provision", "check", "retire"))
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--id", choices=sorted(IDS), required=True)
    args = parser.parse_args(argv)
    try:
        context = json.loads(args.context.read_text())
        package, artifacts = metadata(context, args.id)
        output = args.output or Path(context["paths"]["cache"]) / "tools/stages" / fingerprint(
            context, package, artifacts
        )
        if args.action == "plan":
            result = plan(context, package, artifacts)
        elif args.action == "build":
            stage = build(context, package, artifacts, output)
            result = {
                "stage": str(stage),
                "stage_record": str(stage / "stage.json"),
                "stage_sha256": digest(stage / "stage.json"),
            }
        elif args.action == "provision":
            result = provision(context, package, artifacts, output)
        elif args.action == "check":
            result = check(context, package, artifacts)
        else:
            result = retire(context, package)
        final, _, record_path = layout(context, package)
        statuses = {
            "plan": "planned",
            "build": "built",
            "provision": "provisioned",
            "check": "verified",
            "retire": "retired",
        }
        result.update(
            id=package["id"],
            version=package["version"],
            action=args.action,
            status=statuses[args.action],
            runtime=str(final),
            record=str(record_path),
            executables={}
            if args.action == "retire"
            else {name: str(final / "bin" / name) for name in package["commands"]},
        )
        result["executable_exists"] = {
            name: Path(path).is_file() for name, path in result["executables"].items()
        }
        artifact_paths = []
        if args.action == "build":
            artifact_paths = [stage / "stage.json"] + [
                stage / "root/bin" / name for name in package["commands"]
            ]
        elif args.action in ("provision", "check"):
            artifact_paths = [record_path] + [final / "bin" / name for name in package["commands"]]
        if args.action in ("build", "provision", "check"):
            root = stage / "root" if args.action == "build" else final
            if package["id"] == "helm":
                artifact_paths.extend(root / "libexec" / name for name in ("helm", "vals"))
            elif package["id"] == "openstackclient-full":
                artifact_paths.append((root / "venv/bin/python").resolve())
        result["artifacts"] = []
        for path in artifact_paths:
            if path.is_symlink() or not path.is_file():
                raise ToolError("result artifact is not a regular file: " + str(path))
            result["artifacts"].append({"path": str(path.absolute()), "sha256": digest(path)})
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ToolError, OSError, ValueError, KeyError) as error:
        print("native tools: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
