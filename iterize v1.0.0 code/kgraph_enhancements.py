"""Persistent Knowledge Graph window, sash, zoom and node positions."""


def install(ns):
    UIState = ns["UIState"]
    persist_geometry = ns["persist_geometry"]
    KGWindow = ns["KGWindow"]
    tk = ns["tk"]

    # Prevent the enhancement being installed more than once.
    if getattr(KGWindow, "_persistence_installed", False):
        return

    original_init = KGWindow.__init__
    original_zoom = KGWindow._zoom
    original_press = KGWindow._press

    def save_window_state(self, _event=None):
        """Persist window geometry and the vertical pane position."""

        try:
            self.update_idletasks()

            self.ui.set(
                "kgraph.window",
                self.winfo_geometry(),
            )

            self.ui.set(
                "kgraph.vpane_sash",
                self.vpane.sash_coord(0)[1],
            )

            self.ui.save()

        except tk.TclError:
            pass

    def restore_window_state(self):
        """Restore geometry and divider after Tk has sized both panes."""

        try:
            self.update_idletasks()

            geometry = self.ui.get(
                "kgraph.window",
                "1000x820",
            )

            if geometry:
                self.geometry(geometry)

            self.update_idletasks()

            sash = int(
                self.ui.get(
                    "kgraph.vpane_sash",
                    560,
                )
            )

            pane_height = self.vpane.winfo_height()

            if pane_height > 300:
                sash = max(
                    150,
                    min(sash, pane_height - 150),
                )

            self.vpane.sash_place(
                0,
                0,
                sash,
            )

        except (tk.TclError, TypeError, ValueError):
            pass

    def close_persistent(self):
        """Save all KG state before closing."""

        self._save_window_state()

        try:
            self._save_layout()
        except (OSError, tk.TclError):
            pass

        self.destroy()

    def press(self, event):
        """Make the empty interior of a module circle draggable."""

        # Preserve the existing function/class/outline selection logic.
        original_press(self, event)

        # A function, class or module outline was already selected.
        if self._drag is not None:
            return

        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)

        for module_tag, relative_path in self.tag_rel.items():
            if relative_path not in self.pos:
                continue

            centre_x, centre_y = self.pos[relative_path]
            module_radius = 0.0

            # All objects inside a module share its module tag. The largest
            # oval carrying that tag is the outer module/project circle.
            for item_id in self.canvas.find_withtag(module_tag):
                if self.canvas.type(item_id) != "oval":
                    continue

                coordinates = self.canvas.coords(item_id)

                if len(coordinates) != 4:
                    continue

                x1, y1, x2, y2 = coordinates

                radius = max(
                    abs(x2 - x1),
                    abs(y2 - y1),
                ) / 2.0

                module_radius = max(
                    module_radius,
                    radius,
                )

            if module_radius <= 0:
                continue

            distance_squared = (
                    (canvas_x - centre_x) ** 2
                    + (canvas_y - centre_y) ** 2
            )

            if distance_squared <= module_radius ** 2:
                keys = {
                    relative_path,
                    *(
                        key
                        for key in self.pos
                        if key.startswith(relative_path + "::")
                    ),
                }

                self._drag = (
                    module_tag,
                    keys,
                    canvas_x,
                    canvas_y,
                )

                self._dragged = False
                return

    def zoom(
        self,
        factor,
        event,
    ):
        """Zoom and persist the resulting node positions."""

        original_zoom(
            self,
            factor,
            event,
        )

        try:
            self._save_layout()
        except (OSError, tk.TclError):
            pass

    # Assign these before replacing __init__. The enhanced constructor
    # references them through the KGWindow instance.
    KGWindow._save_window_state = save_window_state
    KGWindow._restore_window_state = restore_window_state
    KGWindow._close_persistent = close_persistent
    KGWindow._press = press
    KGWindow._zoom = zoom

    def enhanced_init(
        self,
        *args,
        **kwargs,
    ):
        original_init(
            self,
            *args,
            **kwargs,
        )

        self.ui = UIState()

        persist_geometry(
            self,
            self.ui,
            "kgraph.window",
            "1000x820",
        )

        def save_after_release(_event=None):
            self.after_idle(
                self._save_window_state,
            )

        self.vpane.bind(
            "<ButtonRelease-1>",
            save_after_release,
            add="+",
        )

        # Also catch releases delivered through the toplevel bind tag.
        self.bind(
            "<ButtonRelease-1>",
            save_after_release,
            add="+",
        )

        # after_idle is too early for a PanedWindow containing two sizeable panes.
        self.after(
            250,
            self._restore_window_state,
        )

        self.protocol(
            "WM_DELETE_WINDOW",
            self._close_persistent,
        )

    KGWindow.__init__ = enhanced_init
    KGWindow._persistence_installed = True