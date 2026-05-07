"""
overlay.py — Transparent always-on-top Tkinter overlay.

Changes from original:
  - Profile selector panel at the top of the sidebar replaces raw skill/depth
    sliders as the primary control.  Selecting a profile auto-fills strength
    and style; the "Manual Override" section still exposes the old sliders.
  - Profile info card shows emoji, name, ~rating, and description.
  - Active profile name shown in the top-bar status line.
  - _on_new_fen now passes the active PlayerProfile (or None) to the engine.
  - All other improvements from previous version retained (ponder arrow,
    eval bar, drag debounce, dedup region updates).
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Optional

import chess

import config
from engine import EngineWorker, MoveResult
from profiles import (
    ALL_PROFILES,
    PROFILE_CATEGORIES,
    PROFILE_DISPLAY_ORDER,
    PlayerProfile,
    default_profile,
)
from vision import VisionLoop

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Windows click-through via pywin32
# ─────────────────────────────────────────────────────────────────────────────
try:
    import win32gui
    import win32con
    _WIN32_AVAILABLE = True
except ImportError:
    _WIN32_AVAILABLE = False
    log.warning("pywin32 not installed — click-through unavailable")


def _set_clickthrough(hwnd: int, enable: bool) -> None:
    if not _WIN32_AVAILABLE:
        return
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if enable:
        style |= win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED
    else:
        style &= ~win32con.WS_EX_TRANSPARENT
        style |= win32con.WS_EX_LAYERED
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)


# ─────────────────────────────────────────────────────────────────────────────
# Arrow drawing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sq_center(sq: chess.Square, flipped: bool = False) -> tuple[int, int]:
    file_idx = chess.square_file(sq)
    rank_idx = chess.square_rank(sq)
    if flipped:
        col = 7 - file_idx
        row = rank_idx
    else:
        col = file_idx
        row = 7 - rank_idx
    half = config.SQUARE_SIZE // 2
    x = col * config.SQUARE_SIZE + half
    y = config.BOARD_CANVAS_Y + row * config.SQUARE_SIZE + half
    return x, y


def _draw_arrow(
    canvas:  tk.Canvas,
    from_sq: chess.Square,
    to_sq:   chess.Square,
    color:   str  = config.ARROW_COLOR,
    width:   int  = config.ARROW_WIDTH,
    flipped: bool = False,
    tag:     str  = "arrow",
) -> None:
    x1, y1 = _sq_center(from_sq, flipped)
    x2, y2 = _sq_center(to_sq,   flipped)
    canvas.create_line(
        x1, y1, x2, y2,
        fill=color, width=width,
        arrow=tk.LAST,
        arrowshape=(
            config.ARROWHEAD_LENGTH,
            config.ARROWHEAD_LENGTH,
            config.ARROWHEAD_WIDTH,
        ),
        tags=tag, capstyle=tk.ROUND,
    )
    r = width // 2 + 2
    canvas.create_oval(
        x1 - r, y1 - r, x1 + r, y1 + r,
        fill=color, outline="", tags=tag,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main overlay window
# ─────────────────────────────────────────────────────────────────────────────

class ChessOverlay:

    def __init__(self, root: tk.Tk) -> None:
        self.root            = root
        self._click_through  = False
        self._flipped        = False
        self._humanise       = False
        self._skill_level    = config.DEFAULT_SKILL_LEVEL
        self._depth          = config.DEFAULT_DEPTH
        self._current_fen    = ""
        self._hwnd: Optional[int] = None
        self._last_board_xy: tuple[int, int] = (-1, -1)

        self._drag_x = 0
        self._drag_y = 0
        self._drag_after_id: Optional[str] = None

        # Active profile (None → legacy manual-slider path)
        self._active_profile: Optional[PlayerProfile] = default_profile()

        self._build_window()
        self._build_ui()
        self._bind_hotkeys()
        self._start_workers()
        self._update_board_region()
        self.root.after(500, self._update_board_region)

    # ── Window setup ──────────────────────────────────────────────────────────

    def _load_calibration(self) -> tuple[int, int]:
        import json
        from pathlib import Path
        cal_file = Path("calibration.json")
        if cal_file.exists():
            try:
                data = json.loads(cal_file.read_text())
                return data.get("x", 100), data.get("y", 100)
            except Exception:
                pass
        return 100, 100

    def _build_window(self) -> None:
        root = self.root
        root.title("Chess Overlay")
        win_x, win_y = self._load_calibration()
        root.geometry(f"{config.WINDOW_WIDTH}x{config.WINDOW_HEIGHT}+{win_x}+{win_y}")
        root.resizable(False, False)
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.wm_attributes("-alpha", 0.92)
        root.config(bg="black")
        root.wm_attributes("-transparentcolor", "black")

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._build_topbar()
        self._build_board_canvas()
        self._build_sidebar()

    def _build_topbar(self) -> None:
        bar = tk.Frame(self.root, bg=config.BG_TOPBAR, height=config.TOP_BAR_HEIGHT)
        bar.pack(side=tk.TOP, fill=tk.X)
        bar.pack_propagate(False)

        tk.Label(
            bar, text="♛  Chess Overlay Engine",
            bg=config.BG_TOPBAR, fg=config.TEXT_MAIN,
            font=("Segoe UI", 11, "bold"),
        ).pack(side=tk.LEFT, padx=10)

        self._status_var = tk.StringVar(value="⏳ Loading engine…")
        tk.Label(
            bar, textvariable=self._status_var,
            bg=config.BG_TOPBAR, fg=config.TEXT_DIM,
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=10)

        close_btn = tk.Button(
            bar, text="✕", bg=config.BG_TOPBAR, fg=config.TEXT_MAIN,
            relief=tk.FLAT, bd=0, padx=8, font=("Segoe UI", 12),
            command=self._on_quit,
            activebackground=config.ACCENT, activeforeground="white",
        )
        close_btn.pack(side=tk.RIGHT)

        minimize_btn = tk.Button(
            bar, text="─", bg=config.BG_TOPBAR, fg=config.TEXT_MAIN,
            relief=tk.FLAT, bd=0, padx=8, font=("Segoe UI", 12),
            command=self.root.iconify,
            activebackground=config.BTN_BG, activeforeground="white",
        )
        minimize_btn.pack(side=tk.RIGHT)

        bar.bind("<ButtonPress-1>", self._drag_start)
        bar.bind("<B1-Motion>",     self._drag_motion)

    def _build_board_canvas(self) -> None:
        self._canvas = tk.Canvas(
            self.root,
            width=config.BOARD_SIZE,
            height=config.BOARD_SIZE,
            bg="black",
            highlightthickness=0,
        )
        self._canvas.place(x=config.BOARD_CANVAS_X, y=config.BOARD_CANVAS_Y)

    # ── Sidebar ───────────────────────────────────────────────────────────────

    def _build_sidebar(self) -> None:
        # Scrollable container so everything fits regardless of screen height
        outer = tk.Frame(
            self.root,
            bg=config.BG_SIDEBAR,
            width=config.SIDEBAR_WIDTH,
        )
        outer.place(
            x=config.BOARD_SIZE,
            y=config.TOP_BAR_HEIGHT,
            width=config.SIDEBAR_WIDTH,
            height=config.BOARD_SIZE,
        )
        outer.pack_propagate(False)

        canvas_sb = tk.Canvas(
            outer, bg=config.BG_SIDEBAR,
            highlightthickness=0,
            width=config.SIDEBAR_WIDTH,
        )
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas_sb.yview)
        canvas_sb.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas_sb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sidebar = tk.Frame(canvas_sb, bg=config.BG_SIDEBAR)
        win_id  = canvas_sb.create_window((0, 0), window=sidebar, anchor="nw")

        def _on_frame_configure(_):
            canvas_sb.configure(scrollregion=canvas_sb.bbox("all"))
        def _on_canvas_configure(event):
            canvas_sb.itemconfig(win_id, width=event.width)

        sidebar.bind("<Configure>", _on_frame_configure)
        canvas_sb.bind("<Configure>", _on_canvas_configure)

        # Mouse-wheel scrolling
        def _on_mousewheel(event):
            canvas_sb.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas_sb.bind_all("<MouseWheel>", _on_mousewheel)

        # Build sections
        self._build_profile_section(sidebar)
        self._build_separator(sidebar)
        self._build_manual_section(sidebar)
        self._build_separator(sidebar)
        self._build_board_section(sidebar)
        self._build_separator(sidebar)
        self._build_eval_section(sidebar)

    def _sidebar_label(
        self, parent: tk.Widget, text: str,
        small: bool = False, pady: tuple = (2, 2),
        fg: str = config.TEXT_MAIN,
    ) -> None:
        font = ("Segoe UI", 8) if small else ("Segoe UI", 9, "bold")
        tk.Label(parent, text=text, bg=config.BG_SIDEBAR, fg=fg, font=font).pack(pady=pady)

    def _build_separator(self, parent: tk.Widget) -> None:
        tk.Frame(parent, bg="#2a2a4a", height=1).pack(fill=tk.X, padx=6, pady=4)

    # ── Profile selector section ──────────────────────────────────────────────

    def _build_profile_section(self, parent: tk.Widget) -> None:
        self._sidebar_label(parent, "PLAYER PROFILE", pady=(10, 4))

        # Build flat label → profile-id mapping in display order
        self._profile_labels: list[str] = []
        self._profile_ids:    list[str] = []

        for cat, ids in PROFILE_CATEGORIES.items():
            # Category header as a non-selectable label item
            self._profile_labels.append(f"── {cat} ──")
            self._profile_ids.append("")          # sentinel: category header
            for pid in ids:
                p = ALL_PROFILES[pid]
                self._profile_labels.append(f"  {p.emoji} {p.name}")
                self._profile_ids.append(pid)

        self._profile_combo_var = tk.StringVar()
        combo = ttk.Combobox(
            parent,
            textvariable=self._profile_combo_var,
            values=self._profile_labels,
            state="readonly",
            width=20,
            font=("Segoe UI", 8),
        )
        combo.pack(padx=8, pady=(0, 4), fill=tk.X)
        combo.bind("<<ComboboxSelected>>", self._on_profile_combo_select)

        # Profile info card
        self._profile_card = tk.Frame(parent, bg="#0d1b2e", bd=0)
        self._profile_card.pack(padx=6, pady=2, fill=tk.X)

        self._profile_title_var = tk.StringVar(value="")
        tk.Label(
            self._profile_card, textvariable=self._profile_title_var,
            bg="#0d1b2e", fg="#ffd740",
            font=("Segoe UI", 9, "bold"),
            wraplength=config.SIDEBAR_WIDTH - 24,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=6, pady=(6, 2))

        self._profile_rating_var = tk.StringVar(value="")
        tk.Label(
            self._profile_card, textvariable=self._profile_rating_var,
            bg="#0d1b2e", fg=config.ACCENT,
            font=("Consolas", 9, "bold"),
        ).pack(anchor=tk.W, padx=6)

        self._profile_desc_var = tk.StringVar(value="")
        tk.Label(
            self._profile_card, textvariable=self._profile_desc_var,
            bg="#0d1b2e", fg=config.TEXT_DIM,
            font=("Segoe UI", 8),
            wraplength=config.SIDEBAR_WIDTH - 24,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=6, pady=(2, 6))

        # Set the default profile in the combo
        self._set_combo_to_profile(default_profile().id)
        self._refresh_profile_card(self._active_profile)

    def _set_combo_to_profile(self, profile_id: str) -> None:
        """Update the combobox display to match a profile id."""
        try:
            idx = self._profile_ids.index(profile_id)
            self._profile_combo_var.set(self._profile_labels[idx])
        except ValueError:
            pass

    def _on_profile_combo_select(self, _=None) -> None:
        label = self._profile_combo_var.get()
        try:
            idx = self._profile_labels.index(label)
        except ValueError:
            return

        pid = self._profile_ids[idx]
        if not pid:
            # Category header selected — revert to last valid
            if self._active_profile:
                self._set_combo_to_profile(self._active_profile.id)
            return

        profile = ALL_PROFILES[pid]
        self._active_profile = profile
        self._refresh_profile_card(profile)

        # Sync manual sliders so they reflect the profile
        self._skill_var.set(min(20, profile.stockfish_elo // 125))
        self._depth_var.set(profile.depth)
        self._blunder_var.set(profile.errors.blunder_rate)
        self._random_var.set(profile.errors.miss_tactic_rate)

        log.info("Profile selected: %s", profile.name)

        # Re-analyse current position with new profile
        if self._current_fen:
            self._engine.request_analysis(
                self._current_fen, profile=profile,
            )

    def _refresh_profile_card(self, profile: Optional[PlayerProfile]) -> None:
        if profile is None:
            self._profile_title_var.set("Manual mode")
            self._profile_rating_var.set("")
            self._profile_desc_var.set("Use the sliders below to configure strength.")
            return
        self._profile_title_var.set(f"{profile.emoji}  {profile.name}")
        self._profile_rating_var.set(f"~{profile.rating} ELO  |  depth {profile.depth}")
        self._profile_desc_var.set(profile.description)

    # ── Manual override section ───────────────────────────────────────────────

    def _build_manual_section(self, parent: tk.Widget) -> None:
        # Collapsible header
        self._manual_expanded = tk.BooleanVar(value=False)
        hdr = tk.Button(
            parent, text="⚙ Manual Override  ▸",
            bg=config.BTN_BG, fg=config.TEXT_DIM,
            relief=tk.FLAT, font=("Segoe UI", 8),
            command=self._toggle_manual_section,
            activebackground=config.BTN_HOVER, activeforeground="white",
        )
        hdr.pack(padx=8, pady=(2, 0), fill=tk.X)
        self._manual_header_btn = hdr

        self._manual_frame = tk.Frame(parent, bg=config.BG_SIDEBAR)
        # Not packed initially (collapsed)

        inner = self._manual_frame

        # Skill level
        self._sidebar_label(inner, "SKILL LEVEL", pady=(8, 2))
        self._skill_var = tk.IntVar(value=self._skill_level)
        ttk.Scale(
            inner, from_=0, to=20, variable=self._skill_var,
            command=self._on_skill_change, orient=tk.HORIZONTAL,
        ).pack(padx=10, fill=tk.X)
        self._skill_disp = tk.Label(
            inner, text=f"Level {self._skill_level}",
            bg=config.BG_SIDEBAR, fg=config.TEXT_DIM,
            font=("Segoe UI", 8),
        )
        self._skill_disp.pack()
        self._skill_var.trace_add(
            "write",
            lambda *_: self._skill_disp.config(text=f"Level {self._skill_var.get()}"),
        )

        # Depth
        self._sidebar_label(inner, "DEPTH", pady=(6, 2))
        self._depth_var = tk.IntVar(value=self._depth)
        ttk.Scale(
            inner, from_=1, to=25, variable=self._depth_var,
            command=self._on_depth_change, orient=tk.HORIZONTAL,
        ).pack(padx=10, fill=tk.X)
        self._depth_disp = tk.Label(
            inner, text=f"Depth {self._depth}",
            bg=config.BG_SIDEBAR, fg=config.TEXT_DIM,
            font=("Segoe UI", 8),
        )
        self._depth_disp.pack()
        self._depth_var.trace_add(
            "write",
            lambda *_: self._depth_disp.config(text=f"Depth {self._depth_var.get()}"),
        )

        # Human-like errors
        self._sidebar_label(inner, "HUMAN ERRORS", pady=(6, 2))
        self._humanise_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            inner, text="Enable errors",
            variable=self._humanise_var, command=self._on_humanise_change,
            bg=config.BG_SIDEBAR, fg=config.TEXT_MAIN,
            selectcolor=config.BTN_BG, activebackground=config.BG_SIDEBAR,
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, padx=10)

        self._sidebar_label(inner, "Blunder rate", small=True, pady=(4, 1))
        self._blunder_var = tk.DoubleVar(value=0.0)
        ttk.Scale(
            inner, from_=0.0, to=0.3, variable=self._blunder_var,
            command=self._on_blunder_change, orient=tk.HORIZONTAL,
        ).pack(padx=10, fill=tk.X)

        self._sidebar_label(inner, "Tactic miss rate", small=True, pady=(4, 1))
        self._random_var = tk.DoubleVar(value=0.0)
        ttk.Scale(
            inner, from_=0.0, to=0.5, variable=self._random_var,
            command=self._on_random_change, orient=tk.HORIZONTAL,
        ).pack(padx=10, fill=tk.X)

        # "Use manual settings" button (clears active profile)
        tk.Button(
            inner, text="Use manual settings",
            bg=config.BTN_BG, fg=config.TEXT_MAIN,
            relief=tk.FLAT, font=("Segoe UI", 8),
            command=self._use_manual_mode,
            activebackground=config.BTN_HOVER, activeforeground="white",
        ).pack(padx=10, pady=(8, 4), fill=tk.X)

    def _toggle_manual_section(self) -> None:
        expanded = not self._manual_expanded.get()
        self._manual_expanded.set(expanded)
        if expanded:
            self._manual_frame.pack(fill=tk.X)
            self._manual_header_btn.config(text="⚙ Manual Override  ▾")
        else:
            self._manual_frame.pack_forget()
            self._manual_header_btn.config(text="⚙ Manual Override  ▸")

    def _use_manual_mode(self) -> None:
        """Clear the active profile and use raw slider values."""
        self._active_profile = None
        self._profile_combo_var.set("")
        self._refresh_profile_card(None)
        log.info("Switched to manual mode")

    # ── Board orientation section ─────────────────────────────────────────────

    def _build_board_section(self, parent: tk.Widget) -> None:
        self._sidebar_label(parent, "BOARD", pady=(8, 2))
        self._flip_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            parent, text="Flipped (Black)",
            variable=self._flip_var, command=self._on_flip_change,
            bg=config.BG_SIDEBAR, fg=config.TEXT_MAIN,
            selectcolor=config.BTN_BG, activebackground=config.BG_SIDEBAR,
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, padx=10)

        self._side_var = tk.StringVar(value="White to move")
        tk.Button(
            parent, textvariable=self._side_var,
            bg=config.BTN_BG, fg=config.TEXT_MAIN,
            relief=tk.FLAT, font=("Segoe UI", 9),
            command=self._toggle_active_color,
        ).pack(padx=10, pady=4, fill=tk.X)

        self._grid_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            parent, text="Show grid",
            variable=self._grid_var, command=self._on_grid_toggle,
            bg=config.BG_SIDEBAR, fg=config.TEXT_MAIN,
            selectcolor=config.BTN_BG, activebackground=config.BG_SIDEBAR,
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, padx=10)

        # Click-through
        self._sidebar_label(parent, "INTERACTION", pady=(8, 2))
        self._ct_btn_text = tk.StringVar(value="🖱  Click-Through OFF")
        tk.Button(
            parent, textvariable=self._ct_btn_text,
            bg=config.BTN_BG, fg=config.TEXT_MAIN,
            relief=tk.FLAT, font=("Segoe UI", 9),
            command=self._toggle_clickthrough,
        ).pack(padx=10, pady=2, fill=tk.X)
        self._sidebar_label(parent, "Ctrl+T to toggle", small=True, fg=config.TEXT_DIM)

    # ── Eval section ──────────────────────────────────────────────────────────

    def _build_eval_section(self, parent: tk.Widget) -> None:
        self._sidebar_label(parent, "EVAL", pady=(10, 4))

        self._eval_canvas = tk.Canvas(
            parent, width=config.SIDEBAR_WIDTH - 20, height=12,
            bg="#333333", highlightthickness=0,
        )
        self._eval_canvas.pack(padx=10, pady=(0, 4))

        self._eval_var = tk.StringVar(value="—")
        tk.Label(
            parent, textvariable=self._eval_var,
            bg=config.BG_SIDEBAR, fg="#ffd740",
            font=("Consolas", 14, "bold"),
        ).pack()

        self._move_var = tk.StringVar(value="Waiting…")
        tk.Label(
            parent, textvariable=self._move_var,
            bg=config.BG_SIDEBAR, fg=config.ARROW_COLOR,
            font=("Consolas", 11),
        ).pack(pady=(0, 2))

        self._ponder_var = tk.StringVar(value="")
        tk.Label(
            parent, textvariable=self._ponder_var,
            bg=config.BG_SIDEBAR, fg=config.TEXT_DIM,
            font=("Consolas", 9),
        ).pack(pady=(0, 2))

        # Active profile badge at the bottom of eval
        self._profile_badge_var = tk.StringVar(value="")
        tk.Label(
            parent, textvariable=self._profile_badge_var,
            bg=config.BG_SIDEBAR, fg="#7c7caa",
            font=("Segoe UI", 8, "italic"),
            wraplength=config.SIDEBAR_WIDTH - 16,
        ).pack(pady=(0, 8))

    # ── Grid ──────────────────────────────────────────────────────────────────

    def _draw_grid(self) -> None:
        self._canvas.delete("grid")
        sq = config.SQUARE_SIZE
        for i in range(9):
            x = i * sq
            self._canvas.create_line(
                x, config.BOARD_CANVAS_Y,
                x, config.BOARD_CANVAS_Y + config.BOARD_SIZE,
                fill="#ffffff", width=1, tags="grid",
            )
            y = config.BOARD_CANVAS_Y + i * sq
            self._canvas.create_line(
                0, y, config.BOARD_SIZE, y,
                fill="#ffffff", width=1, tags="grid",
            )

    # ── Workers ───────────────────────────────────────────────────────────────

    def _start_workers(self) -> None:
        self._engine = EngineWorker(result_callback=self._on_engine_result)
        self._engine.start()

        self._vision = VisionLoop(fen_callback=self._on_new_fen)
        self._vision.start()

        log.info("Workers started")
        pname = self._active_profile.name if self._active_profile else "Manual"
        self._status_var.set(f"✅ Engine ready — profile: {pname}")

    # ── Board region ──────────────────────────────────────────────────────────

    def _update_board_region(self) -> None:
        self.root.update_idletasks()
        wx = self.root.winfo_x()
        wy = self.root.winfo_y()
        screen_x = wx + config.BOARD_CANVAS_X
        screen_y = wy + config.BOARD_CANVAS_Y
        if (screen_x, screen_y) == self._last_board_xy:
            return
        self._last_board_xy = (screen_x, screen_y)
        self._vision.set_board_region(screen_x, screen_y, config.BOARD_SIZE)
        log.debug("Board region updated: %d, %d", screen_x, screen_y)

    # ── Drag ──────────────────────────────────────────────────────────────────

    def _drag_start(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag_motion(self, event: tk.Event) -> None:
        new_x = event.x_root - self._drag_x
        new_y = event.y_root - self._drag_y
        self.root.geometry(f"+{new_x}+{new_y}")
        if self._drag_after_id:
            self.root.after_cancel(self._drag_after_id)
        self._drag_after_id = self.root.after(400, self._update_board_region)

    # ── Click-through ─────────────────────────────────────────────────────────

    def _get_hwnd(self) -> Optional[int]:
        if self._hwnd:
            return self._hwnd
        if not _WIN32_AVAILABLE:
            return None
        try:
            self._hwnd = win32gui.FindWindow(None, "Chess Overlay")
            return self._hwnd
        except Exception:
            return None

    def _toggle_clickthrough(self) -> None:
        self._click_through = not self._click_through
        hwnd = self._get_hwnd()
        if hwnd:
            _set_clickthrough(hwnd, self._click_through)
        if self._click_through:
            self._ct_btn_text.set("🖱  Click-Through ON")
            self._status_var.set("🔵 Click-through active — Ctrl+T to disable")
            self._vision.set_enabled(False)
        else:
            self._ct_btn_text.set("🖱  Click-Through OFF")
            pname = self._active_profile.name if self._active_profile else "Manual"
            self._status_var.set(f"✅ Engine ready — profile: {pname}")
            self._vision.set_enabled(True)

    # ── Arrow rendering ───────────────────────────────────────────────────────

    def _clear_arrows(self) -> None:
        self._canvas.delete("arrow")
        self._canvas.delete("ponder_arrow")

    def _update_eval_bar(self, score_cp: Optional[int]) -> None:
        self._eval_canvas.delete("all")
        w = config.SIDEBAR_WIDTH - 20
        h = 12
        if score_cp is None:
            return
        clamped   = max(-500, min(500, score_cp))
        frac      = (clamped + 500) / 1000.0
        white_px  = int(frac * w)
        self._eval_canvas.create_rectangle(0, 0, w, h, fill="#111111", outline="")
        self._eval_canvas.create_rectangle(0, 0, white_px, h, fill="#eeeeee", outline="")
        self._eval_canvas.create_line(w // 2, 0, w // 2, h, fill="#666666", width=1)

    def _render_move(self, result: MoveResult) -> None:
        self._clear_arrows()

        # Primary arrow
        _draw_arrow(
            self._canvas,
            result.from_sq, result.to_sq,
            color=config.ARROW_COLOR if not result.is_human_error else "#ff6b35",
            flipped=self._flipped,
            tag="arrow",
        )

        # Ponder arrow
        if result.ponder_uci:
            try:
                pm = chess.Move.from_uci(result.ponder_uci)
                _draw_arrow(
                    self._canvas,
                    pm.from_square, pm.to_square,
                    color="#ff9800",
                    width=max(4, config.ARROW_WIDTH - 3),
                    flipped=self._flipped,
                    tag="ponder_arrow",
                )
                self._ponder_var.set(f"Ponder: {result.ponder_uci}")
            except Exception:
                self._ponder_var.set("")
        else:
            self._ponder_var.set("")

        # Eval label
        if result.score is not None:
            if abs(result.score) >= 29000:
                mate_in = abs(result.score) - 29000
                label   = f"M{mate_in}" if result.score > 0 else f"-M{mate_in}"
            else:
                label = f"{result.score / 100:+.2f}"
            self._eval_var.set(label)
            self._update_eval_bar(result.score)
        else:
            self._eval_var.set("—")
            self._update_eval_bar(None)

        # Move label
        try:
            board = chess.Board(result.fen)
            san   = board.san(chess.Move.from_uci(result.uci))
        except Exception:
            san = result.uci
        note = " ⚡" if result.is_human_error else ""
        self._move_var.set(f"Best: {san}{note}")

        # Profile badge
        if result.profile_name:
            self._profile_badge_var.set(f"via {result.profile_name}")

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_new_fen(self, fen: str) -> None:
        """Called from VisionLoop thread — marshal to UI thread."""
        self._current_fen = fen
        piece_count = sum(1 for c in fen.split()[0] if c.isalpha())
        side        = "White" if "w" in fen.split()[1] else "Black"

        def _ui_update(f=fen, pc=piece_count, sd=side) -> None:
            self._status_var.set(f"♟ {pc} pieces · {sd} to move")
            # Profile path takes priority; fall back to legacy manual sliders
            if self._active_profile is not None:
                self._engine.request_analysis(f, profile=self._active_profile)
            else:
                self._engine.request_analysis(
                    f,
                    skill_level=self._skill_level,
                    depth=self._depth,
                    humanise=self._humanise,
                )

        self.root.after(0, _ui_update)

    def _on_engine_result(self, result: MoveResult) -> None:
        self.root.after(0, lambda r=result: self._render_move(r))

    # ── Settings callbacks ────────────────────────────────────────────────────

    def _on_skill_change(self, _=None) -> None:
        self._skill_level = self._skill_var.get()
        self._engine.humaniser.skill = self._skill_level

    def _on_depth_change(self, _=None) -> None:
        self._depth = self._depth_var.get()

    def _on_humanise_change(self) -> None:
        self._humanise = self._humanise_var.get()

    def _on_blunder_change(self, _=None) -> None:
        self._engine.humaniser.blunder_rate = self._blunder_var.get()

    def _on_random_change(self, _=None) -> None:
        self._engine.humaniser.randomness = self._random_var.get()

    def _on_flip_change(self) -> None:
        self._flipped = self._flip_var.get()
        self._vision.set_flipped(self._flipped)
        self._clear_arrows()

    def _toggle_active_color(self) -> None:
        current = "b" if self._vision._active_color == "w" else "w"
        self._vision.set_active_color(current)
        label = "White to move" if current == "w" else "Black to move"
        self._side_var.set(label)
        if self._current_fen:
            parts    = self._current_fen.split()
            parts[1] = current
            new_fen  = " ".join(parts)
            if self._active_profile:
                self._engine.request_analysis(new_fen, profile=self._active_profile)
            else:
                self._engine.request_analysis(
                    new_fen, self._skill_level, self._depth, self._humanise,
                )

    def _on_grid_toggle(self) -> None:
        if self._grid_var.get():
            self._draw_grid()
        else:
            self._canvas.delete("grid")

    # ── Hotkeys ───────────────────────────────────────────────────────────────

    def _bind_hotkeys(self) -> None:
        self.root.bind(config.HOTKEY_CLICKTHROUGH, lambda _: self._toggle_clickthrough())
        self.root.bind(config.HOTKEY_TOGGLE_SIDE,  lambda _: self._toggle_active_color())
        self.root.bind(config.HOTKEY_QUIT,         lambda _: self._on_quit())

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _on_quit(self) -> None:
        log.info("Shutting down…")
        try:
            self._vision.stop()
            self._engine.stop()
        except Exception as exc:
            log.error("Error during shutdown: %s", exc)
        finally:
            self.root.destroy()