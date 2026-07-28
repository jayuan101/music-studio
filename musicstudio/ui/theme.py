"""Application palette and stylesheet.

A single dark theme tuned for long sessions in front of waveforms: low-contrast
background, one accent colour, and colour used only where it carries meaning --
green for lossless, amber for warnings, red for clipping.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

# -- Semantic colours ------------------------------------------------------
BG_DEEP = "#12141a"
BG = "#181b23"
BG_RAISED = "#1f232d"
BG_HOVER = "#262b37"
BORDER = "#2e3440"
TEXT = "#e4e7ee"
TEXT_DIM = "#9aa3b5"
TEXT_FAINT = "#6b7488"

ACCENT = "#5b8dee"
ACCENT_HOVER = "#6f9bf1"
ACCENT_PRESSED = "#4a7ad8"

LOSSLESS = "#4ec9a8"     # bit-perfect
LOSSY = "#c9a24e"        # compressed
WARNING = "#e0a458"
DANGER = "#e06c75"       # clipping, destructive actions
WAVEFORM = "#5b8dee"
WAVEFORM_SEL = "#4ec9a8"


def apply_theme(app) -> None:
    """Apply the palette and stylesheet to a QApplication."""
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BG))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Base, QColor(BG_DEEP))
    palette.setColor(QPalette.AlternateBase, QColor(BG_RAISED))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.Button, QColor(BG_RAISED))
    palette.setColor(QPalette.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipBase, QColor(BG_RAISED))
    palette.setColor(QPalette.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.PlaceholderText, QColor(TEXT_FAINT))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(TEXT_FAINT))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(TEXT_FAINT))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)


STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-size: 13px;
}}

QLabel#Heading {{
    font-size: 20px;
    font-weight: 600;
    color: {TEXT};
}}
QLabel#SubHeading {{
    font-size: 13px;
    color: {TEXT_DIM};
}}
QLabel#SectionLabel {{
    font-size: 11px;
    font-weight: 600;
    color: {TEXT_FAINT};
    text-transform: uppercase;
}}
QLabel#Hint {{
    color: {TEXT_DIM};
    font-size: 12px;
}}
QLabel#Warning {{
    color: {WARNING};
    font-size: 12px;
}}
QLabel#Danger {{
    color: {DANGER};
    font-size: 12px;
}}
QLabel#Good {{
    color: {LOSSLESS};
    font-size: 12px;
}}

/* -- Sidebar ---------------------------------------------------------- */
QListWidget#Sidebar {{
    background: {BG_DEEP};
    border: none;
    border-right: 1px solid {BORDER};
    outline: none;
    padding: 8px 6px;
}}
QListWidget#Sidebar::item {{
    padding: 10px 12px;
    border-radius: 6px;
    color: {TEXT_DIM};
    margin-bottom: 2px;
}}
QListWidget#Sidebar::item:hover {{
    background: {BG_HOVER};
    color: {TEXT};
}}
QListWidget#Sidebar::item:selected {{
    background: {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}

/* -- Cards ------------------------------------------------------------ */
QFrame#Card {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QFrame#Divider {{
    background: {BORDER};
    max-height: 1px;
    border: none;
}}

/* -- Inputs ----------------------------------------------------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {BG_DEEP};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 7px 9px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {TEXT_FAINT};
    background: {BG};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    outline: none;
}}

/* -- Buttons ---------------------------------------------------------- */
QPushButton {{
    background: {BG_HOVER};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px 16px;
    color: {TEXT};
}}
QPushButton:hover  {{ background: #303643; border-color: #3d4553; }}
QPushButton:pressed {{ background: {BG_RAISED}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: {BG}; }}

QPushButton#Primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#Primary:hover   {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton#Primary:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton#Primary:disabled {{ background: #33405c; border-color: #33405c; color: {TEXT_FAINT}; }}

QPushButton#Danger {{ color: {DANGER}; }}
QPushButton#Danger:hover {{ background: #3a2529; border-color: {DANGER}; }}

/* -- Tables ----------------------------------------------------------- */
QTableView, QTreeView {{
    background: {BG_DEEP};
    alternate-background-color: #15181f;
    border: 1px solid {BORDER};
    border-radius: 8px;
    gridline-color: {BORDER};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
    outline: none;
}}
QTableView::item, QTreeView::item {{ padding: 5px 6px; }}
QHeaderView::section {{
    background: {BG_RAISED};
    color: {TEXT_DIM};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 8px 6px;
    font-weight: 600;
    font-size: 12px;
}}
QTableCornerButton::section {{ background: {BG_RAISED}; border: none; }}

/* -- Sliders ---------------------------------------------------------- */
QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT};
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}

/* -- Progress --------------------------------------------------------- */
QProgressBar {{
    background: {BG_DEEP};
    border: 1px solid {BORDER};
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

/* -- Tabs ------------------------------------------------------------- */
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; top: -1px; }}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 9px 16px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom-color: {ACCENT}; font-weight: 600; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

/* -- Misc ------------------------------------------------------------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #39404e; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #4a5364; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #39404e; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
QCheckBox::indicator {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {BG_DEEP};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QRadioButton::indicator {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    background: {BG_DEEP};
}}
QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
    color: {TEXT_DIM};
}}

QSplitter::handle {{ background: {BORDER}; }}
QToolTip {{
    background: {BG_RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 5px;
    border-radius: 4px;
}}
QStatusBar {{ background: {BG_DEEP}; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QMenuBar {{ background: {BG_DEEP}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item:selected {{ background: {BG_HOVER}; }}
QMenu {{ background: {BG_RAISED}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 7px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
"""
