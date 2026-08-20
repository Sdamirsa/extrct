import pytest

from extrct.repair import coerce_to_schema, repair_json_text, run_ladder, validate
from extrct.schema import build_schema

SCHEMA = build_schema([
    {"name": "lvef", "type": "float", "constraints": {"ge": 0, "le": 100}},
    {"name": "severity", "type": "str", "options": ["mild", "moderate", "severe"], "required": False},
])


async def test_clean_first_parse_is_ok_no_layers():
    out = await run_ladder('{"lvef": 55.0, "severity": "mild"}', SCHEMA)
    assert out["final_status"] == "ok"
    assert out["layers_used"] == []
    assert out["attempts"][0]["layer"] == "request"


async def test_fenced_json_repaired():
    out = await run_ladder('```json\n{"lvef": 55.0, "severity": null}\n```', SCHEMA)
    assert out["final_status"] == "repaired"
    assert "json_repair" in out["layers_used"]


async def test_coerce_percent_string_and_enum_case():
    out = await run_ladder('{"lvef": "55%", "severity": "Mild"}', SCHEMA)
    assert out["final_status"] == "repaired"
    assert "coerce" in out["layers_used"]
    assert out["obj"] == {"lvef": 55.0, "severity": "mild"}


async def test_range_violation_never_clamped():
    out = await run_ladder('{"lvef": 250.0, "severity": null}', SCHEMA)
    assert out["final_status"] == "invalid"
    assert any(e["validator"] == "maximum" for a in out["attempts"] for e in a["validation_errors"])


async def test_reprompt_rung_called_with_errors():
    seen = {}

    async def reprompt(raw, errors):
        seen["errors"] = errors
        return '{"lvef": 60.0, "severity": null}'

    out = await run_ladder("not json at all {", SCHEMA,
                           ladder=("json_repair", "coerce", "reprompt"), reprompt=reprompt)
    assert out["final_status"] == "repaired"
    assert "reprompt" in out["layers_used"]
    assert seen["errors"]  # verbatim validation errors were passed


async def test_max_reprompts_zero_skips_rebilling():
    calls = {"n": 0}

    async def reprompt(raw, errors):
        calls["n"] += 1
        return raw

    out = await run_ladder("garbage", SCHEMA, ladder=("reprompt",), reprompt=reprompt,
                           max_reprompts=0)
    assert calls["n"] == 0
    assert out["final_status"] == "invalid"


async def test_ladder_respects_rung_selection():
    # coerce not selected: the castable value stays invalid
    out = await run_ladder('{"lvef": "55%", "severity": null}', SCHEMA, ladder=())
    assert out["final_status"] == "invalid"
    assert out["layers_used"] == []


def test_validate_returns_verbatim_errors():
    errs = validate({"lvef": "not a number", "severity": None}, SCHEMA)
    assert errs and errs[0]["loc"] == ["lvef"]


def test_repair_json_text_deterministic():
    obj, changed = repair_json_text('{"a": 1}')
    assert obj == {"a": 1} and changed is False
    obj, changed = repair_json_text('```json\n{"a": 1}\n```')
    assert obj == {"a": 1} and changed is True


def test_coerce_never_touches_valid_values():
    obj, notes = coerce_to_schema({"lvef": 55.0, "severity": "mild"}, SCHEMA)
    assert notes == []
    assert obj == {"lvef": 55.0, "severity": "mild"}
