"""Package entrypoint that preserves legacy chess_review.py behavior."""

from chess_review import main as _legacy_main


def main() -> int:
    return _legacy_main()


if __name__ == "__main__":
    raise SystemExit(main())
