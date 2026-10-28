"""The ticket workspace: one Autotask ticket, the device it concerns, what every
tool knows about that device, and a remediation plan an engineer reviews.

Read-only to every vendor; what is written is ours (`app/models/ticket.py`).
`scrub.py` removes likely secrets from ticket text before anything else sees
it; `resolve.py` finds the device through the identity layer; `enrich.py`
computes the facts panel in code; `plan.py` asks the model for steps and keeps
only those that cite evidence; `workspace.py` is the listing, the opening,
the polling and the outcome.
"""
