"""GhostVault's visual constants and display scaling.

The fonts, colours and spacings of the whole interface, in one place, so that every part of
it uses exactly the same values.

Scaling. Tk sizes fonts in points, so text follows the display's resolution by itself, but
every size given in pixels (window sizes, paddings, wrap lengths, image sizes) does not.
``Scale`` turns the design's pixel sizes (drawn for a 96 dpi display) into the display's
pixels, so that at 125 %, 150 % or 200 % the proportions stay those of the design instead of
the text growing while everything else stays small. macOS already works in points, so
there the factor is always 1.
"""

from __future__ import annotations

import sys
import tkinter as tk

# --- fonts (family, size in points, style), unchanged from the original interface -----------------
FONT_CARD_NAME = ("Arial", 11, "bold")
FONT_EMPTY = ("Arial", 16)
FONT_TITLE = ("Arial", 18, "bold")
FONT_SECTION = ("Arial", 12, "bold")
FONT_COLUMN = ("Arial", 10, "bold")
FONT_STATUS_GLYPH = ("Arial", 13, "bold")
FONT_SMALL = ("Arial", 9)

# --- colours, unchanged ----------------------------------------------------------------------------
TEXT = "#333333"            # body text in Settings
SECONDARY = "gray"          # progress detail, empty-list notes
PENDING = "#a05000"         # a ghost in the middle of an operation
ERROR = "red"               # damaged records, the Reset button
OK = "green"                # a verified repository
TOOLTIP_BG = "#ffffe0"      # the conventional tooltip yellow
TOOLTIP_BORDER = "#808080"

# --- the design's pixel sizes (at 96 dpi) --------------------------------------------------------
MAIN_SIZE = (900, 600)
SETTINGS_SIZE = (980, 650)
MAIN_MIN = (420, 320)
SETTINGS_MIN = (720, 420)
# Kept free around a window that would not fit the screen: its frame and title bar, and a
# taskbar or panel (design pixels, scaled like the rest).
SCREEN_ALLOWANCE = (40, 100)
CARD_PAD = 8                # inside a card
CARD_GAP = 10               # around a card
THUMBNAIL = (200, 150)
TOP_PAD = 10

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
MOD = "Command" if IS_MAC else "Control"          # the shortcut modifier key
MOD_LABEL = "Cmd" if IS_MAC else "Ctrl"           # how shortcuts are written in tooltips


class Scale:
    """Design pixels (96 dpi) to display pixels for one window system."""

    def __init__(self, widget: tk.Misc) -> None:
        self.factor = 1.0
        try:
            if widget.tk.call("tk", "windowingsystem") != "aqua":
                dpi = float(widget.winfo_fpixels("1i"))
                # Quarter steps, as operating systems offer them (100, 125, 150 % ...); never
                # below 1, so a misreported low resolution cannot shrink the design.
                self.factor = min(3.0, max(1.0, round(dpi / 96 * 4) / 4))
        except (tk.TclError, ValueError):
            self.factor = 1.0

    def px(self, value: float) -> int:
        return int(round(value * self.factor))

    def size(self, pair: tuple[int, int]) -> tuple[int, int]:
        return self.px(pair[0]), self.px(pair[1])

    def geometry(self, pair: tuple[int, int]) -> str:
        width, height = self.size(pair)
        return f"{width}x{height}"

    def fit(self, pair: tuple[int, int], minimum: tuple[int, int],
            screen: tuple[int, int]) -> tuple[tuple[int, int], tuple[int, int], bool]:
        """The window's size and minimum size on a screen of ``screen`` pixels, and whether
        it had to be made smaller than its design size.

        Where the screen has room, the design size scaled, as always. Where it has not (a
        1366 x 768 or 1280 x 720 laptop at 125 or 150 %), the window is as large as the
        screen allows, so its edges, title bar and bottom buttons stay on the screen; Windows
        would otherwise cut it to the screen itself, and X11 and macOS let it run off the
        edge. The minimum size is reduced in the same way."""
        width, height = self.size(pair)
        room_w = max(self.px(minimum[0]) // 2, screen[0] - self.px(SCREEN_ALLOWANCE[0]))
        room_h = max(self.px(minimum[1]) // 2, screen[1] - self.px(SCREEN_ALLOWANCE[1]))
        min_w, min_h = min(self.px(minimum[0]), room_w), min(self.px(minimum[1]), room_h)
        size = (min(width, room_w), min(height, room_h))
        return size, (min_w, min_h), size != (width, height)

    def size_window(self, window: tk.Wm, pair: tuple[int, int], minimum: tuple[int, int]) -> None:
        """Give ``window`` its size (see :meth:`fit`). A window made smaller is also placed
        on the screen, centred across and at the top; otherwise the system places it."""
        screen = (window.winfo_screenwidth(), window.winfo_screenheight())
        (width, height), (min_w, min_h), reduced = self.fit(pair, minimum, screen)
        where = ""
        if reduced:
            where = f"+{max(0, (screen[0] - width) // 2)}+{self.px(10)}"
        window.geometry(f"{width}x{height}{where}")
        try:
            window.minsize(min_w, min_h)
        except tk.TclError:
            pass


def enable_high_dpi() -> None:
    """Ask Windows to give GhostVault real pixels instead of stretching a 96 dpi bitmap,
    which blurs every letter on a scaled display. Must run before the first Tk window.

    System-wide awareness (not per-monitor): Tk 8.6 does not re-layout when a window moves
    to a monitor of another resolution, so per-monitor awareness would leave it at the wrong
    size there; with system awareness Windows scales it instead.
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()  # Windows 7
    except Exception:  # noqa: BLE001 - blurry is better than not starting
        pass
