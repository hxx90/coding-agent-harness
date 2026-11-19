# Bounded target motion

The MuJoCo Manifest now provides `move_to` independently of H1. It tracks a
fixed `target_xyz` in world-frame meters using fresh end-effector proprioception
at each control period. The existing `move_cartesian(delta_xyz, steps)` still
repeats a normalized delta for `steps` periods; its control semantics are unchanged.

`move_to` accepts only these parameters:

| Parameter | Default | Bound |
| --- | --- | --- |
| target_xyz | required | three finite coordinates, each -2 to 2 meters |
| tolerance_xyz | [0.006, 0.006, 0.008] | each 0.0001 to 0.05 meters |
| max_steps | 72 | 1 to 400 total control periods, including settling |
| timeout_seconds | 30 | positive, at most 120 wall seconds |
| settle_steps | 20 | 1 to 20 consecutive in-tolerance periods |

The fixed Panda OSC scaling and clipping remain unchanged. World-frame errors
are rotated into the controller base frame when needed. The result is a typed
`result.motion` report with `reached`, `reason`, `target_xyz`, `position_xyz`,
`error_xyz` (target minus observed), `error_max_m`, `cycles`, `settled_cycles`,
`elapsed_seconds` and an optional local `trace_uri`. Cycle telemetry is written
to the evidence directory, not embedded as a growing model context.

Policy calls return the complete `CommandReceipt` dictionary, for both
`move_to(...)` and `act("move_to", ...)`. They do not unwrap `result`:

```python
before = observe()
receipt = move_to(target_xyz=before["state"]["end_effector_position"])
motion = receipt["result"]["motion"]
print(motion["reached"], motion["reason"], motion["error_max_m"], motion["cycles"])
```

This example holds the observed current position; it is not a grasp policy.
`receipt["motion"]` is invalid. A separate `verify()` call returns a
`VerificationReport`, whose `status` is at the top level. These distinct return
types must not be conflated. No compatibility alias or automatic unwrapping is
provided.

`reason` is `reached`, `step_limit`, `time_limit`, `cycle_budget`, `cancelled` or
`error`. Only `reached` returns a completed command. Other normal exits return
a failed receipt and disarm the adapter. HardwarePolicyRunner records that
receipt, aborts the remaining policy statements and requests stop ACK. A caller
cannot ignore a false `reached` and silently execute a later stage.

Cancellation is checked at the per-cycle await boundary and propagates normally;
it is never converted into a successful tool call. `state.last_target_move`
retains the cancelled report, including a cycle that completed physically before
cancellation arrived. It is queryable after the stop. The synchronous physics
step cannot be preempted midway; the deadline is checked before and after each
control cycle, so shutdown latency can include one control period/render.

`ROBO_SIM_MAX_CONTROL_CYCLES` optionally sets a shared session cycle cap for
both movement APIs and gripper actions. It is an operator setting, not a model
action argument. Existing callers without it keep their previous aggregate
budget. `home`, reconnect and rearming do not replenish the same adapter's cap.
The stop ACK's zero-control cycle uses a separate cleanup allowance. Current
experiments explicitly set the cap; per-policy statement/action/time and CLI
request/token/task-time limits still apply. SimulatorSettings is local to the
simulator process; this adds no Vibe config or Settings Manager/Le Chat field.

Control success never implies a successful Lift: require the existing independent
`lift_object` verifier and report model-requested cleanup separately from fallback
cleanup. H1 and generation recovery remain off by default.
