# PyInstaller spec: builds a single self-contained MeshTracker executable.
#   pip install pyinstaller && pyinstaller packaging/MeshTracker.spec
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

hiddenimports = (
    collect_submodules("tracker")
    + collect_submodules("uvicorn")  # uvicorn picks its loop/protocol modules at runtime
    + collect_submodules(
        "meshtastic",
        filter=lambda m: not m.startswith(("meshtastic.tests", "meshtastic.analysis", "meshtastic.powermon")),
    )
    + collect_submodules("pubsub")
)
if sys.platform == "win32":
    hiddenimports += collect_submodules("bleak.backends.winrt")

datas = [(os.path.join(ROOT, "tracker", "static"), os.path.join("tracker", "static"))]
datas += collect_data_files("meshtastic")

a = Analysis(
    [os.path.join(SPECPATH, "meshtracker.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "pandas", "numpy", "IPython", "pytest",
              "meshtastic.tests", "meshtastic.analysis", "meshtastic.powermon"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="MeshTracker",
    console=True,  # the console window shows status; closing it stops the tracker
    upx=False,
)
