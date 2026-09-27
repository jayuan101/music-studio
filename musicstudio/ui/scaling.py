"""Interface size ("zoom") picked from the screen, applied before Qt starts.

The layout was designed around ~900 logical pixels of height, with 11-13 px
text. On a 1920x1080 laptop at Windows' 100% scaling that left everything
small and the window (a fixed 1280x820) using two-thirds of the screen.

Qt's QT_SCALE_FACTOR scales *everything* together -- fonts, the stylesheet's
px values, every setFixedWidth -- the same way browser zoom does, so nothing
ends up out of proportion. It is only read when QApplication is created,
which is why this module avoids importing Qt and runs first.

No Qt here on purpose: the screen size comes from Win32 directly.
"""

from __future__ import annotations

import os
import sys

#: Logical screen height (after Windows' own scaling) the layout was designed for.
DESIGN_HEIGHT = 900
#: Auto never shrinks below the design, and stops before text gets oversized.
AUTO_MIN, AUTO_MAX = 1.0, 1.35
#: What a manual choice may be.
MANUAL_MIN, MANUAL_MAX = 0.75, 2.0
#: Offered in Preferences ("auto" plus these percentages).
CHOICES = ("auto", "100", "110", "125", "150", "175")


def logical_screen_height() -> int | None:
    """Primary screen height in logical pixels (physical / Windows scaling).

    Uses the physical resolution (DESKTOPVERTRES, unaffected by the process's
    DPI awareness) and the signed-in user's Windows scaling (AppliedDPI), so
    the answer is the same whether or not the process is DPI-aware yet.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import winreg

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hdc = user32.GetDC(0)
        try:
            physical = gdi32.GetDeviceCaps(hdc, 117)  # DESKTOPVERTRES
        finally:
            user32.ReleaseDC(0, hdc)
        dpi = 96
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Control Panel\Desktop\WindowMetrics") as key:
                dpi = int(winreg.QueryValueEx(key, "AppliedDPI")[0]) or 96
        except OSError:
            pass
        if physical <= 0:
            return None
        return round(physical * 96 / dpi)
    except (OSError, AttributeError, ValueError):
        return None


def auto_factor(logical_height: int | None) -> float:
    """Zoom for a screen: its height over the design height, clamped, in 5% steps."""
    if not logical_height:
        return 1.0
    factor = logical_height / DESIGN_HEIGHT
    factor = min(AUTO_MAX, max(AUTO_MIN, factor))
    return round(factor * 20) / 20


def resolve(choice: str, logical_height: int | None = None) -> float:
    """Turn the ``ui_scale`` setting ("auto" or a percentage) into a factor."""
    choice = (choice or "auto").strip().lower()
    if choice != "auto":
        try:
            percent = float(choice.rstrip("%"))
        except ValueError:
            percent = 0
        if percent:
            return min(MANUAL_MAX, max(MANUAL_MIN, percent / 100))
    if logical_height is None:
        logical_height = logical_screen_height()
    return auto_factor(logical_height)


def apply(choice: str) -> float:
    """Set QT_SCALE_FACTOR from the setting. Must run before QApplication.

    A QT_SCALE_FACTOR already in the environment wins, so it stays a way to
    test other sizes without touching the saved setting.
    """
    if os.environ.get("QT_SCALE_FACTOR"):
        try:
            return float(os.environ["QT_SCALE_FACTOR"])
        except ValueError:
            pass
    factor = resolve(choice)
    os.environ["QT_SCALE_FACTOR"] = f"{factor:.2f}"
    return factor
