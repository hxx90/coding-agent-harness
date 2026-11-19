# Experimental native Robosuite Stack

`ROBO_SIM_TASK=Stack` selects Panda Stack; the default remains Lift. Start from
`examples/robo/stack-rgbd-off.toml`. Model/controller and existing actions are
unchanged. H1 is currently required off for Stack. This is not an official
multi-task RoboWorld/CaP-X benchmark result.

The task is to release the red cube on top of the larger green cube. In RGB-D
mode, `state.perception` retains the red-rgbd-v1 contract;
`state.support_perception` is green-rgbd-v1 (the same dominant-channel segmentation
with red/green swapped). Both have at most128 metric points, centroid, ok/count,
and identical frame timestamp/calibration. They use aligned camera RGB/depth
only; no object positions or simulator segmentation IDs enter either cloud.
Occlusion returns ok=false; never treat missing points as a valid target.

`verify("stack_objects")` is an independent evaluator, replacing lift_object for
this scene. It requires official Robosuite Stack success (red raised, contact
with green, no grasp), plus both cubes' linear speed below0.01m/s and angular
speed below0.1rad/s for20 consecutive control cycles (1s at the default20Hz).
Physical state is evaluator-only. Verification itself does not advance physics.
The actor must release, retreat and allow settling using the existing bounded
motion actions. No new unbounded wait/action interface is added. Control success
is not stack success. Stability means this finite window, not indefinite stability.

The Manifest advertises stack_objects and default_verification, so bounded cleanup
scores the correct task, then attempts stop ACK and lease release. Unknown/missing
metadata retains the old lift_object fallback. It cannot select an unadvertised
criterion. Lease, arming, cancellation and action budgets are unchanged.

Tests include camera-only red/green separation, real camera observations without
object truth, negative unstacked grading, an evaluator-only synthetic stacked
fixture with stability admission and invalidation, plus Lift/control regression.
No synthetic fixture or saved policy is passed to the autonomous actor.

SimulatorSettings is local environment configuration; no Settings Manager or
Le Chat Web configuration/allowlist change is needed.
