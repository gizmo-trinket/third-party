"""Tests for scripts/registry.py. Run: python3 -m unittest discover -s tests"""

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import registry  # noqa: E402

RECIPE = ROOT / "tests/fixtures/recipes/example/1.0.0.toml"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class Entries(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def mirror(self, body: str) -> Path:
        return write(self.tmp / "packages/microsoft/onnxruntime/1.23.2.toml", body)

    GOOD = (
        'license = "MIT"\n'
        'license_files = ["https://example.com/LICENSE"]\n'
        '[[asset]]\nplatform = "win-x64"\nurl = "https://example.com/a.zip"\nsha256 = "' + "a" * 64 + '"\n'
    )

    def test_a_recipe_is_valid(self):
        e = registry.load(str(RECIPE))
        self.assertEqual((e["kind"], e["namespace"], e["name"], e["version"]),
                         ("recipe", "gizmo-trinket", "example", "1.0.0"))

    def test_mirror_identity_comes_from_the_path(self):
        e = registry.load(str(self.mirror(self.GOOD)))
        self.assertEqual((e["kind"], e["namespace"], e["name"], e["version"]),
                         ("mirror", "microsoft", "onnxruntime", "1.23.2"))

    def test_non_permissive_license_needs_a_decision(self):
        bad = self.GOOD.replace('"MIT"', '"LicenseRef-Microsoft-DXIL"')
        with self.assertRaisesRegex(registry.Invalid, "license_decision"):
            registry.load(str(self.mirror(bad)))
        # Top-level keys must come before the first [[asset]] table.
        ok = 'license_decision = "Microsoft permits redistribution of dxil.dll; see PR #1"\n' + bad
        registry.load(str(self.mirror(ok)))

    def test_bad_hash_is_rejected(self):
        with self.assertRaisesRegex(registry.Invalid, "sha256"):
            registry.load(str(self.mirror(self.GOOD.replace("a" * 64, "abc"))))

    def test_plain_http_is_rejected(self):
        with self.assertRaisesRegex(registry.Invalid, "https"):
            registry.load(str(self.mirror(self.GOOD.replace("https://example.com/a.zip", "http://example.com/a.zip"))))

    def test_recipe_must_not_take_upstreams_name(self):
        text = RECIPE.read_text().replace('"gizmo-trinket/example"', '"example/example"')
        p = write(self.tmp / "recipes/example/1.0.0.toml", text)
        write(p.parent / "build.sh", "")
        with self.assertRaisesRegex(registry.Invalid, "upstream's name"):
            registry.load(str(p))

    def test_recipe_needs_a_full_commit(self):
        text = RECIPE.read_text().replace("0123456789abcdef0123456789abcdef01234567", "v1.0.0")
        p = write(self.tmp / "recipes/example/1.0.0.toml", text)
        write(p.parent / "build.sh", "")
        with self.assertRaisesRegex(registry.Invalid, "40-character"):
            registry.load(str(p))


class Commands(unittest.TestCase):
    def run_cli(self, *args) -> str:
        return subprocess.run([sys.executable, str(ROOT / "scripts/registry.py"), *args],
                              check=True, capture_output=True, text=True, cwd=ROOT).stdout

    def test_matrix(self):
        rows = json.loads(self.run_cli("matrix", str(RECIPE.relative_to(ROOT))))
        self.assertEqual(rows, [{"recipe": "tests/fixtures/recipes/example/1.0.0.toml",
                                 "platform": "osx-arm64", "runner": "macos-15"}])

    def test_flags_are_nul_separated_and_end_with_cmake_defines(self):
        flags = self.run_cli("flags", str(RECIPE.relative_to(ROOT)), "osx-arm64").split("\0")[:-1]
        self.assertEqual(flags, ["--build_shared_lib", "--cmake_extra_defines", "A=1", "B=2"])

    def test_get_reads_a_platform_field(self):
        self.assertEqual(self.run_cli("get", str(RECIPE.relative_to(ROOT)), "osx-arm64",
                                      "macos_deployment_target").strip(), "13.4")
        self.assertEqual(self.run_cli("get", str(RECIPE.relative_to(ROOT)), "osx-arm64", "absent"), "")

    def test_stage_keeps_only_listed_files_and_preserves_symlinks(self):
        tmp = Path(tempfile.mkdtemp())
        install, src, out = tmp / "install", tmp / "src", tmp / "out"
        write(install / "include/onnxruntime/onnxruntime_c_api.h", "h")
        write(install / "lib/libexample.1.0.0.dylib", "dylib")
        os.symlink("libexample.1.0.0.dylib", install / "lib/libexample.dylib")
        write(install / "lib/pkgconfig/libexample.pc", "not kept")
        write(src / "LICENSE", "MIT")
        write(src / "ThirdPartyNotices.txt", "notices")
        self.run_cli("stage", str(RECIPE.relative_to(ROOT)), "osx-arm64",
                     "--install", str(install), "--source", str(src), "--out", str(out))
        with tarfile.open(out / "example-1.0.0-osx-arm64.tar.gz") as t:
            names = set(t.getnames())
            link = t.getmember("lib/libexample.dylib")
        self.assertIn("include/onnxruntime/onnxruntime_c_api.h", names)
        self.assertIn("LICENSE", names)
        self.assertIn("ThirdPartyNotices.txt", names)
        self.assertIn("recipe.toml", names)
        self.assertNotIn("lib/pkgconfig/libexample.pc", names)
        self.assertTrue(link.issym())
        self.assertEqual(link.linkname, "libexample.1.0.0.dylib")


class MachO(unittest.TestCase):
    # Trimmed from `otool -l` of the first CI build, which came out minos 15.0.
    LISTING = """
Load command 9
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 15.0
      sdk 15.5
Load command 10
      cmd LC_SOURCE_VERSION
  cmdsize 16
  version 0.0
"""

    def test_minos_is_read_from_build_version_only(self):
        self.assertEqual(registry.minos_of(self.LISTING), ["15.0"])

    def test_older_version_min_command(self):
        self.assertEqual(registry.minos_of("      cmd LC_VERSION_MIN_MACOSX\n  cmdsize 16\n  version 13.4\n      sdk 14.0\n"), ["13.4"])


if __name__ == "__main__":
    unittest.main()
