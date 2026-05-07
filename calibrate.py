"""
calibrate.py — Visual calibration helper.

Draws a numbered grid on the overlay so you can verify that the 8×8 square
boundaries line up with the chess.com board underneath before running the
main overlay engine.

Usage:
    python calibrate.py

Controls:
    Arrow keys  — nudge the window 1 px at a time
    Shift+Arrow — nudge 10 px at a time
    S           — save current position to calibration.json
    Q / Escape  — quit
"""

from __future__ import annotations

import json
import tkinter as tk

import config

CAL_FILE = config.CALIBRATION_PATH
GRID_COLOR   = "#ff4040"
LABEL_COLOR  = "#ffff00"
OVERLAY_ALPHA = 0.70


class Calibrator:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Chess Overlay — Calibration")
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.wm_attributes("-alpha", OVERLAY_ALPHA)
        root.config(bg="black")
        root.wm_attributes("-transparentcolor", "black")

        # Load saved position or use defaults
        pos = self._load_position()
        root.geometry(
            f"{config.BOARD_SIZE}x{config.BOARD_SIZE+config.TOP_BAR_HEIGHT}"
            f"+{pos['x']}+{pos['y']}"
        )

        self._build_ui()
        self._bind_keys()
        self._update_status()

    def _build_ui(self) -> None:
        # Top status bar
        bar = tk.Frame(self.root, bg="#1a1a2e", height=config.TOP_BAR_HEIGHT)
        bar.pack(side=tk.TOP, fill=tk.X)
        bar.pack_propagate(False)
        tk.Label(
            bar, text="CALIBRATION MODE — Arrow keys to nudge  |  S=Save  |  Q=Quit",
            bg="#1a1a2e", fg="#e0e0e0", font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=8)

        self._status_var = tk.StringVar(value="")
        tk.Label(
            bar, textvariable=self._status_var,
            bg="#1a1a2e", fg="#888888", font=("Consolas", 9),
        ).pack(side=tk.RIGHT, padx=8)

        bar.bind("<ButtonPress-1>",   self._drag_start)
        bar.bind("<B1-Motion>",       self._drag_motion)
        self._drag_x = self._drag_y = 0

        # Board canvas with calibration grid
        self._canvas = tk.Canvas(
            self.root,
            width=config.BOARD_SIZE,
            height=config.BOARD_SIZE,
            bg="black",
            highlightthickness=0,
        )
        self._canvas.pack()
        self._draw_calibration_grid()

    def _draw_calibration_grid(self) -> None:
        sq = config.BOARD_SIZE / 8.0
        files = "abcdefgh"
        ranks = "87654321"

        for r in range(8):
            for c in range(8):
                x1, y1 = c * sq, r * sq
                x2, y2 = x1 + sq, y1 + sq

                # Alternating transparent / semi-transparent squares
                fill = "" if (r + c) % 2 == 0 else "#ffffff"
                stipple = "" if (r + c) % 2 == 0 else "gray12"
                self._canvas.create_rectangle(
                    x1, y1, x2, y2,
                    outline=GRID_COLOR, width=2,
                    fill=fill, stipple=stipple,
                )

                # Coordinate label
                label = f"{files[c]}{ranks[r]}"
                self._canvas.create_text(
                    x1 + sq / 2, y1 + sq / 2,
                    text=label,
                    fill=LABEL_COLOR,
                    font=("Consolas", 11, "bold"),
                )

        # Border
        self._canvas.create_rectangle(
            0, 0, config.BOARD_SIZE - 1, config.BOARD_SIZE - 1,
            outline=GRID_COLOR, width=3,
        )

    def _bind_keys(self) -> None:
        self.root.bind("<Key-q>",       lambda _: self.root.destroy())
        self.root.bind("<Key-Q>",       lambda _: self.root.destroy())
        self.root.bind("<Escape>",      lambda _: self.root.destroy())
        self.root.bind("<Key-s>",       lambda _: self._save_position())
        self.root.bind("<Key-S>",       lambda _: self._save_position())
        self.root.bind("<Left>",        lambda _: self._nudge(-1, 0))
        self.root.bind("<Right>",       lambda _: self._nudge(1, 0))
        self.root.bind("<Up>",          lambda _: self._nudge(0, -1))
        self.root.bind("<Down>",        lambda _: self._nudge(0, 1))
        self.root.bind("<Shift-Left>",  lambda _: self._nudge(-10, 0))
        self.root.bind("<Shift-Right>", lambda _: self._nudge(10, 0))
        self.root.bind("<Shift-Up>",    lambda _: self._nudge(0, -10))
        self.root.bind("<Shift-Down>",  lambda _: self._nudge(0, 10))
        self.root.focus_force()

    def _nudge(self, dx: int, dy: int) -> None:
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        self.root.geometry(f"+{x}+{y}")
        self._update_status()

    def _drag_start(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag_motion(self, event: tk.Event) -> None:
        nx = event.x_root - self._drag_x
        ny = event.y_root - self._drag_y
        self.root.geometry(f"+{nx}+{ny}")
        self._update_status()

    def _current_position(self) -> dict[str, int]:
        self.root.update_idletasks()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        return {
            "x": x,
            "y": y,
            "board_x": x + config.BOARD_CANVAS_X,
            "board_y": y + config.BOARD_CANVAS_Y,
            "board_size": config.BOARD_SIZE,
            "top_bar_height": config.TOP_BAR_HEIGHT,
        }

    def _update_status(self) -> None:
        if not hasattr(self, "_status_var"):
            return
        data = self._current_position()
        self._status_var.set(f"x={data['x']} y={data['y']}")

    def _save_position(self) -> None:
        data = self._current_position()
        CAL_FILE.write_text(json.dumps(data, indent=2))
        print(f"Saved position: {data}")

    def _load_position(self) -> dict:
        if CAL_FILE.exists():
            try:
                data = json.loads(CAL_FILE.read_text())
                return {
                    "x": int(data.get("x", 100)),
                    "y": int(data.get("y", 100)),
                }
            except Exception:
                pass
        return {"x": 100, "y": 100}


def main() -> None:
    root = tk.Tk()
    Calibrator(root)
    root.mainloop()


if __name__ == "__main__":
    main()
