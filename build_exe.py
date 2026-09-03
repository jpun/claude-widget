"""Build the standalone ClaudeContextWidget.exe.

    python build_exe.py

Produces dist/ClaudeContextWidget.exe and removes the build scratch.

One file on purpose - one thing to copy, nothing to keep together. The cost is
startup: the exe unpacks itself into a temp directory on every launch, about
1.2 s to the window against 0.25 s for a folder build.
"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ICON = HERE / "ctx_widget.ico"
DIST = HERE / "dist"
WORK = HERE / "build"

# comtypes._npsupport imports numpy when it happens to be installed, which
# drags in numpy plus OpenBLAS (~26 MB) for a widget that never touches it.
# The rest is stdlib the widget has no use for.
EXCLUDES = [
    "numpy", "PIL", "matplotlib", "pandas", "scipy",
    "ssl", "_ssl", "email", "http", "urllib.request", "xml",
    "unittest", "doctest", "pydoc", "sqlite3", "lib2to3",
    "multiprocessing", "asyncio", "distutils", "setuptools", "pip",
]


def main() -> None:
    if not ICON.exists():
        subprocess.run([sys.executable, str(HERE / "make_icon.py")], check=True)

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed", "--clean",
        "--name", "ClaudeContextWidget",
        "--icon", str(ICON),
        "--add-data", f"{ICON};.",
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
    ]
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    command.append(str(HERE / "ctx_widget.py"))
    subprocess.run(command, check=True)

    shutil.rmtree(WORK, ignore_errors=True)
    exe = DIST / "ClaudeContextWidget.exe"
    print(f"{exe} ({exe.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
