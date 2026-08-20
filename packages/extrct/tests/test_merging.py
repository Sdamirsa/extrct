import pytest

from extrct.merging import adjudication_task, merge_extractions


def _inputs(*objs):
    return [{"chunk_idx": i, "extracted": o} for i, o in enumerate(objs)]


def test_agreement_is_not_a_conflict():
    out = merge_extractions(_inputs({"lvef": 55.0}, {"lvef": 55.0}))
    assert out["merged"]["lvef"] == 55.0
    assert out["conflicts"] == []
    assert out["stats"]["major_conflicts"] == 0


def test_null_is_abstention_not_a_vote():
    out = merge_extractions(_inputs({"lvef": 55.0}, {"lvef": None}))
    assert out["merged"]["lvef"] == 55.0
    assert out["conflicts"] == []


def test_numeric_tolerance_groups_close_values():
    out = merge_extractions(_inputs({"lvef": 55.0}, {"lvef": 55.3}),
                            config={"numeric_tolerance": 0.01})
    assert out["conflicts"] == []  # within 1% relative tolerance


def test_majority_wins_and_minor_severity():
    out = merge_extractions(_inputs({"s": "mild"}, {"s": "mild"}, {"s": "severe"}))
    assert out["merged"]["s"] == "mild"
    assert out["conflicts"][0]["severity"] == "minor"


def test_tie_is_major_and_label_and_null_nulls_it():
    out = merge_extractions(_inputs({"s": "mild"}, {"s": "severe"}),
                            config={"conflict_policy": "label_and_null"})
    assert out["merged"]["s"] is None
    assert out["conflicts"][0]["severity"] == "major"
    # the full candidate set is surfaced, never silently resolved
    assert {c["value"] for c in out["conflicts"][0]["candidates"]} == {"mild", "severe"}


def test_refuse_strategy_returns_none_on_conflict():
    out = merge_extractions(_inputs({"s": "mild"}, {"s": "severe"}),
                            config={"scalar_strategy": "refuse"})
    assert out["merged"]["s"] is None


def test_list_clustering_dedupes_overlap_extractions():
    out = merge_extractions(_inputs(
        {"meds": ["aspirin 100mg", "metoprolol"]},
        {"meds": ["Aspirin 100 mg", "furosemide"]},   # overlap chunk saw aspirin again
    ), config={"list_similarity": 0.8})
    meds = out["merged"]["meds"]
    assert len(meds) == 3  # aspirin clustered once
    assert "aspirin 100mg" in meds


def test_list_key_acts_as_blocking_key():
    out = merge_extractions(_inputs(
        {"meds": [{"name": "aspirin", "dose": "100mg"}]},
        {"meds": [{"name": "aspirin", "dose": "100 mg daily"}]},
    ), config={"list_key": "name"})
    assert out["stats"]["variables"]  # per-field merge happened
    assert len(out["merged"]["meds"]) == 1


def test_evidence_is_stripped_before_merge():
    out = merge_extractions(_inputs({"lvef": 55.0, "_evidence": {"lvef": "quote"}}))
    assert "_evidence" not in out["merged"]


def test_merge_uid_deterministic():
    a = merge_extractions(_inputs({"x": 1}, {"x": 2}))
    b = merge_extractions(_inputs({"x": 1}, {"x": 2}))
    assert a["merge_uid"] == b["merge_uid"]


def test_no_usable_inputs_raises():
    with pytest.raises(ValueError, match="no usable extractions"):
        merge_extractions([{"chunk_idx": 0, "extracted": None}])


def test_adjudication_task_only_speaks_on_conflicts():
    clean = merge_extractions(_inputs({"x": 1}, {"x": 1}))
    assert adjudication_task(clean) == ""
    tied = merge_extractions(_inputs({"x": 1}, {"x": 2}))
    prompt = adjudication_task(tied)
    assert "x" in prompt and "chunk" in prompt


def test_bad_config_raises():
    with pytest.raises(ValueError, match="scalar_strategy"):
        merge_extractions(_inputs({"x": 1}), config={"scalar_strategy": "coin_flip"})
