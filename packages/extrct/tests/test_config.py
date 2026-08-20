import pytest

from extrct.config import (
    apply_client_overrides,
    coerce,
    expand_grid,
    owned_subset,
    parse_config_rows,
    parse_sweep_rows,
)


def test_coerce_types_loudly():
    assert coerce("42", "int") == 42
    assert coerce("0.5", "float") == 0.5
    assert coerce("true", "bool") is True
    assert coerce('["a", "b"]', "json") == ["a", "b"]
    with pytest.raises(ValueError, match="as bool"):
        coerce("maybe", "bool")
    with pytest.raises(ValueError, match="unknown type"):
        coerce("x", "tuple")


def test_config_rows_must_be_namespaced():
    with pytest.raises(ValueError, match="not namespaced"):
        parse_config_rows([{"name": "model", "type": "str", "value": "m"}])
    out = parse_config_rows([{"name": "ollama.model", "type": "str", "value": "m"},
                             {"name": "extract.max_retries", "type": "int", "value": "3"}])
    assert out == {"ollama.model": "m", "extract.max_retries": 3}


def test_duplicate_config_key_raises():
    rows = [{"name": "ollama.model", "type": "str", "value": "a"},
            {"name": "ollama.model", "type": "str", "value": "b"}]
    with pytest.raises(ValueError, match="duplicate"):
        parse_config_rows(rows)


def test_sweep_rows_json_and_comma_forms():
    out = parse_sweep_rows([
        {"name": "client.temperature", "type": "float", "values": "[0.0, 0.7]"},
        {"name": "ollama.model", "type": "str", "values": "a,b"},
    ])
    assert out["client.temperature"] == [0.0, 0.7]
    assert out["ollama.model"] == ["a", "b"]


def test_expand_grid_stamps_config_uid_and_caps():
    cells = expand_grid({"a.x": [1, 2], "b.y": ["p", "q"]})
    assert len(cells) == 4
    assert all("config_uid" in c for c in cells)
    assert len({c["config_uid"] for c in cells}) == 4  # each cell its own identity
    with pytest.raises(ValueError, match="belongs to a conductor"):
        expand_grid({"a.x": list(range(30)), "b.y": list(range(30))})


def test_apply_overrides_specific_beats_generic():
    kwargs, applied = apply_client_overrides(
        "ollama", {"model": "base"},
        {"client.temperature": 0.5, "ollama.temperature": 0.9, "openrouter.zdr": False})
    assert kwargs["temperature"] == 0.9   # provider prefix wins
    assert "zdr" not in kwargs            # other provider's namespace ignored here
    assert applied == ["temperature"]


def test_apply_overrides_unknown_key_raises():
    with pytest.raises(ValueError, match="not a field"):
        apply_client_overrides("ollama", {}, {"ollama.contexts": 4096})


def test_apply_overrides_forbidden_raises():
    with pytest.raises(ValueError, match="not overridable"):
        apply_client_overrides("ollama", {}, {"ollama.api_key": "sk-x"})


def test_apply_overrides_never_mutates_input():
    base = {"model": "base"}
    kwargs, _ = apply_client_overrides("ollama", base, {"ollama.model": "new"})
    assert base == {"model": "base"}
    assert kwargs["model"] == "new"


def test_owned_subset_validates_names():
    cfg = {"grounding.enabled": True, "certainty.top_logprobs": 5}
    assert owned_subset(cfg, "grounding", {"enabled", "mode"}) == {"enabled": True}
    with pytest.raises(ValueError, match="not an overridable"):
        owned_subset({"grounding.fuzz": 1}, "grounding", {"enabled", "mode"})
