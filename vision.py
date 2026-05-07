"""
vision.py — Screen capture, multi-scale template matching, NMS, FEN builder.

Runs in its own daemon thread.  Posts completed FEN strings to a callback.

Improvements vs original:
  - Castling rights are inferred from king/rook positions instead of always
    being "KQkq".  This eliminates the constant is_valid() warnings and
    produces more accurate FENs.
  - Positions with fewer than MIN_PIECES_FOR_FEN detections are silently
    dropped before FEN construction — no more empty-board spam to the engine.
  - Detection score tracking uses a separate dict so the grid dict contains
    only (file, rank) -> piece_char entries (cleaner, no string key pollution).
  - set_active_color / set_flipped now reset _last_fen so the new setting
    triggers immediate re-analysis.
  - FENs missing either king are rejected before reaching the engine callback,
    eliminating the constant "missing king(s)" WARNING log spam.
  - Template matching is now parallelised across all 12 piece types using a
    ThreadPoolExecutor (OpenCV releases the GIL), giving a meaningful speed-up
    on multi-core machines without any result-ordering changes.
  - Board region update during drag is guarded so only the final settled
    position is sent to set_board_region, preventing redundant captures.
"""

from __future__ import annotations

import threading
import time
import logging
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np
import mss
import chess

import config

log = logging.getLogger(__name__)

# Suppress FEN construction for very sparse detections (overlay not over board)
MIN_PIECES_FOR_FEN = 4


# ─────────────────────────────────────────────────────────────────────────────
# Helper: IoU / Non-Maximum Suppression
# ─────────────────────────────────────────────────────────────────────────────

def _iou(a: tuple, b: tuple) -> float:
    """Intersection over union for two (x, y, w, h) boxes."""
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih   = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter    = iw * ih
    union    = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def nms(detections: list[dict], iou_thresh: float = config.IOU_THRESHOLD) -> list[dict]:
    """Non-Maximum Suppression across all piece types combined."""
    if not detections:
        return []

    boxes = np.array(
        [
            (d["x"], d["y"], d["x"] + d["w"], d["y"] + d["h"], d["score"])
            for d in detections
        ],
        dtype=np.float32,
    )
    x1, y1, x2, y2, scores = boxes.T
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    kept_idx: list[int] = []

    while order.size:
        i = int(order[0])
        kept_idx.append(i)
        if order.size == 1:
            break

        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])

        inter_w = np.maximum(0.0, xx2 - xx1)
        inter_h = np.maximum(0.0, yy2 - yy1)
        inter = inter_w * inter_h
        union = areas[i] + areas[rest] - inter
        iou = np.divide(
            inter,
            union,
            out=np.zeros_like(inter),
            where=union > 0,
        )
        order = rest[iou < iou_thresh]

    return [detections[i] for i in kept_idx]


# ─────────────────────────────────────────────────────────────────────────────
# Castling rights inference
# ─────────────────────────────────────────────────────────────────────────────

def _infer_castling(grid: dict[tuple[int, int], str]) -> str:
    """
    Grant castling rights only when king and rook are on their starting squares.

    Starting squares (file_idx, rank):
      White king e1=(4,0)  White rooks: a1=(0,0), h1=(7,0)
      Black king e8=(4,7)  Black rooks: a8=(0,7), h8=(7,7)

    This prevents the constant is_valid() warnings caused by claiming full
    castling rights on positions where kings/rooks have clearly moved.
    """
    rights = ""
    if grid.get((4, 0)) == "K":
        if grid.get((7, 0)) == "R":
            rights += "K"
        if grid.get((0, 0)) == "R":
            rights += "Q"
    if grid.get((4, 7)) == "k":
        if grid.get((7, 7)) == "r":
            rights += "k"
        if grid.get((0, 7)) == "r":
            rights += "q"
    return rights or "-"


# ─────────────────────────────────────────────────────────────────────────────
# FEN builder
# ─────────────────────────────────────────────────────────────────────────────

def detections_to_fen(
    detections: list[dict],
    board_x: int,
    board_y: int,
    board_px: int = config.BOARD_SIZE,
    active_color: str = "w",
    flipped: bool = False,
) -> Optional[str]:
    """
    Map detections onto an 8x8 grid and build a FEN string.
    Use board_x/board_y only when detections are in screen coordinates.
    Returns None if there are too few pieces or the FEN can't be parsed.
    """
    if len(detections) < MIN_PIECES_FOR_FEN:
        return None

    sq = board_px / 8.0
    grid: dict[tuple[int, int], str] = {}
    scores: dict[tuple[int, int], float] = {}

    for det in detections:
        cx = det["x"] + det["w"] / 2 - board_x
        cy = det["y"] + det["h"] / 2 - board_y
        col = int(cx / sq)
        row = int(cy / sq)
        if not (0 <= col < 8 and 0 <= row < 8):
            continue

        file_idx = col if not flipped else 7 - col
        rank_idx = row if not flipped else 7 - row
        rank = 7 - rank_idx

        key = (file_idx, rank)
        fen_char = config.PIECE_TO_FEN.get(det["piece"], "?")
        if fen_char == "?":
            continue

        if key not in grid or det["score"] > scores.get(key, 0.0):
            grid[key] = fen_char
            scores[key] = det["score"]

    # Build rank strings
    rows: list[str] = []
    for rank in range(7, -1, -1):
        row_str = ""
        empty = 0
        for file_idx in range(8):
            piece = grid.get((file_idx, rank))
            if piece:
                if empty:
                    row_str += str(empty)
                    empty = 0
                row_str += piece
            else:
                empty += 1
        if empty:
            row_str += str(empty)
        rows.append(row_str)

    fen_board = "/".join(rows)
    castling  = _infer_castling(grid)
    fen       = f"{fen_board} {active_color} {castling} - 0 1"

    # Reject before touching chess.Board — avoids engine WARNING spam for
    # mid-transition frames where a king is temporarily undetected.
    piece_chars = set(grid.values())
    if "K" not in piece_chars or "k" not in piece_chars:
        log.debug("FEN rejected: missing king(s) — skipping transient frame")
        return None

    try:
        board = chess.Board(fen)
        if not board.is_valid():
            log.debug("FEN failed is_valid() (unusual position): %s", fen)
            if getattr(config, "VISION_REQUIRE_VALID_FEN", True):
                return None
            # Compatibility escape hatch: allow unusual FENs only when configured.
    except Exception as exc:
        log.error("FEN parse error: %s  fen=%s", exc, fen)
        return None

    return fen


# ─────────────────────────────────────────────────────────────────────────────
# Template loader / matcher
# ─────────────────────────────────────────────────────────────────────────────

class TemplateBank:
    """Load piece PNG templates once; expose match() method."""

    def __init__(self, assets_dir: str = "assets"):
        self.templates: dict[str, np.ndarray] = {}
        self.gray_templates: dict[str, np.ndarray] = {}
        self._load(assets_dir)

    def _load(self, assets_dir: str) -> None:
        path = Path(assets_dir)
        missing: list[str] = []

        for piece in config.PIECES:
            fpath = path / f"{piece}.png"
            if not fpath.exists():
                missing.append(piece)
                continue
            img = cv2.imread(str(fpath), cv2.IMREAD_UNCHANGED)
            if img is None:
                log.error("Could not read template: %s", fpath)
                missing.append(piece)
                continue

            if img.ndim == 3 and img.shape[2] == 4:
                alpha = img[:, :, 3:4] / 255.0
                rgb   = img[:, :, :3].astype(float)
                white = np.ones_like(rgb) * 255
                img   = (rgb * alpha + white * (1 - alpha)).astype(np.uint8)

            self.templates[piece] = img
            if img.ndim == 2:
                self.gray_templates[piece] = img
            else:
                self.gray_templates[piece] = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        if missing:
            log.warning(
                "Missing templates for: %s  —  run create_assets.py first.",
                ", ".join(missing)
            )

    def match(
        self,
        board_img: np.ndarray,
        piece: str,
        scales: list[float] = config.SCALES,
        threshold: float     = config.CONFIDENCE_THRESHOLD,
    ) -> list[dict]:
        """Multi-scale template matching for a single piece type."""
        gray_tmpl = self.gray_templates.get(piece)
        if gray_tmpl is None:
            return []

        th, tw = gray_tmpl.shape[:2]
        detections: list[dict] = []

        if board_img.ndim == 2:
            gray_board = board_img
        else:
            gray_board = cv2.cvtColor(board_img, cv2.COLOR_BGR2GRAY)

        for scale in scales:
            new_w = max(1, int(tw * scale))
            new_h = max(1, int(th * scale))
            resized = cv2.resize(gray_tmpl, (new_w, new_h))

            if resized.shape[0] > gray_board.shape[0] or resized.shape[1] > gray_board.shape[1]:
                continue

            result = cv2.matchTemplate(gray_board, resized, cv2.TM_CCOEFF_NORMED)
            locs   = np.where(result >= threshold)

            for pt in zip(*locs[::-1]):
                x, y = int(pt[0]), int(pt[1])
                score = float(result[y, x])
                detections.append({
                    "piece": piece,
                    "x": x, "y": y,
                    "w": new_w, "h": new_h,
                    "score": score,
                })

        return detections


# ─────────────────────────────────────────────────────────────────────────────
# Vision loop (threaded)
# ─────────────────────────────────────────────────────────────────────────────

class VisionLoop:
    """
    Continuously captures the board region, runs template matching, and
    calls fen_callback(fen_str) whenever the position changes.
    """

    def __init__(
        self,
        fen_callback: Callable[[str], None],
        status_callback: Optional[Callable[[str], None]] = None,
        assets_dir: str = "assets",
    ):
        self.fen_callback   = fen_callback
        self.status_callback = status_callback
        self.bank           = TemplateBank(assets_dir)
        self._stop_evt      = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock    = threading.Lock()
        # One worker per piece type; recreated when the loop is restarted.
        self._executor: Optional[ThreadPoolExecutor] = None

        self.board_screen_x: int = 0
        self.board_screen_y: int = 0
        self.board_px: int       = config.BOARD_SIZE

        self._last_fen:    str  = ""
        self._fen_window: deque[str] = deque(
            maxlen=getattr(config, "VISION_FEN_WINDOW", 5)
        )
        self._active_color: str = "w"
        self._flipped:     bool = False
        self._enabled:     bool = True
        self._last_status: str = ""
        
        # Cache mss.mss() context to avoid expensive handle allocation every frame
        self._mss_context: Optional[mss.mss] = None

    # ── Public API ───────────────────────────────────────────────────────────

    def set_board_region(self, screen_x: int, screen_y: int, board_px: int = config.BOARD_SIZE) -> None:
        with self._state_lock:
            self.board_screen_x = screen_x
            self.board_screen_y = screen_y
            self.board_px       = board_px

    def set_active_color(self, color: str) -> None:
        with self._state_lock:
            self._active_color = color
            self._last_fen = ""   # force re-analysis with new side-to-move
            self._fen_window.clear()

    def get_active_color(self) -> str:
        with self._state_lock:
            return self._active_color

    def toggle_active_color(self) -> str:
        with self._state_lock:
            self._active_color = "b" if self._active_color == "w" else "w"
            self._last_fen = ""
            self._fen_window.clear()
            return self._active_color

    def set_flipped(self, flipped: bool) -> None:
        with self._state_lock:
            self._flipped = flipped
            self._last_fen = ""
            self._fen_window.clear()

    def set_enabled(self, enabled: bool) -> None:
        with self._state_lock:
            self._enabled = enabled
        self._emit_status("Vision active" if enabled else "Vision paused")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ensure_executor()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="VisionLoop")
        self._thread.start()
        self._emit_status("Vision active")
        log.info("VisionLoop started")

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None
        # Clean up mss context on shutdown
        if self._mss_context is not None:
            try:
                self._mss_context.close()
            except Exception:
                pass
            self._mss_context = None
        log.info("VisionLoop stopped")

    # ── Internal ─────────────────────────────────────────────────────────────

    def _ensure_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=12,
                thread_name_prefix="vision",
            )
        return self._executor

    def _emit_status(self, message: str) -> None:
        with self._state_lock:
            if message == self._last_status:
                return
            self._last_status = message
        if self.status_callback is None:
            return
        try:
            self.status_callback(message)
        except Exception as exc:
            log.error("vision status_callback raised: %s", exc)

    def _capture_board(
        self,
        screen_x: int,
        screen_y: int,
        board_px: int,
    ) -> Optional[np.ndarray]:
        region = {
            "left":   screen_x,
            "top":    screen_y,
            "width":  board_px,
            "height": board_px,
        }
        try:
            # Reuse mss.mss() context to avoid expensive handle allocation every frame
            if self._mss_context is None:
                self._mss_context = mss.mss()
            raw = self._mss_context.grab(region)
            img = np.array(raw)
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            return img
        except Exception as exc:
            log.error("Screen capture failed: %s", exc)
            # Reset context on error so it can be recreated
            self._mss_context = None
            self._emit_status("Capture failed")
            return None

    def _detect_all(self, board_img: np.ndarray) -> list[dict]:
        """Match all 12 piece types in parallel (OpenCV releases the GIL)."""
        executor = self._ensure_executor()
        gray_board = cv2.cvtColor(board_img, cv2.COLOR_BGR2GRAY)

        def match_piece(piece: str) -> list[dict]:
            try:
                return self.bank.match(gray_board, piece)
            except Exception as exc:
                log.error("match() raised for %s: %s", piece, exc)
                return []

        all_dets: list[dict] = []
        for dets in executor.map(match_piece, config.PIECES):
            all_dets.extend(dets)
        return nms(all_dets)

    def _stable_fen(self, raw_fen: str) -> Optional[str]:
        """Return a majority-stable FEN, or None while raw frames are noisy."""
        with self._state_lock:
            self._fen_window.append(raw_fen)
            min_votes = max(1, getattr(config, "VISION_FEN_MIN_VOTES", 3))
            candidate, votes = Counter(self._fen_window).most_common(1)[0]
        if votes < min_votes:
            log.debug("FEN pending stability (%d/%d): %s", votes, min_votes, raw_fen)
            return None
        return candidate

    def _run(self) -> None:
        interval = config.VISION_INTERVAL_MS / 1000.0

        while not self._stop_evt.is_set():
            t0 = time.monotonic()

            with self._state_lock:
                enabled = self._enabled
                board_screen_x = self.board_screen_x
                board_screen_y = self.board_screen_y
                board_px = self.board_px
                active_color = self._active_color
                flipped = self._flipped

            if enabled and board_px > 0:
                board_img = self._capture_board(board_screen_x, board_screen_y, board_px)
                if board_img is not None:
                    detections = self._detect_all(board_img)
                    if not detections:
                        self._emit_status("No board detected")
                        elapsed = time.monotonic() - t0
                        sleep_t  = max(0.0, interval - elapsed)
                        self._stop_evt.wait(timeout=sleep_t)
                        continue

                    fen = detections_to_fen(
                        detections,
                        board_x=0,
                        board_y=0,
                        board_px=board_px,
                        active_color=active_color,
                        flipped=flipped,
                    )

                    if fen:
                        stable_fen = self._stable_fen(fen)
                        if stable_fen is None:
                            elapsed = time.monotonic() - t0
                            sleep_t  = max(0.0, interval - elapsed)
                            self._stop_evt.wait(timeout=sleep_t)
                            continue

                        self._emit_status("Board detected")
                        with self._state_lock:
                            is_new = stable_fen != self._last_fen
                            if is_new:
                                self._last_fen = stable_fen
                        if is_new:
                            log.debug("New FEN: %s", stable_fen)
                            try:
                                self.fen_callback(stable_fen)
                            except Exception as exc:
                                log.error("fen_callback raised: %s", exc)
                    else:
                        self._emit_status("Board not recognized")

            elapsed = time.monotonic() - t0
            sleep_t  = max(0.0, interval - elapsed)
            self._stop_evt.wait(timeout=sleep_t)
