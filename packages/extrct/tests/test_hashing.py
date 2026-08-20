from extrct.hashing import canonical_json, content_uid, key_fingerprint, sha256_text


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": [2, 3]}) == canonical_json({"a": [2, 3], "b": 1})


def test_canonical_json_preserves_non_ascii():
    assert "é" in canonical_json({"x": "é"})


def test_content_uid_deterministic_and_short():
    a = content_uid({"x": 1, "y": "z"})
    b = content_uid({"y": "z", "x": 1})
    assert a == b
    assert len(a) == 16


def test_sha256_text_full_length():
    assert len(sha256_text("hello")) == 64


def test_key_fingerprint_never_reversible_marker():
    assert key_fingerprint("") == ""
    assert len(key_fingerprint("sk-secret")) == 8
    assert "secret" not in key_fingerprint("sk-secret")
