"""The app's own modules — all UI-shaped, by rule.

What belongs here: widget building (schema -> form controls, bound to a model
object with bind_value), layout helpers, event handlers, and the single seam that
talks to `extrct` (loading a variables set, validating and storing an output
document). What never belongs here: extraction or schema logic — the library
builds and validates every document; this package renders and relays.

`app.py` stays wiring-only; real code grows here as modules (e.g. render.py,
store.py). Imports resolve run-in-place: `python app.py` puts the app folder on
sys.path, so `import ui` needs no install step.
"""
