"""Application entry point."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from . import APP_NAME, APP_ORG, __version__
from .config import ensure_dirs
from .core import crash_log
from .ui import theme


def create_app(argv: list[str] | None = None) -> QApplication:
    """Build the QApplication with our identity and theme applied."""
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setApplicationVersion(__version__)
    # Fusion renders our stylesheet consistently across Windows versions;
    # the native style overrides several of the colours we set.
    app.setStyle("Fusion")
    theme.apply_theme(app)
    return app


def main(argv: list[str] | None = None) -> int:
    ensure_dirs()
    crash_log.install()
    crash_log.install_qt_message_handler()
    app = create_app(argv)

    from .ui.main_window import MainWindow

    window = MainWindow()
    window.show()

    # Files passed on the command line (or via "Open with") get imported.
    arguments = (argv if argv is not None else sys.argv)[1:]
    if arguments:
        from pathlib import Path

        window.library_panel.import_paths([Path(a) for a in arguments])

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
