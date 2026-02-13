"""Deterministic Stockfish oracle for structured chess analysis."""

from __future__ import annotations

from io import StringIO
from typing import Any, Dict, List, Mapping, Optional, Sequence

from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION, empty_label_counts, sum_label_counts

try:
    import chess
    import chess.engine
    import chess.pgn
except Exception:  # pragma: no cover - environment may not have python-chess installed.
    chess = None  # type: ignore[assignment]

PIECE_VALUES = {
    1: 1,  # pawn
    2: 3,  # knight
    3: 3,  # bishop
    4: 5,  # rook
    5: 9,  # queen
}

BASELINE_CLASSIFICATION_RATING = 1100
_BASE_INACCURACY_CP = 50.0
_BASE_MISTAKE_CP = 120.0
_BASE_BLUNDER_CP = 250.0
_THRESHOLD_STEP_PER_100_RATING = 0.025
_THRESHOLD_SCALE_MIN = 0.75
_THRESHOLD_SCALE_MAX = 1.15
_FIXED_ENGINE_OPTIONS: tuple[tuple[str, int], ...] = (
    ("Threads", 1),
    ("Hash", 16),
    ("MultiPV", 1),
)


def classification_thresholds_cp(player_rating: Optional[int]) -> Dict[str, float]:
    """Return rating-aware move classification thresholds in centipawns."""
    rating = _normalize_rating(player_rating)
    scale = 1.0
    if rating is not None:
        delta = (float(rating) - float(BASELINE_CLASSIFICATION_RATING)) / 100.0
        scale = 1.0 - (delta * _THRESHOLD_STEP_PER_100_RATING)
        scale = max(_THRESHOLD_SCALE_MIN, min(_THRESHOLD_SCALE_MAX, scale))
    return {
        "inaccuracy_cp": float(_BASE_INACCURACY_CP * scale),
        "mistake_cp": float(_BASE_MISTAKE_CP * scale),
        "blunder_cp": float(_BASE_BLUNDER_CP * scale),
    }


def classify_move(
    eval_before: float,
    played_eval: float,
    best_eval: float,
    played_is_best_move: bool = False,
    player_rating: Optional[int] = None,
) -> str:
    """Classify a move from engine comparison to the best line."""
    loss = max(0.0, best_eval - played_eval)
    thresholds = classification_thresholds_cp(player_rating)
    inaccuracy_pawns = float(thresholds["inaccuracy_cp"]) / 100.0
    mistake_pawns = float(thresholds["mistake_cp"]) / 100.0
    blunder_pawns = float(thresholds["blunder_cp"]) / 100.0

    if played_is_best_move and (best_eval - eval_before) > 1.0 and loss <= 0.15:
        return "brilliant"
    if loss < inaccuracy_pawns:
        return "good"
    if loss < mistake_pawns:
        return "inaccuracy"
    if loss < blunder_pawns:
        return "mistake"
    return "blunder"


class StockfishOracle:
    """Runs deterministic Stockfish analysis and returns pure structured data."""

    def __init__(self, stockfish_path: str, depth: int = 15) -> None:
        path = str(stockfish_path or "").strip()
        if not path:
            raise ValueError("stockfish_path must be set")
        if depth <= 0:
            raise ValueError("depth must be > 0")
        self._path = path
        self._depth = int(depth)

    def analyze_game(self, pgn_text: str, *, include_trace: bool = False) -> Dict[str, Any]:
        if chess is None:
            raise RuntimeError("python-chess is required for StockfishOracle")

        game = chess.pgn.read_game(StringIO(pgn_text))
        if game is None:
            raise ValueError("PGN could not be parsed")

        board = game.board()
        mainline_moves = list(game.mainline_moves())
        expected_analysis_fens = _expected_analysis_fens_from_mainline(game=game)
        analyzed_fens: list[str] = []
        white_rating = _normalize_rating(game.headers.get("WhiteElo"))
        black_rating = _normalize_rating(game.headers.get("BlackElo"))

        all_positions: List[Dict[str, Any]] = []
        trace_positions: List[Dict[str, Any]] = []
        key_candidates: List[Dict[str, Any]] = []
        label_counts: Dict[str, int] = {
            "brilliant": 0,
            "good": 0,
            "inaccuracy": 0,
            "mistake": 0,
            "blunder": 0,
        }
        label_counts_by_side: Dict[str, Dict[str, int]] = {
            "white": empty_label_counts(),
            "black": empty_label_counts(),
        }
        forced_mate_events = 0
        illegal_moves = 0

        with chess.engine.SimpleEngine.popen_uci(self._path) as engine:
            self._configure_engine(engine)
            analyzed_fens.append(str(board.fen()))
            current_analysis = self._analyse_position(engine, board)

            for ply_index, move in enumerate(mainline_moves, start=1):
                mover_color = board.turn
                move_number = (ply_index + 1) // 2
                player = "White" if mover_color == chess.WHITE else "Black"

                if move not in board.legal_moves:
                    illegal_moves += 1
                    best_move = current_analysis.get("best_move")
                    best_san = _safe_san(board, best_move) if best_move is not None else None
                    row = {
                        "move_number": move_number,
                        "player": player,
                        "label": "blunder",
                        "material_change": 0,
                        "mate_threat": True,
                        "forcing": False,
                        "tactical_flag": "illegal_move",
                        "played_san": None,
                        "best_san": best_san,
                        "_abs_eval_swing": 10_000.0,
                    }
                    all_positions.append(row)
                    trace_positions.append(
                        {
                            "move_number": move_number,
                            "player": player,
                            "label": "blunder",
                            "material_change": 0,
                            "mate_threat": True,
                            "forcing": False,
                            "tactical_flag": "illegal_move",
                            "played_san": None,
                            "best_san": best_san,
                            "eval_before": None,
                            "played_eval": None,
                            "best_eval": None,
                            "eval_loss": None,
                            "abs_eval_swing": 10_000.0,
                        }
                    )
                    key_candidates.append(row)
                    break

                best_move = current_analysis.get("best_move")
                best_eval = _analysis_eval_for_color(current_analysis, mover_color)
                eval_before = best_eval
                played_san = _safe_san(board, move)
                best_san = _safe_san(board, best_move) if best_move is not None else None

                is_capture = board.is_capture(move)
                gives_check = board.gives_check(move)
                is_promotion = move.promotion is not None
                material_before = _material_for_color(board, mover_color)

                board.push(move)

                material_after = _material_for_color(board, mover_color)
                material_change = material_after - material_before

                analyzed_fens.append(str(board.fen()))
                current_analysis = self._analyse_position(engine, board)
                eval_after = _analysis_eval_for_color(current_analysis, mover_color)
                mate_threat = _is_forced_mate_threat(
                    _analysis_mate_for_color(current_analysis, mover_color)
                )
                if mate_threat:
                    forced_mate_events += 1

                label = classify_move(
                    eval_before=eval_before,
                    played_eval=eval_after,
                    best_eval=best_eval,
                    played_is_best_move=bool(best_move == move),
                    player_rating=white_rating if mover_color == chess.WHITE else black_rating,
                )
                eval_loss = max(0.0, float(best_eval) - float(eval_after))
                label_counts[label] = label_counts.get(label, 0) + 1
                side_key = "white" if mover_color == chess.WHITE else "black"
                label_counts_by_side[side_key][label] = int(label_counts_by_side[side_key].get(label, 0)) + 1
                abs_eval_swing = abs(eval_after - eval_before)

                forcing = bool(is_capture or gives_check or is_promotion or mate_threat)
                tactical_flag = _detect_tactical_flag(
                    label=label,
                    material_change=material_change,
                    mate_threat=mate_threat,
                    is_capture=is_capture,
                    gives_check=gives_check,
                    is_promotion=is_promotion,
                )

                row = {
                    "move_number": move_number,
                    "player": player,
                    "label": label,
                    "material_change": int(material_change),
                    "mate_threat": mate_threat,
                    "forcing": forcing,
                    "tactical_flag": tactical_flag,
                    "played_san": played_san,
                    "best_san": best_san,
                    "_abs_eval_swing": float(abs_eval_swing),
                }
                all_positions.append(row)
                trace_positions.append(
                    {
                        "move_number": move_number,
                        "player": player,
                        "label": label,
                        "material_change": int(material_change),
                        "mate_threat": mate_threat,
                        "forcing": forcing,
                        "tactical_flag": tactical_flag,
                        "played_san": played_san,
                        "best_san": best_san,
                        "eval_before": float(eval_before),
                        "played_eval": float(eval_after),
                        "best_eval": float(best_eval),
                        "eval_loss": float(eval_loss),
                        "abs_eval_swing": float(abs_eval_swing),
                    }
                )
                if label != "good" or forcing or material_change != 0:
                    key_candidates.append(row)

        strict_key_positions = _select_strict_key_positions(
            all_positions=all_positions,
            key_candidates=key_candidates,
            required=4,
        )
        _assert_deterministic_fen_order(expected=expected_analysis_fens, actual=analyzed_fens)
        total_plies = int(len(mainline_moves))
        white_plies = int((total_plies + 1) // 2)
        black_plies = int(total_plies // 2)
        unlabeled_white_plies = max(0, white_plies - int(sum_label_counts(label_counts_by_side["white"])))
        unlabeled_black_plies = max(0, black_plies - int(sum_label_counts(label_counts_by_side["black"])))

        output = {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": {
                "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
                "result": game.headers.get("Result", "*"),
                "engine_depth": self._depth,
                "total_plies": total_plies,
                "total_moves": (total_plies + 1) // 2,
                "white_plies": white_plies,
                "black_plies": black_plies,
                "unlabeled_white_plies": int(unlabeled_white_plies),
                "unlabeled_black_plies": int(unlabeled_black_plies),
                "label_counts_total": dict(label_counts),
                "label_counts_white": dict(label_counts_by_side["white"]),
                "label_counts_black": dict(label_counts_by_side["black"]),
                "label_counts": label_counts,
                "label_counts_by_side": label_counts_by_side,
                "forced_mate_events": forced_mate_events,
                "illegal_moves": illegal_moves,
            },
            "key_positions": strict_key_positions,
        }
        if include_trace:
            output["all_positions"] = [_public_trace_position(row) for row in trace_positions]
        return output

    def _analyse_position(
        self,
        engine: Any,
        board: Any,
    ) -> Dict[str, Any]:
        info = engine.analyse(
            board,
            chess.engine.Limit(depth=self._depth),
            multipv=1,
        )
        if isinstance(info, list):
            top: Mapping[str, Any] = info[0] if info else {}
        else:
            top = info
        score = top.get("score")
        pv = list(top.get("pv") or [])
        best_move = pv[0] if pv else None
        return {
            "score": score,
            "best_move": best_move,
        }

    @staticmethod
    def _configure_engine(engine: Any) -> None:
        for option_name, option_value in _FIXED_ENGINE_OPTIONS:
            try:
                engine.configure({option_name: option_value})
            except Exception:
                continue


def _score_to_pawns(score: Any) -> float:
    mate = score.mate()
    if mate is not None:
        sign = 1.0 if mate > 0 else -1.0
        return sign * (100.0 - min(abs(mate), 99))
    cp = score.score()
    if cp is None:
        return 0.0
    return cp / 100.0


def _normalize_rating(value: Any) -> Optional[int]:
    try:
        rating = int(str(value).strip())
    except Exception:
        return None
    if rating <= 0:
        return None
    return max(800, min(2200, rating))


def _analysis_eval_for_color(analysis: Mapping[str, Any], color: Any) -> float:
    score = analysis.get("score")
    if score is None:
        return 0.0
    return _score_to_pawns(score.pov(color))


def _analysis_mate_for_color(analysis: Mapping[str, Any], color: Any) -> Optional[int]:
    score = analysis.get("score")
    if score is None:
        return None
    return score.pov(color).mate()


def _safe_san(board: Any, move: Any) -> Optional[str]:
    if move is None:
        return None
    try:
        return str(board.san(move))
    except Exception:
        return None


def _material_for_color(board: Any, color: Any) -> int:
    total = 0
    for piece_type, value in PIECE_VALUES.items():
        total += len(board.pieces(piece_type, color)) * value
    return total


def _is_forced_mate_threat(mate: Optional[int]) -> bool:
    return bool(mate is not None and mate < 0)


def _detect_tactical_flag(
    *,
    label: str,
    material_change: int,
    mate_threat: bool,
    is_capture: bool,
    gives_check: bool,
    is_promotion: bool,
) -> str:
    if mate_threat:
        return "mate_threat"
    if material_change <= -3:
        return "hanging_piece"
    if is_capture and gives_check:
        return "capture_check"
    if is_capture:
        return "capture"
    if gives_check:
        return "check"
    if is_promotion:
        return "promotion"
    if label in {"mistake", "blunder"}:
        return "tactical_miss"
    return "none"


def _select_strict_key_positions(
    *,
    all_positions: Sequence[Mapping[str, Any]],
    key_candidates: Sequence[Mapping[str, Any]],
    required: int,
) -> List[Dict[str, Any]]:
    _ = key_candidates
    selected: List[Mapping[str, Any]] = list(_sort_positions_by_swing(all_positions)[:required])

    # If the game is too short, duplicate deterministically to guarantee exactly `required`.
    if len(selected) < required and selected:
        seed = list(selected)
        idx = 0
        while len(selected) < required:
            selected.append(seed[idx % len(seed)])
            idx += 1

    if len(selected) < required:
        while len(selected) < required:
            selected.append(_placeholder_position(len(selected) + 1))

    return [_public_key_position(selected[idx]) for idx in range(required)]


def _sort_positions_by_swing(rows: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            -float(row.get("_abs_eval_swing", 0.0) or 0.0),
            int(row.get("move_number", 0) or 0),
            str(row.get("player", "") or ""),
            str(row.get("played_san", "") or ""),
            str(row.get("best_san", "") or ""),
        ),
    )


def _position_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(row.get("move_number", 0) or 0),
        str(row.get("player", "") or ""),
        str(row.get("played_san", "") or ""),
        str(row.get("best_san", "") or ""),
        str(row.get("label", "") or ""),
        str(row.get("tactical_flag", "") or ""),
    )


def _public_key_position(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "move_number": row.get("move_number"),
        "player": row.get("player"),
        "label": row.get("label"),
        "material_change": row.get("material_change"),
        "mate_threat": row.get("mate_threat"),
        "forcing": row.get("forcing"),
        "tactical_flag": row.get("tactical_flag"),
        "played_san": row.get("played_san"),
        "best_san": row.get("best_san"),
    }


def _public_trace_position(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "move_number": row.get("move_number"),
        "player": row.get("player"),
        "label": row.get("label"),
        "material_change": row.get("material_change"),
        "mate_threat": row.get("mate_threat"),
        "forcing": row.get("forcing"),
        "tactical_flag": row.get("tactical_flag"),
        "played_san": row.get("played_san"),
        "best_san": row.get("best_san"),
        "eval_before": row.get("eval_before"),
        "played_eval": row.get("played_eval"),
        "best_eval": row.get("best_eval"),
        "eval_loss": row.get("eval_loss"),
        "abs_eval_swing": row.get("abs_eval_swing"),
    }


def _placeholder_position(index: int) -> Dict[str, Any]:
    return {
        "move_number": index,
        "player": "White" if index % 2 == 1 else "Black",
        "label": "good",
        "material_change": 0,
        "mate_threat": False,
        "forcing": False,
        "tactical_flag": "none",
        "played_san": None,
        "best_san": None,
        "_abs_eval_swing": 0.0,
    }


def _expected_analysis_fens_from_mainline(*, game: Any) -> list[str]:
    board = game.board()
    expected = [str(board.fen())]
    for move in game.mainline_moves():
        if move not in board.legal_moves:
            break
        board.push(move)
        expected.append(str(board.fen()))
    return expected


def _assert_deterministic_fen_order(*, expected: Sequence[str], actual: Sequence[str]) -> None:
    expected_list = [str(item) for item in expected]
    actual_list = [str(item) for item in actual]
    if actual_list != expected_list:
        raise RuntimeError(
            "Deterministic analysis FEN ordering mismatch."
        )
