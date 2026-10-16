"""The gallery of ghost cards.

Each card has a solid one-pixel border, 8 px inside and 10 px around, the 200 x 150
thumbnail, the name in Arial 11 bold, size, chunk count and date, and the buttons below.
How they are laid out and kept up to date:

* every card has the same width (the widest of what cards contain: thumbnail or button
  row), so columns line up and no long name or extra button widens its column;
* the number of columns follows the window's width, so no card is ever cut off;
* names wrap inside the card (up to three lines), stay selectable and copyable in full
  (also from the right-click menu), and the whole name is in a tooltip when it is longer;
* cards are kept between refreshes and only changed ones are rebuilt: no flicker, and the
  scroll position stays where it was;
* previews are decrypted and decoded within a small time budget on the window thread
  (cached afterwards); the rest, and any that must be downloaded, off the window thread;
* dates are shown in local time.
"""

from __future__ import annotations

import time
import tkinter as tk

from PIL import Image, ImageTk

from ..errors import humanize_error
from ..naming import format_size, local_time
from ..thumbnails import decode_preview, placeholder
from . import style
from .widgets import ScrollArea, SelectableText, Tooltip, add_context_menu, copy_text

STATE_TEXT = {
    "cleanup": "Restored. Removing the remote copy is pending.",
    "deleting": "Being removed from GitHub (pending).",
    "uploading": "Interrupted while ghosting. Finished on next start.",
    "stored": "Interrupted while ghosting. Finished on next start.",
    "removing-original": "Interrupted while ghosting. Finished on next start.",
    "restoring": "Interrupted while restoring. Finished on next start.",
}
DECODE_BUDGET_S = 0.06  # preview work done at once on the window thread per refresh


class Card:
    def __init__(self, frame: tk.Frame, signature, spacer: tk.Frame, fixed: list, image_label=None,
                 file_id: str | None = None) -> None:
        self.frame, self.signature, self.spacer, self.fixed = frame, signature, spacer, fixed
        self.image_label, self.file_id = image_label, file_id


class Gallery:
    def __init__(self, app, parent: tk.Misc) -> None:
        self.app = app
        self.scale = style.Scale(parent)
        self.area = ScrollArea(parent)
        self.area.pack(side="left", fill="both", expand=True)
        self.canvas, self.frame = self.area.canvas, self.area.inner
        self.cards: dict[str, Card] = {}
        self.order: list[str] = []
        self.columns = 0
        self.card_width = 0
        self.message: tk.Label | None = None
        self.images: dict[str, tuple[tuple, ImageTk.PhotoImage, bool]] = {}  # file id -> (key, photo, is preview)
        self._pending_images: set[str] = set()
        self._reflow_job = None
        self.canvas.bind("<Configure>", lambda e: self._schedule_reflow(), add="+")

    # --- rendering ---------------------------------------------------------------------------------
    def render(self, manifests, damaged) -> None:
        if self.message is not None:
            self.message.destroy()
            self.message = None
        if not manifests and not damaged:
            for key in list(self.cards):
                self.cards.pop(key).frame.destroy()
            self.order = []
            self.message = tk.Label(self.frame, text="No ghost files yet. Click Add File.", font=style.FONT_EMPTY)
            self.message.grid(row=0, column=0, padx=self.scale.px(30), pady=self.scale.px(30))
            return
        wanted = [(m["fileId"], ("ok", m)) for m in manifests] + [(f"damaged:{d.path.name}", ("damaged", d))
                                                                   for d in damaged]
        budget_end = time.monotonic() + DECODE_BUDGET_S
        keep = {key for key, _ in wanted}
        for key in list(self.cards):
            if key not in keep:
                self.cards.pop(key).frame.destroy()
        for file_id in list(self.images):
            if file_id not in keep:
                del self.images[file_id]  # a ghost that is gone: its image is not kept
        for key, (kind, item) in wanted:
            signature = self._signature(kind, item)
            card = self.cards.get(key)
            if card is not None and card.signature == signature:
                continue
            if card is not None:
                card.frame.destroy()
            self.cards[key] = (self._ghost_card(item, signature, budget_end) if kind == "ok"
                               else self._damaged_card(item, signature))
        self.order = [key for key, _ in wanted]
        self._size_cards()
        self._layout(force=True)

    @staticmethod
    def _signature(kind, item):
        if kind == "damaged":
            return ("damaged", item.path.name, item.reason)
        preview = item.get("preview") or {}
        return ("ok", item["displayName"], item["state"], item["original"].get("size", 0), len(item["chunks"]),
                item.get("createdAt"), bool(item.get("preview")), bool(preview.get("enabled")))

    def _new_frame(self) -> tuple[tk.Frame, tk.Frame]:
        px = self.scale.px
        frame = tk.Frame(self.frame, bd=1, relief="solid", padx=px(style.CARD_PAD), pady=px(style.CARD_PAD))
        spacer = tk.Frame(frame, width=0, height=0)
        spacer.pack()
        return frame, spacer

    def _ghost_card(self, manifest, signature, budget_end) -> Card:
        app, px = self.app, self.scale.px
        frame, spacer = self._new_frame()
        file_id = manifest["fileId"]
        image_label = tk.Label(frame)
        image_label.pack()
        name = SelectableText(frame, manifest["displayName"], font=style.FONT_CARD_NAME, justify="center",
                              max_lines=3, extra_menu=lambda m=manifest: self._card_menu(m))
        name.pack(fill="x")
        Tooltip(name, lambda n=name: n.text if n.truncated() else "")
        fixed = [image_label]
        for text in (f"Original: {format_size(manifest['original'].get('size', 0))}",
                     f"Chunks: {len(manifest['chunks'])}",
                     f"Added: {local_time(manifest.get('createdAt', ''))}"):
            label = tk.Label(frame, text=text)
            label.pack()
            fixed.append(label)
        state = manifest["state"]
        preview = manifest.get("preview")
        if state == "ready":
            buttons = tk.Frame(frame)
            buttons.pack(pady=5)
            tk.Button(buttons, text="Restore", command=lambda: app.restore_file(manifest)).pack(side="left", padx=2)
            tk.Button(buttons, text="Rename", command=lambda: app.rename_ghost(manifest)).pack(side="left", padx=2)
            if preview:
                tk.Button(buttons, text="Hide preview" if preview.get("enabled") else "Show preview",
                          command=lambda: app.toggle_preview(manifest)).pack(side="left", padx=2)
            fixed.append(buttons)
        else:
            tk.Label(frame, text=STATE_TEXT.get(state, state), fg=style.PENDING, justify="center",
                     wraplength=px(180)).pack(fill="x")
            if state in ("cleanup", "deleting"):
                retry = tk.Button(frame, text="Retry clean-up", command=lambda: app.retry_cleanup(manifest))
                retry.pack(pady=5)
                fixed.append(retry)
        buttons = [w for f in fixed if isinstance(f, tk.Frame) for w in f.winfo_children()] + \
                  [f for f in fixed if isinstance(f, tk.Button)]
        for widget in [frame, image_label] + fixed[1:4] + buttons:  # buttons: Shift+F10 / menu key
            add_context_menu(widget, lambda m=manifest: self._card_menu(m))
        card = Card(frame, signature, spacer, fixed, image_label, file_id)
        self._set_image(card, manifest, budget_end)
        return card

    def _damaged_card(self, damaged, signature) -> Card:
        px = self.scale.px
        frame, spacer = self._new_frame()
        title = tk.Label(frame, text="[damaged record]", fg=style.ERROR)
        title.pack()
        SelectableText(frame, damaged.path.name, font=style.FONT_SMALL, justify="center").pack(fill="x")
        SelectableText(frame, damaged.reason, fg=style.SECONDARY, justify="center", max_lines=6).pack(
            fill="x", pady=(px(2), 0))
        button = tk.Button(frame, text="Recovery...", command=self.app.open_recovery_settings)
        button.pack(pady=5)
        Tooltip(button, "Repair or set aside unreadable records in Settings, Recovery.")
        return Card(frame, signature, spacer, [title, button])

    def _card_menu(self, manifest):
        app = self.app
        items = [("Copy name", lambda: copy_text(self.frame, manifest["displayName"]))]
        if manifest["state"] == "ready" and not app.busy:
            items = [("Restore...", lambda: app.restore_file(manifest)),
                     ("Rename...", lambda: app.rename_ghost(manifest))] + (
                [("Hide preview" if manifest["preview"].get("enabled") else "Show preview",
                  lambda: app.toggle_preview(manifest))] if manifest.get("preview") else []) + [(None, None)] + items
        return items

    # --- images ------------------------------------------------------------------------------------
    def _photo(self, img: Image.Image) -> ImageTk.PhotoImage:
        factor = self.scale.factor
        if factor > 1:
            img = img.resize((self.scale.px(img.width), self.scale.px(img.height)), Image.LANCZOS)
        return ImageTk.PhotoImage(img, master=self.frame)

    def _placeholder(self, manifest) -> ImageTk.PhotoImage:
        img = placeholder(manifest["displayName"], manifest["original"].get("extension", ""),
                          scale=self.scale.factor)
        return ImageTk.PhotoImage(img, master=self.frame)

    def _set_image(self, card: Card, manifest, budget_end) -> None:
        file_id = manifest["fileId"]
        preview = manifest.get("preview") or {}
        key = (bool(manifest.get("preview")), bool(preview.get("enabled")), manifest["displayName"])
        cached = self.images.get(file_id)
        if cached and cached[0] == key:
            self._show(card, cached[1], cached[2])
            return
        if not preview.get("enabled"):
            photo = self._placeholder(manifest)
            self.images[file_id] = (key, photo, False)
            self._show(card, photo, False)
            return
        if time.monotonic() < budget_end:
            data = self._decode(self.app.vault.preview_image(file_id, fetch=False))
            if data is not None:
                photo = self._photo(data)
                self.images[file_id] = (key, photo, True)
                self._show(card, photo, True)
                return
        # Not cached locally, or out of time: a placeholder now, the preview when it is ready.
        self._show(card, self._placeholder(manifest), False)
        if file_id in self._pending_images:
            return
        self._pending_images.add(file_id)
        vault = self.app.vault

        def work():
            img = self._decode(vault.preview_image(file_id, fetch=False))
            if img is None and (preview.get("remote") or {}).get("committed"):
                img = self._decode(vault.preview_image(file_id, fetch=True))
            return img

        def done(img):
            self._pending_images.discard(file_id)
            if img is None:
                return
            photo = self._photo(img)
            self.images[file_id] = (key, photo, True)
            current = self.cards.get(file_id)
            if current is not None and current.image_label is not None and current.image_label.winfo_exists():
                self._show(current, photo, True)

        self.app.tasks.quiet(work, done, lambda exc: self._pending_images.discard(file_id))

    @staticmethod
    def _decode(data: bytes | None):
        if data is None:
            return None
        try:
            return decode_preview(data)
        except Exception:  # noqa: BLE001 - a placeholder is shown instead
            return None

    @staticmethod
    def _show(card: Card, photo, is_preview: bool) -> None:
        card.image_label.configure(image=photo)
        card.image_label.photo = photo
        card.image_label.preview_missing = not is_preview

    @property
    def pending_previews(self) -> int:
        return len(self._pending_images)

    # --- layout ------------------------------------------------------------------------------------
    def _size_cards(self) -> None:
        """One width for every card: the widest fixed content (thumbnail, button row, labels)."""
        cards = [self.cards[k] for k in self.order]
        for card in cards:
            card.spacer.configure(width=0)
        self.frame.update_idletasks()
        widest = self.scale.px(style.THUMBNAIL[0])
        for card in cards:
            for widget in card.fixed:
                widest = max(widest, widget.winfo_reqwidth())
        self.card_width = widest
        for card in cards:
            card.spacer.configure(width=widest)

    def _schedule_reflow(self) -> None:
        if self._reflow_job is None:
            self._reflow_job = self.frame.after_idle(self._reflow)

    def _reflow(self) -> None:
        self._reflow_job = None
        self._layout(force=False)

    def _layout(self, force: bool) -> None:
        if not self.order:
            return
        px = self.scale.px
        gap = px(style.CARD_GAP)
        outer = self.card_width + 2 * px(style.CARD_PAD) + 2 + 2 * gap
        available = max(self.canvas.winfo_width(), 1)
        columns = max(1, available // max(outer, 1))
        if not force and columns == self.columns:
            return
        self.columns = columns
        for n, key in enumerate(self.order):
            self.cards[key].frame.grid(row=n // columns, column=n % columns, padx=gap, pady=gap, sticky="ns")
        self.frame.update_idletasks()
        self.area._update_region()  # noqa: SLF001

    # --- state kept across refreshes ---------------------------------------------------------------
    def forget_image(self, file_id: str) -> None:
        self.images.pop(file_id, None)

    def card_widgets(self, file_id: str) -> tk.Frame | None:
        card = self.cards.get(file_id)
        return card.frame if card else None


def show_error_in(parent, error) -> tk.Label:
    label = tk.Label(parent, text=f"The vault could not be read: {humanize_error(error)}", fg=style.ERROR,
                     wraplength=style.Scale(parent).px(800), justify="left")
    label.grid(row=0, column=0, padx=30, pady=30)
    return label
