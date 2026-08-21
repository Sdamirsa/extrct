"""The handshake files stay well-formed and paired. Offline, stdlib + pytest.

These assertions hold on the template itself and on every app copied from it — they
check structure, not filled-in content (placeholders are legal until the app's
definition of done says otherwise).
"""

import json
import tomllib
from pathlib import Path

APP = Path(__file__).resolve().parents[1]


def test_data_model_shape():
    d = json.loads((APP / "data_model.json").read_text(encoding="utf-8"))
    assert d["handshake"] == "app-io/1.0"
    assert d["app"] and d["description"]
    assert d["inputs"], "an app with no declared inputs cannot be handed anything"
    assert d["outputs"], "an app with no declared outputs is not producing a record"
    for out in d["outputs"]:
        assert out.get("contract"), "every output names its contract"
        assert out.get("identified_by"), "every output says what identifies it"


def test_readme_sections_present():
    text = (APP / "README.md").read_text(encoding="utf-8")
    for section in (
        "## Scope",
        "## Boundaries",
        "## Input / output",
        "## Requirements",
        "## Definition of done",
        "## App-specific behaviours",
        "## Run",
    ):
        assert section in text, f"README.md lost its {section!r} section"


def test_pyproject_names_the_app():
    p = tomllib.loads((APP / "pyproject.toml").read_text(encoding="utf-8"))
    assert p["project"]["name"].startswith("extrct-app-")
    assert any(dep.startswith("extrct") for dep in p["project"]["dependencies"])
