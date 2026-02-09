from src.traits.validate_trait_events import validate_trait_events


def _valid_event() -> dict:
    return {
        "trait_key": "opening_plan_clarity",
        "direction": -1,
        "weight": 1.0,
        "confidence": 0.8,
        "phase": "opening",
        "note": "Built a coherent opening plan and followed it.",
    }


def test_valid_minimal_payload_one_event() -> None:
    payload = {"events": [_valid_event()]}
    is_valid, errors, normalized = validate_trait_events(payload)
    assert is_valid is True
    assert errors == []
    assert normalized["events"][0]["trait_key"] == "opening_plan_clarity"


def test_invalid_payload_missing_events() -> None:
    payload = {}
    is_valid, errors, normalized = validate_trait_events(payload)
    assert is_valid is False
    assert any("events" in err for err in errors)
    assert normalized == {}


def test_invalid_payload_unknown_trait_key() -> None:
    payload = {"events": [{**_valid_event(), "trait_key": "unknown_trait_key"}]}
    is_valid, errors, _ = validate_trait_events(payload)
    assert is_valid is False
    assert any("not in TRAIT_CATALOG" in err for err in errors)


def test_invalid_payload_note_too_long() -> None:
    payload = {"events": [{**_valid_event(), "note": "x" * 201}]}
    is_valid, errors, _ = validate_trait_events(payload)
    assert is_valid is False
    assert any("note" in err and "200" in err for err in errors)


def test_invalid_payload_extra_field_in_event() -> None:
    payload = {"events": [{**_valid_event(), "extra_field": "unexpected"}]}
    is_valid, errors, _ = validate_trait_events(payload)
    assert is_valid is False
    assert any("extra_field" in err for err in errors)


def test_empty_events_payload_currently_invalid() -> None:
    payload = {"events": []}
    is_valid, errors, _ = validate_trait_events(payload)
    # TODO: update expectation if schema minItems is changed to allow empty arrays.
    assert is_valid is False
    assert any("too short" in err or "minItems" in err for err in errors)
