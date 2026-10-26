#!/usr/bin/env python3
"""Copy the library into the Home Assistant custom component.

HACS installs only custom_components/husqvarna_automower_ble/, so the
component carries its own copy of automower_ble with package-relative
imports. Run this after changing the library:

    python tools/sync_component.py          # update the copy
    python tools/sync_component.py --check  # fail if the copy is stale (CI)
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from filecmp import dircmp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "automower_ble"
TARGET = ROOT / "custom_components" / "husqvarna_automower_ble" / "automower_ble"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def build(dest: Path) -> None:
    shutil.copytree(SOURCE, dest, ignore=IGNORE)
    for path in dest.glob("*.py"):
        text = path.read_text()
        text = re.sub(r"^from automower_ble\.", "from .", text, flags=re.MULTILINE)
        path.write_text(text)


def differs(a: Path, b: Path) -> bool:
    cmp = dircmp(a, b, ignore=["__pycache__"])
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return True
    return any(differs(a / d, b / d) for d in cmp.common_dirs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if args.check:
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "automower_ble"
            build(fresh)
            if not TARGET.exists() or differs(fresh, TARGET):
                sys.exit("Component copy is stale: run python tools/sync_component.py")
        print("Component copy is up to date")
        return

    if TARGET.exists():
        shutil.rmtree(TARGET)
    build(TARGET)
    print(f"Updated {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
