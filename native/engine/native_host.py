"""Explicit native host prerequisites; ordinary bootstrap never calls an installer.

Public APIs: status(context), provision(context, yes=False),
request_clt(context, yes=False), dispatch(context, operation, authorize=False).
Root commands are ``native-host status``, ``native-host provision --yes`` and
``native-host clt-request --yes``. The caller supplies the standard engine context.
Only the last two authorize OS changes. CLT installation/Apple license consent
must be completed by the user before provisioning; we never accept a license,
run softwareupdate, or let Homebrew's installer install developer tools.

The pin authenticates the official *installer source*, not a frozen closure of
Homebrew/formula versions. Its official upstream downloads remain Homebrew's
responsibility. Installer/SDK output stays on local stderr. No output or host
state is published. Existing/shared Homebrew, formulas and data are never
uninstalled, including after failure or dotfiles retirement.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import pwd
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from safety import Error, atomic_json, command, digest, load, locked, native, no_symlink_parents

PREFIX = Path("/opt/homebrew")
CLT = Path("/Library/Developer/CommandLineTools")
SYSTEM_BREW_ENV = Path("/etc/homebrew/brew.env")
PIN_FILE = Path(__file__).with_name("host-pins.json")


def _pins():
    pins = load(PIN_FILE)
    if not isinstance(pins, dict) or not isinstance(pins.get("homebrew"), dict):
        raise Error("invalid official Homebrew installer source pin")
    pin = pins.get("homebrew", {})
    revision = pin.get("revision", "")
    if (
        pins.get("schema") != 1
        or not isinstance(revision, str)
        or not re.fullmatch(r"[0-9a-f]{40}", revision)
        or pin.get("repository") != "https://github.com/Homebrew/install"
        or pin.get("url")
        != f"https://raw.githubusercontent.com/Homebrew/install/{revision}/install.sh"
        or not isinstance(pin.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", pin.get("sha256", ""))
        or not isinstance(pin.get("bytes"), int)
        or not 0 < pin["bytes"] <= 1024 * 1024
        or pin.get("prefix") != "/opt/homebrew"
        or pin.get("platform") != "macos-arm64"
        or pin.get("minimum_macos_major") != 15
        or pin.get("brew_origin") != "https://github.com/Homebrew/brew"
    ):
        raise Error("invalid official Homebrew installer source pin")
    return pin


def _host(context):
    native(sys.executable)
    actual = (platform.system(), platform.machine())
    target = context.get("platform")
    if actual[0] == "Linux" and actual[1] in ("x86_64", "amd64") and target == "linux-x64":
        return "linux-x64"
    if actual != ("Darwin", "arm64") or target != "macos-arm64":
        raise Error(
            "native-host requires the real target host; supported macOS is native Apple Silicon"
        )
    version = platform.mac_ver()[0]
    try:
        supported = int(version.split(".")[0]) >= 15
    except ValueError:
        supported = False
    if not supported:
        raise Error("Homebrew host provisioning requires macOS 15 or later")
    return target


def _environment(context, sandbox=None):
    home = Path(context["home"])
    if not home.is_absolute() or "/nix/" in str(home.resolve()):
        raise Error("native-host requires an absolute native home")
    # Construct, never filter, the environment: no BASH_ENV, loader injection,
    # Homebrew mirrors/prefixes, curl config, developer overrides, project Git
    # config, sudo askpass helpers, proxy credentials or caller PATH survive.
    result = {
        "HOME": str(home),
        "USER": pwd.getpwuid(os.getuid()).pw_name,
        "LOGNAME": pwd.getpwuid(os.getuid()).pw_name,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TERM": "dumb",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOMEBREW_NO_ANALYTICS": "1",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "HOMEBREW_NO_ENV_HINTS": "1",
        "HOMEBREW_NO_INSTALL_CLEANUP": "1",
    }
    if sandbox is not None:
        result.update(
            {
                "CURL_HOME": str(sandbox),
                "XDG_CONFIG_HOME": str(sandbox),
                "GIT_TEMPLATE_DIR": str(sandbox),
            }
        )
    else:
        result.update({"CURL_HOME": "/dev/null", "XDG_CONFIG_HOME": "/dev/null"})
    return result


def _run(context, tool, path, args=(), *, mutating=False, sandbox=None, stream=False):
    executable = command(context, tool, path, mutating=mutating)
    try:
        result = subprocess.run(
            [executable, *map(str, args)],
            cwd="/",
            env=_environment(context, sandbox),
            text=True,
            check=False,
            stdout=sys.stderr if stream else subprocess.PIPE,
            stderr=None if stream else subprocess.PIPE,
        )
    except OSError as exc:
        raise Error(f"native-host could not run {tool}: {exc}") from exc
    if result.returncode:
        # Installer output can contain machine-local paths; never put it in a
        # returned JSON receipt. Streaming installation diagnostics stay local.
        raise Error(f"native-host {tool} failed ({result.returncode}); inspect local diagnostics")
    return result.stdout or ""


def _clt_status(context):
    try:
        selected = _run(context, "xcode-select", "/usr/bin/xcode-select", ["--print-path"]).strip()
    except Error:
        return {"status": "missing", "request": "native-host clt-request --yes"}
    # This exact CLT Git is the pinned installer's own gate before it attempts
    # softwareupdate. Xcode alone is not enough to prevent that side effect.
    git = CLT / "usr/bin/git"
    clang = CLT / "usr/bin/clang"
    if not git.is_file() or not clang.is_file():
        return {
            "status": "missing",
            "selected": selected,
            "request": "native-host clt-request --yes",
        }
    try:
        native(git)
        native(clang)
        no_symlink_parents(git, Path("/"))
        no_symlink_parents(clang, Path("/"))
        developer = Path(selected)
        if not developer.is_absolute() or not developer.is_dir():
            raise Error("invalid selected Apple developer directory")
        compiler = Path(
            _run(context, "xcrun", "/usr/bin/xcrun", ["--no-cache", "--find", "clang"]).strip()
        )
        native(compiler)
        if not compiler.resolve().is_relative_to(developer.resolve()):
            raise Error("compiler escapes the selected Apple developer directory")
        if developer.resolve() != CLT.resolve():
            app = developer.parent.parent
            if (
                developer.name != "Developer"
                or developer.parent.name != "Contents"
                or app.suffix != ".app"
            ):
                raise Error("foreign selected developer directory")
        _run(
            context,
            "pkgutil",
            "/usr/sbin/pkgutil",
            ["--pkg-info", "com.apple.pkg.CLTools_Executables"],
        )
        _run(context, "xcrun", "/usr/bin/xcrun", ["--no-cache", "clang", "--version"])
        sdk = Path(
            _run(
                context,
                "xcrun",
                "/usr/bin/xcrun",
                ["--no-cache", "--sdk", "macosx", "--show-sdk-path"],
            ).strip()
        )
        if (
            not sdk.is_absolute()
            or not sdk.is_dir()
            or not sdk.resolve().is_relative_to(developer.resolve())
        ):
            raise Error("foreign or missing Apple macOS SDK")
    except Error as exc:
        return {"status": "blocked", "selected": selected, "reason": str(exc)}
    return {"status": "ready", "selected": selected, "sdk": str(sdk)}


def _record_path(context):
    path = Path(context["paths"]["state"]) / "native-host.json"
    if not path.is_absolute() or not path.resolve().is_relative_to(Path(context["home"]).resolve()):
        raise Error("native-host state escapes the managing user's home")
    no_symlink_parents(path, Path(context["home"]))
    return path


def _lock_state(context):
    _record_path(context)
    state = Path(context["paths"]["state"]) / "native-host-operation"
    no_symlink_parents(state / "operation.lock", Path(context["home"]))
    return state


def _git(context, args):
    return _run(
        context,
        "git",
        str(CLT / "usr/bin/git"),
        [
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "--git-dir",
            str(PREFIX / ".git"),
            *args,
        ],
    )


def _check_brew_environment():
    # Homebrew loads these even with an empty process environment. User-level
    # brew.env is excluded by our controlled XDG_CONFIG_HOME. Do not source or
    # rewrite machine-local system/prefix overrides to make a check pass.
    for path in (SYSTEM_BREW_ENV, PREFIX / "etc/homebrew/brew.env"):
        if os.path.lexists(path):
            raise Error(f"native-host refuses Homebrew startup overrides: {path}")


def _brew_status(context, pin):
    if not os.path.lexists(PREFIX):
        return {"status": "missing", "prefix": str(PREFIX), "ownership": "none"}
    try:
        _check_brew_environment()
        no_symlink_parents(PREFIX / "bin/brew", Path("/"))
        if PREFIX.is_symlink() or not PREFIX.is_dir():
            raise Error("foreign Homebrew prefix collision")
        for path in (PREFIX / ".git", PREFIX / "Library/Homebrew"):
            no_symlink_parents(path / ".provenance-check", Path("/"))
            if path.is_symlink() or not path.is_dir():
                raise Error("foreign Homebrew repository collision")
        brew = PREFIX / "bin/brew"
        if brew.is_symlink() or not brew.is_file():
            raise Error("foreign Homebrew executable collision")
        native(brew)
        if not (CLT / "usr/bin/git").is_file():
            return {
                "status": "unchecked",
                "prefix": str(PREFIX),
                "reason": "Apple CLT Git is required for read-only Homebrew provenance inspection",
                "retirement": "retain-all",
            }
        native(CLT / "usr/bin/git")
        origin = _git(
            context, ["config", "--local", "--no-includes", "--get", "remote.origin.url"]
        ).strip()
        if origin not in (pin["brew_origin"], pin["brew_origin"] + ".git"):
            raise Error("Homebrew repository is not the official HTTPS origin")
        revision = _git(context, ["rev-parse", "--verify", "HEAD"]).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise Error("invalid Homebrew repository revision")
        # Authenticate the entry script against its recorded upstream checkout
        # before executing it. No hooks, filters, shellenv or external diff run.
        recorded_script = _git(context, ["cat-file", "blob", "HEAD:bin/brew"])
        if brew.read_bytes() != recorded_script.encode():
            raise Error("Homebrew executable differs from its recorded source")
        if _run(context, "brew", str(brew), ["--prefix"]).strip() != str(PREFIX):
            raise Error("Homebrew prefix readback mismatch")
        if _run(context, "brew", str(brew), ["--repository"]).strip() != str(PREFIX):
            raise Error("Homebrew repository readback mismatch")
        version = _run(context, "brew", str(brew), ["--version"]).strip().splitlines()
        if not version or not version[0].startswith("Homebrew "):
            raise Error("Homebrew version readback failed")
        ownership = "preexisting-shared"
        record_path = _record_path(context)
        if record_path.exists():
            record = load(record_path)
            identity = [PREFIX.stat().st_dev, PREFIX.stat().st_ino]
            if record.get("schema") != 1 or record.get("prefix") != str(PREFIX):
                raise Error("invalid native-host ownership receipt")
            if record.get("prefix_identity") != identity:
                raise Error("Homebrew prefix was replaced since host provisioning")
            ownership = "dotfiles-provisioned-shared"
        return {
            "status": "ready",
            "prefix": str(PREFIX),
            "ownership": ownership,
            "origin": origin,
            "revision": revision,
            "version": version[0],
            "executable_sha256": digest(brew),
            "retirement": "retain-all",
        }
    except (Error, OSError, ValueError) as exc:
        return {
            "status": "foreign",
            "prefix": str(PREFIX),
            "reason": str(exc),
            "retirement": "retain-all",
        }


def status(context):
    """Read host/tool provenance without downloading, installing or writing state."""
    pin = _pins()
    if _host(context) == "linux-x64":
        return {"status": "not-required", "platform": "linux-x64", "retirement": "retain-all"}
    brew = _brew_status(context, pin)
    clt = _clt_status(context)
    result = "ready" if brew["status"] == "ready" and clt["status"] == "ready" else "prerequisites"
    return {"status": result, "platform": "macos-arm64", "homebrew": brew, "clt": clt}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Error("Homebrew installer source redirects are forbidden")


def _download_installer(pin, destination):
    # No ambient proxy/mirror/auth settings and no redirect to another source.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(pin["url"], timeout=60) as response:
            if response.geturl() != pin["url"]:
                raise Error("Homebrew installer source changed during download")
            data = response.read(1024 * 1024 + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise Error(f"official Homebrew installer download failed: {exc}") from exc
    if len(data) != pin["bytes"] or hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise Error("official Homebrew installer checksum/size mismatch")
    destination.write_bytes(data)
    destination.chmod(0o600)
    return destination


def _authorize(context, yes):
    if yes is not True:
        raise Error("native-host mutation requires explicit --yes authorization")
    _host(context)
    if context.get("_verification_only") and not context.get("_fixture_context"):
        raise Error("verification refuses live native-host mutation")
    if os.geteuid() == 0:
        raise Error(
            "native-host must run as the managing user, not root; Apple privilege consent is separate"
        )


def provision(context, *, yes=False):
    """Provision only a missing, collision-free standard-prefix Homebrew.

    Apple CLT/SDK must already pass inspection. The official installer can ask
    for its normal privilege authorization; we never invoke sudo ourselves or
    acquire/store a password. Failure retains partial files for inspection and
    never removes a prefix, formula or user data to retry automatically.
    """
    _authorize(context, yes)
    if context["platform"] != "macos-arm64":
        raise Error("Homebrew host provision is only supported on macOS Apple Silicon")
    pin = _pins()
    with locked(_lock_state(context)):
        before = _brew_status(context, pin)
        if before["status"] == "foreign":
            raise Error(f"refusing foreign Homebrew prefix: {before['reason']}")
        if before["status"] == "ready":
            return {"status": "unchanged", "homebrew": before, "retirement": "retain-all"}
        clt = _clt_status(context)
        if clt["status"] != "ready":
            raise Error(
                "Apple CLT/SDK consent and installation are required; use native-host clt-request --yes, complete Apple UI, then rerun status"
            )
        # A missing prefix with an old receipt is not an invitation to replace it.
        if _record_path(context).exists():
            raise Error(
                "recorded Homebrew prefix is missing; inspect the retained receipt before recovery"
            )
        _check_brew_environment()
        with tempfile.TemporaryDirectory(
            prefix="dotfiles-homebrew-", dir=Path("/tmp").resolve()
        ) as directory:
            sandbox = Path(directory)
            source = _download_installer(pin, sandbox / "install.sh")
            # Recheck exact downloaded bytes and collision immediately before exec.
            if source.is_symlink() or digest(source) != pin["sha256"]:
                raise Error("Homebrew installer source changed before execution")
            if os.path.lexists(PREFIX):
                raise Error("Homebrew prefix appeared during acquisition; refusing to overwrite it")
            if _clt_status(context)["status"] != "ready":
                raise Error("Apple developer prerequisites changed before installation")
            _check_brew_environment()
            _run(
                context,
                "bash",
                "/bin/bash",
                ["--noprofile", "--norc", source],
                mutating=True,
                sandbox=sandbox,
                stream=True,
            )
        after = _brew_status(context, pin)
        if after["status"] != "ready":
            raise Error(
                "Homebrew installer did not pass independent provenance/prefix readback; partial prefix retained"
            )
        receipt = {
            "schema": 1,
            "prefix": str(PREFIX),
            "prefix_identity": [PREFIX.stat().st_dev, PREFIX.stat().st_ino],
            "installer": {key: pin[key] for key in ("repository", "revision", "url", "sha256")},
            "installed": after,
            "retirement": "retain-all",
        }
        atomic_json(_record_path(context), receipt)
        after["ownership"] = "dotfiles-provisioned-shared"
        return {"status": "provisioned", "homebrew": after, "retirement": "retain-all"}


def request_clt(context, *, yes=False):
    """Open Apple's installer UI only; never claim the asynchronous install passed."""
    _authorize(context, yes)
    if context["platform"] != "macos-arm64":
        raise Error("Apple CLT request is only supported on macOS Apple Silicon")
    with locked(_lock_state(context)):
        before = _clt_status(context)
        if before["status"] == "ready":
            return {"status": "unchanged", "clt": before}
        _run(
            context,
            "xcode-select",
            "/usr/bin/xcode-select",
            ["--install"],
            mutating=True,
            stream=True,
        )
        return {
            "status": "requested",
            "clt": before,
            "next": "Complete Apple's consent/install UI, then run native-host status; no license was accepted.",
        }


def preserve_on_retirement(context):
    """No automatic Homebrew/CLT uninstall, formula cleanup or data removal."""
    return {
        "status": "retained",
        "prefix": str(PREFIX),
        "homebrew": "shared",
        "formulas": "retained",
        "data": "retained",
        "clt": "retained",
    }


def dispatch(context, operation, authorize=False):
    if operation == "status":
        return status(context)
    if operation == "provision":
        return provision(context, yes=authorize)
    if operation == "clt-request":
        return request_clt(context, yes=authorize)
    raise Error(f"unknown native-host operation: {operation}")
