import pytest

from extrct.wrapping import WRAPPER_LOGICS, coverage_ok, wrap_text

LONG = "\n\n".join(
    f"Paragraph {i}. " + "Sentence one about topic. Sentence two with detail. " * 6
    for i in range(12)
)


@pytest.mark.parametrize("logic", WRAPPER_LOGICS)
def test_every_logic_covers_every_character(logic):
    doc = wrap_text(LONG, logic, max_chars=400, overlap_chars=80)
    assert coverage_ok(doc)
    for c in doc["chunks"]:
        assert c["text"] == LONG[c["start"]:c["end"]]  # the offset contract


def test_identity_is_deterministic():
    a = wrap_text(LONG, "paragraph_pack", max_chars=400, overlap_chars=80)
    b = wrap_text(LONG, "paragraph_pack", max_chars=400, overlap_chars=80)
    assert a["wrap_uid"] == b["wrap_uid"]
    assert [c["chunk_uid"] for c in a["chunks"]] == [c["chunk_uid"] for c in b["chunks"]]


def test_params_change_identity():
    a = wrap_text(LONG, "paragraph_pack", max_chars=400, overlap_chars=80)
    b = wrap_text(LONG, "paragraph_pack", max_chars=500, overlap_chars=80)
    assert a["wrap_uid"] != b["wrap_uid"]


def test_oversized_single_unit_falls_back_to_windows():
    one_para = "word " * 500  # no blank lines: one huge paragraph
    doc = wrap_text(one_para, "paragraph_pack", max_chars=400, overlap_chars=50)
    assert doc["n_chunks"] > 1
    assert coverage_ok(doc)


def test_garbage_params_raise():
    with pytest.raises(ValueError, match="unknown wrapping logic"):
        wrap_text("x" * 500, "clever_split")
    with pytest.raises(ValueError, match="sane floor"):
        wrap_text("x" * 500, "fixed_window", max_chars=50)
    with pytest.raises(ValueError, match="overlap_chars"):
        wrap_text("x" * 500, "fixed_window", max_chars=400, overlap_chars=400)
    with pytest.raises(ValueError, match="empty text"):
        wrap_text("")
