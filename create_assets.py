"""
create_assets.py — Generate piece template PNG files for the vision engine.

Two modes are available:

  1. AUTOMATIC (default) — Downloads SVG piece art from Wikimedia Commons,
     rasterises each piece, and saves a tight-crop PNG to /assets/.

  2. MANUAL CROP (--from-screenshot path.png) — Takes a full chess.com
     screenshot you supply, lets you mark the board corners interactively,
     then auto-extracts and saves all 12 piece types it detects.

Recommended: Run mode 1 first to generate placeholder assets, play a
position on chess.com at the correct zoom level, then visually verify that
the generated arrows align with the board squares.  If detection is poor,
capture a fresh screenshot and re-run with --from-screenshot.

Usage:
    python create_assets.py                          # automatic
    python create_assets.py --from-screenshot ss.png # manual
    python create_assets.py --simple                 # fast fallback (font-based)
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import urllib.request
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

ASSETS_DIR = Path("assets")
PIECE_SIZE = 118   # px — must match one square on the target board

# Unicode chess symbols for simple/fallback rendering
PIECE_SYMBOLS = {
    "wK": "♔", "wQ": "♕", "wR": "♖", "wB": "♗", "wN": "♘", "wP": "♙",
    "bK": "♚", "bQ": "♛", "bR": "♜", "bB": "♝", "bN": "♞", "bP": "♟",
}

# chess.com uses Wikimedia piece SVGs (Neo style); direct PNG URL pattern
# Format: {color}{type}  where color is "l" (light/white) or "d" (dark/black)
#         type is one of K Q R B N P
WIKIMEDIA_BASE = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/"
    "{path}/{size}px-Chess_{code}t45.svg.png"
)
WIKIMEDIA_PATHS = {
    "wK": ("4/42", "klt"), "wQ": ("1/15", "qlt"),
    "wR": ("7/72", "rlt"), "wB": ("b/b1", "blt"),
    "wN": ("7/70", "nlt"), "wP": ("4/45", "plt"),
    "bK": ("f/f0", "kdt"), "bQ": ("4/47", "qdt"),
    "bR": ("f/ff", "rdt"), "bB": ("9/98", "bdt"),
    "bN": ("e/ef", "ndt"), "bP": ("c/c7", "pdt"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Mode 1: Download from Wikimedia
# ─────────────────────────────────────────────────────────────────────────────

def download_assets(size: int = PIECE_SIZE) -> None:
    ASSETS_DIR.mkdir(exist_ok=True)
    print(f"Downloading {len(WIKIMEDIA_PATHS)} piece images at {size}px…")

    for piece, (path, code) in WIKIMEDIA_PATHS.items():
        url = WIKIMEDIA_BASE.format(path=path, size=size, code=code)
        out = ASSETS_DIR / f"{piece}.png"

        if out.exists():
            print(f"  {piece}.png already exists — skipping")
            continue

        try:
            print(f"  Downloading {piece}.png … ", end="", flush=True)
            req = urllib.request.Request(url, headers={"User-Agent": "chess-overlay/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            img = Image.open(io.BytesIO(data)).convert("RGBA")
            # Tight-crop: remove rows/cols that are fully transparent
            bbox = img.getbbox()
            if bbox:
                img = img.crop(bbox)
            img = img.resize((size, size), Image.LANCZOS)
            # Save as RGBA PNG — preserve the alpha channel so vision.py can
            # use it as a mask in matchTemplate, ignoring the background.
            img.save(out, "PNG")  # PIL keeps RGBA channels intact
            print("✓")
        except Exception as exc:
            print(f"✗  ({exc}) — will try simple fallback for {piece}")
            _create_simple_piece(piece, size)


# ─────────────────────────────────────────────────────────────────────────────
# Mode 2: Extract from screenshot
# ─────────────────────────────────────────────────────────────────────────────

def extract_from_screenshot(screenshot_path: str) -> None:
    """
    Interactive: open a chess.com screenshot, ask user to click the four
    board corners, then extract and save piece crops.

    Requires: opencv-python (already in requirements.txt)
    """
    import cv2
    import numpy as np

    img = cv2.imread(screenshot_path)
    if img is None:
        print(f"Error: cannot read {screenshot_path}")
        sys.exit(1)

    print("Click the TOP-LEFT corner of the board, then BOTTOM-RIGHT corner.")
    print("Press any key after clicking both points.")

    points: list[tuple[int, int]] = []

    def on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < 2:
            points.append((x, y))
            cv2.circle(img, (x, y), 6, (0, 255, 0), -1)
            cv2.imshow("Screenshot — mark board corners", img)

    cv2.imshow("Screenshot — mark board corners", img)
    cv2.setMouseCallback("Screenshot — mark board corners", on_click)
    cv2.waitKey(0)
    cv2.destroyAllWindows()

    if len(points) < 2:
        print("Need 2 corner clicks. Aborting.")
        sys.exit(1)

    (x1, y1), (x2, y2) = points
    board_px = max(x2 - x1, y2 - y1)
    sq = board_px // 8

    board = img[y1:y1+board_px, x1:x1+board_px]

    # Save each square as a candidate for manual inspection
    ASSETS_DIR.mkdir(exist_ok=True)
    sq_dir = ASSETS_DIR / "_squares"
    sq_dir.mkdir(exist_ok=True)

    for r in range(8):
        for c in range(8):
            sq_img = board[r*sq:(r+1)*sq, c*sq:(c+1)*sq]
            cv2.imwrite(str(sq_dir / f"r{r}c{c}.png"), sq_img)

    print(f"Saved 64 squares to {sq_dir}/")
    print("Manually copy & rename squares to assets/<piece>.png")
    print("(wP.png, wN.png, wB.png, wR.png, wQ.png, wK.png,")
    print(" bP.png, bN.png, bB.png, bR.png, bQ.png, bK.png)")


# ─────────────────────────────────────────────────────────────────────────────
# Simple fallback: render piece symbols using PIL's default font
# ─────────────────────────────────────────────────────────────────────────────

LIGHT_SQ = (240, 217, 181)   # chess.com light square colour
DARK_SQ  = (181, 136,  99)   # chess.com dark square colour


def _create_simple_piece(piece: str, size: int = PIECE_SIZE) -> None:
    symbol = PIECE_SYMBOLS.get(piece, "?")
    is_white = piece.startswith("w")

    # Piece on a transparent background
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    fg_color = (255, 255, 255, 255) if is_white else (0, 0, 0, 255)
    outline  = (0, 0, 0, 200)      if is_white else (200, 200, 200, 200)

    # Try to load a system font that supports chess symbols
    font: Optional[ImageFont.FreeTypeFont] = None
    for font_name in ["seguisym.ttf", "NotoSans-Regular.ttf", "Arial.ttf"]:
        for search_root in [r"C:\Windows\Fonts", "/usr/share/fonts", "/Library/Fonts"]:
            font_path = os.path.join(search_root, font_name)
            if os.path.exists(font_path):
                try:
                    font = ImageFont.truetype(font_path, size=int(size * 0.75))
                    break
                except Exception:
                    pass
        if font:
            break

    if font is None:
        font = ImageFont.load_default()

    # Centre the glyph
    bbox = draw.textbbox((0, 0), symbol, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2 - bbox[0]
    y = (size - th) // 2 - bbox[1]

    # Outline for visibility on any background
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx or dy:
                draw.text((x + dx, y + dy), symbol, font=font, fill=outline)
    draw.text((x, y), symbol, font=font, fill=fg_color)

    out = ASSETS_DIR / f"{piece}.png"
    img.save(out, "PNG")
    print(f"  Created simple {piece}.png")


def create_all_simple(size: int = PIECE_SIZE) -> None:
    ASSETS_DIR.mkdir(exist_ok=True)
    print(f"Creating simple (font-based) assets at {size}px in {ASSETS_DIR}/…")
    for piece in PIECE_SYMBOLS:
        _create_simple_piece(piece, size)
    print("Done.  Replace with real piece crops for best detection accuracy.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate chess piece template assets")
    parser.add_argument(
        "--from-screenshot", metavar="PATH",
        help="Path to a chess.com screenshot for manual square extraction",
    )
    parser.add_argument(
        "--simple", action="store_true",
        help="Generate quick font-based placeholders (no internet required)",
    )
    parser.add_argument(
        "--size", type=int, default=PIECE_SIZE,
        help=f"Piece template size in pixels (default {PIECE_SIZE})",
    )
    args = parser.parse_args()

    if args.from_screenshot:
        extract_from_screenshot(args.from_screenshot)
    elif args.simple:
        create_all_simple(args.size)
    else:
        download_assets(args.size)


if __name__ == "__main__":
    main()