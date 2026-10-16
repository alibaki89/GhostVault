"""Small building blocks shared by the window, the gallery, Settings and the dialogs.

They add behaviour, not appearance: text that looks exactly like a label but can be
selected and copied, scrolling with the mouse wheel and the keyboard, tooltips, and a
context menu. Everything is plain Tk, like the rest of the interface.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

from . import style

# --- copying --------------------------------------------------------------------------------------

SENSITIVE_CLEAR_MS = 60_000
SENSITIVE_NOTE = ("Copied. GhostVault takes it off the clipboard in one minute, and when GhostVault closes, if "
                  "it is still there. It cannot remove copies kept by clipboard history or a clipboard manager "
                  "(such as Windows' Win+V history or a synced clipboard), nor clear it if GhostVault stops "
                  "unexpectedly.")

# The secret GhostVault last put on the clipboard, per application (Tk root): its text and the
# pending clearing. Kept on the root, not on the dialog that copied it, so closing the dialog
# does not cancel the clearing.
_secrets: dict[str, tuple[str, str]] = {}


def _root(widget: tk.Misc) -> tk.Misc:
    return widget._root()  # noqa: SLF001 - the application's Tk instance, which lives as long as GhostVault


def copy_text(widget: tk.Misc, text: str, sensitive: bool = False) -> None:
    """Put ``text`` on the clipboard. A sensitive value (a recovery key or passphrase) is taken
    off the clipboard again after ``SENSITIVE_CLEAR_MS``, and when GhostVault closes, but only
    if the clipboard still holds exactly that value: whatever was copied since is left alone."""
    root = _root(widget)
    root.clipboard_clear()
    root.clipboard_append(text)
    key = str(root)
    previous = _secrets.pop(key, None)
    if previous is not None:
        try:
            root.after_cancel(previous[1])
        except tk.TclError:
            pass
    if sensitive:
        _secrets[key] = (text, root.after(SENSITIVE_CLEAR_MS, lambda: clear_secret(root)))


def clear_secret(widget: tk.Misc) -> bool:
    """Take GhostVault's secret off the clipboard now if it is still there; True if it was."""
    root = _root(widget)
    entry = _secrets.pop(str(root), None)
    if entry is None:
        return False
    text, job = entry
    try:
        root.after_cancel(job)
    except tk.TclError:
        pass
    try:
        if root.clipboard_get() != text:
            return False  # the user copied something else since: not ours to clear
        root.clipboard_clear()
        root.clipboard_append("")
        return True
    except tk.TclError:  # the clipboard is empty or holds no text
        return False


def protect_secret_entry(entry: tk.Entry) -> None:
    """For a field that may show a secret in clear (after "Show"): its selection is not
    exported (no middle-click paste on X11), and Ctrl/Cmd+C copies it only as the Copy button
    would, cleared again later. While hidden, Tk copies asterisks, as for any masked field."""
    entry.configure(exportselection=False)

    def copy(_event=None, cut=False):
        if entry.cget("show"):
            return None  # hidden: Tk's own binding copies the asterisks
        try:  # read from the field: with exportselection off, selection_get() is not its selection
            text = entry.get()[entry.index("sel.first"):entry.index("sel.last")] if entry.selection_present() else ""
        except tk.TclError:
            text = ""
        if text:
            copy_text(entry, text, sensitive=True)
            if cut and str(entry.cget("state")) == "normal":
                entry.delete("sel.first", "sel.last")
        return "break"

    entry.bind("<<Copy>>", copy)
    entry.bind("<<Cut>>", lambda event: copy(event, cut=True))


# --- the mouse wheel and the keyboard ---------------------------------------------------------------

def _wheel_units(event: tk.Event, accumulator: dict) -> int:
    """Lines to scroll for one wheel event, the same on every platform (negative: up)."""
    if event.num == 4:
        return -3
    if event.num == 5:
        return 3
    delta = getattr(event, "delta", 0) or 0
    if abs(delta) >= 120 or event.widget.tk.call("tk", "windowingsystem") != "aqua":
        # Windows, X11 under Tk 9, macOS under Tk 9: 120 per notch, finer for touchpads.
        accumulator["v"] = accumulator.get("v", 0.0) - delta / 40.0
    else:
        accumulator["v"] = accumulator.get("v", 0.0) - delta  # macOS, Tk 8.6: small steps
    units = int(accumulator["v"])
    accumulator["v"] -= units
    return units


class ScrollArea(tk.Frame):
    """A vertically scrolling area: a canvas, a classic scrollbar and an inner frame.

    Compared with the bare canvas the window used before: the mouse wheel and touchpad
    scroll it (anywhere over it, on every platform); Page Up/Down, Home and End scroll it
    when the focus is inside; moving the keyboard focus to something outside the visible
    part scrolls it into view; content shorter than the area does not scroll at all; and,
    with ``fill_width``, the inner frame follows the area's width.
    """

    def __init__(self, parent: tk.Misc, fill_width: bool = False, canvas_options: dict | None = None,
                 **frame_options) -> None:
        super().__init__(parent, **frame_options)
        self.canvas = tk.Canvas(self, **(canvas_options or {}))
        self.scrollbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        scale = style.Scale(self)
        self.canvas.configure(yscrollcommand=self.scrollbar.set, yscrollincrement=scale.px(20))
        if scale.factor > 1:
            width = str(self.scrollbar.cget("width"))
            try:  # a width in plain pixels (Tk 8.6) keeps its proportion; one in points (some Tk 9
                self.scrollbar.configure(width=scale.px(float(width)))  # builds) is scaled by Tk already
            except ValueError:
                pass
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.fill_width = fill_width
        self._wheel: dict = {}
        self.inner.bind("<Configure>", lambda e: self._update_region(), add="+")
        self.canvas.bind("<Configure>", self._on_canvas_configure, add="+")
        top = self.winfo_toplevel()
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            top.bind(sequence, self._on_wheel, add="+")
        for key in ("<Prior>", "<Next>", "<Home>", "<End>"):
            top.bind(key, self._on_key, add="+")
        top.bind("<FocusIn>", self._on_focus, add="+")

    # geometry
    def _on_canvas_configure(self, event) -> None:
        if self.fill_width:
            self.canvas.itemconfigure(self._window, width=event.width)
        self._update_region()

    def _update_region(self) -> None:
        width = max(self.inner.winfo_reqwidth(), self.canvas.winfo_width())
        height = max(self.inner.winfo_reqheight(), self.canvas.winfo_height())
        self.canvas.configure(scrollregion=(0, 0, width, height))

    def can_scroll(self) -> bool:
        return self.inner.winfo_reqheight() > self.canvas.winfo_height()

    # input
    def contains(self, widget) -> bool:
        while widget is not None:
            if widget is self:
                return True
            widget = getattr(widget, "master", None)
        return False

    def scroll(self, units: int) -> None:
        if units and self.can_scroll():
            self.canvas.yview_scroll(units, "units")

    def handle_wheel(self, event) -> str:
        self.scroll(_wheel_units(event, self._wheel))
        return "break"

    def _on_wheel(self, event):
        try:
            under = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):  # the pointer is over a menu or another application
            return None
        if under is not None and self.contains(under):
            return self.handle_wheel(event)
        return None

    def _on_key(self, event):
        focus = self.focus_get()
        if focus is None or not self.contains(focus) or isinstance(focus, (tk.Entry, tk.Text)) and \
                not getattr(focus, "read_only", False):
            return None
        if not self.can_scroll():
            return None
        if event.keysym == "Prior":
            self.canvas.yview_scroll(-1, "pages")
        elif event.keysym == "Next":
            self.canvas.yview_scroll(1, "pages")
        elif event.keysym == "Home":
            self.canvas.yview_moveto(0)
        else:
            self.canvas.yview_moveto(1)
        return "break"

    def _on_focus(self, event) -> None:
        widget = event.widget
        if isinstance(widget, tk.Misc) and widget is not self and self.contains(widget):
            self.after_idle(lambda: self.see(widget))

    def see(self, widget) -> None:
        """Scroll so that ``widget`` (inside the area) is fully visible."""
        try:
            if not widget.winfo_exists() or not self.can_scroll():
                return
            top = widget.winfo_rooty() - self.inner.winfo_rooty()
            bottom = top + widget.winfo_height()
            view_top = self.canvas.canvasy(0)
            view_bottom = view_top + self.canvas.winfo_height()
            total = max(1, int(self.canvas.cget("scrollregion").split()[3]))
        except (tk.TclError, ValueError, IndexError):
            return
        if top < view_top:
            self.canvas.yview_moveto(max(0, top - 8) / total)
        elif bottom > view_bottom:
            self.canvas.yview_moveto(max(0, bottom + 8 - self.canvas.winfo_height()) / total)


def scroll_area_of(widget) -> ScrollArea | None:
    while widget is not None:
        if isinstance(widget, ScrollArea):
            return widget
        widget = getattr(widget, "master", None)
    return None


# --- text that can be selected and copied ----------------------------------------------------------

_NAVIGATION = {"Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next", "Shift_L", "Shift_R",
               "Control_L", "Control_R", "Meta_L", "Meta_R", "Alt_L", "Alt_R", "Super_L", "Super_R"}


def add_context_menu(widget: tk.Misc, items) -> None:
    """A right-click menu (Control-click too on macOS). ``items()`` returns (label, command or
    None) pairs; None makes a disabled entry."""
    def show(event):
        menu = tk.Menu(widget, tearoff=False)
        for label, command in items():
            if label is None:
                menu.add_separator()
            else:
                menu.add_command(label=label, command=command, state="normal" if command else "disabled")
        x, y = event.x_root, event.y_root
        if str(event.type) in ("KeyPress", "2"):  # opened by key: at the widget, not at the pointer
            x, y = widget.winfo_rootx() + 4, widget.winfo_rooty() + widget.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()
        return "break"
    widget.bind("<Button-3>", show, add="+")
    if style.IS_MAC:
        widget.bind("<Button-2>", show, add="+")
        widget.bind("<Control-Button-1>", show, add="+")
    widget.bind("<Shift-F10>", show, add="+")
    for key in ("<Key-App>", "<Key-Menu>"):  # the context-menu key: "App" on Windows, "Menu" on X11
        try:
            widget.bind(key, show, add="+")
        except tk.TclError:
            pass


def _descender_room(widget: tk.Misc) -> int:
    """Extra pixels under each line where Tk draws with X11 core fonts (no Xft): those report
    a shorter descent than their glyphs have, and a Text, unlike a Label, clips each line to
    the reported height, cutting off the bottom of g, j, p, q and y. Elsewhere (Xft, Windows,
    macOS) the metrics are right and lines are spaced exactly as in a Label."""
    try:
        if widget.tk.call("tk", "windowingsystem") != "x11":
            return 0
        try:
            core = widget.tk.eval("::tk::pkgconfig get fontsystem") != "xft"
        except tk.TclError:  # Tk 8.6 has no pkgconfig: core fonts answer with their lowercase aliases
            family = str(tkfont.nametofont("TkDefaultFont", root=widget).actual("family"))
            core = family in ("helvetica", "courier", "times", "fixed", "lucida")
        return 2 if core else 0
    except tk.TclError:
        return 0


class SelectableText(tk.Text):
    """Read-only text that looks like a Label (same font, colour, background, no border) but
    can be selected with the mouse or keyboard, copied (Ctrl/Cmd+C, or the right-click menu)
    and wraps at the width it is given.

    The height follows the text (up to ``max_lines``); ``justify`` is "left" or "center".
    The full text is always what is copied, even when ``max_lines`` cuts the display short.
    """

    read_only = True

    def __init__(self, parent: tk.Misc, text: str = "", font=None, fg=None, justify: str = "left",
                 max_lines: int | None = None, takefocus: bool = False, wrap: str = "word",
                 extra_menu=None, **options) -> None:
        background = parent.cget("background")
        super().__init__(parent, height=1, width=1, wrap=wrap, font=font or "TkDefaultFont",
                         foreground=fg or "black", background=background, borderwidth=0, relief="flat",
                         highlightthickness=0, padx=0, pady=0, insertwidth=0, insertontime=0,
                         spacing1=0, spacing2=_descender_room(parent), spacing3=_descender_room(parent),
                         takefocus=1 if takefocus else 0,
                         cursor="xterm", exportselection=True, selectborderwidth=0, **options)
        self.max_lines = max_lines
        self._lines = 0
        self.tag_configure("all", justify=justify)
        self.set(text)
        self._extra_menu = extra_menu
        self.bind("<Key>", self._on_key)
        for virtual in ("<<Paste>>", "<<Cut>>", "<<Clear>>", "<<PasteSelection>>", "<<Undo>>", "<<Redo>>"):
            self.bind(virtual, lambda e: "break")
        if not style.IS_MAC:
            self.bind("<Button-2>", lambda e: "break")  # middle-click paste on X11
        self.bind(f"<{style.MOD}-a>", self._select_all)
        self.bind(f"<{style.MOD}-A>", self._select_all)
        self.bind("<Tab>", self._focus_next)
        self.bind("<Shift-Tab>", self._focus_prev)
        self.bind("<Configure>", lambda e: self.fit(), add="+")
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind(sequence, self._pass_wheel)
        add_context_menu(self, self._menu_items)

    @property
    def text(self) -> str:
        return self.get("1.0", "end-1c")

    def set(self, text: str) -> None:
        self.delete("1.0", "end")
        self.insert("1.0", text, ("all",))
        self.fit()

    def cget_text(self) -> str:  # tests read text the same way from labels and from this
        return self.text

    def fit(self) -> None:
        """Make the widget exactly as tall as its wrapped text (at most ``max_lines``)."""
        try:
            if self.winfo_width() > 1:
                count = self.count("1.0", "end-1c", "update", "displaylines")
                lines = (count[0] if isinstance(count, tuple) else count) or 0
                lines += 1  # count gives the number of line breaks
            else:
                lines = max(1, self.text.count("\n") + 1)
        except tk.TclError:
            return
        if self.max_lines:
            lines = min(lines, self.max_lines)
        if lines != self._lines:
            self._lines = lines
            self.configure(height=max(1, lines))

    def truncated(self) -> bool:
        try:
            count = self.count("1.0", "end-1c", "displaylines")
            lines = ((count[0] if isinstance(count, tuple) else count) or 0) + 1
        except tk.TclError:
            return False
        return bool(self.max_lines) and lines > self.max_lines

    def _on_key(self, event):
        if event.keysym in _NAVIGATION:
            return None
        if event.state & (0x4 | 0x8 | 0x10) and event.keysym.lower() in ("c", "insert"):
            return None  # Ctrl/Cmd+C (and Ctrl+Insert) copy
        return "break"

    def _select_all(self, _event=None):
        self.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _focus_next(self, _event=None):
        self.tk_focusNext().focus_set()
        return "break"

    def _focus_prev(self, _event=None):
        self.tk_focusPrev().focus_set()
        return "break"

    def _pass_wheel(self, event):
        if getattr(self, "scrolls", False):
            return None  # a text that scrolls itself (a long message)
        area = scroll_area_of(self)
        return area.handle_wheel(event) if area is not None else "break"

    def _menu_items(self):
        has_selection = bool(self.tag_ranges("sel"))
        items = [("Copy", (lambda: copy_text(self, self.get("sel.first", "sel.last"))) if has_selection else None),
                 ("Copy all", lambda: copy_text(self, self.text)),
                 ("Select all", self._select_all)]
        if self._extra_menu:
            items += [(None, None)] + list(self._extra_menu())
        return items


# --- tooltips -------------------------------------------------------------------------------------

class Tooltip:
    """A small note shown after the pointer rests on a widget (and for keyboard focus)."""

    DELAY_MS = 600

    def __init__(self, widget: tk.Misc, text) -> None:
        self.widget, self._text, self._tip, self._job = widget, text, None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<FocusOut>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")

    @property
    def text(self) -> str:
        return self._text() if callable(self._text) else self._text

    def _schedule(self, _event=None) -> None:
        self.hide()
        self._job = self.widget.after(self.DELAY_MS, self.show)

    def show(self) -> None:
        text = self.text
        if not text or self._tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except tk.TclError:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(tip, text=text, background=style.TOOLTIP_BG, foreground="black", relief="solid", borderwidth=1,
                 justify="left", wraplength=style.Scale(self.widget).px(380), padx=4, pady=2,
                 font="TkTooltipFont" if "TkTooltipFont" in tkfont.names() else "TkDefaultFont").pack()
        self._tip = tip

    def hide(self, _event=None) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


# --- glyphs ---------------------------------------------------------------------------------------

def glyph(widget: tk.Misc, preferred: str, fallback: str, font) -> str:
    """``preferred`` if the font (or a fallback font) can draw it here, else ``fallback``.
    Some X11 builds of Tk use core fonts without symbol glyphs, and drew nothing at all."""
    try:
        return preferred if tkfont.Font(widget, font=font).measure(preferred) > 0 else fallback
    except tk.TclError:
        return fallback


def visible_text(widget: tk.Misc) -> str:
    """The text a widget shows, for Labels, Buttons, read-only Entries and SelectableText."""
    if isinstance(widget, SelectableText):
        return widget.text
    if isinstance(widget, tk.Entry):
        return widget.get()
    try:
        return str(widget.cget("text"))
    except tk.TclError:
        return ""
