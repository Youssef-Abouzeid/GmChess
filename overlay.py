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

import ctypes
import logging
import tkinter as tk
from tkinter import ttk
from typing import Optional

import chess

import config
from engine import EngineWorker, MATE_SCORE, MoveResult
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


def _set_capture_exclusion(hwnd: int, enable: bool) -> None:
    """Ask Windows not to include this overlay in screen-capture APIs."""
    if not _WIN32_AVAILABLE:
        return
    # WDA_EXCLUDEFROMCAPTURE is available on modern Windows 10/11. If the OS
    # does not support it, the call simply fails and the overlay still works.
    affinity = 0x11 if enable else 0x00
    try:
        ok = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, affinity)
        if not ok:
            log.debug("SetWindowDisplayAffinity failed for hwnd=%s", hwnd)
    except Exception as exc:
        log.debug("SetWindowDisplayAffinity unavailable: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Arrow drawing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sq_center(sq: chess.Square, flipped: bool = False) -> tuple[float, float]:
    file_idx = chess.square_file(sq)
    rank_idx = chess.square_rank(sq)
    if flipped:
        col = 7 - file_idx
        row = rank_idx
    else:
        col = file_idx
        row = 7 - rank_idx
    square_size = config.BOARD_SIZE / 8.0
    half = square_size / 2.0
    x = col * square_size + half
    y = row * square_size + half
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
        self._click_through  = getattr(config, "BOARD_CLICKTHROUGH_DEFAULT", True)
        self._clickthrough_applied = False
        self._flipped        = False
        self._humanise       = False
        self._skill_level    = config.DEFAULT_SKILL_LEVEL
        self._depth          = config.DEFAULT_DEPTH
        self._current_fen    = ""
        self._pending_fen: Optional[str] = None
        self._pending_fen_after_id: Optional[str] = None
        self._latest_analysis_request_id: Optional[int] = None
        self._player_color   = getattr(config, "PLAYER_COLOR", "w")
        if self._player_color not in ("w", "b"):
            self._player_color = "w"
        self._engine_status  = "Engine starting"
        self._vision_status  = "Vision starting"
        self._position_status = "Waiting for board"
        self._hwnd: Optional[int] = None
        self._last_board_xy: tuple[int, int] = (-1, -1)

        self._drag_x = 0
        self._drag_y = 0
        self._drag_after_id: Optional[str] = None
        self._analysis_after_id: Optional[str] = None

        # Active profile (None → legacy manual-slider path)
        self._active_profile: Optional[PlayerProfile] = default_profile()

        self._build_window()
        self._build_ui()
        self._bind_hotkeys()
        self._configure_capture_exclusion()
        self._start_workers()
        self._update_board_region()
        self.root.after(500, self._update_board_region)
        self.root.after(getattr(config, "CLICKTHROUGH_POLL_MS", 100), self._update_clickthrough_hit_test)

    # ── Window setup ──────────────────────────────────────────────────────────

    def _load_calibration(self) -> tuple[int, int]:
        import json
        from pathlib import Path
        cal_file = Path(__file__).resolve().with_name(config.CALIBRATION_FILE)
        if cal_file.exists():
            try:
                data = json.loads(cal_file.read_text())
                return int(data.get("x", 100)), int(data.get("y", 100))
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

        # Mouse-wheel scrolling only while the pointer is over the sidebar.
        def _on_mousewheel(event):
            canvas_sb.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_mousewheel(_):
            canvas_sb.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_mousewheel(_):
            canvas_sb.unbind_all("<MouseWheel>")

        canvas_sb.bind("<Enter>", _bind_mousewheel)
        canvas_sb.bind("<Leave>", _unbind_mousewheel)
        sidebar.bind("<Enter>", _bind_mousewheel)
        sidebar.bind("<Leave>", _unbind_mousewheel)

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
        profile_depth = profile.depth
        depth_cap = getattr(config, "PROFILE_DEPTH_CAP", None)
        if depth_cap is not None:
            profile_depth = min(profile_depth, int(depth_cap))
        self._skill_var.set(min(20, profile.stockfish_elo // 125))
        self._depth_var.set(profile_depth)
        self._blunder_var.set(profile.errors.blunder_rate)
        self._random_var.set(profile.errors.miss_tactic_rate)

        log.info("Profile selected: %s", profile.name)
        self._refresh_status_bar()

        # Re-analyse current position with new profile
        self._request_current_analysis()

    def _refresh_profile_card(self, profile: Optional[PlayerProfile]) -> None:
        if profile is None:
            self._profile_title_var.set("Manual mode")
            self._profile_rating_var.set("")
            self._profile_desc_var.set("Use the sliders below to configure strength.")
            return
        self._profile_title_var.set(f"{profile.emoji}  {profile.name}")
        profile_depth = profile.depth
        depth_cap = getattr(config, "PROFILE_DEPTH_CAP", None)
        if depth_cap is not None:
            profile_depth = min(profile_depth, int(depth_cap))
        self._profile_rating_var.set(f"~{profile.rating} ELO  |  depth {profile_depth}")
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
        self._refresh_status_bar()
        self._request_current_analysis()
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

        self._player_color_var = tk.StringVar(value=self._player_color_label())
        tk.Button(
            parent, textvariable=self._player_color_var,
            bg=config.BTN_BG, fg=config.TEXT_MAIN,
            relief=tk.FLAT, font=("Segoe UI", 9),
            command=self._toggle_player_color,
        ).pack(padx=10, pady=(0, 4), fill=tk.X)

        tk.Button(
            parent, text="Refresh analysis",
            bg=config.BTN_BG, fg=config.TEXT_MAIN,
            relief=tk.FLAT, font=("Segoe UI", 8),
            command=self._request_current_analysis,
            activebackground=config.BTN_HOVER, activeforeground="white",
        ).pack(padx=10, pady=(0, 4), fill=tk.X)

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
        click_label = "🖱  Board Click-Through ON" if self._click_through else "🖱  Board Click-Through OFF"
        self._ct_btn_text = tk.StringVar(value=click_label)
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
        sq = config.BOARD_SIZE / 8.0
        for i in range(9):
            x = i * sq
            self._canvas.create_line(
                x, 0,
                x, config.BOARD_SIZE,
                fill="#ffffff", width=1, tags="grid",
            )
            y = i * sq
            self._canvas.create_line(
                0, y, config.BOARD_SIZE, y,
                fill="#ffffff", width=1, tags="grid",
            )

    # ── Workers ───────────────────────────────────────────────────────────────

    def _start_workers(self) -> None:
        self._engine = EngineWorker(
            result_callback=self._on_engine_result,
            status_callback=self._on_engine_status,
        )
        self._engine.start()

        self._vision = VisionLoop(
            fen_callback=self._on_new_fen,
            status_callback=self._on_vision_status,
        )
        self._vision.start()

        log.info("Workers started")
        self._refresh_status_bar()

    def _refresh_status_bar(self) -> None:
        profile = self._active_profile.name if self._active_profile else "Manual"
        prefix = "Board click-through ON | " if self._click_through else ""
        self._status_var.set(
            f"{prefix}{self._engine_status} | {self._vision_status} | "
            f"{self._position_status} | {profile}"
        )

    def _on_engine_status(self, message: str) -> None:
        self.root.after(0, lambda m=message: self._set_engine_status(m))

    def _set_engine_status(self, message: str) -> None:
        self._engine_status = message
        self._refresh_status_bar()

    def _on_vision_status(self, message: str) -> None:
        self.root.after(0, lambda m=message: self._set_vision_status(m))

    def _set_vision_status(self, message: str) -> None:
        self._vision_status = message
        self._refresh_status_bar()

    def _request_current_analysis(self) -> None:
        if not self._current_fen:
            return
        if not self._is_my_turn(self._current_fen):
            self._latest_analysis_request_id = None
            self._clear_arrows()
            self._move_var.set("Best: waiting for your turn")
            self._ponder_var.set("")
            self._eval_var.set("...")
            self._update_eval_bar(None)
            return
        if self._active_profile is not None:
            request_id = self._engine.request_analysis(self._current_fen, profile=self._active_profile)
        else:
            request_id = self._engine.request_analysis(
                self._current_fen,
                skill_level=self._skill_level,
                depth=self._depth,
                humanise=self._humanise,
            )
        if request_id is not None:
            self._latest_analysis_request_id = request_id

    def _schedule_current_analysis(self, delay_ms: int = 250) -> None:
        if self._active_profile is not None or not self._current_fen:
            return
        if self._analysis_after_id:
            self.root.after_cancel(self._analysis_after_id)
        self._analysis_after_id = self.root.after(delay_ms, self._run_scheduled_analysis)

    def _run_scheduled_analysis(self) -> None:
        self._analysis_after_id = None
        self._request_current_analysis()

    # ── Board region ──────────────────────────────────────────────────────────

    def _cancel_pending_fen(self) -> None:
        self._pending_fen = None
        if self._pending_fen_after_id:
            self.root.after_cancel(self._pending_fen_after_id)
            self._pending_fen_after_id = None

    def _fen_turn(self, fen: str) -> str:
        try:
            return fen.split()[1]
        except Exception:
            return ""

    def _is_my_turn(self, fen: str) -> bool:
        return self._fen_turn(fen) == self._player_color

    def _player_color_label(self) -> str:
        side = "White" if self._player_color == "w" else "Black"
        return f"My arrows: {side}"

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

    def _configure_capture_exclusion(self) -> None:
        self.root.update_idletasks()
        hwnd = self._get_hwnd()
        if hwnd:
            _set_capture_exclusion(
                hwnd,
                getattr(config, "EXCLUDE_OVERLAY_FROM_CAPTURE", True),
            )

    def _apply_clickthrough(self, enable: bool) -> None:
        if enable == self._clickthrough_applied:
            return
        hwnd = self._get_hwnd()
        if hwnd:
            _set_clickthrough(hwnd, enable)
            self._clickthrough_applied = enable

    def _cursor_over_board(self) -> bool:
        if not _WIN32_AVAILABLE:
            return False
        try:
            cursor_x, cursor_y = win32gui.GetCursorPos()
            board_x = self.root.winfo_rootx() + config.BOARD_CANVAS_X
            board_y = self.root.winfo_rooty() + config.BOARD_CANVAS_Y
        except Exception:
            return False
        return (
            board_x <= cursor_x < board_x + config.BOARD_SIZE
            and board_y <= cursor_y < board_y + config.BOARD_SIZE
        )

    def _update_clickthrough_hit_test(self) -> None:
        should_passthrough = self._click_through and self._cursor_over_board()
        self._apply_clickthrough(should_passthrough)
        try:
            self.root.after(
                getattr(config, "CLICKTHROUGH_POLL_MS", 100),
                self._update_clickthrough_hit_test,
            )
        except tk.TclError:
            pass

    def _toggle_clickthrough(self) -> None:
        self._click_through = not self._click_through
        if self._click_through:
            self._ct_btn_text.set("Board Click-Through ON")
        else:
            self._ct_btn_text.set("Board Click-Through OFF")
            self._apply_clickthrough(False)
        self._apply_clickthrough(self._click_through and self._cursor_over_board())
        self._refresh_status_bar()
        return
        if self._click_through:
            self._ct_btn_text.set("🖱  Click-Through ON")
        else:
            self._ct_btn_text.set("🖱  Click-Through OFF")
        self._refresh_status_bar()

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

        arrow_count = max(1, getattr(config, "ARROW_CANDIDATE_COUNT", 3))
        candidate_uci = [result.uci]
        for uci, _ in result.all_candidates:
            if uci not in candidate_uci:
                candidate_uci.append(uci)
            if len(candidate_uci) >= arrow_count:
                break

        colors = [
            config.ARROW_COLOR,
            getattr(config, "SECOND_ARROW_COLOR", "#ffd54f"),
            getattr(config, "THIRD_ARROW_COLOR", "#a0522d"),
        ]
        widths = [
            config.ARROW_WIDTH,
            max(4, config.ARROW_WIDTH - 2),
            max(3, config.ARROW_WIDTH - 3),
        ]

        for idx, uci in enumerate(candidate_uci[:arrow_count]):
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                continue
            color = colors[min(idx, len(colors) - 1)]
            width = widths[min(idx, len(widths) - 1)]
            _draw_arrow(
                self._canvas,
                move.from_square, move.to_square,
                color=color,
                width=width,
                flipped=self._flipped,
                tag="arrow",
            )
        self._ponder_var.set("")

        # Eval label
        if result.score is not None:
            if abs(result.score) >= MATE_SCORE - 1000:
                mate_in = MATE_SCORE - abs(result.score)
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
        def _ui_update(f=fen) -> None:
            if f == self._current_fen:
                return
            if f == self._pending_fen:
                return
            self._pending_fen = f
            if self._pending_fen_after_id:
                self.root.after_cancel(self._pending_fen_after_id)
            delay_ms = getattr(config, "FEN_STABILITY_MS", 300)
            self._pending_fen_after_id = self.root.after(delay_ms, self._accept_pending_fen)

        self.root.after(0, _ui_update)

    def _accept_pending_fen(self) -> None:
        self._pending_fen_after_id = None
        fen = self._pending_fen
        self._pending_fen = None
        if not fen or fen == self._current_fen:
            return

        self._current_fen = fen
        piece_count = sum(1 for c in fen.split()[0] if c.isalpha())
        side        = "White" if "w" in fen.split()[1] else "Black"
        self._position_status = f"{piece_count} pieces, {side} to move"
        self._clear_arrows()
        if self._is_my_turn(fen):
            self._move_var.set("Best: analyzing...")
        else:
            self._move_var.set("Best: waiting for your turn")
        self._ponder_var.set("")
        self._eval_var.set("...")
        self._update_eval_bar(None)
        self._refresh_status_bar()
        self._request_current_analysis()

    def _on_engine_result(self, result: MoveResult) -> None:
        self.root.after(0, lambda r=result: self._render_fresh_move(r))

    def _render_fresh_move(self, result: MoveResult) -> None:
        if result.fen != self._current_fen:
            log.debug("Ignoring stale engine result for FEN: %s", result.fen)
            return
        if not self._is_my_turn(result.fen):
            self._clear_arrows()
            self._move_var.set("Best: waiting for your turn")
            self._ponder_var.set("")
            return
        if (
            self._latest_analysis_request_id is not None
            and result.request_id != self._latest_analysis_request_id
        ):
            log.debug(
                "Ignoring stale engine result request %s; latest is %s",
                result.request_id,
                self._latest_analysis_request_id,
            )
            return
        self._render_move(result)

    # ── Settings callbacks ────────────────────────────────────────────────────

    def _on_skill_change(self, _=None) -> None:
        self._skill_level = self._skill_var.get()
        self._engine.humaniser.skill = self._skill_level
        self._schedule_current_analysis()

    def _on_depth_change(self, _=None) -> None:
        self._depth = self._depth_var.get()
        self._schedule_current_analysis()

    def _on_humanise_change(self) -> None:
        self._humanise = self._humanise_var.get()
        self._request_current_analysis()

    def _on_blunder_change(self, _=None) -> None:
        self._engine.humaniser.blunder_rate = self._blunder_var.get()
        self._schedule_current_analysis()

    def _on_random_change(self, _=None) -> None:
        self._engine.humaniser.randomness = self._random_var.get()
        self._schedule_current_analysis()

    def _on_flip_change(self) -> None:
        self._cancel_pending_fen()
        self._flipped = self._flip_var.get()
        self._vision.set_flipped(self._flipped)
        self._clear_arrows()
        self._position_status = "Waiting for board"
        self._refresh_status_bar()

    def _toggle_active_color(self) -> None:
        self._cancel_pending_fen()
        current = self._vision.toggle_active_color()
        label = "White to move" if current == "w" else "Black to move"
        self._side_var.set(label)
        if self._current_fen:
            parts    = self._current_fen.split()
            parts[1] = current
            new_fen  = " ".join(parts)
            self._current_fen = new_fen
            side = "White" if current == "w" else "Black"
            piece_count = sum(1 for c in parts[0] if c.isalpha())
            self._position_status = f"{piece_count} pieces, {side} to move"
            self._refresh_status_bar()
            self._request_current_analysis()

    def _toggle_player_color(self) -> None:
        self._cancel_pending_fen()
        self._player_color = "b" if self._player_color == "w" else "w"
        self._player_color_var.set(self._player_color_label())
        self._clear_arrows()
        self._refresh_status_bar()
        self._request_current_analysis()

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
