"""Deterministic Stockfish oracle for structured chess analysis."""

from __future__ import annotations

from io import StringIO
from typing import Any, Dict, List, Mapping, Optional

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


def classify_move(
    eval_before: float,
    played_eval: float,
    best_eval: float,
    played_is_best_move: bool = False,
) -> str:
    """Classify a move from engine comparison to the best line."""
    loss = max(0.0, best_eval - played_eval)

    if played_is_best_move and (best_eval - eval_before) > 1.0 and loss <= 0.15:
        return "brilliant"
    if loss < 0.5:
        return "good"
    if loss < 1.0:
        return "inaccuracy"
    if loss < 2.5:
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

    def analyze_game(self, pgn_text: str) -> Dict[str, Any]:
        if chess is None:
            raise RuntimeError("python-chess is required for StockfishOracle")

        game = chess.pgn.read_game(StringIO(pgn_text))
        if game is None:
            raise ValueError("PGN could not be parsed")

        board = game.board()
        mainline_moves = list(game.mainline_moves())

        key_positions: List[Dict[str, Any]] = []
        label_counts: Dict[str, int] = {
            "brilliant": 0,
            "good": 0,
            "inaccuracy": 0,
            "mistake": 0,
            "blunder": 0,
        }
        forced_mate_events = 0
        illegal_moves = 0

        with chess.engine.SimpleEngine.popen_uci(self._path) as engine:
            self._configure_engine(engine)
            current_analysis = self._analyse_position(engine, board)

            for ply_index, move in enumerate(mainline_moves, start=1):
                mover_color = board.turn
                move_number = (ply_index + 1) // 2
                player = "White" if mover_color == chess.WHITE else "Black"

                if move not in board.legal_moves:
                    illegal_moves += 1
                    key_positions.append(
                        {
                            "move_number": move_number,
                            "player": player,
                            "label": "blunder",
                            "material_change": 0,
                            "mate_threat": True,
                            "forcing": False,
                            "tactical_flag": "illegal_move",
                        }
                    )
                    break

                best_move = current_analysis.get("best_move")
                best_eval = _analysis_eval_for_color(current_analysis, mover_color)
                eval_before = best_eval

                is_capture = board.is_capture(move)
                gives_check = board.gives_check(move)
                is_promotion = move.promotion is not None
                material_before = _material_for_color(board, mover_color)

                board.push(move)

                material_after = _material_for_color(board, mover_color)
                material_change = material_after - material_before

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
                )
                label_counts[label] = label_counts.get(label, 0) + 1

                forcing = bool(is_capture or gives_check or is_promotion or mate_threat)
                tactical_flag = _detect_tactical_flag(
                    label=label,
                    material_change=material_change,
                    mate_threat=mate_threat,
                    is_capture=is_capture,
                    gives_check=gives_check,
                    is_promotion=is_promotion,
                )

                if label != "good" or forcing or material_change != 0:
                    key_positions.append(
                        {
                            "move_number": move_number,
                            "player": player,
                            "label": label,
                            "material_change": int(material_change),
                            "mate_threat": mate_threat,
                            "forcing": forcing,
                            "tactical_flag": tactical_flag,
                        }
                    )

        return {
            "game_summary": {
                "result": game.headers.get("Result", "*"),
                "total_plies": len(mainline_moves),
                "total_moves": (len(mainline_moves) + 1) // 2,
                "label_counts": label_counts,
                "forced_mate_events": forced_mate_events,
                "illegal_moves": illegal_moves,
            },
            "key_positions": key_positions,
        }

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
        for option_name, option_value in (("Threads", 1), ("Hash", 16)):
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
