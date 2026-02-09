from src.traits.trait_book import generate_trait_book_markdown


def _trait_row(trait_id: int, key: str, name: str, category: str, severity: float = 1.0) -> dict:
    return {
        "id": trait_id,
        "key": key,
        "name": name,
        "category": category,
        "description": f"{name} description.",
        "severity_weight": severity,
    }


def _player_trait_row(trait_id: int, confidence: float, trend_ema: float) -> dict:
    return {
        "trait_id": trait_id,
        "confidence": confidence,
        "trend_ema": trend_ema,
        "last_seen_game_id": 120,
    }


def _section_lines(markdown: str, start_heading: str, end_headings: tuple[str, ...]) -> list[str]:
    lines = markdown.splitlines()
    collecting = False
    collected: list[str] = []
    for line in lines:
        if line.strip() == start_heading:
            collecting = True
            continue
        if collecting and any(line.strip() == heading for heading in end_headings):
            break
        if collecting:
            collected.append(line)
    return collected


def test_trait_book_classification_ordering_and_limits() -> None:
    player_row = {"id": 7, "platform_user": "logan"}
    traits_rows = [
        _trait_row(1, "strength_alpha", "Strength Alpha", "opening"),
        _trait_row(2, "strength_beta", "Strength Beta", "strategy"),
        _trait_row(3, "strength_gamma", "Strength Gamma", "time"),
        _trait_row(4, "strength_delta", "Strength Delta", "psych"),
        _trait_row(5, "strength_epsilon", "Strength Epsilon", "tactics"),
        _trait_row(6, "strength_zeta", "Strength Zeta", "endgame"),
        _trait_row(7, "weakness_a", "Weakness A", "tactics"),
        _trait_row(8, "weakness_b", "Weakness B", "tactics"),
        _trait_row(9, "weakness_c", "Weakness C", "strategy"),
        _trait_row(10, "weakness_d", "Weakness D", "time"),
        _trait_row(11, "weakness_e", "Weakness E", "psych"),
        _trait_row(12, "weakness_f", "Weakness F", "endgame"),
        _trait_row(13, "improve_a", "Improve A", "opening"),
        _trait_row(14, "improve_b", "Improve B", "strategy"),
        _trait_row(15, "improve_c", "Improve C", "psych"),
        _trait_row(16, "worsen_a", "Worsen A", "tactics"),
        _trait_row(17, "worsen_b", "Worsen B", "strategy"),
        _trait_row(18, "worsen_c", "Worsen C", "endgame"),
        _trait_row(19, "watch_a", "Watch A", "time"),
        _trait_row(20, "watch_b", "Watch B", "opening"),
        _trait_row(21, "excluded_low", "Excluded Low", "strategy"),
    ]
    player_traits_rows = [
        _player_trait_row(1, 0.80, -0.40),
        _player_trait_row(2, 0.65, -0.30),
        _player_trait_row(3, 0.90, -0.22),
        _player_trait_row(4, 0.70, -0.10),
        _player_trait_row(5, 0.61, 0.05),
        _player_trait_row(6, 0.62, 0.10),
        _player_trait_row(7, 0.60, 0.50),
        _player_trait_row(8, 0.90, 0.35),
        _player_trait_row(9, 0.70, 0.22),
        _player_trait_row(10, 0.56, 0.20),
        _player_trait_row(11, 0.80, 0.19),
        _player_trait_row(12, 0.95, 0.18),
        _player_trait_row(13, 0.50, -0.30),
        _player_trait_row(14, 0.40, -0.25),
        _player_trait_row(15, 0.45, -0.20),
        _player_trait_row(16, 0.50, 0.30),
        _player_trait_row(17, 0.52, 0.26),
        _player_trait_row(18, 0.36, 0.21),
        _player_trait_row(19, 0.60, 0.13),
        _player_trait_row(20, 0.57, -0.14),
        _player_trait_row(21, 0.20, 0.05),
    ]

    markdown = generate_trait_book_markdown(
        player_row,
        traits_rows,
        player_traits_rows,
        games_analyzed=40,
        cutoff_label="40 games",
        snapshot_utc="2026-02-09T00:00:00+00:00",
        window_info={"start_game_id": 21, "end_game_id": 40, "start_played_at": "2026-02-01", "end_played_at": "2026-02-09"},
    )

    strengths_lines = [
        line for line in _section_lines(markdown, "## Core Strengths", ("## Recurring Weaknesses",)) if line.startswith("- **")
    ]
    weaknesses_lines = [
        line for line in _section_lines(markdown, "## Recurring Weaknesses", ("## Traits in Transition",)) if line.startswith("- **")
    ]
    improving_lines = [
        line
        for line in _section_lines(markdown, "### Improving", ("### Worsening",))
        if line.startswith("- **")
    ]
    worsening_lines = [
        line
        for line in _section_lines(markdown, "### Worsening", ("### Watchlist", "## Focus Areas (Next 20 Games)"))
        if line.startswith("- **")
    ]
    watchlist_lines = [
        line
        for line in _section_lines(markdown, "### Watchlist", ("## Focus Areas (Next 20 Games)",))
        if line.startswith("- **")
    ]

    assert len(strengths_lines) == 5
    assert "Strength Alpha" in strengths_lines[0]
    assert "Strength Beta" in strengths_lines[1]
    assert "Strength Gamma" in strengths_lines[2]
    assert "Strength Zeta" not in markdown

    assert len(weaknesses_lines) == 5
    assert "Weakness A" in weaknesses_lines[0]
    assert "Weakness B" in weaknesses_lines[1]
    assert "Weakness F" not in markdown

    assert len(improving_lines) == 2
    assert "Improve A" in improving_lines[0]
    assert "Improve B" in improving_lines[1]
    assert "Improve C" not in "\n".join(improving_lines)

    assert len(worsening_lines) == 2
    assert "Worsen A" in worsening_lines[0]
    assert "Worsen B" in worsening_lines[1]
    assert "Worsen C" not in "\n".join(worsening_lines)

    assert len(watchlist_lines) == 2
    assert "Watch A" in markdown
    assert "Watch B" in markdown
    assert "Excluded Low" not in markdown


def test_focus_areas_are_exactly_three_and_diverse_when_possible() -> None:
    player_row = {"id": 7, "platform_user": "logan"}
    traits_rows = [
        _trait_row(1, "weakness_a", "Weakness A", "tactics"),
        _trait_row(2, "weakness_b", "Weakness B", "tactics"),
        _trait_row(3, "worsen_a", "Worsen A", "tactics"),
        _trait_row(4, "worsen_b", "Worsen B", "strategy"),
    ]
    player_traits_rows = [
        _player_trait_row(1, 0.85, 0.50),
        _player_trait_row(2, 0.80, 0.35),
        _player_trait_row(3, 0.50, 0.30),
        _player_trait_row(4, 0.52, 0.26),
    ]

    markdown = generate_trait_book_markdown(
        player_row,
        traits_rows,
        player_traits_rows,
        games_analyzed=40,
        cutoff_label="40 games",
        snapshot_utc="2026-02-09T00:00:00+00:00",
        window_info={},
    )
    focus_lines = [
        line
        for line in _section_lines(markdown, "## Focus Areas (Next 20 Games)", ("## Methodology",))
        if line.startswith("1. ") or line.startswith("2. ") or line.startswith("3. ")
    ]

    assert len(focus_lines) == 3
    assert "Weakness A" in focus_lines[0]
    assert "Weakness B" in focus_lines[1]
    assert "Worsen B" in focus_lines[2]
