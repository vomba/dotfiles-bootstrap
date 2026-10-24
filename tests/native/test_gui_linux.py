"""Offline consumer behavior; disposable providers never touch host apps/profiles.

These fixtures exercise the production recipe, archives, ownership and native
command contracts. They do not qualify actual GUI/sandbox/keyring functionality.
"""

import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socketserver
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("linux_gui_recipe", BASE / "native/gui/linux.py")
GUI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUI)
import manifest


PROVIDER = r"""
import json,os,pathlib,shutil,sys
root=pathlib.Path(__file__).parent
state=root/'provider.json'
db=json.loads(state.read_text())
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
db['calls'].append([name,*args])
def save():state.write_text(json.dumps(db))
def end(code=0,text=''):
 save()
 print(text,end='')
 raise SystemExit(code)
if name=='ps':end(text=db.get('processes',''))
if name=='systemctl':end(0 if db.get('socket_active',True) else 3)
if name=='ldd':end(text=db.get('linkage','libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6\n'))
if name=='dpkg-query':
 if args[0]=='-S':
  for pkg,p in db['installed'].items():
   if args[-1] in p.get('owned',[]):end(text=pkg+': '+args[-1]+'\n')
  end(1)
 pkg=args[-1]
 if pkg in db.get('missing',[]):end(1)
 if '${Version}' in args[1]:
  if pkg not in db['installed']:end(1)
  end(text='installed\t'+db['installed'][pkg]['version'])
 end(text='installed')
if name=='dpkg-deb':
 payload=pathlib.Path(args[-2])
 control=(payload/'DEBIAN/control').read_text()
 pathlib.Path(args[-1]).write_text(control)
 end()
if name=='apt-get':
 if args[0]=='--simulate':
  pkg=args[-1]
  end(text='Remv '+pkg+' ['+db['installed'][pkg]['version']+']\n'+db.get('foreign_apt_transition',''))
 if args[0]=='install':
  stage=pathlib.Path(args[-1]).parent
  rec=json.loads((stage/'stage.json').read_text());s=rec['source']
  owned=[]
  for rel in rec['details']['payload']:
   src=stage/'payload'/rel;dst=pathlib.Path(db['sysroot'])/rel
   dst.parent.mkdir(parents=True,exist_ok=True)
   if dst.exists() or dst.is_symlink():dst.unlink()
   if src.is_symlink():dst.symlink_to(os.readlink(src))
   else:shutil.copy2(src,dst)
   owned.append(str(dst))
  db['installed'][s['package']]={'version':s['installed_version'],'owned':owned}
  if db.get('corrupt_install'):
   dst=pathlib.Path(db['sysroot'])/s['executable'].lstrip('/')
   dst.write_bytes(b'corrupt');dst.chmod(0o755)
  end()
 if args[0]=='remove':
  pkg=args[-1]
  for file in db['installed'][pkg]['owned']:pathlib.Path(file).unlink(missing_ok=True)
  del db['installed'][pkg]
  end()
if name=='unsquashfs':
 s=db['snap_source']
 end(text='name: chromium\nversion: '+s['version']+'\nconfinement: strict\nbase: core24\nplugs:\n  desktop:\n    default-provider: gnome-46-2404\n')
if name=='snap':
 s=db['snap_source'];verb=args[0]
 if verb=='download':
  target=pathlib.Path(next(x.split('=',1)[1] for x in args if x.startswith('--basename=')))
  shutil.copyfile(db['snap_file'],str(target)+'.snap')
  pathlib.Path(str(target)+'.assert').write_text('type: snap-revision\nsnap-id: '+s['snap_id']+'\nsnap-revision: '+str(s['revision'])+'\nfixture-signature\n')
  end()
 if verb=='list':
  if args[1]!='chromium':end(text=args[1]+' 1 1 latest/stable canonical -\n')
  end(0 if db.get('snap') else 1,text='chromium\n' if db.get('snap') else '')
 if verb=='ack':end(1 if db.get('deny_assertion') else 0)
 if verb in ('install','refresh') and '--hold=forever' not in args:
  if verb=='refresh':
   assert '--revision='+str(s['revision']) in args
   assert '--channel=latest/stable' in args
  db['snap']={'id':s['snap_id'],'version':s['version'],'revision':str(s['revision']),
              'confinement':'strict','status':'active','hold':'','tracking-channel':'latest/stable'}
  snap=pathlib.Path(db['sysroot'])/'var/lib/snapd/snaps'/('chromium_'+str(s['revision'])+'.snap')
  snap.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(db['snap_file'],snap)
  exe=pathlib.Path(db['sysroot'])/'snap/bin/chromium';exe.parent.mkdir(parents=True,exist_ok=True)
  exe.write_text('#!/bin/sh\nexit 0\n');exe.chmod(0o755)
  end()
 if verb=='refresh' and '--hold=forever' in args:
  assert args[-1]=='chromium'
  db['snap']['hold']='forever';end()
 if verb=='connect':db.setdefault('connections',[]).append(args[1]);end()
 if verb=='connections':
  lines=['Interface Plug Slot Notes']
  for plug in db.get('connections',[]):lines.append(plug.split(':')[1]+' '+plug+' :'+plug.split(':')[1]+' manual')
  end(text='\n'.join(lines)+'\n')
 if verb=='switch':end()
 if verb=='disable':db['snap']['status']='installed';end()
 if verb=='enable':db['snap']['status']='active';end()
end(89,text='unsupported fake provider invocation '+name+' '+repr(args))
"""


class GuiLinuxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.root = self.home / "fixture"
        self.root.mkdir()
        self.sources = json.loads((BASE / "native/gui/linux-sources.json").read_text())
        self.packages = []
        for path in (BASE / "native/manifests").glob("*.json"):
            for package in json.loads(path.read_text()).get("packages", []):
                if "linux-x64" in package["platforms"]:
                    self.packages.append(copy.deepcopy(package))
        self.ctx = {
            "base": str(self.root),
            "root": str(self.root),
            "platform": "linux-x64",
            "home": str(self.home),
            "dotfiles": {"desktop": {"enable": True}},
            "paths": {
                key: str(self.home / key)
                for key in ("config", "data", "cache", "state", "runtime", "bin")
            },
            "executables": {},
            "_fixture_context": True,
            "_fixture_system_root": str(self.home / "system"),
            "packages": self.packages,
        }
        (self.root / "native/gui").mkdir(parents=True)
        (self.home / "system").mkdir()
        self.db = {"calls": [], "installed": {}, "sysroot": self.ctx["_fixture_system_root"]}
        self.save_db()
        for name in GUI.TOOLS:
            path = self.root / name
            path.write_text("#!" + sys.executable + "\n" + PROVIDER)
            path.chmod(0o755)
            self.ctx["executables"][name] = str(path)
        self.artifact_files = {}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(GUI, "BASE", self.root).start()
        mock.patch.object(GUI, "download", side_effect=self.fetch).start()
        self.flush_sources()

    def save_db(self):
        (self.root / "provider.json").write_text(json.dumps(self.db))

    def read_db(self):
        self.db = json.loads((self.root / "provider.json").read_text())
        return self.db

    def flush_sources(self):
        (self.root / "native/gui/linux-sources.json").write_text(json.dumps(self.sources))

    def fetch(self, url, sha, cache):
        path = self.artifact_files[url]
        if GUI.digest(path) != sha:
            raise GUI.Error("fixture checksum mismatch")
        return path

    def tar(self, files):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
            for name, content in files.items():
                member = tarfile.TarInfo(name)
                member.mode = (
                    0o755
                    if name.startswith("bin/")
                    or name.endswith(("/slack", "/capacities", "chrome-sandbox"))
                    else 0o644
                )
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        return buffer.getvalue()

    def fixture(self, package_id):
        source = self.sources[package_id]
        artifact = self.root / (package_id + ".asset")
        if package_id == "gui-kitty":
            files = {
                "bin/kitty": b"\x7fELFkitty",
                "bin/kitten": b"\x7fELFkitten",
                "share/icons/hicolor/256x256/apps/kitty.png": b"fixture icon",
                "lib/libsqlite3.so.0": b"\x7fELFbundled sqlite",
            }
            for name in ("kitty.desktop", "kitty-open.desktop"):
                files["share/applications/" + name] = (
                    b"[Desktop Entry]\nType=Application\nExec=kitty +open %U\nTryExec=kitty\nIcon=kitty\n"
                )
            artifact.write_bytes(self.tar(files))
        elif source["format"] == "deb":
            control = (
                "Package: "
                + source["package"]
                + "\nVersion: "
                + source["version"]
                + "\nArchitecture: amd64\n"
            ).encode()
            files = {source["executable"].lstrip("/"): b"\x7fELFapp"}
            source["script_owned_paths"] = []
            source.pop("publisher_sha512", None)
            if package_id == "gui-slack":
                cron = b"#!/bin/sh\nexit 99\n"
                source["remove_cron"]["sha256"] = __import__("hashlib").sha256(cron).hexdigest()
                files[source["remove_cron"]["path"]] = cron
                files["usr/lib/slack/chrome-sandbox"] = b"\x7fELFsandbox"
            members = {
                "debian-binary": b"2.0\n",
                "control.tar.xz": self.tar({"control": control}),
                "data.tar.xz": self.tar(files),
            }
            data = bytearray(b"!<arch>\n")
            for name, content in members.items():
                data.extend(
                    f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}`\n".encode()
                )
                data.extend(content)
                if len(content) % 2:
                    data.extend(b"\n")
            artifact.write_bytes(data)
        elif source["format"] == "snap":
            artifact.write_bytes(b"hsqs fixture snap data")
            import hashlib

            source.update(
                sha3_384=hashlib.sha3_384(artifact.read_bytes()).hexdigest(),
                size=artifact.stat().st_size,
            )
            self.db.update(snap_source=source, snap_file=str(artifact))
            self.save_db()
        else:
            artifact.write_bytes(b"\x7fELFstatic tw")
            license_path = self.root / "LICENSE"
            license_path.write_bytes(b"MIT fixture notice\n")
            source["license_source"]["sha256"] = GUI.digest(license_path)
            self.artifact_files[source["license_source"]["url"]] = license_path
        source["sha256"] = GUI.digest(artifact)
        self.artifact_files[source["url"]] = artifact
        self.flush_sources()
        return source

    def execute(self, action, package_id):
        return GUI.execute(self.ctx, action, package_id)

    def snap_server(self):
        state = self.root / "provider.json"

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.rfile.readline()
                while self.rfile.readline().strip():
                    pass
                db = json.loads(state.read_text())
                data = (
                    {"status-code": 200, "result": db["snap"]}
                    if db.get("snap")
                    else {"status-code": 404}
                )
                content = json.dumps(data).encode()
                self.wfile.write(
                    b"HTTP/1.0 200 OK\r\nContent-Length: "
                    + str(len(content)).encode()
                    + b"\r\n\r\n"
                    + content
                )

        server = socketserver.UnixStreamServer(str(self.home / "snapd.socket"), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def test_independent_desktop_flag_and_complete_existing_owners(self):
        context = manifest.compose(
            BASE,
            BASE,
            self.home,
            "linux-x64",
            flags=("desktop.enable=true", "desktop.niri.enable=false", "apps.enable=false"),
        )
        selected = {p["id"]: p for p in context["packages"]}
        for name in (
            *GUI.IDS,
            "jetbrains-mono-nf",
            "desktop-grim",
            "desktop-slurp",
            "gui-nautilus",
            "gui-wdisplays",
        ):
            self.assertIn(name, selected)
        for command in (
            "kitty",
            "tw",
            "chromium",
            "capacities",
            "nautilus",
            "grim",
            "slurp",
            "wdisplays",
            "slack",
        ):
            self.assertIn(command, context["executables"])
        disabled = manifest.compose(
            BASE, BASE, self.home, "linux-x64", flags=("desktop.enable=false",)
        )
        self.assertFalse(GUI.IDS.intersection(p["id"] for p in disabled["packages"]))
        self.ctx["packages"] = []
        with self.assertRaisesRegex(GUI.Error, "not enabled"):
            self.execute("provision", "gui-kitty")
        self.assertEqual(self.read_db()["calls"], [])

    def test_user_runtime_commands_check_and_owned_retirement(self):
        for package_id, command in (("gui-kitty", "kitty"), ("gui-tabiew", "tw")):
            with self.subTest(package_id=package_id):
                self.fixture(package_id)
                installed = self.execute("provision", package_id)
                executable = Path(installed["executables"][command])
                self.assertTrue(executable.is_file())
                self.assertTrue((Path(self.ctx["paths"]["bin"]) / command).is_symlink())
                self.assertEqual(self.execute("check", package_id)["status"], "verified")
                if command == "kitty":
                    desktop = self.home / "applications/kitty.desktop"
                    self.assertIn(str(executable), desktop.read_text())
                self.execute("retire", package_id)
                self.assertFalse(executable.exists())

    def test_foreign_launcher_and_runtime_refused_before_install(self):
        self.fixture("gui-tabiew")
        target = Path(self.ctx["paths"]["bin"]) / "tw"
        target.parent.mkdir()
        target.write_text("foreign tool")
        with self.assertRaisesRegex(GUI.Error, "foreign GUI launcher"):
            self.execute("provision", "gui-tabiew")
        self.assertEqual(target.read_text(), "foreign tool")
        self.assertFalse((Path(self.ctx["paths"]["runtime"]) / "gui-tabiew").exists())
        target.unlink()
        package, source = GUI.selected(self.ctx, "gui-tabiew")
        _, _, runtime = GUI.locations(self.ctx, package)
        runtime.mkdir(parents=True)
        (runtime / "foreign").write_text("keep")
        with self.assertRaisesRegex(GUI.Error, "foreign user runtime"):
            self.execute("provision", "gui-tabiew")
        self.assertEqual((runtime / "foreign").read_text(), "keep")

    def test_each_missing_selected_and_installed_dependency_fails(self):
        for package in [p for p in self.packages if p["id"] in GUI.IDS]:
            self.fixture(package["id"])
            for dependency in package["requires"]:
                with self.subTest(package=package["id"], dependency=dependency):
                    full = self.ctx["packages"]
                    self.ctx["packages"] = [p for p in full if p["id"] != dependency]
                    with self.assertRaisesRegex(GUI.Error, "missing selected dependency"):
                        self.execute("build", package["id"])
                    self.ctx["packages"] = full
                    record = next(p for p in full if p["id"] == dependency)
                    if record["provider"] == "apt":
                        self.read_db()["missing"] = [record["name"]]
                        self.save_db()
                        with self.assertRaisesRegex(GUI.Error, "missing installed dependency"):
                            self.execute("build", package["id"])
                        self.read_db()["missing"] = []
                        self.save_db()

    def test_native_debs_preserve_state_and_remove_no_foreign_files(self):
        for package_id in ("gui-slack", "gui-capacities"):
            self.fixture(package_id)
            profile = (
                self.home
                / ".config"
                / {"gui-slack": "Slack", "gui-capacities": "Capacities"}[package_id]
                / "database"
            )
            profile.parent.mkdir(parents=True)
            profile.write_bytes(b"private fixture state")
            installed = self.execute("provision", package_id)
            self.assertTrue(Path(next(iter(installed["executables"].values()))).is_file())
            if package_id == "gui-slack":
                self.assertFalse((self.home / "system/etc/cron.daily/slack").exists())
                self.assertEqual(
                    (self.home / "system/usr/lib/slack/chrome-sandbox").stat().st_mode & 0o7777,
                    0o4755,
                )
            self.execute("check", package_id)
            self.execute("retire", package_id)
            self.assertEqual(profile.read_bytes(), b"private fixture state")
        calls = self.read_db()["calls"]
        self.assertFalse(any("purge" in call or "autoremove" in call for call in calls))

    def test_native_foreign_path_modified_output_and_running_app_refused(self):
        source = self.fixture("gui-slack")
        target = self.home / "system" / source["executable"].lstrip("/")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"foreign")
        with self.assertRaisesRegex(GUI.Error, "foreign native package path"):
            self.execute("provision", "gui-slack")
        self.assertEqual(target.read_bytes(), b"foreign")
        target.unlink()
        self.db["processes"] = "slack\n"
        self.save_db()
        with self.assertRaisesRegex(GUI.Error, "close the application"):
            self.execute("provision", "gui-slack")
        self.read_db()["processes"] = ""
        self.save_db()
        self.execute("provision", "gui-slack")
        target.write_bytes(b"user replacement")
        with self.assertRaisesRegex(GUI.Error, "foreign/modified"):
            self.execute("retire", "gui-slack")
        self.assertEqual(target.read_bytes(), b"user replacement")

    def test_failed_payload_readback_never_publishes_receipt(self):
        self.fixture("gui-capacities")
        self.db["corrupt_install"] = True
        self.save_db()
        with self.assertRaisesRegex(GUI.Error, "payload readback"):
            self.execute("provision", "gui-capacities")
        state = Path(self.ctx["paths"]["state"]) / "gui-linux"
        self.assertFalse((state / "gui-capacities.json").exists())
        self.assertTrue((state / "gui-capacities.pending.json").exists())

    def test_missing_dynamic_library_and_unsafe_archive_fail_closed(self):
        self.fixture("gui-kitty")
        self.db["linkage"] = "libGL.so.1 => not found\n"
        self.save_db()
        with self.assertRaisesRegex(GUI.Error, "missing/non-native ELF dependency"):
            self.execute("build", "gui-kitty")
        self.read_db()["linkage"] = "statically linked\n"
        self.save_db()
        source = self.sources["gui-kitty"]
        archive = self.artifact_files[source["url"]]
        archive.write_bytes(self.tar({"../escape": b"foreign"}))
        source["sha256"] = GUI.digest(archive)
        self.flush_sources()
        with self.assertRaisesRegex(GUI.Error, "unsafe relative path"):
            self.execute("build", "gui-kitty")
        self.assertFalse((self.home / "escape").exists())

    def test_chromium_assertions_interfaces_hold_disable_and_state_preservation(self):
        self.fixture("gui-chromium")
        self.snap_server()
        profile = self.home / ".config/chromium/Default/Preferences"
        profile.parent.mkdir(parents=True)
        profile.write_bytes(b"synthetic browser profile; never parsed")
        snap_state = self.home / "snap/chromium/common/state"
        snap_state.parent.mkdir(parents=True)
        snap_state.write_bytes(b"synthetic existing snap data")
        installed = self.execute("provision", "gui-chromium")
        self.assertIn("chromium", installed["executables"])
        checked = self.execute("check", "gui-chromium")
        self.assertEqual(
            installed["details"]["installed_version"], self.sources["gui-chromium"]["version"]
        )
        self.assertEqual(checked["details"]["installed_version"], self.read_db()["snap"]["version"])
        self.execute("provision", "gui-chromium")
        self.execute("retire", "gui-chromium")
        self.assertEqual(profile.read_bytes(), b"synthetic browser profile; never parsed")
        self.assertEqual(snap_state.read_bytes(), b"synthetic existing snap data")
        db = self.read_db()
        calls = db["calls"]
        snap_calls = [c[1:] for c in calls if c[0] == "snap"]
        self.assertLess(
            next(i for i, c in enumerate(snap_calls) if c[0] == "ack"),
            next(i for i, c in enumerate(snap_calls) if c[0] == "install"),
        )
        self.assertIn(["refresh", "--hold=forever", "chromium"], snap_calls)
        self.assertIn(["connect", "chromium:password-manager-service"], snap_calls)
        self.assertIn(["connect", "chromium:chromium-config"], snap_calls)
        self.assertFalse(
            any(
                c[0] in ("remove", "forget") or "--dangerous" in c or "--devmode" in c
                for c in snap_calls
            )
        )
        self.assertEqual(db["snap"]["status"], "installed")
        self.assertEqual(db["snap"]["hold"], "forever")
        self.assertTrue(all(c[1] == "is-active" for c in calls if c[0] == "systemctl"))
        self.assertIn("physical_host_gate", installed["details"])

    def test_chromium_inactive_daemon_and_failed_assertion_are_not_bypassed(self):
        self.fixture("gui-chromium")
        self.db["socket_active"] = False
        self.save_db()
        with self.assertRaisesRegex(GUI.Error, "bootstrap services"):
            self.execute("provision", "gui-chromium")
        self.read_db().update(socket_active=True, deny_assertion=True)
        self.save_db()
        with self.assertRaisesRegex(GUI.Error, "command failed"):
            self.execute("provision", "gui-chromium")
        calls = self.read_db()["calls"]
        self.assertFalse(any(c[:2] == ["snap", "install"] for c in calls))
        self.assertFalse(
            (Path(self.ctx["paths"]["state"]) / "gui-linux/gui-chromium.json").exists()
        )

    def test_native_retirement_refuses_foreign_reverse_dependencies(self):
        self.fixture("gui-slack")
        installed = self.execute("provision", "gui-slack")
        self.read_db()["foreign_apt_transition"] = "Remv foreign-application [1.0]\n"
        self.save_db()
        with self.assertRaisesRegex(GUI.Error, "foreign packages"):
            self.execute("retire", "gui-slack")
        self.assertTrue(Path(installed["executables"]["slack"]).exists())
        self.assertFalse(any(c[:2] == ["apt-get", "remove"] for c in self.read_db()["calls"]))

    def test_receipt_cannot_claim_foreign_runtime(self):
        self.fixture("gui-tabiew")
        self.execute("provision", "gui-tabiew")
        receipt = Path(self.ctx["paths"]["state"]) / "gui-linux/gui-tabiew.json"
        record = json.loads(receipt.read_text())
        foreign = self.home / "foreign-application-state"
        foreign.mkdir()
        (foreign / "database").write_bytes(b"preserve this data")
        record.update(runtime=str(foreign), files=GUI.inventory(foreign))
        receipt.write_text(json.dumps(record))
        with self.assertRaisesRegex(GUI.Error, "foreign runtime"):
            self.execute("retire", "gui-tabiew")
        self.assertEqual((foreign / "database").read_bytes(), b"preserve this data")

    def test_chromium_foreign_snap_is_never_adopted_or_disabled(self):
        self.fixture("gui-chromium")
        self.db["snap"] = {"version": "foreign"}
        self.save_db()
        with self.assertRaisesRegex(GUI.Error, "foreign Chromium snap"):
            self.execute("provision", "gui-chromium")
        self.assertFalse(
            any(
                c[:2] in (["snap", "ack"], ["snap", "install"], ["snap", "disable"])
                for c in self.read_db()["calls"]
            )
        )

    def test_installed_version_requires_matching_provider_readback(self):
        self.fixture("gui-chromium")
        self.snap_server()
        self.execute("provision", "gui-chromium")
        receipt = Path(self.ctx["paths"]["state"]) / "gui-linux/gui-chromium.json"
        record = json.loads(receipt.read_text())
        record["installed_version"] = "999.0.0"
        receipt.write_text(json.dumps(record))
        with self.assertRaisesRegex(GUI.Error, "version disagrees"):
            self.execute("check", "gui-chromium")


if __name__ == "__main__":
    unittest.main()
