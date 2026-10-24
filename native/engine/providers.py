"""Explicit APT, Homebrew, mise, artifact and recipe operations.

Provision is the only package installer. All successful installations are
read back before publishing ownership/provenance. No adapter starts services.

Recipe stdout is exactly one JSON object with id, status, artifacts and
executables. Status is planned/built/provisioned/verified/retired for the
corresponding action. artifacts are {path:absolute regular file,sha256:hex};
check/provision require at least one actual owned output. executables maps names
to absolute native paths. Diagnostics/build logs belong on stderr. Optional
details retain recipe-specific readback without changing this envelope.
"""

from __future__ import annotations

import json
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib

from safety import (
    Error,
    atomic_json,
    check_inventory,
    digest,
    download,
    extract,
    fingerprint,
    inside,
    inventory,
    load,
    native,
    no_symlink_parents,
    relative,
    run,
    command,
)


def mise_run(context, arguments, *, capture=True, extra_env=None):
    """Run only the pinned binary against an isolated canonical config copy."""
    original = Path(context["base"]) / "apps/mise"
    with tempfile.TemporaryDirectory(
        prefix="dotfiles-mise-", dir=Path("/tmp").resolve()
    ) as temporary:
        selected = Path(temporary)
        shutil.copyfile(original / "mise.toml", selected / "config.toml")
        shutil.copyfile(original / "mise.lock", selected / "mise.lock")
        sidecars = original / ".mise/locks"
        if sidecars.exists():
            for path in sidecars.rglob("*"):
                if path.is_symlink() or not path.resolve().is_relative_to(original.resolve()):
                    raise Error("mise sidecars must be ordinary committed owning-source files")
            shutil.copytree(sidecars, selected / ".mise/locks")
        empty = selected / "empty-system"
        empty.mkdir()
        (empty / "config.toml").write_text("")
        environment = {
            "MISE_CONFIG_DIR": str(selected),
            "MISE_GLOBAL_CONFIG_FILE": str(selected / "config.toml"),
            "MISE_SYSTEM_CONFIG_DIR": str(empty),
            "MISE_SYSTEM_CONFIG_FILE": str(empty / "config.toml"),
            "MISE_TRUSTED_CONFIG_PATHS": str(selected / "config.toml"),
            "MISE_CEILING_PATHS": "/",
            "MISE_NO_ENV": "1",
            "MISE_NO_HOOKS": "1",
            "MISE_SAFE": "1",
            "MISE_LOCKED": "1",
        }
        environment.update(extra_env or {})
        return run(
            [context["executables"]["mise"], *arguments],
            context,
            cwd="/",
            capture=capture,
            extra_env=environment,
        )


def recipe(context, package, action, context_path, output=None):
    path = inside(package["_origin"], package.get("path", package.get("recipe")), existing=True)
    argv = [
        native(sys.executable),
        "-I",
        "-B",
        path,
        action,
        "--context",
        context_path,
        "--id",
        package["id"],
    ]
    if output is not None:
        argv.extend(["--output", output])
    argv.extend(package.get("args", []))
    if action in package.get("privileged_actions", []):
        if context.get("_verification_only"):
            raise Error(
                "verification never authorizes privileged recipe mutation; build/check unprivileged instead"
            )
        if os.geteuid() != 0:
            argv = ["/usr/bin/sudo", "--", *argv]
    text = run(argv, context)
    try:
        result = json.loads(text)
    except ValueError as exc:
        raise Error(f"recipe must emit exactly one JSON result: {package['id']}") from exc
    statuses = {
        "plan": "planned",
        "build": "built",
        "provision": "provisioned",
        "check": "verified",
        "retire": "retired",
        "recover": "recovered",
    }
    if (
        not isinstance(result, dict)
        or result.get("id") != package["id"]
        or result.get("status") != statuses[action]
    ):
        raise Error(f"recipe result id/status mismatch: {package['id']} {action}")
    artifacts, executables = result.get("artifacts"), result.get("executables")
    if not isinstance(artifacts, list) or not isinstance(executables, dict):
        raise Error("recipe result requires artifacts list and executables mapping")
    if action in ("check", "provision") and not artifacts:
        raise Error(
            f"recipe must report independently verifiable installed artifacts: {package['id']}"
        )
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise Error("recipe artifact descriptor requires path and SHA-256")
        target = Path(artifact["path"])
        native(target, exists=False)
        if not target.is_file() or digest(target) != artifact.get("sha256"):
            raise Error(f"recipe artifact readback failed: {target}")
    for name, executable in executables.items():
        native(executable, exists=action in ("check", "provision"))
    return result


def artifact_spec(package, platform):
    variants = package.get("artifacts", package.get("sources", package.get("platforms", {})))
    if isinstance(variants, list):
        variants = package.get("source", package.get("platform_artifacts", {}))
    if isinstance(variants, dict) and platform in variants:
        spec = dict(variants[platform])
    else:
        spec = {
            key: value
            for key, value in package.items()
            if key in ("url", "sha256", "archive", "bin", "app", "strip_components")
        }
    if "url" not in spec or "sha256" not in spec:
        raise Error(f"missing pinned artifact for {package['id']} on {platform}")
    for key in ("bin", "app"):
        value = spec.get(key)
        if isinstance(value, str):
            relative(value)
        elif isinstance(value, dict):
            for name, source in value.items():
                relative(name)
                relative(source)
    return spec


def prefix(context, package):
    relative(package["id"])
    relative(package["version"])
    runtime_id = package.get("runtime_id", package["id"])
    relative(runtime_id)
    return Path(context["paths"]["runtime"]) / runtime_id / package["version"]


def artifact_check(context, package):
    destination = prefix(context, package)
    record = destination / ".dotfiles-artifact.json"
    if destination.is_symlink() or not record.is_file():
        raise Error(f"unrecorded artifact installation: {destination}")
    receipt = load(record)
    spec = artifact_spec(package, context["platform"])
    if receipt.get("source") != spec or receipt.get("version") != package["version"]:
        raise Error(f"artifact receipt selection mismatch: {package['id']}")
    expected = receipt["files"]
    actual = inventory(destination)
    actual.pop(".dotfiles-artifact.json", None)
    if actual != expected:
        raise Error(f"artifact payload verification failed: {package['id']}")
    return receipt


def owned_link(path, target, state):
    path = Path(path)
    target = str(target)
    no_symlink_parents(path, Path(state["home"]))
    records = state.setdefault("links", {})
    key = str(path)
    actual = fingerprint(path)
    journal_path = (
        Path(state.get("_state_dir", Path(state["home"]) / ".local/state/dotfiles"))
        / "links-journal.json"
    )
    journal = load(journal_path) if journal_path.exists() else {"home": state["home"], "links": {}}
    if journal.get("home") != state["home"]:
        raise Error("artifact link journal belongs to another home")
    after = {"kind": "symlink", "target": target}
    pending = journal["links"].get(key, {})
    recovered_transition = pending.get("after") == actual == after and pending.get("status") in (
        "pending",
        "applied",
    )
    if actual is not None and actual != records.get(key) and not recovered_transition:
        raise Error(f"foreign artifact link collision: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".dotfiles-candidate")
    if os.path.lexists(temporary):
        raise Error(f"foreign staging link: {temporary}")
    journal["links"][key] = {"before": actual, "after": after, "status": "pending"}
    atomic_json(journal_path, journal)
    temporary.symlink_to(target)
    os.replace(temporary, path)
    records[key] = fingerprint(path)
    if records[key] != after:
        raise Error(f"artifact link readback mismatch: {path}")
    journal["links"][key]["status"] = "applied"
    atomic_json(journal_path, journal)


def install_artifact(context, package, state):
    state["_state_dir"] = context["paths"]["state"]
    destination = prefix(context, package)
    spec = artifact_spec(package, context["platform"])
    registration = None
    if spec.get("app"):
        registration = command(
            context,
            "lsregister",
            "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
            mutating=True,
        )
    if destination.exists() or destination.is_symlink():
        receipt = artifact_check(context, package)
    else:
        no_symlink_parents(destination, Path(context["home"]))
        source = download(
            spec["url"], spec["sha256"], Path(context["paths"]["cache"]) / "downloads"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".artifact-", dir=destination.parent))
        try:
            kind = spec.get("archive", "tar")
            if kind in ("raw", "binary", "file", "appimage"):
                name = spec.get("bin")
                if not isinstance(name, str):
                    raise Error("raw artifact requires bin path")
                target = inside(stage, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                target.chmod(0o755)
            elif kind == "dmg":
                if context["platform"] != "macos-arm64":
                    raise Error("DMG installation requires macOS")
                mount = stage / ".mount"
                mount.mkdir()
                run(
                    [
                        "/usr/bin/hdiutil",
                        "attach",
                        "-readonly",
                        "-nobrowse",
                        "-mountpoint",
                        mount,
                        source,
                    ],
                    context,
                )
                try:
                    app = spec.get("app")
                    if not app:
                        raise Error("DMG artifact requires app name")
                    shutil.copytree(inside(mount, app, existing=True), stage / app, symlinks=True)
                finally:
                    run(["/usr/bin/hdiutil", "detach", mount], context)
                    mount.rmdir()
            else:
                extract(source, stage, kind, int(spec.get("strip_components", 0)))
            for name in (spec.get("bin"), spec.get("app")):
                if isinstance(name, str):
                    inside(stage, name, existing=True)
            receipt = {
                "schema_version": 1,
                "id": package["id"],
                "version": package["version"],
                "source": spec,
                "files": inventory(stage),
            }
            atomic_json(stage / ".dotfiles-artifact.json", receipt)
            os.replace(stage, destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        artifact_check(context, package)
    owned_link(destination.parent / "current", destination, state)
    binaries = spec.get("bin")
    if isinstance(binaries, str):
        binaries = {Path(binaries).name: binaries}
    for name, source in (binaries or {}).items():
        if "/" in name:
            raise Error("artifact command name must be a basename")
        executable = inside(destination, source, existing=True)
        native(executable)
        owned_link(Path(context["paths"]["bin"]) / name, executable, state)
    if spec.get("app"):
        app = inside(destination, spec["app"], existing=True)
        if not app.is_dir() or app.suffix != ".app":
            raise Error("application artifact is not an .app bundle")
        owned_link(Path(context["home"]) / "Applications" / app.name, app, state)
        assert registration is not None
        run([registration, "-f", app], context)
    return receipt


def mise_metadata(context, package):
    manifest = Path(context["base"]) / "apps/mise/mise.toml"
    lock = manifest.with_suffix(".lock")
    if not manifest.is_file() or not lock.is_file():
        raise Error("mise requires committed manifest and lock")
    config, locked = tomllib.loads(manifest.read_text()), tomllib.loads(lock.read_text())
    name = package["tool"]
    selection = config.get("tools", {}).get(name)
    if isinstance(selection, dict):
        selection = selection.get("version")
    if selection != package["version"]:
        raise Error(f"mise package and canonical manifest disagree: {name}")
    entries = locked.get("tools", {}).get(name, [])
    if isinstance(entries, dict):
        entries = [entries]
    matches = [entry for entry in entries if entry.get("version") == package["version"]]
    if len(matches) != 1:
        raise Error(f"mise lock missing/ambiguous selection: {name}")
    entry = matches[0]
    backend = entry.get("backend", "")
    declared_backend = config.get("tool_alias", {}).get(name)
    if declared_backend and backend != declared_backend:
        raise Error(f"mise backend differs from the canonical declared owner: {name}")
    if not backend.startswith(("aqua:", "github:", "npm:", "go:")) and backend not in (
        "core:node",
        "core:go",
        "core:python",
    ):
        raise Error(f"unapproved mise backend: {backend}")
    platform_record = entry.get(
        "platforms." + context["platform"], entry.get("platforms", {}).get(context["platform"])
    )
    result = {"manifest_sha256": digest(manifest), "lock_sha256": digest(lock), "selection": entry}
    if (
        platform_record
        and platform_record.get("checksum", "").startswith("sha256:")
        and platform_record.get("url")
    ):
        result["integrity"] = (
            "pinned upstream archive SHA-256; installed executable digests recorded separately"
        )
        return result
    if entry.get("backend", "").startswith("npm:") and isinstance(entry.get("aube"), dict):
        descriptor = entry["aube"]
        graph_root = inside(manifest.parent, descriptor.get("path"), existing=True)
        graph = inside(graph_root, "aube-lock.yaml", existing=True)
        checksum = (
            "sha256:" + hashlib.sha256(graph.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        )
        if descriptor.get("digest") != checksum:
            raise Error(f"native npm graph digest mismatch: {name}")
        request = load(graph_root / "package.json")
        npm_name = entry["backend"][4:]
        if request.get("dependencies") != {npm_name: package["version"]} or set(request) - {
            "name",
            "private",
            "dependencies",
        }:
            raise Error(
                f"native npm graph manifest does not bind exactly the selected package: {name}"
            )
        configured = config["tools"][name]
        approved = configured.get("allow_builds", []) if isinstance(configured, dict) else []
        if not isinstance(approved, list) or any(
            not isinstance(value, str) or not value for value in approved
        ):
            raise Error("npm lifecycle approvals must be an explicit package list")
        result["graph"] = {
            "backend": "npm",
            "path": descriptor["path"],
            "digest": checksum,
            "manifest_sha256": digest(graph_root / "package.json"),
            "approved_builds": approved,
            "scope": "native mise frozen dependency graph and package integrity; installed tree digest recorded, not a claim about native system ABI",
        }
        result["integrity"] = (
            "committed aube dependency graph replayed by pinned mise --locked; unapproved dependency scripts disabled"
        )
        return result
    verification = package.get("verification", {})
    if verification.get("backend") == "go" and entry.get("backend", "").startswith("go:"):
        module = verification.get("module", "")
        module_version = verification.get("module_version", "")
        if not module or module_version.removeprefix("v") != package["version"].removeprefix("v"):
            raise Error(f"Go backend requires exact declared module/version: {name}")
        if verification.get("sumdb") != "sum.golang.org":
            raise Error("Go backend must retain the public checksum database")
        toolchain = verification.get("toolchain", "")
        if not toolchain.startswith("go@"):
            raise Error("Go backend requires pinned Go toolchain")
        selected_go = [
            p
            for p in context["packages"]
            if p.get("tool") == "go" and p.get("version") == toolchain[3:]
        ]
        if len(selected_go) != 1:
            raise Error("Go backend toolchain is not selected coherently")
        result["integrity"] = (
            "exact Go module/version via sum.golang.org and pinned toolchain; not byte-closed transitive build provenance"
        )
        result["verification"] = verification
        return result
    raise Error(
        f"mise backend requires pinned archive or an explicitly approved source-backend verification policy: {name}"
    )


def go_environment(context, metadata):
    verification = metadata["verification"]
    executable = native(context["executables"]["go"])
    version = run([executable, "version"], context)
    if "go" + verification["toolchain"][3:] not in version:
        raise Error("installed Go compiler does not match pinned toolchain")
    return {
        "PATH": str(Path(executable).parent) + ":/usr/bin:/bin",
        "GOSUMDB": "sum.golang.org",
        "GOPROXY": "https://proxy.golang.org",
        "GONOSUMDB": "",
        "GOPRIVATE": "",
        "GOENV": "off",
        "GOWORK": "off",
        "GOTOOLCHAIN": "local",
    }


def macos_adapter(context):
    """The one concrete Homebrew desktop safety adapter, not a plugin registry."""
    path = inside(context["base"], "native/gui/macos.py", existing=True)
    specification = importlib.util.spec_from_file_location("_dotfiles_native_macos", path)
    if specification is None or specification.loader is None:
        raise Error("cannot load the declared macOS desktop safety adapter")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def brew_info(context, package):
    flag = "--cask" if package["provider"] == "cask" else "--formula"
    return json.loads(
        run(
            [
                command(context, "brew", "/opt/homebrew/bin/brew"),
                "info",
                "--json=v2",
                flag,
                package["name"],
            ],
            context,
        )
    )


def package_status(context, package, context_path):
    provider = package["provider"]
    if provider == "apt":
        text = run(
            [
                command(context, "dpkg-query", "/usr/bin/dpkg-query"),
                "-W",
                "-f=${Status}\t${Version}",
                package["name"],
            ],
            context,
        )
        if not text.startswith("install ok installed\t"):
            raise Error(f"APT package not installed: {package['name']}")
        return {"installed": text.split("\t", 1)[1].strip()}
    if provider in ("brew", "cask"):
        info = brew_info(context, package)
        records = info["casks" if provider == "cask" else "formulae"]
        installed = records[0].get("installed") if records else None
        if not installed:
            raise Error(f"Homebrew package not installed: {package['name']}")
        result = {"installed": installed}
        if package.get("macos"):
            result["macos"] = macos_adapter(context).check(context, package, info)
        return result
    if provider == "artifact":
        return {"installed": package["version"], "receipt": artifact_check(context, package)}
    if provider == "mise":
        metadata = mise_metadata(context, package)
        root = mise_run(context, ["where", f"{package['tool']}@{package['version']}"]).strip()
        expected = Path(context["paths"]["data"]) / "mise/installs"
        if not Path(root).resolve().is_relative_to(expected.resolve()):
            raise Error(f"mise resolved outside trusted installation root: {root}")
        installed_files, executables = {}, {}
        for name, declared in package.get("executables", {}).items():
            binary_name = Path(declared).name
            executable = mise_run(
                context, ["which", binary_name, "--tool", f"{package['tool']}@{package['version']}"]
            ).strip()
            native(executable)
            if not Path(executable).resolve().is_relative_to(Path(root).resolve()):
                raise Error(f"mise executable escaped selected tool/version: {name}")
            executables[name] = executable
            installed_files[name] = digest(executable)
            if metadata.get("verification", {}).get("backend") == "go":
                verification = metadata["verification"]
                output = run(
                    [context["executables"]["go"], "version", "-m", executable],
                    context,
                    extra_env=go_environment(context, metadata),
                )
                if (
                    verification["module"] not in output
                    or verification["module_version"] not in output
                ):
                    raise Error(f"Go installed module identity mismatch: {name}")
        result = {
            "installed": package["version"],
            "provenance": metadata,
            "prefix": root,
            "installed_files": installed_files,
            "executables": executables,
        }
        if "graph" in metadata:
            result["installed_tree_sha256"] = hashlib.sha256(
                json.dumps(inventory(root), sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        return result
    if provider == "recipe":
        return {"check": recipe(context, package, "check", context_path)}
    raise Error(f"unsupported provider: {provider}")


def shared_runtime(package, entry=None, context=None):
    """Retention is sticky across feature/manifest changes and package history."""
    return bool(
        package.get("retain_shared_runtime")
        or (entry or {}).get("retain_shared_runtime")
        or (entry or {}).get("shared_service")
        or any(
            service.get("existing_unit") and service.get("package_id") == package["id"]
            for service in (context or {}).get("services", [])
        )
    )


def mutation_guard(context, package, context_path):
    """Read-only opt-in runtime preflight, before ANY native provider mutation.

    Existing distro units use the one service controller's package/payload proof.
    The sole recipe callback shape is an unprivileged repository update-check for
    a retained cask runtime; it cannot grant consent or activate/stop services.
    """
    identifiers = package.get("mutation_requires_inactive_services", [])
    check = package.get("safety_check")
    if not identifiers and not check:
        return
    fixture = context.get("_verification_only") and context.get("_fixture_context")
    if os.geteuid() == 0 and not fixture:
        raise Error(
            "shared-runtime mutation preflight must run unprivileged before provider escalation"
        )
    if identifiers:
        import services

        available = {item["id"]: item for item in context["services"]}
        for identifier in identifiers:
            service = available.get(identifier)
            if (
                service is None
                or service.get("manager") != "systemd-system"
                or not service.get("existing_unit")
                or service.get("package_id") != package["id"]
                or service.get("unit_package") != package.get("name")
                or service.get("_origin") != package.get("_origin")
            ):
                raise Error(
                    f"runtime guard requires the same-origin package's existing APT unit: {identifier}"
                )
            try:
                observed = services.observe(context, service)
            except (Error, OSError, ValueError):
                # Fresh-host installation may have no package/unit at all.
                # Never equate an unverified foreign loaded unit with absence.
                try:
                    package_status(context, package, context_path)
                except Error:
                    executable = command(
                        context,
                        "systemctl",
                        "/usr/bin/systemctl",
                        mutating=bool(context.get("_verification_only")),
                    )
                    files = run(
                        [
                            executable,
                            "--system",
                            "list-unit-files",
                            service["name"],
                            "--no-legend",
                            "--no-pager",
                        ],
                        context,
                    )
                    loaded = run(
                        [
                            executable,
                            "--system",
                            "list-units",
                            "--all",
                            "--plain",
                            "--no-legend",
                            "--no-pager",
                            service["name"],
                        ],
                        context,
                    )
                    if not files.strip() and not loaded.strip():
                        continue
                raise
            if observed.get("ActiveState") not in ("inactive", "failed"):
                raise Error(
                    f"running/unknown shared runtime blocks package mutation: {identifier}; no daemon or workload was stopped"
                )
    if check:
        source = inside(package["_origin"], check["path"], existing=True)
        if fixture and not source.resolve().is_relative_to(Path(context["home"]).resolve()):
            raise Error("verification runtime callback must be under disposable HOME")
        result = json.loads(
            run(
                [
                    native(sys.executable),
                    "-I",
                    "-B",
                    source,
                    "update-check",
                    "--context",
                    context_path,
                    "--id",
                    package["id"],
                ],
                context,
            )
        )
        if (
            result.get("id") != package["id"]
            or result.get("status") != "update-check"
            or result.get("details", {}).get("safe") is not True
        ):
            raise Error("runtime safety callback did not prove mutation safe")


def apt_current(context, package, context_path):
    """A verified unchanged APT candidate is a true no-op, even if shared/active."""
    try:
        installed = package_status(context, package, context_path)["installed"]
    except Error:
        return False
    executable = command(
        context, "apt-cache", "/usr/bin/apt-cache", mutating=bool(context.get("_verification_only"))
    )
    output = run([executable, "policy", "--", package["name"]], context)
    candidates = [
        line.strip().split(":", 1)[1].strip()
        for line in output.splitlines()
        if line.strip().startswith("Candidate:")
    ]
    if len(candidates) != 1 or candidates[0] == "(none)":
        raise Error("cannot establish selected signed-repository APT candidate")
    return candidates[0] == installed


def apt_removal_plan(context, package):
    """Prove a generic retirement removes exactly its one requested OS package.

    This is unprivileged, never --autoremove/purge, and runs again immediately
    before the actual transaction. APT reverse-dependency cascades are refused
    even if their other packages were separately recorded by dotfiles.
    """
    query = command(
        context,
        "dpkg-query",
        "/usr/bin/dpkg-query",
        mutating=bool(context.get("_verification_only")),
    )
    name = package["name"]
    canonical = run([query, "-W", "-f=${binary:Package}", name], context).strip()
    if (
        not canonical
        or len(canonical.split()) != 1
        or canonical.split(":", 1)[0] != name.split(":", 1)[0]
        or (":" in name and canonical != name)
    ):
        raise Error(f"cannot resolve exact installed APT package identity: {name}")
    executable = command(
        context, "apt-get", "/usr/bin/apt-get", mutating=bool(context.get("_verification_only"))
    )
    output = run(
        [executable, "--simulate", "remove", "--no-auto-remove", "--", name],
        context,
        extra_env={"LC_ALL": "C"},
    )
    removed = []
    for line in output.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0] == "Remv":
            if len(words) < 2:
                raise Error("malformed APT removal simulation")
            removed.append(words[1])
        elif words[0] in ("Inst", "Conf", "Purg"):
            raise Error(
                "APT retirement simulation includes additional install/configure/purge operations; preserved"
            )
    if len(removed) != 1 or removed[0].split(":", 1)[0] != canonical.split(":", 1)[0]:
        raise Error(
            f"APT retirement would remove beyond the exact owned package {name}: {removed}; preserved"
        )
    return {"requested": canonical, "removed": removed}


def apt_transaction(context, arguments):
    if arguments and arguments[0] == "remove":
        marker = arguments.index("--") if "--" in arguments else -1
        if (
            marker < 0
            or arguments[:marker] != ["remove", "--yes", "--no-auto-remove"]
            or len(arguments[marker + 1 :]) != 1
        ):
            raise Error(
                "generic APT retirement requires exactly remove --yes --no-auto-remove -- PACKAGE; purge/autoremove/cascades are forbidden"
            )
        apt_removal_plan(context, {"name": arguments[marker + 1]})
    executable = command(context, "apt-get", "/usr/bin/apt-get", mutating=True)
    fixture = context.get("_verification_only") and context.get("_fixture_context")
    policy = (
        Path(context["paths"]["state"]) / "fixture-policy-rc.d"
        if fixture
        else Path("/usr/sbin/policy-rc.d")
    )
    if policy.exists() or policy.is_symlink():
        raise Error(
            "APT service suppression refuses an existing policy-rc.d; administrator must reconcile it explicitly"
        )
    sudo = [] if os.geteuid() == 0 or fixture else ["/usr/bin/sudo"]
    if fixture:
        policy.parent.mkdir(parents=True, exist_ok=True)
    script = "import os,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o755); os.fchmod(fd,0o755); os.write(fd,b'#!/bin/sh\\nexit 101\\n'); os.close(fd)"
    run(sudo + [native(sys.executable), "-I", "-B", "-c", script, policy], context)
    try:
        run(sudo + [executable, *arguments], context, capture=False)
    finally:
        if (
            policy.is_file()
            and not policy.is_symlink()
            and policy.read_text() == "#!/bin/sh\nexit 101\n"
        ):
            run(sudo + ["/bin/rm", "--", policy], context)
        else:
            raise Error("APT policy changed during provision; preserved for inspection")


def provision(context, context_path, records, identifiers=()):
    records["_state_dir"] = context["paths"]["state"]
    selected = set(identifiers)
    available = {p["id"] for p in context["packages"]}
    if selected - available:
        raise Error(f"unknown package ids: {sorted(selected - available)}")
    # Explicit id selection includes its declared dependency closure.
    by_id = {p["id"]: p for p in context["packages"]}

    def include(identifier):
        for dependency in by_id[identifier].get("requires", []):
            if dependency not in selected:
                selected.add(dependency)
                include(dependency)

    for identifier in tuple(selected):
        include(identifier)
    results = records.setdefault("packages", {})
    ordered = sorted(
        context["packages"],
        key=lambda package: (package["provider"] == "recipe", package["id"] == "shell-completions"),
    )
    for package in ordered:
        identifier = package["id"]
        if selected and identifier not in selected:
            continue
        if package.get("requires_services"):
            from services import require_services

            require_services(context, package)
        provider = package["provider"]
        previous = results.get(identifier)
        previous_package = previous.get("selection", {}) if previous else {}
        preexisting = results.get(identifier, {}).get("preexisting")
        if preexisting is None:
            try:
                package_status(context, package, context_path)
                preexisting = provider not in ("recipe", "artifact")
            except (Error, OSError, ValueError):
                preexisting = False
        if provider == "apt":
            protected = bool(
                package.get("mutation_requires_inactive_services")
                or previous_package.get("mutation_requires_inactive_services")
            )
            if not protected or not apt_current(context, package, context_path):
                mutation_guard(context, package, context_path)
                if previous_package and previous_package != package:
                    mutation_guard(context, previous_package, context_path)
                apt_transaction(
                    context, ["install", "--yes", "--no-install-recommends", "--", package["name"]]
                )
        elif provider in ("brew", "cask"):
            operation = "install"
            preserve_preexisting = False
            if package.get("macos"):
                info = brew_info(context, package)
                adapter = macos_adapter(context)
                adapter.preflight(
                    context,
                    package,
                    info,
                    results.get(identifier, {}).get("observed", {}).get("macos"),
                )
                manager_records = info["casks" if provider == "cask" else "formulae"]
                already_installed = bool(manager_records and manager_records[0].get("installed"))
                preserve_preexisting = bool(preexisting and already_installed)
                if already_installed and not preserve_preexisting:
                    operation = "upgrade"
            if not preserve_preexisting:
                mutation_guard(context, package, context_path)
                if previous_package and previous_package != package:
                    mutation_guard(context, previous_package, context_path)
                run(
                    [
                        command(context, "brew", "/opt/homebrew/bin/brew", mutating=True),
                        operation,
                        "--cask" if provider == "cask" else "--formula",
                        "--",
                        package["name"],
                    ],
                    context,
                    capture=False,
                )
        elif provider == "mise":
            metadata = mise_metadata(context, package)
            platform_data = metadata["selection"].get(
                "platforms." + context["platform"],
                metadata["selection"].get("platforms", {}).get(context["platform"]),
            )
            extra_env = None
            if metadata.get("verification", {}).get("backend") == "go":
                extra_env = go_environment(context, metadata)
            elif "graph" in metadata:
                node = native(context["executables"]["node"])
                selected_node = [p for p in context["packages"] if p.get("tool") == "node"]
                if (
                    len(selected_node) != 1
                    or run([node, "--version"], context).strip()
                    != "v" + selected_node[0]["version"]
                ):
                    raise Error("native npm graph requires the declared pinned Node runtime")
                extra_env = {"PATH": str(Path(node).parent) + ":/usr/bin:/bin"}
            else:
                # Independently verify the selected archive before mise may execute it.
                download(
                    platform_data["url"],
                    platform_data["checksum"].removeprefix("sha256:"),
                    Path(context["paths"]["cache"]) / "downloads",
                )
                for artifact in platform_data.get("additional_artifacts", []):
                    download(
                        artifact["url"],
                        artifact["checksum"].removeprefix("sha256:"),
                        Path(context["paths"]["cache"]) / "downloads",
                    )
            mise_run(
                context,
                ["install", "--locked", f"{package['tool']}@{package['version']}"],
                capture=False,
                extra_env=extra_env,
            )
        elif provider == "artifact":
            install_artifact(context, package, records)
        elif provider == "recipe":
            output = Path(context["paths"]["cache"]) / "builds" / identifier
            output.parent.mkdir(parents=True, exist_ok=True)
            if package.get("build_before_provision"):
                if os.geteuid() == 0:
                    raise Error(
                        "source builds must run as an unprivileged user; engine elevates only the declared install action"
                    )
                recipe(context, package, "build", context_path, output)
            recipe(context, package, "provision", context_path, output)
        status = package_status(context, package, context_path)
        if provider == "mise":
            context["executables"].update(status["executables"])
            # The original three tools are strict user-global shims, never
            # fallback version-specific links when integration/features disable.
            if package["tool"] not in ("cidr", "terraform", "helmfile"):
                for name, executable in status["executables"].items():
                    owned_link(Path(context["paths"]["bin"]) / name, executable, records)
            atomic_json(context_path, context)
        if previous and previous.get("selection") != package:
            records.setdefault("history", {}).setdefault(identifier, []).append(previous)
        shared_service = bool(previous and previous.get("shared_service")) or any(
            service.get("existing_unit") and service.get("package_id") == identifier
            for service in context["services"]
        )
        results[identifier] = {
            "selection": package,
            "observed": status,
            "preexisting": preexisting,
            "shared_service": shared_service,
            "retain_shared_runtime": shared_runtime(package, previous, context),
        }
        records.get("retained_shared_runtime", {}).pop(identifier, None)
        atomic_json(Path(context["paths"]["state"]) / "packages.json", records)
    if any(p["provider"] == "mise" for p in ordered) and context.get("dotfiles", {}).get(
        "dev", {}
    ).get("mise", {}).get("enable", False):
        shims = Path(context["paths"]["data"]).parent / "mise-shims"
        old_shims = records.setdefault("shims", {})
        if shims.is_symlink():
            raise Error("foreign mise shim directory")
        if shims.exists():
            for path in shims.iterdir():
                if fingerprint(path) != old_shims.get(path.name):
                    raise Error(f"foreign mise shim collision: {path}")
        mise_run(
            context,
            ["reshim", "--force"],
            extra_env={"MISE_SHIMS_DIR": str(shims), "MISE_OFFLINE": "1"},
        )
        expected_target = context["executables"]["mise"]
        for path in shims.iterdir():
            if not path.is_symlink() or os.readlink(path) != expected_target:
                raise Error(f"mise shim readback is not the pinned mise executable: {path}")
        records["shims"] = {path.name: fingerprint(path) for path in shims.iterdir()}
        atomic_json(Path(context["paths"]["state"]) / "packages.json", records)
    return records


def prerequisites(context):
    pins = load(Path(context["base"]) / "native/engine/pins.json")
    records_path = Path(context["paths"]["state"]) / "bootstrap.json"
    records = (
        load(records_path) if records_path.exists() else {"home": context["home"], "links": {}}
    )
    for identifier in ("mise", "chezmoi"):
        pin = pins[identifier]
        spec = pin["platforms"][context["platform"]]
        package = {
            "id": "bootstrap/" + identifier,
            "version": pin["version"],
            "platforms": {context["platform"]: spec},
        }
        install_artifact(context, package, records)
        atomic_json(records_path, records)
    return records
