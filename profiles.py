"""
profiles.py — Human player profiles for Chess Overlay Engine.

Each profile bundles together:
  - Stockfish strength (ELO + search depth)
  - How many candidate moves to consider (multipv)
  - Style preferences (aggression, sharpness, solidity …)
  - Human error patterns (blunders, missed tactics, consistency)

Three profile categories are provided:

  RATING_PROFILES  — Skill-level archetypes from 1200 to 2900
  LEGEND_PROFILES  — Famous historical / active GMs
  STYLE_PROFILES   — Pure style archetypes (Aggressor, Fortress, …)

Usage inside engine.py:

    from profiles import ALL_PROFILES, select_move_for_profile

    profile  = ALL_PROFILES["magnus_carlsen"]
    chosen, is_err = select_move_for_profile(board, candidates, profile)
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Optional

import chess

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Parameter dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StyleParams:
    """
    Controls how the engine selects among the top-N candidate moves.

    All values 0.0–1.0 unless stated otherwise.

    aggression        — Bonus weight for captures, checks, king-side thrusts.
    sharpness         — Bonus for imbalanced, double-edged positions.
    solidity          — How strongly the best (safest) move is preferred.
                        High solidity = rarely deviates from engine top-1.
    sacrifice_tendency— Extra willingness to enter lines that lose material
                        temporarily for activity / initiative.
    draw_avoidance    — 0 = happy to repeat / draw; 1 = always fights on.
    endgame_precision — Accuracy bonus applied once fewer than 10 pieces remain.
    """
    aggression:         float = 0.50
    sharpness:          float = 0.50
    solidity:           float = 0.60
    sacrifice_tendency: float = 0.25
    draw_avoidance:     float = 0.50
    endgame_precision:  float = 0.50


@dataclass
class ErrorParams:
    """
    Controls the frequency and type of human-like mistakes injected.

    All values are per-move probabilities (0.0–1.0).

    blunder_rate      — P(pick a random legal move instead of any candidate).
    miss_tactic_rate  — P(skip the best move; pick from the rest weighted by score).
    consistency       — 0 = wildly erratic quality, 1 = rock-solid every move.
                        Internally maps to softmax temperature.
    time_pressure_amp — Multiplier applied to blunder/miss rates when the
                        position is highly complex (many legal moves).
    """
    blunder_rate:      float = 0.00
    miss_tactic_rate:  float = 0.00
    consistency:       float = 1.00
    time_pressure_amp: float = 0.00


@dataclass
class PlayerProfile:
    """Complete behavioural blueprint for a simulated player."""

    # ── Identity ──────────────────────────────────────────────────────────────
    id:          str
    name:        str
    emoji:       str
    category:    str        # "rating" | "legend" | "style"
    description: str

    # ── Engine strength ───────────────────────────────────────────────────────
    rating:        int      # approximate ELO shown in the UI
    stockfish_elo: int      # UCI_Elo passed to Stockfish (≤2850)
    depth:         int      # search depth
    multipv:       int      # number of candidate lines to analyse (1–5)

    # ── Behaviour ─────────────────────────────────────────────────────────────
    style:  StyleParams
    errors: ErrorParams

    # ── Flavour (informational only) ─────────────────────────────────────────
    openings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.emoji} {self.name} (~{self.rating})"


# ─────────────────────────────────────────────────────────────────────────────
# Move selection core
# ─────────────────────────────────────────────────────────────────────────────

def _move_characteristics(move_uci: str, board: chess.Board, legal_count_cache: Optional[int] = None) -> dict:
    """
    Return a lightweight dict of binary/float features for a move.

    The board must NOT have the move pushed yet.
    
    Args:
        move_uci: The move to analyze
        board: Current board position
        legal_count_cache: Optional pre-computed legal moves count to avoid O(n²)
    """
    move = chess.Move.from_uci(move_uci)
    # Cache legal_moves.count() to avoid O(n²) when called in loops
    if legal_count_cache is None:
        legal_count_cache = board.legal_moves.count()
    
    features: dict = {
        "is_capture":      board.is_capture(move),
        "is_en_passant":   board.is_en_passant(move),
        "is_castling":     board.is_castling(move),
        "gives_check":     False,
        "is_promotion":    move.promotion is not None,
        "legal_count":     legal_count_cache,
    }

    analysis_board = board.copy(stack=False)
    analysis_board.push(move)
    features["gives_check"] = analysis_board.is_check()
    features["is_checkmate"] = analysis_board.is_checkmate()

    return features


def _style_multiplier(
    move_uci: str,
    features: dict,
    delta_cp: float,          # how many centipawns worse than best (≥ 0)
    profile: PlayerProfile,
    piece_count: int,
) -> float:
    """
    Return a style-based weight multiplier (> 0).

    The multiplier is *relative*; it only matters compared to other candidates.
    """
    style = profile.style
    mult  = 1.0

    # ── Aggression ────────────────────────────────────────────────────────────
    if features["gives_check"]:
        mult *= 1.0 + style.aggression * 0.30
    if features["is_checkmate"]:
        mult *= 10.0                             # always prefer mate
    if features["is_capture"] and not features["is_en_passant"]:
        mult *= 1.0 + style.aggression * 0.25
    if features["is_promotion"]:
        mult *= 1.0 + style.aggression * 0.15

    # ── Solidity: penalise moves far from best ────────────────────────────────
    # A very solid player (solidity=1) strongly prefers the engine's top choice.
    if delta_cp > 0:
        penalty_factor = style.solidity * 0.008   # how aggressively to penalise
        mult *= math.exp(-delta_cp * penalty_factor)

    # ── Endgame precision bonus ───────────────────────────────────────────────
    if piece_count <= 10:
        # Precise endgame players get an extra "best-move" pull
        if delta_cp == 0:
            mult *= 1.0 + style.endgame_precision * 0.40

    # ── Sacrifice tendency: boost losing-material moves when aggressive ────────
    # (Heuristic: a capture where we "give" is implicitly more aggressive)
    if delta_cp > 150 and style.sacrifice_tendency > 0.5:
        mult *= 1.0 + (style.sacrifice_tendency - 0.5) * 0.6

    return max(mult, 1e-6)


def select_move_for_profile(
    board: chess.Board,
    candidates: list[tuple[str, Optional[int]]],   # (uci, score_cp) sorted best-first
    profile: PlayerProfile,
) -> tuple[str, bool]:
    """
    Choose a move from `candidates` according to `profile`.

    Returns (chosen_uci, is_human_error).
    An "error" is any pick other than candidates[0] (the engine's top move).
    """
    if not candidates:
        raise ValueError("candidates must be non-empty")

    legal_moves = list(board.legal_moves)
    legal_uci = [m.uci() for m in legal_moves]
    legal_uci_set = set(legal_uci)
    if not legal_uci:
        raise ValueError("board has no legal moves")

    candidates = [c for c in candidates if c[0] in legal_uci_set]
    if not candidates:
        chosen = random.choice(legal_uci)
        log.warning("Profile %s: no legal engine candidates; using %s", profile.id, chosen)
        return chosen, True

    best_uci, best_raw = candidates[0]

    # Single option — nothing to decide.
    if len(candidates) == 1:
        return best_uci, False

    best_cp   = best_raw if best_raw is not None else 0
    piece_cnt = sum(1 for sq in chess.SQUARES if board.piece_at(sq))

    # ── Complexity amplifier for time-pressure errors ─────────────────────────
    legal_cnt   = len(legal_moves)
    complexity  = min(1.0, legal_cnt / 40.0)   # 0 = simple, 1 = very complex
    amp         = 1.0 + profile.errors.time_pressure_amp * complexity

    # ── Outright blunder: random legal move ───────────────────────────────────
    effective_blunder = profile.errors.blunder_rate * amp
    if random.random() < effective_blunder:
        chosen = random.choice(legal_uci)
        log.debug("Profile %s: blunder → %s (random legal)", profile.id, chosen)
        return chosen, chosen != best_uci

    # ── Tactical miss: skip the best move, pick from the rest ─────────────────
    effective_miss = profile.errors.miss_tactic_rate * amp
    if len(candidates) >= 2 and random.random() < effective_miss:
        pool = candidates[1:]
        scores = [c[1] if c[1] is not None else best_cp - 200 for c in pool]
        min_s  = min(scores)
        weights = [math.exp((s - min_s) / 60.0) for s in scores]
        chosen = random.choices([c[0] for c in pool], weights=weights)[0]
        log.debug("Profile %s: missed tactic → %s", profile.id, chosen)
        return chosen, True

    # ── Style-weighted selection among all candidates ──────────────────────────
    # Temperature: lower = more peaked around best move.
    # consistency=1 → temperature≈10 (almost always best)
    # consistency=0 → temperature≈220 (nearly uniform)
    temperature = 10.0 + (1.0 - profile.errors.consistency) * 210.0

    # Reuse the legal move count collected above.
    legal_count_cache = legal_cnt

    weights = []
    for uci, raw_score in candidates:
        sc    = raw_score if raw_score is not None else best_cp - 300
        delta = max(0.0, best_cp - sc)

        # Softmax base weight
        base  = math.exp(-delta / temperature)

        try:
            feats = _move_characteristics(uci, board, legal_count_cache)
        except Exception as exc:
            log.debug("Skipping candidate %s during feature extraction: %s", uci, exc)
            weights.append(0.0)
            continue
        smult = _style_multiplier(uci, feats, delta, profile, piece_cnt)

        weights.append(base * smult)

    total = sum(weights)
    if total <= 0:
        return best_uci, False

    probs  = [w / total for w in weights]
    chosen = random.choices([c[0] for c in candidates], weights=probs)[0]

    is_error = chosen != best_uci
    if is_error:
        log.debug(
            "Profile %s: style diverged from best (%s → %s)",
            profile.id, best_uci, chosen,
        )
    return chosen, is_error


# ─────────────────────────────────────────────────────────────────────────────
# ── Profile definitions ───────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def _p(id_, name, emoji, cat, desc, rating, elo, depth, multipv,
       style: StyleParams, errors: ErrorParams, openings=None) -> PlayerProfile:
    """Shorthand constructor."""
    return PlayerProfile(
        id=id_, name=name, emoji=emoji, category=cat, description=desc,
        rating=rating, stockfish_elo=elo, depth=depth, multipv=multipv,
        style=style, errors=errors,
        openings=openings or [],
    )


# ══════════════════════════════════════════════════════════════════════════════
# RATING ARCHETYPES
# ══════════════════════════════════════════════════════════════════════════════

RATING_PROFILES: dict[str, PlayerProfile] = {

    "r1200": _p(
        "r1200", "Beginner (1200)", "🟤", "rating",
        "Hangs pieces, misses basic tactics, plays random-looking moves. "
        "Every game is an adventure.",
        1200, 1200, 5, 4,
        StyleParams(aggression=0.55, sharpness=0.40, solidity=0.15,
                    sacrifice_tendency=0.40, draw_avoidance=0.60, endgame_precision=0.05),
        ErrorParams(blunder_rate=0.22, miss_tactic_rate=0.45,
                    consistency=0.35, time_pressure_amp=0.50),
        openings=["Random moves"],
    ),

    "r1400": _p(
        "r1400", "Casual (1400)", "🔵", "rating",
        "Knows basic piece values, misses multi-move combinations. "
        "Occasionally hangs pieces under pressure.",
        1400, 1400, 7, 4,
        StyleParams(aggression=0.52, sharpness=0.42, solidity=0.30,
                    sacrifice_tendency=0.30, draw_avoidance=0.60, endgame_precision=0.15),
        ErrorParams(blunder_rate=0.13, miss_tactic_rate=0.30,
                    consistency=0.52, time_pressure_amp=0.40),
        openings=["e4 e5", "d4 d5"],
    ),

    "r1600": _p(
        "r1600", "Club Player (1600)", "🟢", "rating",
        "Understands basic tactics and development. Misses deeper combinations "
        "and often goes wrong in the endgame.",
        1600, 1600, 9, 3,
        StyleParams(aggression=0.53, sharpness=0.45, solidity=0.45,
                    sacrifice_tendency=0.22, draw_avoidance=0.62, endgame_precision=0.25),
        ErrorParams(blunder_rate=0.07, miss_tactic_rate=0.18,
                    consistency=0.66, time_pressure_amp=0.35),
        openings=["Italian Game", "Queen's Gambit Declined"],
    ),

    "r1800": _p(
        "r1800", "Intermediate (1800)", "🟡", "rating",
        "Solid opening knowledge, handles basic tactics. Occasional lapses in "
        "complex middlegames and technical endgames.",
        1800, 1800, 11, 3,
        StyleParams(aggression=0.53, sharpness=0.48, solidity=0.60,
                    sacrifice_tendency=0.18, draw_avoidance=0.65, endgame_precision=0.40),
        ErrorParams(blunder_rate=0.04, miss_tactic_rate=0.10,
                    consistency=0.78, time_pressure_amp=0.28),
        openings=["Ruy López", "Nimzo-Indian", "Caro-Kann"],
    ),

    "r2000": _p(
        "r2000", "Advanced (2000)", "🟠", "rating",
        "Strong tactical awareness, decent strategic understanding. "
        "Misses subtle long-range plans and deep endgame technique.",
        2000, 2000, 13, 3,
        StyleParams(aggression=0.54, sharpness=0.50, solidity=0.72,
                    sacrifice_tendency=0.16, draw_avoidance=0.67, endgame_precision=0.55),
        ErrorParams(blunder_rate=0.02, miss_tactic_rate=0.06,
                    consistency=0.86, time_pressure_amp=0.20),
        openings=["Sicilian Defence", "King's Indian", "Queen's Gambit"],
    ),

    "r2200": _p(
        "r2200", "Expert / NM (2200)", "🔴", "rating",
        "National Master strength. Handles most tactics and positional ideas. "
        "Infrequent but notable strategic inaccuracies.",
        2200, 2200, 15, 3,
        StyleParams(aggression=0.54, sharpness=0.52, solidity=0.80,
                    sacrifice_tendency=0.14, draw_avoidance=0.68, endgame_precision=0.70),
        ErrorParams(blunder_rate=0.010, miss_tactic_rate=0.025,
                    consistency=0.91, time_pressure_amp=0.15),
        openings=["Sicilian Najdorf", "Grünfeld", "Semi-Slav"],
    ),

    "r2400": _p(
        "r2400", "FM / IM (2400)", "🟣", "rating",
        "FIDE Master / International Master strength. Near-perfect tactics, "
        "sophisticated strategy. Rare inaccuracies under pressure.",
        2400, 2400, 17, 3,
        StyleParams(aggression=0.54, sharpness=0.53, solidity=0.87,
                    sacrifice_tendency=0.13, draw_avoidance=0.69, endgame_precision=0.82),
        ErrorParams(blunder_rate=0.005, miss_tactic_rate=0.012,
                    consistency=0.95, time_pressure_amp=0.10),
        openings=["Sicilian Najdorf", "Ruy López Berlin", "Catalan"],
    ),

    "r2600": _p(
        "r2600", "Grandmaster (2600)", "⚫", "rating",
        "Full GM strength. Exceptional in all phases. Only the most profound "
        "engine moves reveal inaccuracies.",
        2600, 2600, 19, 2,
        StyleParams(aggression=0.54, sharpness=0.54, solidity=0.92,
                    sacrifice_tendency=0.12, draw_avoidance=0.70, endgame_precision=0.90),
        ErrorParams(blunder_rate=0.003, miss_tactic_rate=0.006,
                    consistency=0.97, time_pressure_amp=0.06),
        openings=["Najdorf", "Berlin", "Catalan", "Grünfeld"],
    ),

    "r2800": _p(
        "r2800", "Super GM (2800+)", "👑", "rating",
        "World-class play. Effectively engine-level strength with the tiniest "
        "human variability to keep things interesting.",
        2800, 2800, 22, 2,
        StyleParams(aggression=0.54, sharpness=0.54, solidity=0.97,
                    sacrifice_tendency=0.11, draw_avoidance=0.70, endgame_precision=0.97),
        ErrorParams(blunder_rate=0.001, miss_tactic_rate=0.002,
                    consistency=0.99, time_pressure_amp=0.02),
        openings=["All major systems"],
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# LEGENDARY PLAYERS
# ══════════════════════════════════════════════════════════════════════════════

LEGEND_PROFILES: dict[str, PlayerProfile] = {

    # ── Magnus Carlsen ────────────────────────────────────────────────────────
    "magnus_carlsen": _p(
        "magnus_carlsen", "Magnus Carlsen", "♟", "legend",
        "The GOAT. Universal style, near-perfect endgame technique, "
        "relentless pressure. Prefers complex positions over easy draws. "
        "Rarely blunders — errors are subtle strategic inaccuracies.",
        2853, 2853, 22, 3,
        StyleParams(aggression=0.58, sharpness=0.60, solidity=0.88,
                    sacrifice_tendency=0.35, draw_avoidance=0.82, endgame_precision=0.98),
        ErrorParams(blunder_rate=0.001, miss_tactic_rate=0.002,
                    consistency=0.97, time_pressure_amp=0.02),
        openings=["1.e4 / 1.d4", "Sicilian (both colours)", "Ruy López"],
    ),

    # ── Hikaru Nakamura ───────────────────────────────────────────────────────
    "hikaru_nakamura": _p(
        "hikaru_nakamura", "Hikaru Nakamura", "⚡", "legend",
        "Internet chess king. Blazing-fast tactician who thrives in chaos. "
        "Loves sharp king-side attacks. More variance than Carlsen — "
        "brilliant one move, slightly careless the next.",
        2794, 2794, 20, 3,
        StyleParams(aggression=0.80, sharpness=0.82, solidity=0.75,
                    sacrifice_tendency=0.55, draw_avoidance=0.88, endgame_precision=0.80),
        ErrorParams(blunder_rate=0.003, miss_tactic_rate=0.005,
                    consistency=0.88, time_pressure_amp=0.12),
        openings=["King's Indian", "Najdorf", "Dutch", "f3 Nimzo"],
    ),

    # ── Garry Kasparov ────────────────────────────────────────────────────────
    "garry_kasparov": _p(
        "garry_kasparov", "Garry Kasparov", "🔥", "legend",
        "The most dominant champion in history. Ultra-aggressive, dynamic "
        "sacrificial genius. Never satisfied with a draw. Exceptional "
        "preparation — dangerous from move 1.",
        2851, 2851, 21, 3,
        StyleParams(aggression=0.92, sharpness=0.88, solidity=0.70,
                    sacrifice_tendency=0.68, draw_avoidance=0.97, endgame_precision=0.88),
        ErrorParams(blunder_rate=0.002, miss_tactic_rate=0.003,
                    consistency=0.90, time_pressure_amp=0.08),
        openings=["1.e4", "King's Indian (Black)", "Sicilian Najdorf"],
    ),

    # ── Anatoly Karpov ────────────────────────────────────────────────────────
    "anatoly_karpov": _p(
        "anatoly_karpov", "Anatoly Karpov", "🐍", "legend",
        "Positional boa constrictor. Applies slow, suffocating pressure "
        "through prophylactic thinking and subtle piece manoeuvres. "
        "Extremely solid — almost never enters needless complications.",
        2780, 2780, 20, 3,
        StyleParams(aggression=0.22, sharpness=0.28, solidity=0.96,
                    sacrifice_tendency=0.08, draw_avoidance=0.52, endgame_precision=0.94),
        ErrorParams(blunder_rate=0.002, miss_tactic_rate=0.003,
                    consistency=0.96, time_pressure_amp=0.04),
        openings=["1.e4 / 1.d4", "Caro-Kann", "Nimzo / QID (Black)"],
    ),

    # ── Mikhail Tal ───────────────────────────────────────────────────────────
    "mikhail_tal": _p(
        "mikhail_tal", "Mikhail Tal", "🌪", "legend",
        "The Magician from Riga. Sacrifices material at will, creates "
        "maximum chaos, and thrives when opponents cannot find the refutation. "
        "Accuracy is secondary — the attack is everything.",
        2705, 2705, 17, 4,
        StyleParams(aggression=0.98, sharpness=0.97, solidity=0.35,
                    sacrifice_tendency=0.92, draw_avoidance=0.97, endgame_precision=0.45),
        ErrorParams(blunder_rate=0.009, miss_tactic_rate=0.006,
                    consistency=0.68, time_pressure_amp=0.20),
        openings=["1.e4", "Sicilian (Black)", "King's Gambit"],
    ),

    # ── Bobby Fischer ─────────────────────────────────────────────────────────
    "bobby_fischer": _p(
        "bobby_fischer", "Bobby Fischer", "🎯", "legend",
        "Classical precision meets relentless aggression. Every move serves "
        "a clear purpose. Exceptional endgame technique, clinical exploitation "
        "of the slightest advantage. 6-0, 6-0.",
        2785, 2785, 20, 3,
        StyleParams(aggression=0.72, sharpness=0.68, solidity=0.80,
                    sacrifice_tendency=0.42, draw_avoidance=0.82, endgame_precision=0.95),
        ErrorParams(blunder_rate=0.002, miss_tactic_rate=0.003,
                    consistency=0.95, time_pressure_amp=0.05),
        openings=["1.e4", "Najdorf / King's Indian (Black)", "Nimzo"],
    ),

    # ── Fabiano Caruana ───────────────────────────────────────────────────────
    "fabiano_caruana": _p(
        "fabiano_caruana", "Fabiano Caruana", "🧱", "legend",
        "Modern universal player. Rock-solid preparation, exceptional tactical "
        "awareness disguised as positional play. Nearly as consistent as "
        "Carlsen with a sharper opening repertoire.",
        2844, 2844, 22, 3,
        StyleParams(aggression=0.60, sharpness=0.62, solidity=0.90,
                    sacrifice_tendency=0.30, draw_avoidance=0.68, endgame_precision=0.90),
        ErrorParams(blunder_rate=0.001, miss_tactic_rate=0.002,
                    consistency=0.97, time_pressure_amp=0.03),
        openings=["1.e4", "Sicilian Najdorf / Petroff (Black)", "Catalan"],
    ),

    # ── Viswanathan Anand ─────────────────────────────────────────────────────
    "viswanathan_anand": _p(
        "viswanathan_anand", "Viswanathan Anand", "🐅", "legend",
        "The Tiger of Madras. Blazing speed, universal style, exceptional "
        "practical intuition. World champion across multiple time controls. "
        "Slightly more aggressive and intuitive than purely computer-like.",
        2817, 2817, 20, 3,
        StyleParams(aggression=0.68, sharpness=0.68, solidity=0.82,
                    sacrifice_tendency=0.40, draw_avoidance=0.72, endgame_precision=0.86),
        ErrorParams(blunder_rate=0.002, miss_tactic_rate=0.004,
                    consistency=0.93, time_pressure_amp=0.06),
        openings=["1.e4", "Sicilian / Najdorf", "Berlin", "Catalan"],
    ),

    # ── Judith Polgar ─────────────────────────────────────────────────────────
    "judith_polgar": _p(
        "judith_polgar", "Judit Polgár", "⚔️", "legend",
        "The greatest female player ever. Crushing, relentless attacker — "
        "favoured the Sicilian Dragon, unstoppable king-side attacks. "
        "Beat Kasparov, Karpov, Anand. Pure aggression with GM-level technique.",
        2735, 2735, 19, 3,
        StyleParams(aggression=0.90, sharpness=0.85, solidity=0.65,
                    sacrifice_tendency=0.62, draw_avoidance=0.88, endgame_precision=0.78),
        ErrorParams(blunder_rate=0.005, miss_tactic_rate=0.008,
                    consistency=0.84, time_pressure_amp=0.12),
        openings=["1.e4", "Sicilian Dragon (Black)", "King's Indian"],
    ),

    # ── Wesley So ────────────────────────────────────────────────────────────
    "wesley_so": _p(
        "wesley_so", "Wesley So", "🛡", "legend",
        "Exceptionally solid — one of the lowest blunder rates among active "
        "super-GMs. Extremely hard to beat. Methodical, positional style "
        "with hidden tactical resources.",
        2778, 2778, 20, 3,
        StyleParams(aggression=0.38, sharpness=0.42, solidity=0.94,
                    sacrifice_tendency=0.14, draw_avoidance=0.62, endgame_precision=0.92),
        ErrorParams(blunder_rate=0.002, miss_tactic_rate=0.003,
                    consistency=0.97, time_pressure_amp=0.03),
        openings=["1.e4 / 1.d4", "Berlin", "English (Black)"],
    ),

    # ── Alireza Firouzja ─────────────────────────────────────────────────────
    "alireza_firouzja": _p(
        "alireza_firouzja", "Alireza Firouzja", "🚀", "legend",
        "The new generation's wild card. Fearless attacker, loves "
        "unbalanced positions, willing to take huge risks for the initiative. "
        "Higher variance than older super-GMs — breathtaking highs and lows.",
        2793, 2793, 20, 4,
        StyleParams(aggression=0.87, sharpness=0.90, solidity=0.60,
                    sacrifice_tendency=0.65, draw_avoidance=0.92, endgame_precision=0.78),
        ErrorParams(blunder_rate=0.005, miss_tactic_rate=0.007,
                    consistency=0.82, time_pressure_amp=0.18),
        openings=["1.e4", "Sicilian / King's Indian (Black)", "Sharp lines"],
    ),

    # ── Paul Morphy ───────────────────────────────────────────────────────────
    "paul_morphy": _p(
        "paul_morphy", "Paul Morphy", "🎩", "legend",
        "19th-century prodigy whose intuitive attacking brilliance was a "
        "century ahead of its time. Rapid development, open-file domination, "
        "devastating combinative attacks. Would destroy any modern 2400.",
        2690, 2500, 16, 3,
        StyleParams(aggression=0.88, sharpness=0.80, solidity=0.55,
                    sacrifice_tendency=0.72, draw_avoidance=0.90, endgame_precision=0.55),
        ErrorParams(blunder_rate=0.010, miss_tactic_rate=0.012,
                    consistency=0.78, time_pressure_amp=0.15),
        openings=["1.e4 e5", "King's Gambit", "Open games"],
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
# STYLE ARCHETYPES
# ══════════════════════════════════════════════════════════════════════════════

STYLE_PROFILES: dict[str, PlayerProfile] = {

    "the_aggressor": _p(
        "the_aggressor", "The Aggressor", "💥", "style",
        "Maximum chaos. Attacks at every opportunity, sacrifices material for "
        "activity, avoids draws at all costs. High-risk, high-reward chess.",
        2300, 2300, 14, 4,
        StyleParams(aggression=0.98, sharpness=0.95, solidity=0.25,
                    sacrifice_tendency=0.90, draw_avoidance=0.99, endgame_precision=0.35),
        ErrorParams(blunder_rate=0.010, miss_tactic_rate=0.010,
                    consistency=0.72, time_pressure_amp=0.25),
        openings=["King's Gambit", "Sicilian Dragon", "Benko Gambit"],
    ),

    "the_fortress": _p(
        "the_fortress", "The Fortress", "🏰", "style",
        "Passive, draw-seeking, ultra-defensive. Avoids complications, "
        "prefers rock-solid setups, content with a half-point.",
        2200, 2200, 14, 2,
        StyleParams(aggression=0.10, sharpness=0.12, solidity=0.99,
                    sacrifice_tendency=0.02, draw_avoidance=0.05, endgame_precision=0.80),
        ErrorParams(blunder_rate=0.005, miss_tactic_rate=0.008,
                    consistency=0.95, time_pressure_amp=0.05),
        openings=["Berlin Defence", "Petroff", "London System"],
    ),

    "the_tactician": _p(
        "the_tactician", "The Tactician", "🔭", "style",
        "A combination hunter. Seeks complex tactical positions, calculates "
        "deeply, and pounces on any tactical opportunity. Strategic plans "
        "take a back seat to immediate threats.",
        2350, 2350, 15, 4,
        StyleParams(aggression=0.78, sharpness=0.92, solidity=0.50,
                    sacrifice_tendency=0.70, draw_avoidance=0.80, endgame_precision=0.55),
        ErrorParams(blunder_rate=0.006, miss_tactic_rate=0.004,
                    consistency=0.80, time_pressure_amp=0.18),
        openings=["Sicilian", "King's Indian", "Dutch Defence"],
    ),

    "the_grinder": _p(
        "the_grinder", "The Positional Grinder", "⚙️", "style",
        "Slow, methodical, suffocating. Accumulates tiny advantages over "
        "many moves, excels in technical endgames. Patience is the weapon.",
        2350, 2350, 15, 2,
        StyleParams(aggression=0.20, sharpness=0.25, solidity=0.92,
                    sacrifice_tendency=0.06, draw_avoidance=0.60, endgame_precision=0.97),
        ErrorParams(blunder_rate=0.004, miss_tactic_rate=0.006,
                    consistency=0.95, time_pressure_amp=0.06),
        openings=["Catalan", "Queen's Gambit", "London System"],
    ),

    "the_gambiteer": _p(
        "the_gambiteer", "The Gambiteer", "♟️", "style",
        "Material for initiative. Throws pawns (or pieces) at the opponent "
        "to seize the initiative and create dynamic imbalances. "
        "Never lets the opponent settle.",
        2250, 2250, 13, 4,
        StyleParams(aggression=0.85, sharpness=0.88, solidity=0.38,
                    sacrifice_tendency=0.88, draw_avoidance=0.90, endgame_precision=0.40),
        ErrorParams(blunder_rate=0.012, miss_tactic_rate=0.015,
                    consistency=0.72, time_pressure_amp=0.22),
        openings=["King's Gambit", "Benko Gambit", "Albin Counter-Gambit",
                  "Latvian Gambit"],
    ),

    "the_universal": _p(
        "the_universal", "The Universal Player", "🌐", "style",
        "Perfectly balanced — no stylistic leanings, no systematic weakness. "
        "Adapts to whatever the position demands. A solid, all-round 2400.",
        2400, 2400, 16, 3,
        StyleParams(aggression=0.50, sharpness=0.50, solidity=0.75,
                    sacrifice_tendency=0.25, draw_avoidance=0.65, endgame_precision=0.75),
        ErrorParams(blunder_rate=0.005, miss_tactic_rate=0.010,
                    consistency=0.92, time_pressure_amp=0.08),
        openings=["Flexible — depends on opponent"],
    ),

    "the_endgame_artist": _p(
        "the_endgame_artist", "The Endgame Artist", "🎨", "style",
        "Loves to simplify into endgames and then outplay opponents with "
        "superior technique. Avoids sharp middlegames. Ruthless once pieces "
        "are exchanged.",
        2300, 2300, 14, 2,
        StyleParams(aggression=0.30, sharpness=0.30, solidity=0.88,
                    sacrifice_tendency=0.10, draw_avoidance=0.55, endgame_precision=0.99),
        ErrorParams(blunder_rate=0.005, miss_tactic_rate=0.010,
                    consistency=0.93, time_pressure_amp=0.06),
        openings=["Exchange variations", "Berlin", "Reti"],
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Master lookup
# ─────────────────────────────────────────────────────────────────────────────

ALL_PROFILES: dict[str, PlayerProfile] = {
    **RATING_PROFILES,
    **LEGEND_PROFILES,
    **STYLE_PROFILES,
}

# Ordered lists for the UI (category → list of profile IDs)
PROFILE_CATEGORIES: dict[str, list[str]] = {
    "⭐ Rating Archetypes": list(RATING_PROFILES.keys()),
    "🏆 Legendary Players": list(LEGEND_PROFILES.keys()),
    "🎭 Style Archetypes":  list(STYLE_PROFILES.keys()),
}

# Flat ordered list for a single dropdown
PROFILE_DISPLAY_ORDER: list[str] = (
    list(RATING_PROFILES.keys())
    + list(LEGEND_PROFILES.keys())
    + list(STYLE_PROFILES.keys())
)

# ── Convenience helpers ───────────────────────────────────────────────────────

def get_profile(profile_id: str) -> PlayerProfile:
    """Retrieve a profile by ID; raises KeyError if not found."""
    return ALL_PROFILES[profile_id]


def default_profile() -> PlayerProfile:
    """Return the default profile (full-strength universal)."""
    return ALL_PROFILES["r2000"]
