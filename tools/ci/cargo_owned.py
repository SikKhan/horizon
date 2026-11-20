#!/usr/bin/env python3
"""Whether only Cargo wrote into build folders while the harnesses ran (.github/workflows/parity-hosted.yml).

    python3 tools/ci/cargo_owned.py snapshot STATE FOLDER...   # before the harnesses
    python3 tools/ci/cargo_owned.py check STATE FOLDER...      # after them: exit 1 when anything else wrote there

Hosted parity caches its build folders in the clear: a differential check (2026-11-20) found nothing of the game's
data in them. That holds only while the harnesses leave nothing of theirs there, so main saves a build folder only
when every file added or changed since the snapshot is one of Cargo's: inside a profile's deps/, build/,
.fingerprint/, incremental/ or examples/, a dep-info file (.d), a Cargo lock file, or an artifact Cargo copied up
beside its dep-info file. check prints the paths it refuses (build paths, never file contents).
"""
import json
import os
import sys
from pathlib import Path

CARGO_FOLDERS = {"deps", "build", ".fingerprint", "incremental", "examples"}
TOP_FILES = {".rustc_info.json", "CACHEDIR.TAG", "source-mtimes-v1.json"}
# What Cargo copies up into a profile folder: executables (no extension) and libraries.
ARTIFACT_EXTENSIONS = {"", "rlib", "rmeta", "so", "a", "dylib", "dll", "exe", "pdb", "lib", "wasm"}


def files(folders):
    found = {}
    for folder in folders:
        for directory, _, names in os.walk(folder):
            for name in names:
                path = os.path.join(directory, name)
                try:
                    stat = os.lstat(path)
                except FileNotFoundError:
                    continue
                found[path] = [stat.st_size, stat.st_mtime_ns]
    return found


def cargos(relative, folder):
    """Whether RELATIVE (a file's path under a build FOLDER: PROFILE/...) is one Cargo writes. Builds for another
    target (TRIPLE/PROFILE/...) are refused: parity's builds have none."""
    parts = relative.parts
    if len(parts) == 1:
        return parts[0] in TOP_FILES
    if len(parts) > 2:
        return parts[1] in CARGO_FOLDERS
    name = parts[1]
    if name.endswith(".d") or name.startswith(".cargo-"):
        return True
    # An artifact Cargo copied up: a binary or library kind, beside its own dep-info file.
    stem, _, extension = name.partition(".")
    return extension in ARTIFACT_EXTENSIONS and (folder / parts[0] / f"{stem}.d").exists()


def main(argv):
    if len(argv) < 3 or argv[0] not in ("snapshot", "check"):
        print(__doc__, file=sys.stderr)
        return 2
    mode, state, folders = argv[0], Path(argv[1]), [Path(folder) for folder in argv[2:]]
    if mode == "snapshot":
        state.write_text(json.dumps(files(folders)), encoding="utf-8")
        return 0
    before = json.loads(state.read_text(encoding="utf-8"))
    refused = []
    for folder in folders:
        for path, seen in files([folder]).items():
            if before.get(path) != seen and not cargos(Path(path).relative_to(folder), folder):
                refused.append(path)
    for path in sorted(refused)[:40]:
        print(f"not Cargo's: {path}")
    print(f"{len(refused)} files written outside Cargo's own layout" if refused else
          "only Cargo's own files were written")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
