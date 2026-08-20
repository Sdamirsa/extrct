import pytest

from extrct.job import load_job, parse_job

RAW = {
    "provider": {"provider": "ollama", "model": "test-model", "num_ctx": 8192},
    "schema": {
        "root_name": "echo",
        "variables": [
            {"name": "lvef", "type": "float", "constraints": {"ge": 0, "le": 100}},
            {"name": "severity", "type": "str", "options": ["mild", "severe"], "required": False},
        ],
    },
    "extract": {"ladder": ["json_repair", "coerce"], "run_tags": "test"},
    "grounding": {"enabled": True},
    "certainty": {"enabled": True, "top_logprobs": 3},
    "storage": {"backend": "sqlite", "path": "runs.sqlite"},
}


def test_parse_job_normalizes_everything():
    doc = parse_job(RAW)
    assert doc["version"] == "job-def/1.0"
    assert doc["client"]["version"] == "client-def/1.0"
    assert doc["schema"]["schema_uid"]
    assert doc["schema"]["encoding"] == "strict_nullable"
    assert doc["extract"]["ladder"] == ["json_repair", "coerce"]
    assert len(doc["job_uid"]) == 16


def test_provider_and_client_are_the_same_section():
    with_client = {**RAW}
    with_client["client"] = with_client.pop("provider")
    assert parse_job(with_client)["job_uid"] == parse_job(RAW)["job_uid"]
    both = {**RAW, "client": RAW["provider"]}
    with pytest.raises(ValueError, match="same section"):
        parse_job(both)


def test_storage_is_deployment_not_identity():
    a = parse_job(RAW)
    b = parse_job({**RAW, "storage": {"backend": "postgres", "dsn": "postgresql://x"}})
    c = parse_job({k: v for k, v in RAW.items() if k != "storage"})
    assert a["job_uid"] == b["job_uid"] == c["job_uid"]


def test_content_sections_do_change_identity():
    a = parse_job(RAW)
    b = parse_job({**RAW, "certainty": {"enabled": False}})
    assert a["job_uid"] != b["job_uid"]


def test_unknown_section_raises():
    with pytest.raises(ValueError, match="unknown section"):
        parse_job({**RAW, "logging": {"level": "debug"}})


def test_unknown_key_in_section_raises():
    with pytest.raises(ValueError, match="not a grounding setting"):
        parse_job({**RAW, "grounding": {"threshold": 0.9}})


def test_vocab_and_range_checks():
    with pytest.raises(ValueError, match="not one of"):
        parse_job({**RAW, "grounding": {"mode": "Inline"}})  # the measured capital-I trap
    with pytest.raises(ValueError, match="fuzzy_threshold"):
        parse_job({**RAW, "grounding": {"fuzzy_threshold": 0.2}})
    with pytest.raises(ValueError, match="not one of"):
        parse_job({**RAW, "storage": {"backend": "mysql"}})


def test_request_evidence_tombstone():
    bad = {**RAW, "schema": {**RAW["schema"], "request_evidence": True}}
    with pytest.raises(ValueError, match="grounding section"):
        parse_job(bad)


def test_schema_variables_required():
    with pytest.raises(ValueError, match="schema.variables"):
        parse_job({"provider": RAW["provider"], "schema": {"root_name": "x"}})
    with pytest.raises(ValueError, match="client section"):
        parse_job({"schema": RAW["schema"]})


def test_client_settings_validated_through_registry():
    bad = {**RAW, "provider": {"provider": "ollama", "contexts": 4096}}
    with pytest.raises(ValueError, match="Allowed:"):
        parse_job(bad)


def test_load_job_yaml_round_trip(tmp_path):
    import yaml

    p = tmp_path / "job.yaml"
    p.write_text(yaml.safe_dump(RAW), encoding="utf-8")
    doc = load_job(str(p))
    assert doc["job_uid"] == parse_job(RAW)["job_uid"]  # same content, same identity


def test_load_job_rejects_non_mapping(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_job(str(p))
