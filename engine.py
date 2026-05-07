"""
engine.py — Stockfish wrapper with profile-driven human-like move selection.

Key improvements over the original:
  - Multi-PV analysis: asks Stockfish for top-N candidate moves, then applies
    the active PlayerProfile's style logic to pick among them.  This produces
    far more nuanced human simulation than the old single-move + random-swap
    approach.
  - ProfiledAnalysisRequest carries a full PlayerProfile so style and error
    parameters travel together without global state.
  - configure() is still called only on skill changes (avoids NNUE reload
    overhead noted in the original log).
  - Backward-compatible: request_analysis() still works without a profile
    (falls back to the legacy HumaniserConfig path).
  - MoveResult now also exposes all_candidates for future multi-arrow display.
"""

from __future__ import annotations

import logging
import queue
import random
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import chess
import chess.engine

import config
from profiles import PlayerProfile, select_move_for_profile

log = logging.getLogger(__name__)

MATE_SCORE = 30_000


def _score_to_cp(score: Optional[chess.engine.PovScore]) -> Optional[int]:
    """Encode mate scores near +/-30000 while preserving mate distance."""
    if score is None:
        return None

    rel = score.relative
    if rel.is_mate():
        mate = rel.mate()
        if mate is None:
            return None
        sign = 1 if mate >= 0 else -1
        return sign * (MATE_SCORE - abs(mate))
    return rel.score()


# ─────────────────────────────────────────────────────────────────────────────
# Legacy humaniser (kept for backward compatibility with manual sidebar sliders)
# ─────────────────────────────────────────────────────────────────────────────

class HumaniserConfig:
    """Maps skill level (0–20) to error probabilities (legacy path)."""

    def __init__(
        self,
        skill: int = 20,
        blunder_rate: float = 0.0,
        randomness: float = 0.0,
    ):
        self.skill        = max(0, min(20, skill))
        self.blunder_rate = blunder_rate
        self.randomness   = randomness

    def natural_error_prob(self) -> float:
        return (20 - self.skill) / 20 * 0.30

    def should_blunder(self) -> bool:
        p = self.blunder_rate + self.natural_error_prob()
        return random.random() < p

    def should_randomise(self) -> bool:
        return random.random() < self.randomness


def _pick_human_move_legacy(
    board: chess.Board, best_uci: str, humaniser: HumaniserConfig
) -> str:
    """Legacy random-replacement error injection."""
    legal = list(board.legal_moves)
    if len(legal) <= 1:
        return best_uci
    if humaniser.should_randomise():
        return random.choice(legal).uci()
    if humaniser.should_blunder():
        non_best = [m for m in legal if m.uci() != best_uci]
        if non_best:
            return random.choice(non_best).uci()
    return best_uci


# ─────────────────────────────────────────────────────────────────────────────
# Request / result containers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AnalysisRequest:
    fen:         str
    skill_level: int
    depth:       int
    humanise:    bool
    # Profile-based path (None → legacy path)
    profile:     Optional[PlayerProfile] = None
    request_id:  int = 0


@dataclass
class MoveResult:
    uci:             str            # chosen move in UCI notation
    from_sq:         chess.Square
    to_sq:           chess.Square
    fen:             str
    score:           Optional[int]  # centipawns, positive = good for side to move
    ponder_uci:      Optional[str] = None
    is_human_error:  bool          = False
    # All candidates considered (for future multi-arrow display)
    all_candidates:  List[Tuple[str, Optional[int]]] = field(default_factory=list)
    # Profile that produced this result
    profile_name:    str           = ""
    request_id:      int           = 0


# ─────────────────────────────────────────────────────────────────────────────
# Engine thread
# ─────────────────────────────────────────────────────────────────────────────

class EngineWorker:
    """
    Wraps Stockfish in a background thread.

    Profile path (recommended):
        worker.request_analysis(fen, profile=my_profile)

    Legacy path (manual sliders):
        worker.request_analysis(fen, skill_level=15, depth=12, humanise=True)
    """

    def __init__(
        self,
        result_callback: Callable[[MoveResult], None],
        status_callback: Optional[Callable[[str], None]] = None,
    ):
        self.result_callback = result_callback
        self.status_callback = status_callback
        self._q: "queue.Queue[Optional[AnalysisRequest]]" = queue.Queue(maxsize=1)
        self._thread:  Optional[threading.Thread]       = None
        self._engine:  Optional[chess.engine.SimpleEngine] = None

        # Legacy humaniser (used when no profile is set)
        self.humaniser = HumaniserConfig()

        self._configured_skill: Optional[int] = None
        self._configured_elo:   Optional[int] = None   # track profile ELO too
        self._game_token: Optional[object] = None
        self._request_id_lock = threading.Lock()
        self._next_request_id = 1

    def _emit_status(self, message: str) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(message)
        except Exception as exc:
            log.error("status_callback raised: %s", exc)

    def _allocate_request_id(self) -> int:
        with self._request_id_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            return request_id

    # ── Public API ────────────────────────────────────────────────────────────

    @staticmethod
    def _request_key(req: AnalysisRequest) -> tuple:
        """Return the fields that make a pending request truly duplicate."""
        if req.profile is None:
            return (req.fen, "manual", req.skill_level, req.depth, req.humanise)

        profile = req.profile
        return (
            req.fen,
            "profile",
            profile.id,
            profile.stockfish_elo,
            req.depth,
            profile.multipv,
            profile.style,
            profile.errors,
        )

    def request_analysis(
        self,
        fen:         str,
        skill_level: int                  = config.DEFAULT_SKILL_LEVEL,
        depth:       int                  = config.DEFAULT_DEPTH,
        humanise:    bool                 = False,
        profile:     Optional[PlayerProfile] = None,
    ) -> Optional[int]:
        """
        Submit a new analysis request.

        If `profile` is supplied, all strength/style/error parameters come
        from the profile and skill_level/humanise are ignored.
        
        Duplicate equivalent requests are dropped to prevent redundant cycles.
        """
        if profile is not None:
            depth_cap = getattr(config, "PROFILE_DEPTH_CAP", None)
            profile_depth = profile.depth
            if depth_cap is not None:
                profile_depth = min(profile_depth, int(depth_cap))

            # Profile overrides manual settings
            req = AnalysisRequest(
                fen=fen,
                skill_level=20,           # full engine; ELO limit set separately
                depth=profile_depth,
                humanise=False,           # profile handles errors itself
                profile=profile,
            )
        else:
            req = AnalysisRequest(
                fen=fen,
                skill_level=skill_level,
                depth=depth,
                humanise=humanise,
                profile=None,
            )

        # Keep only the newest pending request. Equivalent requests are ignored,
        # but a same-FEN request with different settings replaces stale work.
        req_key = self._request_key(req)
        while True:
            try:
                existing = self._q.get_nowait()
            except queue.Empty:
                break

            if existing is None:
                self._q.put_nowait(None)
                return None
            if self._request_key(existing) == req_key:
                self._q.put_nowait(existing)
                return existing.request_id

        req.request_id = self._allocate_request_id()

        while True:
            try:
                self._q.put_nowait(req)
                return req.request_id
            except queue.Full:
                try:
                    existing = self._q.get_nowait()
                except queue.Empty:
                    continue
                if existing is None:
                    self._q.put_nowait(None)
                    return None
                if self._request_key(existing) == req_key:
                    self._q.put_nowait(existing)
                    return existing.request_id

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._emit_status("Engine starting")
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="EngineWorker"
        )
        self._thread.start()
        log.info("EngineWorker started")

    def stop(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=5)
        if self._engine:
            try:
                self._engine.quit()
            except Exception:
                pass
        self._emit_status("Engine stopped")
        log.info("EngineWorker stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_engine(self) -> bool:
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(config.STOCKFISH_PATH)
            threads = getattr(config, "STOCKFISH_THREADS", 1)
            hash_mb = getattr(config, "STOCKFISH_HASH_MB", 128)
            try:
                self._engine.configure({"Threads": threads, "Hash": hash_mb})
            except Exception:
                pass
            log.info(
                "Stockfish loaded from %s (Threads=%d, Hash=%dMiB)",
                config.STOCKFISH_PATH, threads, hash_mb,
            )
            self._configured_skill = None
            self._configured_elo   = None
            # Create the token on first successful load; restarts preserve it.
            if self._game_token is None:
                self._game_token = object()
            self._emit_status("Engine ready")
            return True
        except FileNotFoundError:
            log.error(
                "Stockfish not found at '%s'.  "
                "Download from https://stockfishchess.org and update STOCKFISH_PATH",
                config.STOCKFISH_PATH,
            )
            self._emit_status("Stockfish not found")
            return False
        except Exception as exc:
            log.error("Failed to start Stockfish: %s", exc)
            self._emit_status(f"Engine failed: {exc}")
            return False

    def _restart_engine(self) -> bool:
        log.warning("Attempting Stockfish restart…")
        self._emit_status("Restarting engine")
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:
                pass
            self._engine = None
        # Preserve _game_token across restarts to avoid NNUE reload crash loop
        # Only reset on explicit new game, not on crash recovery
        self._configured_skill = None
        self._configured_elo   = None
        return self._load_engine()

    def _configure_strength_legacy(self, skill_level: int) -> bool:
        """Configure skill level (legacy, slider-based path)."""
        if skill_level == self._configured_skill and self._configured_elo is None:
            return True
        try:
            if skill_level < 20:
                elo = max(1320, 500 + skill_level * 100)
                self._engine.configure({"UCI_LimitStrength": True, "UCI_Elo": elo})
            else:
                self._engine.configure({"UCI_LimitStrength": False})
            self._configured_skill = skill_level
            self._configured_elo   = None
            log.debug("Strength configured (legacy): skill=%d", skill_level)
            return True
        except chess.engine.EngineTerminatedError:
            log.error("Engine died during configure — restarting")
            return False
        except Exception as exc:
            log.error("configure() error: %s", exc)
            return False

    def _configure_strength_profile(self, profile: PlayerProfile) -> bool:
        """Configure engine strength from a PlayerProfile."""
        elo = profile.stockfish_elo
        if elo == self._configured_elo:
            return True
        try:
            if elo < 2850:
                self._engine.configure({
                    "UCI_LimitStrength": True,
                    "UCI_Elo":           max(1320, elo),
                })
            else:
                self._engine.configure({"UCI_LimitStrength": False})
            self._configured_elo   = elo
            self._configured_skill = None
            log.debug(
                "Strength configured (profile): %s ELO=%d depth=%d multipv=%d",
                profile.name, elo, profile.depth, profile.multipv,
            )
            return True
        except chess.engine.EngineTerminatedError:
            log.error("Engine died during configure — restarting")
            return False
        except Exception as exc:
            log.error("configure() error: %s", exc)
            return False

    # ── Profile-based analysis (multi-PV) ────────────────────────────────────

    def _analyse_with_profile(
        self, board: chess.Board, req: AnalysisRequest
    ) -> Optional[MoveResult]:
        """
        Run a multi-PV search and apply profile-based move selection.

        Returns a MoveResult or None on failure.
        """
        profile = req.profile
        assert profile is not None

        if not self._configure_strength_profile(profile):
            if not self._restart_engine():
                return None
            if not self._configure_strength_profile(profile):
                return None

        limit    = chess.engine.Limit(depth=req.depth)
        multipv  = max(1, profile.multipv, getattr(config, "ARROW_CANDIDATE_COUNT", 3))

        try:
            infos = self._engine.analyse(
                board, limit,
                multipv=multipv,
                info=chess.engine.INFO_SCORE | chess.engine.INFO_PV,
                game=self._game_token,
            )
        except chess.engine.EngineTerminatedError:
            log.error("Engine died during analyse() — restarting")
            self._restart_engine()
            return None
        except Exception as exc:
            log.error("Engine analyse() error: %s", exc)
            return None

        # Build candidate list from multipv results
        candidates: List[Tuple[str, Optional[int]]] = []
        pv_by_uci: dict[str, list[chess.Move]] = {}
        for info in infos:
            pv = info.get("pv")
            if not pv:
                continue
            uci = pv[0].uci()
            sc  = info.get("score")
            cp = _score_to_cp(sc)
            candidates.append((uci, cp))
            pv_by_uci[uci] = pv

        if not candidates:
            log.warning("No candidates from multipv analyse for FEN: %s", req.fen)
            return None

        # Profile-based move selection
        try:
            chosen_uci, is_error = select_move_for_profile(board, candidates, profile)
        except Exception as exc:
            log.error("Profile move selection failed: %s", exc)
            return None

        try:
            move_obj = chess.Move.from_uci(chosen_uci)
        except ValueError:
            log.warning("Profile selected malformed move %s for FEN: %s", chosen_uci, req.fen)
            return None
        if move_obj not in board.legal_moves:
            log.warning("Profile selected illegal move %s for FEN: %s", chosen_uci, req.fen)
            return None

        # Ponder comes from the selected move's PV line, not necessarily PV #1.
        ponder_uci: Optional[str] = None
        chosen_pv = pv_by_uci.get(chosen_uci)
        if chosen_pv and len(chosen_pv) >= 2:
            ponder_uci = chosen_pv[1].uci()

        chosen_score = next(
            (score for uci, score in candidates if uci == chosen_uci),
            candidates[0][1],
        )
        return MoveResult(
            uci=chosen_uci,
            from_sq=move_obj.from_square,
            to_sq=move_obj.to_square,
            fen=req.fen,
            score=chosen_score,
            ponder_uci=ponder_uci,
            is_human_error=is_error,
            all_candidates=candidates,
            profile_name=profile.name,
            request_id=req.request_id,
        )

    # ── Legacy analysis (single-PV + humaniser) ───────────────────────────────

    def _analyse_legacy(
        self, board: chess.Board, req: AnalysisRequest
    ) -> Optional[MoveResult]:
        """Original single-PV play() path, used when no profile is set."""
        if not self._configure_strength_legacy(req.skill_level):
            if not self._restart_engine():
                return None
            if not self._configure_strength_legacy(req.skill_level):
                return None

        limit = chess.engine.Limit(depth=req.depth)
        try:
            result = self._engine.play(
                board, limit,
                info=chess.engine.INFO_SCORE,
                game=self._game_token,
            )
        except chess.engine.EngineTerminatedError:
            log.error("Engine died during play() — restarting")
            self._restart_engine()
            return None
        except Exception as exc:
            log.error("Engine play() error: %s", exc)
            return None

        if result.move is None:
            log.warning("Engine returned no move for FEN: %s", req.fen)
            return None

        best_uci   = result.move.uci()
        ponder_uci = result.ponder.uci() if result.ponder else None

        score_cp = _score_to_cp(result.info.get("score"))

        is_error   = False
        chosen_uci = best_uci
        if req.humanise:
            chosen_uci = _pick_human_move_legacy(board, best_uci, self.humaniser)
            is_error   = chosen_uci != best_uci

        try:
            move_obj = chess.Move.from_uci(chosen_uci)
        except ValueError:
            log.warning("Selected malformed move %s for FEN: %s", chosen_uci, req.fen)
            return None
        if move_obj not in board.legal_moves:
            log.warning("Selected illegal move %s for FEN: %s", chosen_uci, req.fen)
            return None
        return MoveResult(
            uci=chosen_uci,
            from_sq=move_obj.from_square,
            to_sq=move_obj.to_square,
            fen=req.fen,
            score=score_cp,
            ponder_uci=ponder_uci,
            is_human_error=is_error,
            all_candidates=[(best_uci, score_cp)],
            profile_name="Manual",
            request_id=req.request_id,
        )

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _analyse(self, req: AnalysisRequest) -> None:
        if self._engine is None:
            return

        try:
            board = chess.Board(req.fen)
        except Exception as exc:
            log.error("Invalid FEN '%s': %s", req.fen, exc)
            return

        if board.is_game_over():
            log.debug("Game over — skipping analysis")
            return

        if not board.king(chess.WHITE) or not board.king(chess.BLACK):
            log.warning("Skipping FEN with missing king(s): %s", req.fen)
            return

        # Choose analysis path
        if req.profile is not None:
            result = self._analyse_with_profile(board, req)
        else:
            result = self._analyse_legacy(board, req)

        if result is None:
            return

        log.debug(
            "Analysis done [%s]: %s (score %s cp, ponder %s, error=%s)",
            result.profile_name, result.uci,
            result.score, result.ponder_uci, result.is_human_error,
        )
        try:
            self.result_callback(result)
        except Exception as exc:
            log.error("result_callback raised: %s", exc)

    # ── Worker loop ───────────────────────────────────────────────────────────

    def _run(self) -> None:
        if not self._load_engine():
            log.error("EngineWorker exiting — Stockfish unavailable")
            return

        while True:
            req = self._q.get()
            if req is None:
                break
            try:
                self._emit_status("Analyzing")
                self._analyse(req)
                self._emit_status("Engine ready")
            except Exception as exc:
                log.error(
                    "Unhandled error in _analyse: %s — attempting engine restart",
                    exc,
                )
                self._restart_engine()
        self._emit_status("Engine stopped")
