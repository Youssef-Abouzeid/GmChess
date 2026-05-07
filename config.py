# config.py — Central configuration for Chess Overlay Engine

# ── Window geometry ──────────────────────────────────────────────────────────
WINDOW_WIDTH   = 1130
WINDOW_HEIGHT  = 990
BOARD_SIZE     = 950         # board canvas square (px)
SIDEBAR_WIDTH  = 180         # right-side panel (px)
TOP_BAR_HEIGHT = 40          # drag handle / status bar (px)

# Derived board offsets within the overlay window
BOARD_CANVAS_X = 0
BOARD_CANVAS_Y = TOP_BAR_HEIGHT
SQUARE_SIZE    = BOARD_SIZE // 8   # 118 px per square

# ── Computer vision ──────────────────────────────────────────────────────────
SCALES               = [0.8, 0.9, 1.0, 1.1, 1.2]
CONFIDENCE_THRESHOLD = 0.68
IOU_THRESHOLD        = 0.2
VISION_FPS           = 4          # captures per second
VISION_INTERVAL_MS   = 1000 // VISION_FPS

# ── Piece identifiers ────────────────────────────────────────────────────────
PIECES = ["wP","wN","wB","wR","wQ","wK",
          "bP","bN","bB","bR","bQ","bK"]

PIECE_TO_FEN = {
    "wP":"P","wN":"N","wB":"B","wR":"R","wQ":"Q","wK":"K",
    "bP":"p","bN":"n","bB":"b","bR":"r","bQ":"q","bK":"k",
}

# ── Stockfish ────────────────────────────────────────────────────────────────
# Adjust to your local Stockfish binary path
STOCKFISH_PATH = r"C:\Program files\stockfish\stockfish.exe"
DEFAULT_SKILL_LEVEL = 20        # 0 (weakest) – 20 (strongest)
DEFAULT_DEPTH       = 15
ANALYSIS_TIMEOUT_MS = 5000      # kept for reference; depth-only limit now used

# Number of CPU threads Stockfish may use.
# Set to the number of physical cores on your machine for best performance.
# 1 is safe; 4+ is faster on modern CPUs.
STOCKFISH_THREADS = 4

# Hash table size in MiB.  Larger = fewer cache misses in long searches.
# 128 is safe on most machines; 256–512 is better if you have spare RAM.
STOCKFISH_HASH_MB = 256

# ── Arrow style ──────────────────────────────────────────────────────────────
ARROW_COLOR        = "#00e676"   # bright green  (best move)
ARROW_WIDTH        = 8
ARROWHEAD_LENGTH   = 22
ARROWHEAD_WIDTH    = 14
ARROW_ALPHA        = 0.82

# ── UI colours ───────────────────────────────────────────────────────────────
BG_TOPBAR   = "#1a1a2e"
BG_SIDEBAR  = "#16213e"
BG_BOARD    = "#0f3460"
ACCENT      = "#e94560"
TEXT_MAIN   = "#e0e0e0"
TEXT_DIM    = "#888888"
BTN_BG      = "#0f3460"
BTN_HOVER   = "#e94560"

# ── Hotkeys ──────────────────────────────────────────────────────────────────
HOTKEY_CLICKTHROUGH = "<Control-t>"
HOTKEY_TOGGLE_SIDE  = "<Control-s>"
HOTKEY_QUIT         = "<Control-q>"