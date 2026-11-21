# PickPlace single-can pilot

Set `ROBO_SIM_TASK=PickPlace`, `ROBO_SIM_OBSERVATION_MODE=rgbd`, and
`ROBO_SIM_GEOMETRY_LIBRARY=off` in the simulator MCP environment. The default
remains Lift. Merge `examples/robo/pickplace-rgbd-off.toml` into the existing MCP
configuration without replacing model/provider settings.

This selects unmodified Robosuite 1.5.2 **PickPlaceCan**, a single red soda can
and its assigned compartment. It does not run the four-object PickPlace task
or the RoboWorld/CaP-X benchmark protocol. Robot, OSC controller, action receipt,
lease, arming, cancellation, shared cycle budget and stop behavior are unchanged.

## Observation and goal

The bins arena uses **agentview**, because it has no sideview camera. The camera
pipeline uses aligned flipped RGB/depth, metric optical-Z depth, public camera
calibration and the existing red-rgbd-v1 color segmentation. `state.perception`
contains at most 128 world-z-up points and a centroid of the visible red surface.
It is not the physical can center or a guaranteed grasp pose. Occlusion can
produce `ok=false`; check before using it. No object position, simulator
segmentation IDs, contact state or scoring state is supplied to the actor.

`state.task_goal` is explicitly **public-fixed-workcell-specification**, not
camera perception. Native fixture defaults place the can's destination at XY
`[0.1975, 0.4025]`, with floor Z `0.82` and walls reaching Z `0.9`. These fixed
coordinates describe where the task asks the object to go, not where it is.
The scorer test checks this specification against the native destination.
This contract assumes native fixed bins; changing fixture geometry requires a
new specification and validation. It does not demonstrate visually locating an
unknown destination bin.

## Independent grading

Use `verify("place_object")` and a separate `robo_verify` call. Success requires
the official native bin predicate (including retreat), an explicit no-grasp
condition, and peak-to-peak object position drift at most 2 mm and orientation drift at
most 2 degrees across 20 consecutive control cycles. Native can mesh contacts
produce instantaneous velocity jitter even when pose drift is only tens of
micrometers, so this new task uses a bounded pose window rather than Stack's
instantaneous speed thresholds. Verification checks current state
again; cached stability alone cannot pass. Verify does not advance simulation.
The actor must release, retreat and allow settling within the existing budget.
`home` resets stability but never refills the cycle budget.

Object truth is used only inside the independent evaluator. Its positive test
fixtures place the object directly to test scoring; those are not autonomous
successes and are never sent to the actor. Wrong-bin, unsettled, grasped and
moving conditions must fail. Cleanup obtains `place_object` from the manifest,
then independently stops and releases; fallback cleanup is reported separately.

SimulatorSettings is local MCP environment configuration. No new core settings
field or Settings Manager / Le Chat Web allowlist change is required.
