"""GhostVault's Tk interface.

Modules:

* ``app``: the main window (top bar, progress, gallery) and the operations it starts;
* ``gallery``: the cards;
* ``settings``: the Settings window;
* ``dialogs``: names, passphrases, recovery keys, and results with copyable text;
* ``widgets``: selectable text, scrolling, tooltips, context menus;
* ``tasks``: work off the window thread;
* ``style``: the fonts, colours and sizes the interface uses, and display scaling.
"""

from .app import GhostVaultGUI, report_callback_exception
from .style import enable_high_dpi

__all__ = ["GhostVaultGUI", "enable_high_dpi", "report_callback_exception"]
