from extrct.xai.grounding import ALIGNER_VERSION, align, ground_fields, tokenize

SOURCE = ("Echocardiography performed today. LVEF measured at 55 percent. "
          "Mild mitral regurgitation noted. LVEF stable at 55 percent on review. "
          "CD96+ cells were absent.")


def test_tokenize_character_classes():
    toks = [t for t, _, _ in tokenize("CD96+ at 55%")]
    assert toks == ["cd", "96", "+", "at", "55", "%"]


def test_exact_match_with_offsets_into_original():
    res = align("LVEF measured at 55 percent", SOURCE)
    assert res["status"] == "match_exact"
    assert SOURCE[res["start"]:res["end"]] == "LVEF measured at 55 percent"
    assert res["score"] == 1.0
    assert res["aligner"] == ALIGNER_VERSION


def test_case_insensitive_but_length_preserving():
    res = align("mild MITRAL regurgitation", SOURCE)
    assert res["status"] == "match_exact"
    assert res["matched_text"] == "Mild mitral regurgitation"  # original casing preserved


def test_symbol_identifiers_survive():
    res = align("CD96+ cells", SOURCE)
    assert res["status"] == "match_exact"


def test_repeated_quotes_map_to_successive_occurrences():
    out = ground_fields({"a": "55 percent", "b": "55 percent"}, SOURCE)
    a, b = out["fields"]["a"], out["fields"]["b"]
    assert a["status"] == b["status"] == "match_exact"
    assert b["start"] > a["start"]  # second mention, not the same span twice


def test_fuzzy_match_below_exact_above_threshold():
    res = align("LVEF was measured at 55 percent today", SOURCE, fuzzy_threshold=0.6)
    assert res["status"] == "match_fuzzy"
    assert 0.6 <= res["score"] < 1.0


def test_unlocatable_quote_is_the_gate():
    res = align("aortic stenosis severe", SOURCE)
    assert res["status"] is None
    assert "coverage" in res["reason"]


def test_empty_quote_reported_not_crashed():
    out = ground_fields({"x": "", "y": None}, SOURCE)
    assert out["fields"]["x"]["reason"] == "no evidence quote emitted"
    assert out["summary"]["unlocated"] == 2
    assert out["summary"]["grounding_clean"] is False


def test_summary_clean_when_everything_grounds():
    out = ground_fields({"lvef": "LVEF measured at 55 percent"}, SOURCE)
    assert out["summary"]["grounding_clean"] is True
    assert out["summary"]["exact"] == 1
