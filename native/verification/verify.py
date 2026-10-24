"""Offline, Nix-free verification with explicit, repository-owned acquisition.

Python API (ordinary context dict from native.engine; no active context needed):
  host_context(context) -> host-only context, ignoring target features/platform.
  host_prerequisites(context) -> existing git/bash/zsh native-provider records;
      root bootstrap/provision merges these by id, rather than a second installer.
  provision(context) -> internal locked tools, explicit network/mutation action.
  check(context, files=None, run_tests=True) -> [{name, status, detail}], all stages.
  format_files(context, files) -> same results; files is a mandatory explicit union.
Paths in files are repository-relative to context['root']; no glob expansion.
check's default union is Git cached + nonignored untracked files, not HEAD alone.
Only CPython 3.11..3.14 on the two native hosts is supported by the wheel lock.
Ruff/ShellCheck/gitleaks/wheels live in versioned internal prefixes, never PATH,
user profiles, hooks or mise global config. Gitleaks reuses the canonical mise
lock's artifact identity without invoking mise or changing its global writer.
System git/bash/zsh are existing native provider prerequisites, independent of
feature selection. Missing ones must be provisioned through repository bootstrap.
No check/format operation downloads, installs, reads secrets.yaml for formatting,
or installs hooks. The caller serializes its one explicit format union.
Formatting/style/shell checks exclude integrity-bound locks, mise sidecars and
vendor/upstream assets. JSON formatting additionally requires the source-controlled
authored JSON scope. Read-only JSON syntax and secret scanning retain those inputs.
Test subprocesses receive DOTFILES_BOOTSTRAP_SOURCE as this component's resolved,
consumed public base, never an inherited environment value or guessed checkout.

CLI: python3 -I -B native/verification/verify.py {provision,check,format}
     --context CONTEXT.json [--files-from FILE_UNION.json] [--no-tests]
provision additionally requires --yes. Context home/paths must be explicit.
"""

from __future__ import annotations

import argparse
import copy
import fnmatch
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "native/engine"))
import manifest
from safety import (
    Error,
    atomic_json,
    digest,
    download,
    extract,
    inside,
    inventory,
    load,
    locked,
    native,
    no_symlink_parents,
)

LOCK = BASE / "native/manifests/verification.json"
CONFIG = BASE / "native/verification/ruff.toml"
RECEIPT = ".dotfiles-verification.json"


def host_context(context):
    result = copy.deepcopy(context)
    result["platform"] = manifest.platform()
    if platform.python_implementation() != "CPython" or not (3, 11) <= sys.version_info[:2] <= (
        3,
        14,
    ):
        raise Error(
            "verification requires native CPython 3.11..3.14; run ./bootstrap bootstrap --yes"
        )
    native(sys.executable)
    for key in ("root", "base", "home"):
        value = Path(result[key])
        if not value.is_absolute() or "/nix/" in str(value.resolve()):
            raise Error(f"verification requires a native absolute {key}")
    for key in ("runtime", "cache", "state"):
        path = Path(result["paths"][key])
        no_symlink_parents(path / "verification", Path(result["home"]))
        if "/nix/" in str(path.resolve()):
            raise Error(f"verification path depends on Nix: {key}")
    # Never inherit feature-selected executables (including target-platform tools).
    result["executables"] = {"python3": native(sys.executable)}
    result.pop("_verification_test_tools", None)
    result["packages"] = []
    result["services"] = []
    return result


def host_prerequisites(context):
    """Existing providers selected by host, not the target feature profile."""
    host = manifest.platform()
    packages = load(BASE / "native/manifests/base.json")["packages"]
    result = []
    required = context.get("verification", {}).get("native_packages", [])
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        raise Error("verification native_packages must be declared package IDs")
    for name in dict.fromkeys(("git", "bash", "zsh", *required)):
        candidates = [p for p in packages if p["id"] == name and host in p["platforms"]]
        if len(candidates) != 1:
            raise Error(f"verification requires one declared host provider: {name}")
        record = copy.deepcopy(candidates[0])
        record.pop("when", None)
        record["_origin"] = str(BASE)
        result.append(record)
    return result


def tool_specs(context):
    pins = load(LOCK)
    selected = {}
    host = context["platform"]
    for name, pin in pins["tools"].items():
        if "canonical_mise_tool" in pin:
            source = inside(BASE, pin["canonical_lock"], existing=True)
            lock = tomllib.loads(source.read_text())
            records = lock.get("tools", {}).get(pin["canonical_mise_tool"], [])
            if isinstance(records, dict):
                records = [records]
            if (
                not isinstance(records, list)
                or len(records) != 1
                or not isinstance(records[0], dict)
            ):
                raise Error(f"missing/ambiguous canonical verification lock: {name}")
            record = records[0]
            # Match providers.mise_metadata: generated mise locks can use literal
            # dotted platform keys as well as nested platform tables.
            platform_key = "platforms." + host
            platforms = record.get("platforms", {})
            artifact = record.get(platform_key)
            if platform_key not in record and isinstance(platforms, dict):
                artifact = platforms.get(host)
            if (
                not isinstance(artifact, dict)
                or not isinstance(artifact.get("url"), str)
                or not artifact["url"].strip()
            ):
                raise Error(
                    f"missing/malformed canonical verification host artifact: {name}, {host}"
                )
            try:
                parsed = urlsplit(artifact["url"])
            except ValueError as exc:
                raise Error(f"malformed canonical verification host URL: {name}, {host}") from exc
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise Error(
                    f"canonical verification artifact requires credential-free HTTPS: {name}, {host}"
                )
            checksum = artifact.get("checksum")
            if (
                not isinstance(checksum, str)
                or not checksum.startswith("sha256:")
                or len(checksum) != 71
                or any(c not in "0123456789abcdef" for c in checksum[7:])
            ):
                raise Error(f"verification requires exact SHA-256: {name}, {host}")
            spec = {"url": artifact["url"], "sha256": checksum[7:], "archive": "tar", "bin": name}
            version = record["version"]
            if name == "node":
                # Consume the canonical Node artifact, but install only its runtime
                # and license: npm and its symlink/install surface are not check tools.
                archive_root = Path(parsed.path).name.removesuffix(".tar.gz")
                if not archive_root.startswith("node-v") or "/" in archive_root:
                    raise Error("canonical Node archive identity is unsupported")
                spec["members"] = [archive_root + "/bin/node", archive_root + "/LICENSE"]
                spec["bin"] = archive_root + "/bin/node"
        else:
            spec = pin["platforms"][host]
            version = pin["version"]
        selected[name] = {"version": version, "sources": [spec], "bin": spec["bin"]}
    wheels = []
    abi = f"{sys.version_info.major}.{sys.version_info.minor}"
    for name, pin in pins["schema_wheels"].items():
        candidates = [
            f
            for f in pin["files"]
            if "platform" not in f or (f["platform"] == host and f["python"] == abi)
        ]
        if len(candidates) != 1:
            raise Error(f"no unique locked schema wheel for {name}, {host}, CPython {abi}")
        wheels.append(
            {**candidates[0], "archive": "zip", "distribution": name, "version": pin["version"]}
        )
    # The digest of the entire coherent source selection versions this assembly.
    import hashlib

    identity = hashlib.sha256(json.dumps(wheels, sort_keys=True).encode()).hexdigest()[:20]
    selected["schema"] = {"version": f"cp{abi}-{identity}", "sources": wheels}
    return selected


def destination(context, name, spec):
    return Path(context["paths"]["runtime"]) / "verification" / name / spec["version"]


def readback(context, name, spec):
    target = destination(context, name, spec)
    no_symlink_parents(target / RECEIPT, Path(context["home"]))
    if target.is_symlink() or not target.is_dir() or not (target / RECEIPT).is_file():
        raise Error(
            f"verification tool {name} unavailable/unowned; run the repository verification provision command: "
            "python3 -I -B native/verification/verify.py provision --context CONTEXT.json --yes"
        )
    receipt = load(target / RECEIPT)
    if receipt.get("selection") != spec or receipt.get("platform") != context["platform"]:
        raise Error(
            f"verification provenance mismatch: {name}; preserve foreign state and inspect {target}"
        )
    actual = inventory(target)
    actual.pop(RECEIPT, None)
    if actual != receipt.get("files"):
        raise Error(
            f"verification payload changed: {name}; inspect/recover the owned internal prefix {target}"
        )
    if "bin" in spec:
        return native(inside(target, spec["bin"], existing=True))
    return str(target)


def publish_exclusive(stage, target):
    """Atomically publish a complete tree without replacing even an empty foreign directory."""
    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (os.fsencode(stage), os.fsencode(target), 0x00000004)  # RENAME_EXCL
    elif sys.platform == "linux":
        rename = library.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        arguments = (
            -100,
            os.fsencode(stage),
            -100,
            os.fsencode(target),
            1,
        )  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise Error("exclusive verification publication requires a supported native host")
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


def provision(context):
    """Explicit acquisition only. Reuses engine download/extract/ownership primitives."""
    context = host_context(context)
    if context.get("_verification_only"):
        selected_home = Path(context["home"]).resolve()
        real_home = Path.home().resolve()
        if selected_home == real_home or real_home.is_relative_to(selected_home):
            raise Error("verification tool acquisition requires a disposable home")
    state = Path(context["paths"]["state"]) / "verification"
    no_symlink_parents(state / "operation.lock", Path(context["home"]))
    cache = Path(context["paths"]["cache"]) / "verification"
    no_symlink_parents(cache / "downloads", Path(context["home"]))
    result = {}
    with locked(state):
        for name, spec in tool_specs(context).items():
            target = destination(context, name, spec)
            no_symlink_parents(target / RECEIPT, Path(context["home"]))
            if target.exists() or target.is_symlink():
                result[name] = readback(context, name, spec)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=".verification-", dir=target.parent
            ) as temporary:
                stage = Path(temporary) / "tree"
                stage.mkdir()
                for source in spec["sources"]:
                    archive = download(source["url"], source["sha256"], cache / "downloads")
                    unpacked = Path(temporary) / "unpacked"
                    extract(archive, unpacked, source["archive"])
                    if source.get("members"):
                        paths = [
                            inside(unpacked, name, existing=True) for name in source["members"]
                        ]
                    else:
                        paths = unpacked.rglob("*")
                    for path in paths:
                        if path.is_symlink():
                            raise Error("verification artifacts must not contain symlinks")
                        output = stage / path.relative_to(unpacked)
                        if path.is_dir():
                            output.mkdir(parents=True, exist_ok=True)
                        else:
                            if output.exists():
                                raise Error(
                                    f"verification assembly collision: {output.relative_to(stage)}"
                                )
                            output.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(path, output)
                    shutil.rmtree(unpacked)
                if "bin" in spec:
                    native(inside(stage, spec["bin"], existing=True))
                receipt = {
                    "schema_version": 1,
                    "platform": context["platform"],
                    "selection": spec,
                    "files": inventory(stage),
                }
                atomic_json(stage / RECEIPT, receipt)
                publish_exclusive(stage, target)
            result[name] = readback(context, name, spec)
    return result


def system_tool(context, name):
    if name == "sh":
        value = "/bin/sh"
    else:
        package = next(p for p in host_prerequisites(context) if p["id"] == name)
        relative = package["executables"][name]
        prefix = (
            Path("/usr")
            if package["provider"] == "apt"
            else Path("/opt/homebrew/opt") / package["name"]
        )
        value = str(prefix / relative)
    if context.get("_fixture_context"):
        value = context.get("_verification_executables", {}).get(name, value)
    try:
        return native(value)
    except Error as exc:
        raise Error(
            f"native {name} prerequisite unavailable; run ./bootstrap provision --yes "
            f"with the declared {name} provider enabled (verification never installs)"
        ) from exc


def environment(context, home):
    path = "/usr/bin:/bin:/usr/sbin:/sbin"
    if context["platform"] == "macos-arm64":
        path = (
            "/opt/homebrew/opt/bash/bin:/opt/homebrew/opt/zsh/bin:/opt/homebrew/opt/git/bin:" + path
        )
    result = {
        "HOME": str(home),
        "PATH": path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "DOTFILES_BOOTSTRAP_SOURCE": str(BASE),
        "MISE_AUTO_INSTALL": "0",
        "MISE_NO_ENV": "1",
        "MISE_NO_HOOKS": "1",
        "XDG_CONFIG_HOME": str(Path(home) / "config"),
        "XDG_CACHE_HOME": str(Path(home) / "cache"),
        "XDG_STATE_HOME": str(Path(home) / "state"),
        "XDG_DATA_HOME": str(Path(home) / "data"),
    }
    result.update(context.get("_verification_test_tools", {}))
    test_tools = context.get("_verification_test_tools", {})
    if test_tools:
        result["PATH"] = (
            ":".join(dict.fromkeys(str(Path(value).parent) for value in test_tools.values()))
            + ":"
            + result["PATH"]
        )
    return result


def invoke(context, argv, *, cwd=None, quiet=False, report=False):
    native(argv[0])
    with tempfile.TemporaryDirectory(prefix="dotfiles-verify-home-") as temporary:
        result = subprocess.run(
            [str(x) for x in argv],
            cwd=cwd or context["root"],
            env=environment(context, temporary),
            capture_output=True,
            text=True,
            check=False,
        )
    if report and not quiet:
        # Behavioral fixtures are synthetic; scanner output never uses this route.
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode:
        # Secret scanners' stdout/stderr never enter logs, exceptions or reports.
        detail = "output suppressed" if quiet else (result.stdout + result.stderr).strip()
        raise Error(
            f"{Path(argv[0]).name} exited {result.returncode}" + (f": {detail}" if detail else "")
        )
    return result.stdout


def file_union(context):
    git = system_tool(context, "git")
    output = invoke(
        context, [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--"]
    )
    return sorted(set(output.rstrip("\0").split("\0"))) if output else []


def authored_file(value, scope):
    path = Path(value)
    if path.name in scope["format_excluded_names"]:
        return False
    if any(part in scope["format_excluded_components"] for part in path.parts):
        return False
    if any(fnmatch.fnmatchcase(value, pattern) for pattern in scope["format_excluded_globs"]):
        return False
    if path.suffix == ".json":
        return any(fnmatch.fnmatchcase(value, pattern) for pattern in scope["format_json_globs"])
    return True


def selected_files(context, files, *, formatting=False):
    root = Path(context["root"]).resolve()
    scope = load(LOCK)["scope"]
    result = []
    for value in sorted(set(files)):
        path = inside(root, value)
        parts = Path(value).parts
        if any(p in scope["excluded_components"] for p in parts):
            continue
        if formatting and not authored_file(value, scope):
            continue
        if path.is_symlink():
            raise Error(f"verification refuses source symlink: {value}")
        if not path.exists():
            continue  # Deleted index entries have no current worktree content.
        if not path.is_file():
            raise Error(f"verification expects regular source files: {value}")
        result.append(path)
    return result


def shell_kind(path):
    if path.suffix == ".tmpl":
        return None  # Go-template input is not executable shell source.
    if path.name in (".zshenv", ".zshrc", ".zprofile", ".zlogin"):
        return "zsh"
    if path.suffix in (".sh", ".bash"):
        return "bash"
    if path.suffix == ".zsh":
        return "zsh"
    with path.open("rb") as stream:
        line = stream.readline(256)
    if line.startswith(b"#!"):
        text = line.decode("ascii", errors="ignore")
        for name in ("zsh", "bash", "sh"):
            if any(token.rsplit("/", 1)[-1] == name for token in text[2:].split()):
                return name
    return None


def canonical_json(path):
    return json.dumps(load(path), ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def schema_worker(library, root, files):
    # Separate interpreter prevents dependency imports leaking into caller/test state.
    sys.path.insert(0, library)
    from jsonschema import Draft202012Validator
    from referencing import Registry

    bindings = load(LOCK)["scope"]["schema_bindings"]
    root = Path(root)
    for name in files:
        value = load(root / name)
        if name.endswith(".schema.json") or Path(name).name == "schema.json":
            Draft202012Validator.check_schema(value)
        for binding in bindings:
            if fnmatch.fnmatchcase(name, binding["glob"]):
                schema = load(BASE / binding["schema"])
                Draft202012Validator.check_schema(schema)
                validator = Draft202012Validator(schema, registry=Registry())
                errors = list(validator.iter_errors(value))
                if errors:
                    # Avoid embedding instance values (possibly secret-bearing JSON).
                    raise Error(
                        f"schema validation failed: {name}; keyword={errors[0].validator}; "
                        f"path={'/'.join(map(str, errors[0].absolute_path))}"
                    )
    return 0


def unit_worker(root, library=None):
    if library is not None:
        os.environ["DOTFILES_VERIFICATION_SCHEMA_LIBRARY"] = library
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for selection in load(LOCK)["scope"]["test_suites"]:
        directory = Path(root) / selection["directory"]
        if directory.is_dir():
            suite.addTests(
                loader.discover(
                    str(directory), pattern=selection["pattern"], top_level_dir=str(directory)
                )
            )
            # Different un-packaged test directories intentionally have independent tops.
            loader = unittest.TestLoader()
    if not suite.countTestCases():
        raise Error("verification discovered zero behavioral tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def fixture_tools(context, tools):
    """Qualify test-only tools from repository receipts, never inherited paths."""
    from providers import artifact_check, prefix

    verified = {
        "DOTFILES_AI_NATIVE_NODE": tools["node"],
        "DOTFILES_TEST_PYTHON": context["executables"]["python3"],
    }
    pins = load(BASE / "native/engine/pins.json")
    for name, variable in (("chezmoi", "DOTFILES_TEST_CHEZMOI"), ("mise", "DOTFILES_TEST_MISE")):
        pin = pins[name]
        spec = pin["platforms"][context["platform"]]
        package = {
            "id": "bootstrap/" + name,
            "version": pin["version"],
            "platforms": {context["platform"]: spec},
        }
        no_symlink_parents(
            prefix(context, package) / ".dotfiles-artifact.json", Path(context["home"])
        )
        artifact_check(context, package)
        binary = spec["bin"]
        if isinstance(binary, dict):
            binary = binary[name]
        verified[variable] = native(inside(prefix(context, package), binary, existing=True))
    for name in context.get("verification", {}).get("native_packages", []):
        verified["DOTFILES_TEST_" + name.upper()] = system_tool(context, name)
    context["_verification_test_tools"] = verified


def rendered_shell_checks(context, tools):
    """Check actual template consumers, never feed Go-template input to shells."""
    import configuration

    fixtures = context["_verification_test_tools"]
    for platform_name in manifest.PLATFORMS:
        with tempfile.TemporaryDirectory(prefix="dotfiles-render-check-") as temporary:
            view = manifest.compose(
                context["base"], context["root"], Path(temporary) / "home", platform_name
            )
            view["executables"]["chezmoi"] = fixtures["DOTFILES_TEST_CHEZMOI"]
            staged = configuration.stage(view)
            for path in (staged / "home").rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                kind = shell_kind(path)
                if kind is None:
                    continue
                invoke(
                    context,
                    [system_tool(context, kind), *(["-f"] if kind == "zsh" else []), "-n", path],
                )
                if kind != "zsh":
                    invoke(context, [tools["shellcheck"], "--shell", kind, path])


def check(context, files=None, run_tests=True):
    context = host_context(context)
    results = []

    def stage(name, function):
        try:
            function()
        except (Error, OSError, ValueError, KeyError) as exc:
            results.append({"name": name, "status": "failed", "detail": str(exc)})
        else:
            results.append({"name": name, "status": "passed", "detail": ""})

    paths = selected_files(context, file_union(context) if files is None else files)
    formatted = selected_files(
        context, [str(p.relative_to(context["root"])) for p in paths], formatting=True
    )
    specs = tool_specs(context)
    tools = {}
    for name, spec in specs.items():
        stage(
            f"prerequisite:{name}",
            lambda name=name, spec=spec: tools.update({name: readback(context, name, spec)}),
        )

    def python_style():
        python = [p for p in formatted if p.suffix == ".py"]
        if python:
            if "ruff" not in tools:
                raise Error("Ruff unavailable; run explicit verification provision")
            invoke(context, [tools["ruff"], "format", "--config", CONFIG, "--check", "--", *python])

    stage("python-format", python_style)

    def json_style():
        for path in formatted:
            if path.suffix == ".json" and path.read_text() != canonical_json(path):
                raise Error(
                    f"JSON format differs: {path.relative_to(context['root'])}; run repository format with explicit file union"
                )

    stage("json-format", json_style)

    def json_syntax():
        for path in paths:
            if path.suffix == ".json":
                # Read-only syntax validation includes source locks and upstream JSON.
                # Canonical serialization rejects NaN/Infinity without writing inputs.
                canonical_json(path)

    stage("json-syntax", json_syntax)

    def schemas():
        if "schema" not in tools:
            raise Error("schema validator unavailable; run explicit verification provision")
        names = [str(p.relative_to(context["root"])) for p in formatted if p.suffix == ".json"]
        invoke(
            context,
            [
                sys.executable,
                "-I",
                "-B",
                __file__,
                "_schema",
                tools["schema"],
                context["root"],
                *names,
            ],
        )

    stage("json-schema", schemas)
    for path in formatted:
        kind = shell_kind(path)
        if kind:
            stage(
                f"shell-syntax:{path.relative_to(context['root'])}",
                lambda path=path, kind=kind: invoke(
                    context,
                    [
                        system_tool(context, kind),
                        *(["-f"] if kind == "zsh" else []),
                        "-n",
                        str(path),
                    ],
                ),
            )
            if kind != "zsh":

                def shellcheck(path=path, kind=kind):
                    if "shellcheck" not in tools:
                        raise Error("ShellCheck unavailable; run explicit verification provision")
                    invoke(
                        context, [tools["shellcheck"], "--norc", "--shell", kind, "--", str(path)]
                    )

                stage(f"shellcheck:{path.relative_to(context['root'])}", shellcheck)

    def secrets():
        if "gitleaks" not in tools:
            raise Error("gitleaks unavailable; run explicit verification provision")
        with tempfile.TemporaryDirectory(prefix="dotfiles-secret-scan-") as temporary:
            for source in paths:
                target = Path(temporary) / source.relative_to(context["root"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            with tempfile.NamedTemporaryFile(prefix="dotfiles-empty-ignore-") as ignore:
                invoke(
                    context,
                    [
                        tools["gitleaks"],
                        "dir",
                        "--no-banner",
                        "--no-color",
                        "--redact=100",
                        "--config",
                        BASE / "native/verification/gitleaks.toml",
                        "--gitleaks-ignore-path",
                        ignore.name,
                        "--exit-code=1",
                        temporary,
                    ],
                    quiet=True,
                )

    stage("gitleaks", secrets)
    stage("native-fixture-prerequisites", lambda: fixture_tools(context, tools))
    fixtures_ready = results[-1]["status"] == "passed"
    if fixtures_ready:
        stage("rendered-shells", lambda: rendered_shell_checks(context, tools))
    if run_tests and fixtures_ready:
        stage(
            "unit-tests",
            lambda: invoke(
                context,
                [
                    sys.executable,
                    "-I",
                    "-B",
                    __file__,
                    "_unit",
                    context["root"],
                    *([tools["schema"]] if "schema" in tools else []),
                ],
                report=True,
            ),
        )
    return results


def format_files(context, files):
    if files is None:
        raise Error("format requires one caller-supplied explicit file union")
    context = host_context(context)
    # Lock the existing source directory, not a file in real HOME or a new checkout artifact.
    import fcntl

    descriptor = os.open(context["root"], os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Error("another formatter holds the source union lock") from exc
        paths = selected_files(context, files, formatting=True)
        python = [p for p in paths if p.suffix == ".py"]
        # Validate every JSON before starting writes, never alter encrypted YAML.
        json_outputs = {p: canonical_json(p) for p in paths if p.suffix == ".json"}
        if python:
            spec = tool_specs(context)["ruff"]
            ruff = readback(context, "ruff", spec)
            invoke(context, [ruff, "format", "--config", CONFIG, "--", *python])
        for path, text in json_outputs.items():
            if path.read_text() != text:
                from safety import atomic_bytes

                atomic_bytes(path, text.encode(), mode=path.stat().st_mode & 0o777)
        return [
            {"name": "format", "status": "passed", "detail": f"{len(paths)} selected source files"}
        ]
    finally:
        os.close(descriptor)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_schema":
        return schema_worker(argv[1], argv[2], argv[3:])
    if argv and argv[0] == "_unit":
        return unit_worker(argv[1], argv[2] if len(argv) > 2 else None)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("provision", "check", "format"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--files-from", help="JSON list of repository-relative source files")
    parser.add_argument("--no-tests", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    context = load(args.context)
    files = load(args.files_from) if args.files_from else None
    if files is not None and (
        not isinstance(files, list) or any(not isinstance(x, str) for x in files)
    ):
        raise Error("--files-from must contain a JSON list of relative file names")
    if args.action == "provision":
        if not args.yes:
            raise Error("verification tool acquisition requires explicit --yes")
        print(json.dumps(provision(context), indent=2))
        return 0
    results = (
        format_files(context, files)
        if args.action == "format"
        else check(context, files, not args.no_tests)
    )
    print(json.dumps(results, indent=2))
    return int(any(item["status"] == "failed" for item in results))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Error, OSError, ValueError, KeyError) as exc:
        print(f"verification: {exc}", file=sys.stderr)
        raise SystemExit(2)
