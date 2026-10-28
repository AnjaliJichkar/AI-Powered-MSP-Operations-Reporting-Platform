"""The Client Business Review (TBR), generated against the 2026 template.

The pipeline, in the order it runs (`pipeline.py`):

1. **facts** (`collect.py`) — every number, date and name is computed in code
   from the connectors, with a stable id and a written definition.
2. **findings** (`findings.py`) — rules over the facts. A claim that something
   is *absent* needs evidence from two tools before it is asserted.
3. **narrative** (`narrative.py`) — one model call per section. The model sees
   only that section's facts and findings, writes sentences that cite them by
   id, and may not write a digit: values are substituted by code.
4. **review** (`review.py`) — an engineer reads, edits, adds and approves.
   Nothing unapproved that needs approval is rendered.
5. **render** (`render.py`) — python-pptx fills the template slide by slide,
   drops slides with nothing to say, and refuses to write a file in which any
   placeholder survives (`gate.py`).

The slide-by-slide contract is `docs/reference/tbr-slide-map.md`.
"""
