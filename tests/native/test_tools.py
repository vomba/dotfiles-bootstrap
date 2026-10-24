"""Offline fixture coverage for the real exceptional-tool recipe entry points."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "native_tools", BASE / "native/recipes/tools/tools.py"
)
TOOLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOLS)


class ToolFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.context = {
            "platform": "linux-x64",
            "home": str(self.home),
            "paths": {key: str(self.root / key) for key in ("runtime", "state", "cache", "bin")},
            "executables": {},
        }
        self.output = self.root / "stages"
        self.archives = {}

    def script(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nset -eu\n" + text)
        path.chmod(0o755)
        return path

    def archive(self, name, files):
        path = self.root / (name + ".tgz")
        with tarfile.open(path, "w:gz") as tar:
            for relative, text in files.items():
                data = text.encode()
                member = tarfile.TarInfo(relative)
                member.size = len(data)
                member.mode = (
                    0o755
                    if relative.endswith((".sh", "/helm", "/vals", "/diff"))
                    or relative.split("/")[-1] in TOOLS.TENV_COMMANDS
                    else 0o644
                )
                tar.addfile(member, io.BytesIO(data))
        self.archives[name] = path
        return path

    def fake_fetch(self, source, cache):
        return self.archives[source["fixture"]]

    def helm_fixture(self):
        package, artifacts = TOOLS.metadata(self.context, "helm")
        package = copy.deepcopy(package)
        artifacts = copy.deepcopy(artifacts)
        helm = """#!/bin/sh
set -eu
if [ "$1" = completion ]; then printf '# completion %s\n' "$2"; exit; fi
if [ "$1" = plugin ]; then
  for p in diff secrets secrets-getter secrets-post-renderer; do
    test -f "$HELM_PLUGINS/$p/plugin.yaml" || exit 71
    printf '%s\n' "$p"
  done
  exit
fi
if [ "$1" = secrets ]; then
  shift
  export HELM_PLUGIN_DIR="$HELM_PLUGINS/secrets"
  exec "$HELM_PLUGIN_DIR/scripts/run.sh" "$@"
fi
exit 72
"""
        self.archive("helm", {"linux-amd64/helm": helm})
        self.archive("helm-vals", {"vals": "#!/bin/sh\nprintf 'vals fixture\\n'\n"})
        for plugin, artifact in zip(
            TOOLS.HELM_PLUGINS,
            ("helm-diff", "helm-secrets", "helm-secrets-getter", "helm-secrets-post-renderer"),
        ):
            files = {plugin + "/plugin.yaml": "name: " + plugin + "\nversion: fixture\n"}
            if plugin == "diff":
                files[plugin + "/plugin.yaml"] += (
                    "platformHooks:\n  install:\n    - command: downloader\n"
                )
                files[plugin + "/bin/diff"] = "#!/bin/sh\nexit 0\n"
                files[plugin + "/install-binary.sh"] = "#!/bin/sh\nexit 99\n"
            else:
                files[plugin + "/scripts/run.sh"] = (
                    '#!/bin/sh\nset -eu\n[ "$1" = decrypt ] || exit 73\nexec "$HELM_SECRETS_SOPS_PATH" --decrypt "$2"\n'
                )
            self.archive(artifact, files)
        helper = self.script(
            self.root / "trusted/sops",
            '[ "$1" = --decrypt ] || exit 74\ncase "$(cat "$2")" in\n  ENC:*) sed "s/^ENC://" "$2";;\n  *) exit 29;;\nesac\n',
        )
        for name in ("sops", "age", "curl", "kubectl", "gpg", "gpgconf", "jq", "yq", "bash"):
            self.context["executables"][name] = str(helper if name == "sops" else Path("/bin/sh"))
        for name in package["sources"]:
            artifacts[name]["platforms"]["linux-x64"] = {"fixture": name}
        return package, artifacts

    def tenv_fixture(self):
        package, artifacts = TOOLS.metadata(self.context, "tenv")
        package, artifacts = copy.deepcopy(package), copy.deepcopy(artifacts)
        files = {
            name: '#!/bin/sh\nif [ "${1:-}" = completion ]; then printf "# completion %s\\n" "$2"; else printf "'
            + name
            + '\\n"; fi\n'
            for name in (*TOOLS.TENV_COMMANDS, "terraform")
        }
        files.update(
            {name: name + " fixture\n" for name in ("LICENSE", "README.md", "CHANGELOG.md")}
        )
        self.archive("tenv", files)
        artifacts["tenv"]["platforms"]["linux-x64"] = {"fixture": "tenv"}
        return package, artifacts

    def openstack_fixture(self):
        package, artifacts = TOOLS.metadata(self.context, "openstackclient-full")
        self.global_python_called = self.root / "global-python-called"
        global_python = self.script(
            self.root / "global/python3",
            "touch " + shlex.quote(str(self.global_python_called)) + "\nprintf '3.14\\n'\n",
        )
        self.context["executables"].update(python3=str(global_python), python=str(global_python))
        vpython = self.script(
            self.root / "trusted/venv-python",
            """
[ "$1" = -I ] && [ "$2" = -B ] || exit 81
shift 2
directory=$(dirname "$0")
if [ "$1" = -m ] && [ "$2" = pip ]; then
  case "$3" in
    install)
      for name in """
            + " ".join(package["commands"])
            + """; do
        printf '#!%s/python\\nprint("fixture")\\n' "$directory" > "$directory/$name"
      done
      exit;;
    check) test -f "$directory/openstack"; exit;;
  esac
fi
[ "$1" = "$directory/openstack" ] || exit 82
if [ "${2:-}" = complete ]; then printf '# openstack fixture completion\\n'
else printf 'openstack fixture\\n'; fi
""",
        )
        scoped = self.script(
            self.root / "trusted/python3.12",
            """
[ "$1" = -I ] && [ "$2" = -B ] || exit 83
shift 2
if [ "$1" = -c ]; then printf '3.12\\n'; exit; fi
[ "$1" = -m ] && [ "$2" = venv ] && [ "$3" = --copies ] || exit 84
mkdir -p "$4/bin"
cp """
            + shlex.quote(str(vpython))
            + """ "$4/bin/python"
printf 'home = /trusted/python312\\ncommand = fixture stage\\n' > "$4/pyvenv.cfg"
printf 'stage activation\\n' > "$4/bin/activate"
printf 'stage activation\\n' > "$4/bin/Activate.ps1"
""",
        )
        self.context["executables"]["python3.12"] = str(scoped)
        return package, artifacts

    def provision(self, package, artifacts, output=None):
        with mock.patch.object(TOOLS, "fetch", side_effect=self.fake_fetch):
            return TOOLS.provision(self.context, package, artifacts, output or self.output)

    def test_helm_composes_four_plugins_and_uses_trusted_local_helpers(self):
        package, artifacts = self.helm_fixture()
        record = self.provision(package, artifacts)
        helm = Path(self.context["paths"]["bin"]) / "helm"
        completed = subprocess.run(
            [helm, "plugin", "list"], check=True, text=True, capture_output=True
        )
        self.assertEqual(completed.stdout.splitlines(), list(TOOLS.HELM_PLUGINS))
        encrypted = self.root / "local-values.yaml"
        encrypted.write_text("ENC:answer: synthetic\n")
        hostile = self.root / "hostile"
        self.script(hostile / "sops", "exit 97\n")
        completed = subprocess.run(
            [helm, "secrets", "decrypt", encrypted],
            env=dict(
                os.environ,
                PATH=str(hostile),
                HELM_PLUGINS=str(hostile),
                HELM_SECRETS_SOPS_PATH=str(hostile / "sops"),
            ),
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "answer: synthetic\n")
        encrypted.write_text("not encrypted\n")
        self.assertEqual(
            subprocess.run([helm, "secrets", "decrypt", encrypted], capture_output=True).returncode,
            29,
        )
        runtime = Path(record["runtime"])
        self.assertNotIn("platformHooks:", (runtime / "plugins/diff/plugin.yaml").read_text())
        self.assertFalse((runtime / "plugins/diff/install-binary.sh").exists())
        self.assertEqual(TOOLS.check(self.context, package, artifacts)["status"], "verified")
        self.assertNotIn("/nix/", helm.read_text())
        self.assertNotIn(str(self.output), helm.read_text())

    def test_missing_helm_plugin_never_promotes_partial_assembly(self):
        package, artifacts = self.helm_fixture()
        self.archive("helm-secrets-getter", {"secrets-getter/README": "missing plugin metadata"})
        with self.assertRaises(TOOLS.ToolError):
            self.provision(package, artifacts)
        final, _, record = TOOLS.layout(self.context, package)
        self.assertFalse(final.exists())
        self.assertFalse(record.exists())
        self.assertFalse((Path(self.context["paths"]["bin"]) / "helm").exists())

    def test_tenv_exposes_every_nonterraform_command_and_native_completions(self):
        package, artifacts = self.tenv_fixture()
        record = self.provision(package, artifacts)
        runtime = Path(record["runtime"])
        self.assertEqual(set(p.name for p in (runtime / "bin").iterdir()), set(TOOLS.TENV_COMMANDS))
        self.assertFalse((runtime / "bin/terraform").exists())
        self.assertFalse((Path(self.context["paths"]["bin"]) / "terraform").exists())
        for name in TOOLS.TENV_COMMANDS:
            output = subprocess.check_output([Path(self.context["paths"]["bin"]) / name], text=True)
            self.assertEqual(output.strip(), name)
        for relative in (
            "share/zsh/site-functions/_tenv",
            "share/bash-completion/completions/tenv",
            "share/fish/vendor_completions.d/tenv.fish",
        ):
            self.assertTrue((runtime / relative).read_text().startswith("# completion"))

    def test_foreign_launcher_and_runtime_are_preserved(self):
        package, artifacts = self.tenv_fixture()
        command = Path(self.context["paths"]["bin"]) / "tenv"
        command.parent.mkdir()
        command.write_text("foreign")
        with self.assertRaisesRegex(TOOLS.ToolError, "foreign launcher"):
            self.provision(package, artifacts)
        self.assertEqual(command.read_text(), "foreign")
        command.unlink()
        final, _, _ = TOOLS.layout(self.context, package)
        final.mkdir(parents=True)
        (final / "foreign").write_text("preserve")
        with self.assertRaisesRegex(TOOLS.ToolError, "foreign runtime"):
            self.provision(package, artifacts)
        self.assertEqual((final / "foreign").read_text(), "preserve")

    def test_install_failure_rolls_back_and_retires_only_owned_outputs(self):
        package, artifacts = self.tenv_fixture()
        original = TOOLS.os.replace
        count = [0]

        def failing_replace(source, target):
            if Path(target).parent == Path(self.context["paths"]["bin"]):
                count[0] += 1
                if count[0] == 2:
                    raise OSError("fixture promotion failure")
            return original(source, target)

        with mock.patch.object(TOOLS.os, "replace", side_effect=failing_replace):
            with self.assertRaisesRegex(OSError, "promotion failure"):
                self.provision(package, artifacts)
        final, _, record = TOOLS.layout(self.context, package)
        self.assertFalse(final.exists())
        self.assertFalse(record.exists())
        self.assertFalse((Path(self.context["paths"]["bin"]) / "tenv").exists())
        self.provision(package, artifacts)
        foreign = Path(self.context["paths"]["bin"]) / "unrelated"
        foreign.write_text("preserve")
        TOOLS.retire(self.context, package)
        self.assertEqual(foreign.read_text(), "preserve")
        self.assertFalse(final.exists())
        self.assertFalse(record.exists())

    def test_modified_owned_outputs_block_check_and_retirement(self):
        package, artifacts = self.tenv_fixture()
        record = self.provision(package, artifacts)
        (Path(record["runtime"]) / "bin/tenv").write_text("foreign replacement")
        with self.assertRaisesRegex(TOOLS.ToolError, "modified"):
            TOOLS.check(self.context, package, artifacts)
        with self.assertRaisesRegex(TOOLS.ToolError, "modified"):
            TOOLS.retire(self.context, package)
        self.assertTrue((Path(self.context["paths"]["bin"]) / "tenv").is_symlink())

    def test_bad_checksum_never_promotes_cache_entry(self):
        data = b"corrupt fixture"
        source = {"url": "https://invalid.example/fixture", "sha256": "0" * 64}
        response = io.BytesIO(data)
        response.url = source["url"]
        with mock.patch.object(TOOLS.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(TOOLS.ToolError, "checksum"):
                TOOLS.fetch(source, self.root / "download-cache")
        self.assertEqual(list((self.root / "download-cache").iterdir()), [])

    def test_archive_path_and_symlink_escape_are_rejected(self):
        for member_name, kind in (("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            archive = self.root / "unsafe.tar"
            with tarfile.open(archive, "w") as tar:
                info = tarfile.TarInfo(member_name)
                info.type = kind
                info.linkname = "/etc/passwd"
                tar.addfile(info, io.BytesIO(b""))
            with self.assertRaises(TOOLS.ToolError):
                TOOLS.unpack(archive, self.root / ("extract-" + str(kind)))
        self.assertFalse((self.root / "escape").exists())

    def test_complete_openstack_plugin_inventory_and_hash_lock_constraints(self):
        package, _ = TOOLS.metadata(self.context, "openstackclient-full")
        lock, requirements = TOOLS.python_lock(self.context, package)
        expected = {
            "aodhclient",
            "osc-placement",
            "python-barbicanclient",
            "python-designateclient",
            "python-heatclient",
            "python-ironicclient",
            "python-magnumclient",
            "python-manilaclient",
            "python-mistralclient",
            "python-neutronclient",
            "python-octaviaclient",
            "python-watcherclient",
            "python-zaqarclient",
            "python-zunclient",
        }
        self.assertTrue(expected.issubset(lock["roots"]))
        self.assertTrue(expected.issubset(lock["distributions"]))
        self.assertEqual(lock["python"], "3.12")
        self.assertEqual(
            {line.split("==", 1)[0] for line in requirements.read_text().splitlines()},
            set(lock["distributions"]),
        )
        for record in lock["distributions"].values():
            self.assertEqual(set(record["platforms"]), {"linux-x64", "macos-arm64"})
            for artifact in record["platforms"].values():
                self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(artifact["filename"].endswith(".whl"))
                self.assertTrue(artifact["url"].startswith("https://files.pythonhosted.org/"))
        bad = copy.deepcopy(package)
        bad["python_lock"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(TOOLS.ToolError, "digest mismatch"):
            TOOLS.python_lock(self.context, bad)

    def test_openstack_rejects_incomplete_graph_and_incoherent_hash_requirements(self):
        package, _ = TOOLS.metadata(self.context, "openstackclient-full")
        lock, requirements = TOOLS.python_lock(self.context, package)
        for failure in ("missing-plugin", "missing-requirement", "changed-hash", "wrong-abi"):
            with self.subTest(failure=failure):
                candidate = copy.deepcopy(lock)
                lines = requirements.read_text().splitlines()
                if failure == "missing-plugin":
                    del candidate["distributions"]["python-magnumclient"]
                    lines = [line for line in lines if not line.startswith("python-magnumclient==")]
                elif failure == "missing-requirement":
                    lines = lines[1:]
                elif failure == "changed-hash":
                    candidate["distributions"]["python-magnumclient"]["platforms"]["linux-x64"][
                        "sha256"
                    ] = "0" * 64
                else:
                    candidate["python"] = "3.14"
                directory = self.root / failure
                directory.mkdir()
                lock_path, requirements_path = (
                    directory / "lock.json",
                    directory / "requirements.txt",
                )
                lock_path.write_text(json.dumps(candidate))
                requirements_path.write_text("\n".join(lines) + "\n")
                scoped = copy.deepcopy(package)
                scoped["_origin"] = str(directory)
                scoped["python_lock"] = {
                    "path": lock_path.name,
                    "requirements": requirements_path.name,
                    "sha256": TOOLS.digest(lock_path),
                    "requirements_sha256": TOOLS.digest(requirements_path),
                }
                with self.assertRaises(TOOLS.ToolError):
                    TOOLS.python_lock(self.context, scoped)

    def test_openstack_uses_scoped_python312_with_global_python314(self):
        package, artifacts = self.openstack_fixture()
        self.assertEqual(package["requires"], ["python312"])
        wheel = self.root / "fixture.whl"
        wheel.write_bytes(b"offline wheel fixture")
        with mock.patch.object(TOOLS, "fetch", return_value=wheel) as fetch:
            record = TOOLS.provision(self.context, package, artifacts, self.output)
        lock, _ = TOOLS.python_lock(self.context, package)
        self.assertCountEqual(
            [call.args[0] for call in fetch.call_args_list],
            [
                entry["platforms"][self.context["platform"]]
                for entry in lock["distributions"].values()
            ],
        )
        self.assertFalse(self.global_python_called.exists())
        runtime = Path(record["runtime"])
        self.assertEqual({p.name for p in (runtime / "bin").iterdir()}, set(package["commands"]))
        self.assertEqual(
            subprocess.check_output([Path(self.context["paths"]["bin"]) / "openstack"], text=True),
            "openstack fixture\n",
        )
        self.assertEqual(
            (runtime / "share/bash-completion/completions/openstack").read_text(),
            "# openstack fixture completion\n",
        )
        self.assertFalse((runtime / "wheelhouse").exists())
        self.assertFalse((runtime / "venv/bin/activate").exists())
        self.assertFalse((runtime / "venv/bin/Activate.ps1").exists())
        self.assertNotIn("command =", (runtime / "venv/pyvenv.cfg").read_text())
        for name in package["commands"]:
            self.assertTrue(
                (runtime / "venv/bin" / name)
                .read_text()
                .startswith("#!" + str(runtime / "venv/bin/python") + "\n")
            )
        self.assertEqual(TOOLS.check(self.context, package, artifacts)["status"], "verified")

    def test_openstack_missing_or_wrong_scoped_python_never_uses_global(self):
        package, artifacts = self.openstack_fixture()
        for value, error in (
            (None, "missing trusted executable: python3.12"),
            (str(self.root / "absent/python3.12"), "missing executable:"),
            (self.context["executables"]["python3"], "requires native Python 3.12"),
        ):
            with self.subTest(scoped=value):
                if value is None:
                    self.context["executables"].pop("python3.12", None)
                else:
                    self.context["executables"]["python3.12"] = value
                with mock.patch.object(TOOLS, "fetch") as fetch:
                    with self.assertRaisesRegex(TOOLS.ToolError, error):
                        TOOLS.build(self.context, package, artifacts, self.output)
                fetch.assert_not_called()
                self.assertFalse((self.output / package["id"]).exists())

    def test_openstack_fingerprint_tracks_only_scoped_interpreter(self):
        package, artifacts = self.openstack_fixture()
        identity = TOOLS.fingerprint(self.context, package, artifacts)
        self.context["executables"].update(
            python3="/new/global/python3", python="/new/global/python"
        )
        self.assertEqual(TOOLS.fingerprint(self.context, package, artifacts), identity)
        self.context["executables"]["python3.12"] = "/new/scoped/python3.12"
        self.assertNotEqual(TOOLS.fingerprint(self.context, package, artifacts), identity)

    def test_nonpython_assemblies_do_not_enable_global_python(self):
        for name in ("helm", "openstack-rust", "tenv"):
            with self.subTest(package=name):
                package, _ = TOOLS.metadata(self.context, name)
                self.assertNotIn("python", package.get("requires", []))
                self.assertNotIn("python312", package.get("requires", []))

    def test_rust_openstack_assembles_distinct_cli_and_tui(self):
        package, artifacts = TOOLS.metadata(self.context, "openstack-rust")
        package, artifacts = copy.deepcopy(package), copy.deepcopy(artifacts)
        for artifact, command in (("openstack-cli", "osc"), ("openstack-tui", "ostui")):
            self.archive(artifact, {command: "#!/bin/sh\nprintf '" + command + "\\n'\n"})
            artifacts[artifact]["platforms"]["linux-x64"] = {"fixture": artifact}
        record = self.provision(package, artifacts)
        runtime = Path(record["runtime"])
        self.assertEqual({path.name for path in (runtime / "bin").iterdir()}, {"osc", "ostui"})
        for command in ("osc", "ostui"):
            launcher = Path(self.context["paths"]["bin"]) / command
            self.assertEqual(os.readlink(launcher), str(runtime / "bin" / command))
            self.assertEqual(subprocess.check_output([launcher], text=True), command + "\n")
        self.assertEqual(TOOLS.check(self.context, package, artifacts)["status"], "verified")

    def test_concurrent_mutation_is_refused(self):
        state = Path(self.context["paths"]["state"])
        with TOOLS.operation_lock(state):
            with self.assertRaises(BlockingIOError):
                with TOOLS.operation_lock(state):
                    self.fail("second operation acquired lock")

    def test_failed_local_completion_propagates_without_installation(self):
        package, artifacts = self.tenv_fixture()
        self.archive(
            "tenv",
            {
                **{name: "#!/bin/sh\nexit 38\n" for name in (*TOOLS.TENV_COMMANDS, "terraform")},
                **{name: "fixture" for name in ("LICENSE", "README.md", "CHANGELOG.md")},
            },
        )
        with self.assertRaisesRegex(TOOLS.ToolError, "exit 38"):
            self.provision(package, artifacts)
        final, _, record = TOOLS.layout(self.context, package)
        self.assertFalse(final.exists())
        self.assertFalse(record.exists())

    def test_updates_retain_known_owned_runtime_and_retire_both_versions(self):
        package, artifacts = self.tenv_fixture()
        first = self.provision(package, artifacts)
        updated = dict(package, version=package["version"] + "-assembly.2")
        second = self.provision(updated, artifacts, self.root / "second-stage")
        self.assertEqual(second["retained"][0]["runtime"], first["runtime"])
        self.assertTrue(Path(first["runtime"]).is_dir())
        self.assertEqual(
            os.readlink(Path(self.context["paths"]["bin"]) / "tenv"),
            str(Path(second["runtime"]) / "bin/tenv"),
        )
        TOOLS.retire(self.context, updated)
        self.assertFalse(Path(first["runtime"]).exists())
        self.assertFalse(Path(second["runtime"]).exists())

    def test_interrupted_install_rolls_back_only_recorded_candidate(self):
        package, artifacts = self.tenv_fixture()
        record = self.provision(package, artifacts)
        _, state, record_path = TOOLS.layout(self.context, package)
        record_path.unlink()
        TOOLS.atomic_json(state / "tenv.journal.json", {"previous": None, "candidate": record})
        with self.assertRaisesRegex(TOOLS.ToolError, "interrupted"):
            TOOLS.check(self.context, package, artifacts)
        with TOOLS.operation_lock(state):
            TOOLS.recover_install(self.context, package)
        self.assertFalse(Path(record["runtime"]).exists())
        self.assertFalse((Path(self.context["paths"]["bin"]) / "tenv").exists())
        self.assertFalse((state / "tenv.journal.json").exists())

    def test_foreign_replacement_blocks_retirement_without_touching_other_links(self):
        package, artifacts = self.tenv_fixture()
        record = self.provision(package, artifacts)
        command = Path(self.context["paths"]["bin"]) / "tofu"
        command.unlink()
        command.write_text("foreign replacement")
        with self.assertRaisesRegex(TOOLS.ToolError, "foreign"):
            TOOLS.retire(self.context, package)
        self.assertEqual(command.read_text(), "foreign replacement")
        self.assertTrue((Path(self.context["paths"]["bin"]) / "tenv").is_symlink())
        self.assertTrue(Path(record["runtime"]).is_dir())

    def test_mutated_build_stage_is_not_installed(self):
        package, artifacts = self.tenv_fixture()
        with mock.patch.object(TOOLS, "fetch", side_effect=self.fake_fetch):
            stage = TOOLS.build(self.context, package, artifacts, self.output)
        (stage / "root/bin/tenv").write_text("mutated stage")
        with self.assertRaisesRegex(TOOLS.ToolError, "modified build"):
            self.provision(package, artifacts)
        self.assertFalse(TOOLS.layout(self.context, package)[0].exists())

    def test_native_tool_path_rejects_store_and_relative_executables(self):
        for path in ("python3", "/nix/store/synthetic/bin/python3"):
            with self.assertRaisesRegex(TOOLS.ToolError, "native executable"):
                TOOLS.native_path(path)

    def test_partial_plugin_family_update_is_rejected(self):
        package, artifacts = self.helm_fixture()
        artifacts["helm-secrets-getter"]["version"] = "999.0.0"
        context = dict(self.context, packages=[package], artifacts=artifacts)
        with self.assertRaisesRegex(TOOLS.ToolError, "plugin family"):
            TOOLS.metadata(context, "helm")

    def test_primary_source_bump_requires_matching_assembly_version(self):
        package, artifacts = self.tenv_fixture()
        artifacts["tenv"]["version"] = "999.0.0"
        context = dict(self.context, packages=[package], artifacts=artifacts)
        with self.assertRaisesRegex(TOOLS.ToolError, "update coherently"):
            TOOLS.metadata(context, "tenv")

    def test_cli_emits_one_json_result_with_real_file_digests(self):
        package, artifacts = self.tenv_fixture()
        context = dict(self.context, packages=[package], artifacts=artifacts)
        context_path = self.root / "context.json"
        context_path.write_text(json.dumps(context))
        output = io.StringIO()
        with (
            mock.patch.object(TOOLS, "fetch", side_effect=self.fake_fetch),
            contextlib.redirect_stdout(output),
        ):
            status = TOOLS.main(
                [
                    "provision",
                    "--id",
                    "tenv",
                    "--context",
                    str(context_path),
                    "--output",
                    str(self.output),
                ]
            )
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(set(result["executables"]), set(TOOLS.TENV_COMMANDS))
        self.assertTrue(result["artifacts"])
        for artifact in result["artifacts"]:
            path = Path(artifact["path"])
            self.assertTrue(path.is_absolute())
            self.assertTrue(path.is_file())
            self.assertFalse(path.is_symlink())
            self.assertEqual(artifact["sha256"], TOOLS.digest(path))


if __name__ == "__main__":
    unittest.main()
