import json
import logging
import sys
from pathlib import Path
from typing import Any

from src.traits.trait_catalog import TRAIT_CATALOG

logger = logging.getLogger(__name__)

_PHASE_VALUES = {"opening", "middlegame", "endgame"}
_DIRECTION_VALUES = {-1, 0, 1}
_EVIDENCE_STRENGTH_VALUES = {"minor", "standard", "major"}


def _load_schema() -> dict[str, Any]:
    schema_path = Path(__file__).with_name("trait_event_schema.json")
    with schema_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _canonical_trait_keys() -> set[str]:
    keys: set[str] = set()
    if isinstance(TRAIT_CATALOG, list):
        for item in TRAIT_CATALOG:
            if isinstance(item, dict):
                key = item.get("key")
                if isinstance(key, str):
                    keys.add(key)
            elif isinstance(item, str):
                keys.add(item)
    elif isinstance(TRAIT_CATALOG, dict):
        for key, value in TRAIT_CATALOG.items():
            if isinstance(key, str):
                keys.add(key)
            if isinstance(value, dict):
                nested_key = value.get("key")
                if isinstance(nested_key, str):
                    keys.add(nested_key)
    return keys


def _normalize_event(event: dict[str, Any], index: int, errors: list[str]) -> dict[str, Any]:
    normalized = dict(event)

    if "trait_key" in normalized:
        trait_key = normalized["trait_key"]
        if isinstance(trait_key, str):
            normalized["trait_key"] = trait_key.strip()
        else:
            errors.append(f"events[{index}].trait_key must be a string.")

    if "phase" in normalized:
        phase = normalized["phase"]
        if isinstance(phase, str):
            normalized["phase"] = phase.strip()
        else:
            errors.append(f"events[{index}].phase must be a string.")

    if "weight" in normalized:
        weight = normalized["weight"]
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            errors.append(f"events[{index}].weight must be numeric.")
        else:
            normalized["weight"] = float(weight)

    if "confidence" in normalized:
        confidence = normalized["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            errors.append(f"events[{index}].confidence must be numeric.")
        else:
            normalized["confidence"] = float(confidence)

    if "direction" in normalized:
        direction = normalized["direction"]
        if isinstance(direction, bool) or not isinstance(direction, int):
            errors.append(f"events[{index}].direction must be an integer.")

    if "move_number" in normalized:
        move_number = normalized["move_number"]
        if isinstance(move_number, bool) or not isinstance(move_number, int):
            errors.append(f"events[{index}].move_number must be an integer.")

    return normalized


def _validate_with_jsonschema(payload: dict[str, Any]) -> list[str]:
    schema = _load_schema()
    errors: list[str] = []
    try:
        from jsonschema import Draft202012Validator
    except Exception:
        # jsonschema is not installed; apply minimal schema checks here.
        return _minimal_schema_checks(payload, schema)

    validator = Draft202012Validator(schema)
    for error in validator.iter_errors(payload):
        path = ".".join(str(p) for p in error.absolute_path)
        if path:
            errors.append(f"{path}: {error.message}")
        else:
            errors.append(error.message)
    return sorted(errors)


def _minimal_schema_checks(payload: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        errors.append("Payload must be an object.")
        return errors

    top_required = schema.get("required", [])
    for field in top_required:
        if field not in payload:
            errors.append(f"'{field}' is a required property")

    if schema.get("additionalProperties") is False:
        allowed_top_fields = set(schema.get("properties", {}).keys())
        for key in payload:
            if key not in allowed_top_fields:
                errors.append(f"Additional top-level property '{key}' is not allowed.")

    events = payload.get("events")
    if events is None:
        return errors
    if not isinstance(events, list):
        errors.append("'events' must be an array.")
        return errors

    min_items = schema.get("properties", {}).get("events", {}).get("minItems")
    if isinstance(min_items, int) and len(events) < min_items:
        errors.append(f"events must not be empty (minItems={min_items}).")

    event_schema = schema.get("$defs", {}).get("trait_event", {})
    event_required = set(event_schema.get("required", []))
    allowed_event_fields = set(event_schema.get("properties", {}).keys())
    event_additional_props = event_schema.get("additionalProperties")

    for i, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"events[{i}] must be an object.")
            continue
        for field in event_required:
            if field not in event:
                errors.append(f"events[{i}].{field} is required.")
        if event_additional_props is False:
            for key in event:
                if key not in allowed_event_fields:
                    errors.append(f"events[{i}] has additional property '{key}' not allowed.")

    return errors


def _manual_required_shape_checks(payload: Any, errors: list[str]) -> None:
    if not isinstance(payload, dict):
        errors.append("Payload must be an object.")
        return

    if "events" not in payload:
        errors.append("'events' field is required.")
        return

    events = payload.get("events")
    if not isinstance(events, list):
        errors.append("'events' must be an array.")


def _manual_field_sanity(normalized_payload: dict[str, Any], errors: list[str]) -> None:
    events = normalized_payload.get("events")
    if not isinstance(events, list):
        return

    canonical_keys = _canonical_trait_keys()

    for i, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"events[{i}] must be an object.")
            continue

        trait_key = event.get("trait_key")
        if isinstance(trait_key, str):
            if trait_key not in canonical_keys:
                errors.append(f"events[{i}].trait_key '{trait_key}' is not in TRAIT_CATALOG.")
        else:
            errors.append(f"events[{i}].trait_key must be a string.")

        direction = event.get("direction")
        if not (isinstance(direction, int) and not isinstance(direction, bool)):
            errors.append(f"events[{i}].direction must be an integer in {{-1,0,1}}.")
        elif direction not in _DIRECTION_VALUES:
            errors.append(f"events[{i}].direction must be one of -1, 0, 1.")

        phase = event.get("phase")
        if not isinstance(phase, str):
            errors.append(f"events[{i}].phase must be a string.")
        elif phase not in _PHASE_VALUES:
            errors.append(f"events[{i}].phase must be one of opening, middlegame, endgame.")

        weight = event.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            errors.append(f"events[{i}].weight must be numeric.")
        else:
            numeric_weight = float(weight)
            if not (0.5 <= numeric_weight <= 2.0):
                errors.append(f"events[{i}].weight must be between 0.5 and 2.0.")

        confidence = event.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            errors.append(f"events[{i}].confidence must be numeric.")
        else:
            numeric_confidence = float(confidence)
            if not (0.5 <= numeric_confidence <= 1.0):
                errors.append(f"events[{i}].confidence must be between 0.5 and 1.0.")

        note = event.get("note")
        if not isinstance(note, str):
            errors.append(f"events[{i}].note must be a string.")
        elif not (1 <= len(note) <= 200):
            errors.append(f"events[{i}].note length must be between 1 and 200 characters.")

        if "move_number" in event:
            move_number = event["move_number"]
            if not (isinstance(move_number, int) and not isinstance(move_number, bool)):
                errors.append(f"events[{i}].move_number must be an integer >= 1.")
            elif move_number < 1:
                errors.append(f"events[{i}].move_number must be >= 1.")

        if "evidence_strength" in event:
            evidence_strength = event["evidence_strength"]
            if not isinstance(evidence_strength, str):
                errors.append(f"events[{i}].evidence_strength must be a string.")
            elif evidence_strength not in _EVIDENCE_STRENGTH_VALUES:
                errors.append(
                    f"events[{i}].evidence_strength must be one of minor, standard, major."
                )


def validate_trait_events(payload: dict) -> tuple[bool, list[str], dict]:
    errors: list[str] = []
    _manual_required_shape_checks(payload, errors)
    if errors:
        logger.warning(
            "Trait-event payload invalid: %d errors; first 3: %s",
            len(errors),
            "; ".join(errors[:3]),
        )
        logger.debug("Trait-event payload full errors: %s", errors)
        return False, errors, {}

    events = payload.get("events", [])
    normalized_events: list[Any] = []
    for i, event in enumerate(events):
        if isinstance(event, dict):
            normalized_events.append(_normalize_event(event, i, errors))
        else:
            normalized_events.append(event)
    normalized_payload = {"events": normalized_events}

    schema_errors = _validate_with_jsonschema(normalized_payload)
    errors.extend(schema_errors)
    _manual_field_sanity(normalized_payload, errors)

    if errors:
        deduped_errors = list(dict.fromkeys(errors))
        logger.warning(
            "Trait-event payload invalid: %d errors; first 3: %s",
            len(deduped_errors),
            "; ".join(deduped_errors[:3]),
        )
        logger.debug("Trait-event payload full errors: %s", deduped_errors)
        return False, deduped_errors, {}

    return True, [], normalized_payload


def _main() -> int:
    logging.basicConfig(level=logging.WARNING)
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        logger.warning("Invalid JSON input: %s", exc)
        print("INVALID")
        return 1

    is_valid, _, _ = validate_trait_events(payload)
    if is_valid:
        print("OK")
        return 0

    print("INVALID")
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
