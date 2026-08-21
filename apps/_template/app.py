"""Entry point template. The app renders; the library decides.

Keep this file wiring-only — page config plus calls into `ui/`, where real code
grows. Every document is built and validated by `extrct`, and the app's I/O must
match data_model.json (which this stub reads, so drift shows up on first page load).
"""

import json
from pathlib import Path

import streamlit as st

HANDSHAKE = json.loads(
    (Path(__file__).parent / "data_model.json").read_text(encoding="utf-8")
)

st.set_page_config(page_title=HANDSHAKE["app"], layout="wide")
st.title(HANDSHAKE["app"])
st.caption(HANDSHAKE["description"])

st.info(
    "Template stub — replace this body, keep the shape. "
    "README.md holds the handshake this app must honour."
)

with st.expander("Declared inputs and outputs (data_model.json)"):
    st.json(HANDSHAKE)
