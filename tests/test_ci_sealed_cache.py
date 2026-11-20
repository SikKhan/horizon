"""tools/ci/sealed-cache.sh: what a cache entry holds of the vanilla data is encrypted and authenticated, and an entry
that fails its check is discarded with a warning (the run goes on cold), never decrypted and never a failure."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/ci/sealed-cache.sh"
NEEDED = ("bash", "openssl", "zstd", "tar")


@unittest.skipUnless(sys.platform == "linux" and Path("/usr/bin/python3").exists()
                     and all(shutil.which(tool) for tool in NEEDED), "needs Linux with " + ", ".join(NEEDED))
class SealedCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.workspace, self.temp = self.root / "work", self.root / "temp"
        self.folder = self.workspace / ".local/parity-runner"
        (self.folder / "oracle-cache").mkdir(parents=True)
        self.temp.mkdir()
        self.file = self.folder / "oracle-cache/out.txt"
        self.file.write_bytes(b"oracle output\n")
        # A nanosecond date: the oracle shim replays a run only while its outputs keep theirs.
        os.utime(self.file, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
        self.archive = self.temp / "sealed/oracle/archive"

    def run_script(self, *arguments, passphrase="correct horse"):
        env = {**os.environ, "GITHUB_WORKSPACE": str(self.workspace), "RUNNER_TEMP": str(self.temp),
               "VANILLA_DATA_PASSPHRASE": passphrase}
        return subprocess.run(["bash", str(SCRIPT), *map(str, arguments)], env=env, capture_output=True, text=True)

    def seal(self):
        result = self.run_script("seal", "oracle", self.archive, self.folder)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def unseal(self, label="oracle", passphrase="correct horse"):
        stage = self.temp / "stage"
        result = self.run_script("unseal", label, self.archive, stage, passphrase=passphrase)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return stage, result.stdout

    def assert_discarded(self, why, **unseal):
        stage, out = self.unseal(**unseal)
        self.assertFalse(stage.exists())
        self.assertIn(f"::warning::oracle cache entry discarded ({why})", out)

    def test_a_sealed_folder_unseals_into_the_stage_with_its_dates(self):
        self.seal()
        self.assertNotIn(b"oracle output", self.archive.read_bytes())
        stage, out = self.unseal()
        copy = stage / str(self.file).lstrip("/")
        self.assertEqual(copy.read_bytes(), b"oracle output\n")
        self.assertEqual(copy.stat().st_mtime_ns, self.file.stat().st_mtime_ns)
        self.assertIn("unsealed oracle", out)
        self.assertNotIn(str(self.file), out)

    def test_a_changed_byte_is_discarded_before_decrypting(self):
        self.seal()
        data = bytearray(self.archive.read_bytes())
        data[len(data) // 2] ^= 1
        self.archive.write_bytes(bytes(data))
        self.assert_discarded("its authentication tag does not match")

    def test_an_entry_sealed_under_another_label_is_discarded(self):
        self.seal()
        stage = self.temp / "stage"
        result = self.run_script("unseal", "prepared", self.archive, stage)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stage.exists())
        self.assertIn("::warning::prepared cache entry discarded (its authentication tag does not match)", result.stdout)

    def test_an_entry_without_its_tag_is_discarded(self):
        self.seal()
        Path(f"{self.archive}.mac").unlink()
        self.assert_discarded("no authentication tag")

    def test_another_passphrase_is_discarded(self):
        self.seal()
        self.assert_discarded("its authentication tag does not match", passphrase="another")

    def test_no_entry_is_a_cold_start(self):
        stage, out = self.unseal()
        self.assertFalse(stage.exists())
        self.assertIn("no oracle cache entry", out)

    def test_an_excluded_folder_is_never_sealed_nor_placed(self):
        # The oracle shim's runtime memo: JDK file hashes guarded by stat data of one machine (tools/parity_shim.py).
        memo = self.folder / "runtime-memo"
        (memo / "jdk").mkdir(parents=True)
        (memo / "jdk/hashes.json").write_bytes(b"{}")
        result = self.run_script("seal", "oracle", self.archive, "--exclude", memo, self.folder)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stage, _ = self.unseal()
        self.assertTrue((stage / str(self.file).lstrip("/")).exists())
        self.assertFalse((stage / str(memo).lstrip("/")).exists())
        self.assertTrue(memo.exists())

    def test_an_entry_holding_an_excluded_path_has_it_removed_on_unsealing(self):
        memo = self.folder / "runtime-memo"
        memo.mkdir()
        (memo / "x.json").write_bytes(b"{}")
        self.seal()
        stage = self.temp / "stage"
        result = self.run_script("unseal", "oracle", self.archive, "--exclude", memo, stage)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((stage / str(self.file).lstrip("/")).exists())
        self.assertFalse((stage / str(memo).lstrip("/")).exists())
        self.assertIn("held an excluded path", result.stdout)

    def test_only_folders_under_the_checkout_or_runner_temp_are_sealed(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        result = self.run_script("seal", "oracle", self.archive, outside)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.archive.exists())


if __name__ == "__main__":
    unittest.main()
