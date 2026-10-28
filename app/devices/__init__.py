"""The device identity layer, behind `DEVICE_IDENTITY_ENABLED` (default off).

One row per machine (`Device`), one row per vendor record of it
(`DeviceVendorLink`), and a queue of records a pass could not place
(`DeviceUnresolved`). `resolve.py` fills them once a day from the same cached
inventories and the same three passes the coverage board uses
(`app/coverage/core.py`), so the two cannot disagree; `identity.py` is what
the board, the review's coverage findings and the device lookup tools read
when the flag is on. Nothing here talks to a vendor except through the
connector factory (hard rules 1 and 5), nothing writes to a vendor (hard rule
2), and nothing deletes: a record the vendor stops returning goes inactive, a
device nothing has seen is retired.
"""
