"""
main.py — Entry point for Chess Overlay Engine.

Usage:
    python main.py

Prerequisites:
    1.  pip install -r requirements.txt
    2.  Download Stockfish and set STOCKFISH_PATH in config.py
    3.  Run create_assets.py to generate piece templates (or replace with
        tightly-cropped PNGs from a chess.com screenshot in the /assets folder)
"""

import logging
import sys
import tkinter as tk

# ── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)-18s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("chess_overlay.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def main() -> None:
    log.info("Chess Overlay Engine starting")

    root = tk.Tk()

    # Guard: import overlay AFTER root exists (some Tkinter vars need a root)
    from overlay import ChessOverlay
    app = ChessOverlay(root)   # noqa: F841

    try:
        root.mainloop()
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    finally:
        log.info("Exited cleanly")


if __name__ == "__main__":
    main()
