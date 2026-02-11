"""Review output validation utilities."""

from __future__ import annotations

import re
from typing import Iterable, List, Set

import chess

SAN_PATTERN = re.compile(
    r"\b(?:O-O-O|O-O|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?|[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?|[a-h][1-8](?:=[QRBN])?[+#]?)\b"
)


def validate_suggested_moves(board: chess.Board, text_output: str) -> str:
    """Remove SAN-like move tokens that are illegal in every game position."""
    if not text_output:
        return text_output

    tokens = _candidate_san_tokens(text_output)
    if not tokens:
        return text_output

    positions = _positions_from_board_history(board)
    illegal_tokens = {token for token in tokens if not _is_legal_in_any_position(token, positions)}
    if not illegal_tokens:
        return text_output

    cleaned_lines: List[str] = []
    for line in text_output.splitlines():
        cleaned_line = line
        for token in illegal_tokens:
            cleaned_line = _remove_token_from_line(token, cleaned_line)
        cleaned_line = _normalize_line_spacing(cleaned_line)
        if _line_has_content(cleaned_line):
            cleaned_lines.append(cleaned_line)
    return "\n".join(cleaned_lines)


def filter_output_to_allowed_sans(text_output: str, allowed_sans: Iterable[str]) -> str:
    """Remove SAN tokens that are not present in the allowed SAN set."""
    if not text_output:
        return text_output

    allowed: Set[str] = {str(token).strip() for token in allowed_sans if str(token).strip()}
    if not allowed:
        return text_output

    cleaned_lines: List[str] = []
    for line in text_output.splitlines():
        cleaned_line = line
        for token in _candidate_san_tokens(cleaned_line):
            if token not in allowed:
                cleaned_line = _remove_token_from_line(token, cleaned_line)
        cleaned_line = _normalize_line_spacing(cleaned_line)
        if _line_has_content(cleaned_line):
            cleaned_lines.append(cleaned_line)
    return "\n".join(cleaned_lines)


def _candidate_san_tokens(text: str) -> List[str]:
    return list(dict.fromkeys(SAN_PATTERN.findall(text)))


def _positions_from_board_history(board: chess.Board) -> List[chess.Board]:
    root = board.copy(stack=False)
    positions = [root.copy(stack=False)]
    for move in board.move_stack:
        root.push(move)
        positions.append(root.copy(stack=False))
    return positions


def _is_legal_in_any_position(token: str, positions: List[chess.Board]) -> bool:
    for pos in positions:
        try:
            pos.parse_san(token)
            return True
        except Exception:
            continue
    return False


def _token_in_line(token: str, line: str) -> bool:
    return bool(re.search(rf"\b{re.escape(token)}\b", line))


def _remove_token_from_line(token: str, line: str) -> str:
    if not _token_in_line(token, line):
        return line
    return re.sub(rf"\b{re.escape(token)}\b", "", line)


def _normalize_line_spacing(line: str) -> str:
    normalized = re.sub(r"\s{2,}", " ", line)
    normalized = re.sub(r"\s+([,.;:!?])", r"\1", normalized)
    normalized = re.sub(r"\(\s+\)", "", normalized)
    return normalized.rstrip()


def _line_has_content(line: str) -> bool:
    stripped = line.strip()
    return stripped not in {"", "-", "*", "+", ">", "|"}
