# Small reports, ordinary control flow

`robo.report(stage, status, *, observations=(), measurements=None)` records a
program's assessment of one stage. It is an SDK call, not another model tool.

Use `status` equal to `pass`, `fail`, or `unknown`. Give a short stage name,
selected JSON measurements, and optionally up to four observation IDs obtained
by the same job. The host records those observations' original sampling times;
reporting does not acquire or refresh a sensor sample. Historical evidence stays
historical. Keep arrays and large candidate collections in the local program,
rather than printing every candidate.

Identify what selected measurements describe: the entity's role, image region or
sensor location, coordinate frame, and corresponding observation. Before/after
comparisons need the same entity. Neither a high segmentation score nor similar
pixel counts establish identity. Keep ambiguous identity or effects `unknown`.

The report is finite JSON, limited to 2048 serialized UTF-8 bytes including its
evidence references and source label. It consumes the existing event and log-byte
budgets. Invalid or oversized reports are rejected, never silently shortened.
An exhausted job budget retains the existing supervisor termination behavior.
There is no extra output allowance, device permission, or execution budget.

Each accepted report is an immutable `program_report` event. Its job and program
identities come from the host, and `source="program"` identifies the claim's
origin. The latest report is also attached to that job's status and snapshot,
even when earlier logs fill the current event page. Page cursors still address
the complete original event stream; the snapshot does not consume those events.

## A report is not a guard or a grader

The program decides how to measure effects and when to stop. In a program's
`main` function, report a stage assessment and branch before any action that
depends on it:

```python
# `status`, `evidence`, and `measurements` come from this program's sensor check.
robo.report("effect check", status,
            observations=[evidence["id"]], measurements=measurements)
if status != "pass":
    return
# Only now proceed with a dependent action, under the normal freshness checks.
```

`report` alone never stops a program, verifies a physical task, releases a
gripper, retries an action, clears an uncertain receipt, or grants fresh motion
authority. Its `pass` can be wrong. Independent device verification, confirmed
stop, process exit, and model completion remain separate facts. Do not make a
generic program guard depend on the evaluator's private score or on its default
`unknown` state; check the particular prerequisite using available sensors.

This API supplies no task-specific thresholds, geometry, skills, or recovery
actions. It supports concise feedback and normal Python branching, without a
new execution state machine.

An effect check must distinguish the action's required effect from failure. A
condition that remains true when the action fails is insufficient, as is a
requested pose without evidence of the observed pose. The host preserves these
claims but does not validate their physical meaning. Invalid JSON/non-finite
measurements and oversized reports have distinct error messages; both remain
rejected without publishing evidence or granting authority.
