"""Fleet-wide coverage: which managed devices each tool actually covers.

NinjaOne's managed computers are the population. For every one of them and
every tool that should have an agent on it — Bitdefender, ConnectSecure,
later Cove — the question is the same: does the tool know this machine, and
is what it knows current? `core.py` answers it from the vendors' inventories
with the hostname match every other cross-tool join uses, within one site and
never across two; `compute.py` runs that for a whole client from the cached
tenant inventories; `config.py` is where a tool is added and where every
threshold lives, with the definition beside it.

The same core is what the business review's "no protection agent" and scan
coverage findings read, so the deck and the board cannot disagree.
"""
