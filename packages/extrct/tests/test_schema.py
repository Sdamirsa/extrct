import pytest

from extrct.schema import (
    NATIVE_REQUIRED,
    STRICT_NULLABLE,
    SchemaError,
    Variable,
    augment_with_evidence,
    build_schema,
    evidence_schema,
    leaf_paths,
    schema_envelope,
    to_pydantic_source,
    unwrap_schema,
)

ROWS = [
    {"name": "lvef", "type": "float", "description": "LVEF", "constraints": {"ge": 0, "le": 100}},
    {"name": "severity", "type": "str", "options": ["mild", "moderate", "severe"], "required": False},
    {"name": "study", "type": "object"},
    {"name": "modality", "type": "str", "parent": "study", "options": "TTE,CMR"},
    {"name": "findings", "type": "str", "parent": "study", "is_list": True},
]


def test_strict_nullable_every_property_required_and_optional_is_nullable():
    s = build_schema(ROWS, encoding=STRICT_NULLABLE)
    assert set(s["required"]) == {"lvef", "severity", "study"}
    assert s["properties"]["severity"]["type"] == ["string", "null"]
    assert s["properties"]["lvef"]["minimum"] == 0
    assert s["properties"]["lvef"]["maximum"] == 100
    assert s["additionalProperties"] is False


def test_native_required_optional_absent_from_required():
    s = build_schema(ROWS, encoding=NATIVE_REQUIRED)
    assert "severity" not in s["required"]
    assert s["properties"]["severity"]["type"] == "string"


def test_encodings_hash_differently():
    a = schema_envelope(ROWS, encoding=STRICT_NULLABLE)
    b = schema_envelope(ROWS, encoding=NATIVE_REQUIRED)
    assert a["schema_uid"] != b["schema_uid"]


def test_nesting_and_lists():
    s = build_schema(ROWS)
    study = s["properties"]["study"]
    assert study["properties"]["modality"]["enum"] == ["TTE", "CMR"]  # comma-string parsed
    assert study["properties"]["findings"]["type"] == "array"


def test_options_json_string_form():
    v = Variable.from_row({"name": "m", "options": '["TTE", "CMR"]'})
    assert v.options == ["TTE", "CMR"]


def test_duplicate_names_raise():
    with pytest.raises(SchemaError, match="duplicate"):
        build_schema([{"name": "a"}, {"name": "a"}])


def test_unknown_type_raises():
    with pytest.raises(SchemaError, match="unknown type"):
        build_schema([{"name": "a", "type": "decimal"}])


def test_parent_must_be_object():
    with pytest.raises(SchemaError, match="must be 'object'"):
        build_schema([{"name": "a", "type": "str"}, {"name": "b", "parent": "a"}])


def test_object_without_children_raises():
    with pytest.raises(SchemaError, match="no children"):
        build_schema([{"name": "a", "type": "object"}])


def test_missing_parent_raises():
    with pytest.raises(SchemaError, match="not a variable"):
        build_schema([{"name": "b", "parent": "ghost"}])


def test_leaf_paths_skips_object_arrays():
    rows = [
        {"name": "x", "type": "str"},
        {"name": "items", "type": "object", "is_list": True},
        {"name": "y", "type": "str", "parent": "items"},
    ]
    s = build_schema(rows)
    assert leaf_paths(s) == ["x"]


def test_evidence_mirror_and_augment():
    s = build_schema(ROWS, encoding=STRICT_NULLABLE)
    aug = augment_with_evidence(s, STRICT_NULLABLE)
    ev = aug["properties"]["_evidence"]
    # mirrors the value structure, quotes at scalar leaves, arrays stay arrays
    assert set(ev["properties"]) == {"lvef", "severity", "study"}
    assert ev["properties"]["study"]["properties"]["findings"]["type"] == "array"
    assert "_evidence" in aug["required"]
    # the original schema is untouched (purity)
    assert "_evidence" not in s["properties"]


def test_envelope_evidence_off_is_byte_identical():
    plain = schema_envelope(ROWS)
    with_flag_off = schema_envelope(ROWS, request_evidence=False)
    assert plain["schema_uid"] == with_flag_off["schema_uid"]
    on = schema_envelope(ROWS, request_evidence=True)
    assert on["schema_uid"] != plain["schema_uid"]
    assert on["request_evidence"] is True


def test_evidence_schema_standalone():
    s = build_schema(ROWS)
    ev = evidence_schema(s, STRICT_NULLABLE)
    assert list(ev["properties"]) == ["_evidence"]
    assert ev["required"] == ["_evidence"]


def test_unwrap_schema_accepts_both_shapes():
    env = schema_envelope(ROWS)
    s1, enc1 = unwrap_schema(env)
    s2, enc2 = unwrap_schema(env["schema"])
    assert s1 == s2
    assert enc1 == STRICT_NULLABLE
    assert enc2 is None
    assert unwrap_schema(None) == (None, None)


def test_to_pydantic_source_execs_and_validates():
    pydantic = pytest.importorskip("pydantic")
    src = to_pydantic_source(ROWS, root_name="Echo")
    ns: dict = {}
    exec(src, ns)  # noqa: S102 - the point of the test
    model = ns["Echo"]
    obj = model(lvef=55.0, severity=None,
                study={"modality": "TTE", "findings": ["ok"]})
    assert obj.lvef == 55.0
    with pytest.raises(pydantic.ValidationError):
        model(lvef=250.0, severity=None, study={"modality": "TTE", "findings": []})
