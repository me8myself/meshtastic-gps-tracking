"""Entry script for the packaged executable (PyInstaller needs a plain script)."""

import multiprocessing

from tracker.__main__ import entry

if __name__ == "__main__":
    multiprocessing.freeze_support()
    entry()
