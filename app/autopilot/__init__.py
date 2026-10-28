"""Autopilot: the agent doing triage, one level of autonomy at a time.

Level 0 is shadow mode — it predicts and is scored, and writes nothing
anywhere but our own tables. `shadow/` holds it and deliberately imports no
connector factory, so that guarantee is structural rather than a promise
(`tests/autopilot/test_imports.py`).
"""
