"""PresetBridge: QML palette strip's delegate to the preset store.

The shared color-preset collection lives in ToolShelfStore (QSettings
``inkfall/m5/toolbar/presets``); QML holds no preset state. Every
mutation goes through the store functions, then re-emits so all
bound strips (stroke + fill) refresh. Selection is per-strip (stroke
vs fill independence) and synced from the strip's live color.

M7.1.4.3 additions:
- Fill strips expose ONE permanent, non-persisted "No Fill" slot
  (``noFillAvailable``) with its own selection state; it lives outside
  ``colors``, so it can never be removed or reordered.
- Right-click is forwarded to the host (``contextMenuRequested``) so
  the themed Python ``InkfallMenu`` owns the menu, its icons and
  on-screen clamping instead of the default-styled QML ``Menu``.
"""

from PySide6.QtCore import QObject, Property, Signal, Slot


class PresetBridge(QObject):
    colorsChanged = Signal()
    selectionChanged = Signal()
    wellChanged = Signal()
    addTileClicked = Signal()
    orderChanged = Signal()  # store mutation: strip syncs + notifies
    #: Swatch index plus the click point in the QML scene (the host maps
    #: it to global coordinates and opens the themed menu).
    contextMenuRequested = Signal(int, float, float)
    #: The permanent No Fill slot was activated by the user.
    noFillPicked = Signal()

    def __init__(self, *, target="color", get_colors=None,
                 set_colors=None, well_colors=None, swatch_edge=None,
                 on_select=None, on_no_fill=None, no_fill=False,
                 parent=None) -> None:
        super().__init__(parent)
        self._target = str(target)
        self._get_colors = get_colors
        self._set_colors = set_colors
        self._well_colors = well_colors
        self._swatch_edge = swatch_edge
        self._on_select = on_select
        self._on_no_fill = on_no_fill
        self._no_fill = bool(no_fill)
        self._no_fill_selected = False
        self._colors = list(self._read())
        self._selected = ""

    # -- store passthrough ------------------------------------------------
    def _read(self):
        try:
            if self._get_colors is not None:
                return [str(c) for c in self._get_colors()]
        except Exception:
            pass
        return []

    def _write(self, colors) -> None:
        try:
            if self._set_colors is not None:
                self._set_colors([str(c) for c in colors])
        except Exception:
            pass

    @Slot()
    def refresh(self) -> None:  # noqa: N802
        """Re-read the store (external mutation / sibling strip)."""
        try:
            colors = self._read()
            if [c.lower() for c in colors] != [
                    c.lower() for c in self._colors]:
                self._colors = colors
                self.colorsChanged.emit()
        except Exception:
            pass

    # -- bindable ----------------------------------------------------------
    def _get_colors_prop(self):
        return list(self._colors)

    colors = Property(list, _get_colors_prop, notify=colorsChanged)

    def _get_selected(self) -> str:
        return self._selected

    def _set_selected(self, value: str) -> None:
        value = str(value or "")
        if value.lower() != self._selected.lower():
            self._selected = value
            # A real color selection always leaves the No Fill slot.
            self._no_fill_selected = False
            self.selectionChanged.emit()

    selectedColor = Property(str, _get_selected, _set_selected,
                             notify=selectionChanged)

    target = Property(str, lambda self: self._target)

    #: True only on fill strips: renders the permanent leading slot.
    #: ``_no_fill`` is fixed in ``__init__`` and never mutated, so this is
    #: a genuinely constant property. Declaring it ``constant=True`` (the
    #: correct Qt semantics for an immutable value) also makes it
    #: bindable: a no-notify Property is registered as non-bindable and
    #: makes QML print "depends on non-bindable properties" for
    #: ``PaletteStrip.qml``'s ``leading`` binding.
    noFillAvailable = Property(bool, lambda self: bool(self._no_fill),
                               constant=True)

    def _get_no_fill_selected(self) -> bool:
        return bool(self._no_fill_selected)

    noFillSelected = Property(bool, _get_no_fill_selected,
                              notify=selectionChanged)

    def _well(self):
        try:
            if self._well_colors is not None:
                return self._well_colors()
        except Exception:
            pass
        return ("#20252e", "#2a3140")

    wellBg = Property(str, lambda self: self._well()[0],
                      notify=wellChanged)
    wellEdge = Property(str, lambda self: self._well()[1],
                        notify=wellChanged)

    def refreshWell(self) -> None:
        self.wellChanged.emit()

    # -- intents ------------------------------------------------------------
    @Slot(int)
    def select(self, index: int) -> None:  # noqa: N802
        try:
            colors = self._read()
            if 0 <= int(index) < len(colors):
                # Always hand the color to the host: re-picking the same
                # color must still re-enable fill on a hollow shape.
                color = str(colors[int(index)])
                was_no_fill = self._no_fill_selected
                self._no_fill_selected = False
                changed = color.lower() != self._selected.lower()
                self._selected = color
                if changed or was_no_fill:
                    self.selectionChanged.emit()
                if self._on_select is not None:
                    self._on_select(color)
        except Exception:
            pass

    @Slot()
    def selectNoFill(self) -> None:  # noqa: N802
        """Activate the permanent No Fill slot (fill strips only)."""
        if not self._no_fill:
            return
        try:
            self._no_fill_selected = True
            self._selected = ""
            self.selectionChanged.emit()
            if self._on_no_fill is not None:
                self._on_no_fill()
            else:
                self.noFillPicked.emit()
        except Exception:
            pass

    def sync_no_fill(self, selected: bool) -> None:
        """Python-side sync: reflect the strip's fill_enabled state."""
        selected = bool(selected) and self._no_fill
        if selected == self._no_fill_selected:
            return
        self._no_fill_selected = selected
        if selected:
            self._selected = ""
        self.selectionChanged.emit()

    @Slot(int, float, float)
    def showContextMenu(self, index: int, x: float, y: float) -> None:  # noqa: N802
        """Forward a swatch right-click to the themed host menu."""
        try:
            self.contextMenuRequested.emit(int(index), float(x), float(y))
        except Exception:
            pass

    @Slot(int)
    def removeAt(self, index: int) -> None:  # noqa: N802
        """Remove a preset (keeps >= 1, matches _remove_preset)."""
        try:
            colors = self._read()
            if len(colors) <= 1:
                return
            if 0 <= int(index) < len(colors):
                del colors[int(index)]
                self._write(colors)
                self._colors = list(colors)
                self.colorsChanged.emit()
                try:
                    self.orderChanged.emit()
                except Exception:
                    pass
        except Exception:
            pass

    @Slot(int, int)
    def movePreset(self, src: int, dst: int) -> None:  # noqa: N802
        try:
            colors = self._read()
            src, dst = int(src), int(dst)
            if (0 <= src < len(colors) and 0 <= dst < len(colors)
                    and src != dst):
                item = colors.pop(src)
                colors.insert(dst, item)
                self._write(colors)
                self._colors = list(colors)
                self.colorsChanged.emit()
                try:
                    self.orderChanged.emit()
                except Exception:
                    pass
        except Exception:
            pass

    @Slot()
    def clickAdd(self) -> None:  # noqa: N802
        try:
            self.addTileClicked.emit()
        except Exception:
            pass

    @Slot(str, result=str)
    def swatchEdge(self, color: str) -> str:  # noqa: N802
        """Contrasting outline for swatches that would vanish on the
        well (white/black/gray); transparent otherwise."""
        try:
            if self._swatch_edge is not None:
                return str(self._swatch_edge(str(color)) or "transparent")
        except Exception:
            pass
        return "transparent"

    # -- python-side sync ----------------------------------------------------
    def sync_selected(self, color: str) -> None:
        self._set_selected(str(color or ""))

    def sync_colors(self) -> None:
        self.refresh()
