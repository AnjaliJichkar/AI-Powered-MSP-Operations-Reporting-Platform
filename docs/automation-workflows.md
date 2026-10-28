# Writing an automation workflow

A workflow is **data** — one YAML file in `app/automation/definitions/`,
reviewed like code — assembled from **actions**: small, tested blocks in
`app/automation/actions/`. The engine (`app/automation/`) compiles a
definition into the steps it already knows how to run, under the controls
that have not changed: a per-client switch, a dry run, a two-person approval,
an audit row for every step, an undo window. No model is involved anywhere:
nothing under `app/automation/` imports the agent, the autopilot or the
Anthropic client, and `tests/automation/test_imports.py` fails the suite if
that ever changes — the actions and the definition loader included.

The only write any workflow can make today is adding an internal note to an
Autotask ticket (`autotask.add_note`), through the engine's own write client.
Every write an action may make is on `WRITE_SCOPES` in
`app/automation/actions/base.py`, and widening that list is the decision, made
in a commit that says so.

This page is in three parts: how to write an action, how to write a
definition (the device-online note as the worked example), and how to check
and try one before it is turned on.

## 1. Writing an action

An action is one block: "read the device's state from NinjaOne", "find the
device's tickets in Autotask", "add an internal note". It is written once,
tested on its own against fixtures, and used by any definition that names it.
Read `app/automation/actions/reads.py` (the reads), `notes.py` (the notes'
wording), `write.py` (the one write) and `practice.py` (the engine's own table)
beside this page.

### The contract

```python
READ_DEVICE = register_action(
    Action(
        name="ninjaone.read_device",          # connector.verb_noun; stable
        version=1,                            # bump on any change of behaviour
        title="NinjaOne: device status, last contact, last boot",
        connectors=("ninjaone",),             # from CONNECTORS; a pure block says ("none",)
        mode="read",                          # or "write"
        inputs={"device": Input("device", "The machine, from devices.resolve.")},
        outputs={
            "read": Output("read", "The read: its value and time, or why it failed."),
            "ok": Output("boolean", "Whether the tool answered."),
        },
        run=read_device,                      # the action itself
        idempotency=("device",),              # with the run and the step: one act
    )
)
```

Every action declares:

- **`name`** and **`version`**. The name is what a definition writes; a
  change of behaviour is a new version, so a run read later is read against
  what it ran.
- **`title`**: plain words, which the workflow page prints for the step.
- **`inputs`** and **`outputs`**, each typed from `TYPES`: `string`,
  `integer`, `number`, `boolean`, `list[string]`, `ticket` (the arrival a
  trigger saw), `device` (the identity layer's match), `read` (one tool read
  with its time, or why it failed), `note` (a draft or a skip), `facts` (plain
  JSON). The loader checks every reference in a definition against these, so
  a step that hands a string where a device is wanted is refused at load.
  An optional input is `Input(..., required=False)` and arrives as `None`
  when its step was skipped.
- **`connectors`**: which systems it reaches (`autotask`, `ninjaone`,
  `bitdefender`, `devices` — our identity layer — `practice`, or `none`). A
  test runs every action against a recording fake and fails if one reaches a
  connector it did not declare.
- **`mode`** and, for a write, **`write_scope`**: the exact write, in words,
  which must be on `WRITE_SCOPES`.
- the functions: `run` (the action; for a read, the read; for a write, the
  one thing that changes anything), and for a write also `precondition`
  (read only), `plan` (what would change, in the approver's words), `verify`
  (read only; the write is done when it passes, never because `run`
  returned) and `undo` — or no undo, in which case the action cannot be
  undone and every page says so. `before` captures what the undo restores.
- **`idempotency`**: the input names that, with the run and the step,
  identify one act. A second attempt of the same step is the same act.
- the words for a write (`checks`, `does`, `confirms`, `undoes`), which the
  workflow page prints for the client's authoriser.

The registry refuses an action that breaks any of this: a write with no
verify, a write scope off the allow-list, a connector or a type it does not
know, a name taken twice. `tests/automation/test_actions.py` proves each
refusal.

### What a function is handed

An `ActionContext`: the resolved `inputs`, the `client_id`, the `step_id`,
`now`, the client's `settings`, and what its kind of workflow has — a run
step has the `session` (its own transaction), the `run_id`, the `attempt`,
`dry_run` and `before`; a note step has the `tools` (the `Tools` protocol:
the reads, injected, never a connector import) and the `ticket`; a report
step has the `period`. `verify` and `undo` also get `outputs`, what `run`
returned. An action uses what its kind is given and nothing else; a read
action that is handed no tools raises rather than guessing.

### What makes an action safe

1. **A read returns the read.** Its value and the time it was read, or the
   reason it failed — never a blank that could be mistaken for a fact. What a
   failed read means is the note's decision, not the read's.
2. **`run` is idempotent for one (run, step).** Create-if-absent, set-to-
   value, never increment or append. A worker that dies between the action
   and the record of it runs it again on recovery.
3. **A write is done when `verify` passes.** Verify reads the world back and
   reports only what the step owns, because an undo compares those facts with
   what is there before it puts anything back and leaves alone anything that
   changed since.
4. **No connector import.** Reads go through `Tools`, writes through the
   `NoteWriter` the caller hands in. The isolation test and the import guard
   keep it so.
5. **Nothing generated.** A note's wording is Python string formatting in
   `actions/notes.py` over values a tool returned, and every note is pinned
   word for word in `tests/api/test_automation_notes.py`; a change to the
   wording is a change to what lands on a client's ticket and fails a test
   until it is meant.
6. **Loops live in actions.** A definition names an action once; an action
   that needs a list takes it as one input (`reports.chronic_devices` walks
   the period's tickets itself).

### Testing an action

Every action has its own tests in `tests/automation/test_actions.py`, against
the same fake `Tools` the note tests use, independent of any workflow: what
it returns when the tool answers, when the tool does not, and when it is
handed nothing. A write action's precondition, plan, run, verify and the
absence of an undo are each tested. Add an entry to `ISOLATION_INPUTS` there,
or the isolation test fails for the new action by name.

## 2. Writing a definition

One file per workflow, `app/automation/definitions/<name>.yaml`, the `name`
inside matching the file name. The worked example is the device-online note:

```yaml
name: device_online
title: Device back online
version: 1
kind: note                       # run | note | report
description: When a device-down alert arrives, read whether the machine is back, …
note_mode: preview               # the mode a client starts in

triggers:
  - key: down_alert
    kind: alert                  # manual | ticket | alert | schedule, the same fields as before
    title: a device or server down alert
    rule:
      source: Monitoring Alert
      title_contains_any: [offline, device down, server down, not responding, unreachable, no contact]

steps:
  - id: device
    action: devices.resolve
    inputs:
      ticket: "{ticket.self}"
  - id: state
    action: ninjaone.read_device
    inputs:
      device: "{steps.device.device}"
    when:
      value: "{steps.device.confident}"
      equals: true
  - id: alerts
    action: ninjaone.read_open_alerts
    inputs:
      device: "{steps.device.device}"
    when:
      - value: "{steps.device.confident}"
        equals: true
      - value: "{steps.state.ok}"
        equals: true
  - id: compose
    action: notes.device_online
    inputs:
      ticket: "{ticket.self}"
      device: "{steps.device.device}"
      state: "{steps.state.read}"
      alerts: "{steps.alerts.read}"
  - id: post
    action: autotask.add_note
    inputs:
      ticket_id: "{ticket.id}"
      note: "{steps.compose.note}"

writes: "Device <device> is back online: last contact <time> … — or — Device <device> is still offline …"
```

Read it top to bottom: the machine is resolved; its state is read only when
the match is confident; the open alerts only when the state came back; the
note is composed from whatever was read (a read that was skipped arrives as
nothing, and the note action says so in one line or writes nothing, as its
core fact dictates); and the one write is the last step. The `when` gates
are what the builder used to do with `if` statements, written where a
reviewer can see them.

### What a definition declares

- `name`, `title`, `version`, `description` (one sentence), `kind`.
- `allowed_on`: `any`, `demo` or `live` (the practice workflow is `demo`).
- `triggers`: the four kinds with the fields they always had — `manual`;
  `ticket` and `alert` with a `rule` (`queue`, `issue_type`,
  `sub_issue_type`, `source`, `title_equals`, `title_contains`,
  `title_contains_any`); `schedule` with `every: 30 days`. A note workflow
  has one alert trigger; a report one schedule.
- `inputs` (run workflows): each with `type`, `description`, `required` or a
  `default`, `min`/`max` or `min_length`/`max_length`, and a `source`:
  `engineer`, or the ticket's `number`, `title`, `contact`, `company` or
  `site` for a triggered draft (the arrival carries no device, so a machine
  is found by `devices.resolve` inside the workflow, not taken as an input).
- `settings`: what the client must set, each with `type`, `description`,
  `required` or a `default`. A required setting has no default, and the
  workflow **cannot be turned on for a client until it is set**: the
  Automations page shows "Needs settings" with the list behind it, and
  `manage_client.py report-config --automation-file` checks every value
  against the declared type and refuses a key the definition does not
  declare (`triggers`, the per-trigger rule overrides, is always allowed).
- `steps`: each an `id`, an `action`, its `inputs`, optionally `title`,
  `depends_on` (an earlier step; a run refuses the step unless that one is
  done) and `when`.
- `approval.window`, `undo_window` (durations: `24 hours`, `30 days`),
  `note_mode` for a note workflow, `writes` (the note's wording with
  `<placeholders>`, printed on the workflow page; it is presentation and
  never fills anything).

### The reference syntax, which is the only syntax

A value is a literal — a string, a number, a boolean, a list of strings — or
exactly one reference:

| Reference | Means |
| --- | --- |
| `{inputs.x}` | the run's input `x` |
| `{steps.<id>.<output>}` | an output of an earlier step, by the action's declared output name |
| `{settings.x}` | a setting the definition declares and the client set |
| `{ticket.x}` | a field of the ticket that started a note workflow: `self` (the whole ticket), `id`, `number`, `title`, `description`, `queue`, `issue_type`, `sub_issue_type`, `source`, `contact`, `company`, `site`; a run workflow has only `number` |

There are no templates (`"run-{inputs.label}"` is refused), no expressions,
no scripts, no regex. A `when` is one reference and one operator —
`equals`, `not_equals`, `in`, `exists`, `greater_than`, `less_than` — or a
list of them, all of which must hold. There are no loops.

### What the loader refuses

Every file is checked when the engine starts and by `scripts/automation.py
validate`; the first bad file stops the load, because an engine with half
its workflows is not an engine. The error is one line naming the file, the
step and the problem:

```
disk_space.yaml: history: autotask.find_related_tickets needs input 'days'
threat.yaml: compose: {steps.detections.read} refers to a step that has not run yet …
practice.yaml: set_status: depends on 'close_out', which is not an earlier step (a dependency on a later step is a cycle)
```

Refused: an unknown action; a missing, unknown or mistyped input; a
reference to a later step, to itself, or to an output the action does not
have; a dependency on a later step or a cycle; a write action without a
verify; a setting or an input the definition does not declare; a value that
is not a literal or one reference; a condition with no operator, two, a
literal where a reference goes, or a numeric comparison of a non-number; a
note workflow that does not end with its one write, or takes inputs; a
report that writes; a bad duration, kind, name or source. The tests in
`tests/automation/test_library.py` produce each one.

### What a run records

A run records the definition's name, version and a SHA-256 of its
**canonical form** (`automation_runs.definition_hash`; a note records it
too), so a run can always be read against what it ran. The hash is of the
parsed definition, not of the file's bytes: the same definition is not the
same bytes on two machines — a checkout with `core.autocrlf` writes CRLF on
Windows and LF on Linux, and an editor adds or strips a final newline, a
byte order mark or trailing spaces — and a run's record of what it ran must
not move because of any of that. Two files that parse to the same definition
hash the same; a changed step, input, trigger or word of the description
does not. (`.gitattributes` keeps the files themselves LF everywhere, so a
diff is a diff; the hash does not depend on it.)

Each step row records the inputs its references resolved to and the outputs
the action returned (`automation_steps.inputs`, `outputs`); the run page's
"Values" show both.

## 3. Validate and try

```bash
# Every definition, one line each: OK, or the problem.
python scripts/automation.py validate

# One workflow's dry run on one client: real reads, no writes.
python scripts/automation.py try practice --client <slug>
python scripts/automation.py try device_online --client <slug> --ticket <number>
```

`validate` is also run by the test suite. `try` runs the preconditions and
plans of a run workflow (on a run that is never created, rolled back at the
end), or the reads and the note of a note workflow for one open ticket (the
write step is listed and never run), and prints what each step would do. It
refuses when the workflow is already switched on for the client: it is for
before go-live. A new workflow is tried on the demo tenant first, then on a
live client with its switch off, then turned on.

### Adding a workflow, in order

1. Write the actions it needs that do not exist yet, with their tests, and
   a line on `WRITE_SCOPES` if one writes — that line is the decision, made
   in its own commit.
2. Write the definition. Run `validate` until it is clean.
3. Run `try` on the demo tenant and read what each step would do.
4. Commit the file. The engine loads it at start; the Automations page shows
   it off for every client, "Needs settings" where it declares a required
   setting the client has not set.
5. Set the client's settings (`manage_client.py report-config
   --automation-file`), `try` on the client with the switch off, then turn
   it on.
