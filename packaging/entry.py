"""Entry point for the packaged Windows app (both RedtoothApp.exe and redtooth.exe).

RedtoothApp.exe (no console): no arguments opens the app; with arguments it runs that command quietly.
redtooth.exe (console): the normal command line, for the terminal and the first-run setup window.
"""
import multiprocessing
import sys
from pathlib import Path

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from redtooth.cli import app_main, main

    if Path(sys.executable).stem.lower() == "redtoothapp":
        app_main()
    else:
        main()
