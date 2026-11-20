"""tools/ci/cargo_owned.py: main saves hosted parity's build folders in the clear only when the harnesses wrote
nothing there but Cargo's own files."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/ci"))
import cargo_owned  # noqa: E402


class CargoOwnedTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.target = self.root / "parity-target"
        self.write("release/deps/libworld-1a2b.rlib")
        self.write("release/world_probe.d")
        self.state = self.root / "state.json"
        self.assertEqual(self.main("snapshot"), 0)

    def write(self, relative, data=b"x"):
        path = self.target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def main(self, mode):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status = cargo_owned.main([mode, str(self.state), str(self.target)])
        self.out = out.getvalue()
        return status

    def test_cargo_builds_pass(self):
        for relative in ("release/deps/libgameplay-9f.rlib", "release/.fingerprint/world-1a/lib-world",
                         "release/build/mimalloc-3c/out/libmimalloc.a", "release/incremental/world-1/s-x/0.o",
                         "release/examples/client_probe", "fast/horizon-client", "fast/horizon-client.d",
                         "fast/libhorizon_world.rlib", "fast/libhorizon_world.d", "release/.cargo-lock",
                         ".rustc_info.json", "CACHEDIR.TAG"):
            self.write(relative)
        self.assertEqual(self.main("check"), 0, self.out)

    def test_a_changed_cargo_file_passes(self):
        path = self.target / "release/world_probe.d"
        path.write_bytes(b"longer dep-info")
        self.assertEqual(self.main("check"), 0, self.out)

    def test_anything_else_is_refused(self):
        for relative in ("release/world.json", "release/saves/level.dat", "oracle.txt",
                         "x86_64-unknown-linux-gnu/release/deps/liba.rlib"):
            with self.subTest(relative):
                self.state.unlink()
                self.assertEqual(self.main("snapshot"), 0)
                path = self.write(relative)
                self.assertEqual(self.main("check"), 1)
                self.assertIn(f"not Cargo's: {path}", self.out)
                os.remove(path)

    def test_files_that_were_already_there_are_not_judged(self):
        self.write("release/old-notes.txt")
        self.state.unlink()
        self.assertEqual(self.main("snapshot"), 0)
        self.assertEqual(self.main("check"), 0, self.out)


if __name__ == "__main__":
    unittest.main()
