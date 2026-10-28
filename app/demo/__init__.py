"""The demo tenant: a fictional franchisee, generated, served by fake vendors.

Nothing in this package reads a real vendor, a real tenant or a recorded
fixture. `dataset.py` generates the world from a seed (deterministic, so the
same seed is the same customers, devices and tickets on every process),
`vocabulary.py` holds the public words it is built from (fictional company
names, Windows KB numbers, CISA known-exploited CVE ids, common software
titles), `scenarios.py` lists what the world must contain and checks it, and
the `*_api.py` modules answer each vendor's HTTP API from the world so the
real connector clients run against them unchanged (`connectors.py`).

Timestamps are relative: the world stores days-ago offsets and every response
resolves them against the clock at serve time, so the demo is always "the last
ninety days" whenever it is opened.
"""
