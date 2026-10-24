"""Real ELF consumers distinguish private SDK and stock Noble bzip2 SONAMEs."""

import ctypes.util
import importlib.util
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

PUBLIC = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "desktop_linkage_builder", PUBLIC / "native/recipes/desktop/desktop_build.py"
)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
NATIVE_ELF = (
    sys.platform == "linux"
    and platform.machine() in ("x86_64", "amd64")
    and all(shutil.which(p) for p in ("cc", "readelf", "ldd"))
)


@unittest.skipUnless(NATIVE_ELF, "native Linux x86-64 C/ELF toolchain required")
class PrivateLibraryResolution(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="desktop-linkage-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.prefix = self.root / "opt/dotfiles/dux/fixture"
        (self.prefix / "bin").mkdir(parents=True)
        (self.prefix / "qt/lib").mkdir(parents=True)
        self.source = self.root / "consumer.c"
        self.source.write_text(
            "extern const char *BZ2_bzlibVersion(void);\nint main(void) { return BZ2_bzlibVersion()[0] == 0; }\n"
        )

    def test_stock_soname_remains_a_working_host_dependency(self):
        executable = self.prefix / "bin/host-consumer"
        subprocess.run(
            [
                "cc",
                str(self.source),
                "-Wl,-l:libbz2.so.1.0",
                "-Wl,-rpath,$ORIGIN/../qt/lib",
                "-o",
                str(executable),
            ],
            check=True,
            capture_output=True,
        )
        observed = subprocess.run([str(executable)], check=False, capture_output=True)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        checked = builder.inspect_elf(executable, runtime=True)
        self.assertIn("libbz2.so.1.0 =>", checked["linkage"])

    def test_sdk_soname_resolving_outside_private_prefix_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        library_source = self.root / "private.c"
        library_source.write_text('const char *BZ2_bzlibVersion(void) { return "fixture"; }\n')
        library = outside / "libbz2.so.1"
        subprocess.run(
            [
                "cc",
                "-shared",
                "-fPIC",
                "-Wl,-soname,libbz2.so.1",
                str(library_source),
                "-o",
                str(library),
            ],
            check=True,
            capture_output=True,
        )
        executable = self.prefix / "bin/sdk-consumer"
        subprocess.run(
            [
                "cc",
                str(self.source),
                str(library),
                "-Wl,-rpath,$ORIGIN/../../../../../outside",
                "-o",
                str(executable),
            ],
            check=True,
            capture_output=True,
        )
        observed = subprocess.run([str(executable)], check=False, capture_output=True)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        with self.assertRaisesRegex(ValueError, "Private library escaped"):
            builder.inspect_elf(executable, runtime=True)

    def test_shared_plugin_does_not_reclassify_system_gtk_transitive_imports(self):
        gtk = ctypes.util.find_library("gtk-3")
        if not gtk:
            self.skipTest("native GTK3 runtime required")
        source = self.root / "gtk-plugin.c"
        source.write_text(
            "extern void gtk_init(void *, void *);\nvoid plugin(void) { gtk_init(0, 0); }\n"
        )
        plugin = self.prefix / "qt/plugins/platformthemes/fixture.so"
        plugin.parent.mkdir(parents=True)
        subprocess.run(
            [
                "cc",
                "-shared",
                "-fPIC",
                str(source),
                "-Wl,-l:" + gtk,
                "-Wl,-rpath,$ORIGIN/../../lib",
                "-o",
                str(plugin),
            ],
            check=True,
            capture_output=True,
        )
        checked = builder.inspect_elf(plugin, runtime=True)
        self.assertIn("libwayland-client.so.0 =>", checked["linkage"])

    @unittest.skipUnless(
        shutil.which("patchelf") and shutil.which("gcc"), "native ELF packaging tools required"
    )
    def test_bzip2_probe_does_not_leave_the_packaged_library_mapped(self):
        source = self.root / "bzip-source"
        source.mkdir()
        (source / "bzlib_private.h").write_text('#define BZ_VERSION "1.0.8"\n')
        for name in ("blocksort", "huffman", "crctable", "randtable", "compress", "decompress"):
            (source / (name + ".c")).write_text("/* Minimal ABI fixture translation unit. */\n")
        (source / "bzlib.c").write_text(
            "extern int getpid(void);\n"
            'const char *BZ2_bzlibVersion(void) { return getpid() < 0 ? "" : "1.0.8, fixture"; }\n'
        )
        script = (
            "import pathlib,sys;sys.path.insert(0,"
            + repr(str(PUBLIC / "native/recipes/desktop"))
            + ");"
            "import desktop_build as b;"
            "root=pathlib.Path(sys.argv[1]);prefix=pathlib.Path(sys.argv[2]);"
            "b.private_bzip2(root/'bzip-source',prefix/'qt',"
            "{'bzip2_version':'1.0.8','bzip2_soname':'libbz2.so.1'},root,b.environment());"
            "b.patch_runtime(prefix);print('packaging survived native API validation')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(self.root), str(self.prefix)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        library = self.prefix / "qt/lib/libbz2.so.1.0.8"
        checked = builder.inspect_elf(library, runtime=True)
        self.assertIn("(RUNPATH)", checked["dynamic"])


if __name__ == "__main__":
    unittest.main()
