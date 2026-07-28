"""Entry point for the packaged application.

PyInstaller turns whatever script it is given into the top-level ``__main__``
module. Pointing it at ``musicstudio/__main__.py`` therefore strips that file
of its package context and its relative imports fail at runtime. A separate
launcher that imports the package by name avoids the problem entirely, and
keeps ``python -m musicstudio`` working for development.
"""

import multiprocessing
import sys

from musicstudio.app import main

if __name__ == "__main__":
    # Required before anything else on Windows, or a frozen app that spawns a
    # subprocess can relaunch the whole GUI instead.
    multiprocessing.freeze_support()
    sys.exit(main())
