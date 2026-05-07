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
CALIBRATION_FILE = "calibration.json"

# ── Computer vision ──────────────────────────────────────────────────────────
SCALES               = [0.9, 1.0, 1.1]
CONFIDENCE_THRESHOLD = 0.72
IOU_THRESHOLD        = 0.2
VISION_FPS           = 8         # captures per second
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
DEFAULT_DEPTH       = 7
PROFILE_DEPTH_CAP   = 8         # cap profile searches for quick arrows; set None to disable
ANALYSIS_TIMEOUT_MS = 5000      # kept for reference; depth-only limit now used

# Number of CPU threads Stockfish may use.
# Set to the number of physical cores on your machine for best performance.
# 1 is safe; 4+ is faster on modern CPUs.
STOCKFISH_THREADS = 8

# Hash table size in MiB.  Larger = fewer cache misses in long searches.
# 128 is safe on most machines; 256–512 is better if you have spare RAM.
STOCKFISH_HASH_MB = 512

# ── Arrow style ──────────────────────────────────────────────────────────────
ARROW_COLOR        = "#00e676"   # bright green  (best move)
SECOND_ARROW_COLOR = "#ffd54f"   # yellow        (second candidate)
THIRD_ARROW_COLOR  = "#a0522d"   # reddish brown (third candidate)
ARROW_CANDIDATE_COUNT = 3
ARROW_WIDTH        = 8
ARROWHEAD_LENGTH   = 22
ARROWHEAD_WIDTH    = 14
ARROW_ALPHA        = 0.82
PLAYER_COLOR       = "w"         # arrows only render when this side is to move
BOARD_CLICKTHROUGH_DEFAULT = True
CLICKTHROUGH_POLL_MS      = 100

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
