from __future__ import annotations

import os
from pathlib import Path


def get_output_root() -> Path:
    return Path(os.environ["CHESS_OUTPUT_DIR"])


def get_trait_books_root() -> Path:
    return get_output_root() / "trait_books"
