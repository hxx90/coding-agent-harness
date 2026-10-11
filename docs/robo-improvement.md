# Small, evidence-gated capability improvement

Robo can accumulate helper code without putting task solutions in its runtime.
This first loop improves **one pure effect predicate**, not a physical controller:

1. An operator selects an observed failure, declares the capability's meaning and
   supplies development cases plus a separate cross-task replay set.
2. The existing CodingSession reads the baseline and development feedback, then
   proposes one small Python patch. It cannot access transfer files or labels.
3. A trusted evaluator runs baseline/candidate on all cases. Candidate execution
   receives inputs only, not labels, task names, scores or device capabilities.
4. Promote only with development **and** cross-task gains, no newly incorrect
   cases, and acceptable source growth. Otherwise keep the baseline.
5. Persist the selected source and evidence. Admission here is to an **offline
   example capability**, never automatic deployment to a robot.

The generic implementation is `robo/improvement.py`; benchmark extraction and
model wiring stay in `examples/robo_improve`. No skill registry, new database,
task state-machine DSL or runtime API was added. The gate does not execute source
itself: its evaluator and proposer wrappers are operator-owned trust boundaries.

## First capability and actual result

The reusable question is: **is a previously occupied, measured seat now vacant?**
It is not "did the gripper retain the object?" A lifted object can remain inside
the same XY column, so that column's occupancy needs a height boundary.

The example exports point heights from immutable V4 CaP-X recordings. Extraction
pins the already-audited geometry source, opens SQLite read-only, and verifies
that decoded RGB-D matches the referenced image bytes and image hashes.

| Replay set | Before | After |
| --- | ---: | ---: |
| Restack development: one vacated seat + one before-action control | 1/2 | 2/2 |
| Lifting cross-task replay: ten vacated seats + ten before-action controls | 12/20 | 20/20 |
| Total correct predicate outputs | 13/22 | 22/22 |

On 2026-10-11, Claude Opus 5 proposed the patch through Docker Proxy 0.1.612:
4 model turns, 17,057 reported tokens (15,439 input / 1,618 output). The model
received the input contract and compact **restack-only** evidence, with guidance
to repair geometric selection. This is model-generated repair of an
operator-selected failure, not autonomous discovery of an unrestricted research
agenda. `seat_vacancy.py` preserves the resulting 17-line source byte-for-byte;
it adds 9 lines over the 8-line baseline. The additional minimum initial-occupancy
guard was not exercised by these 22 recordings; synthetic boundary tests cover
it separately. Do not attribute the replay gains to that guard.

An independent negative-control proposal changes the baseline count threshold
40→41. Its behavior is identical on the suite, so the same gate rejects it as
`unchanged_behavior`. Unit tests also reject regressions, task-only overfitting,
invalid predictions, syntax errors and excessive source growth.

Important limits:

- These recordings had already been inspected during development. Transfer means
  **another task's recorded observations**, not held-out generalization.
- Before-action controls are not actual failed-grasp attempts. Neither a pushed
  object nor a dropped object is excluded by seat vacancy; the tests demonstrate
  this limitation explicitly.
- No new physical trajectory, GPU operation or benchmark trial was run. The old
  V4 70/70 result and its source remain untouched.
- `True` is a predicate claim under its assumptions; `False` means vacancy was
  not established, not necessarily that an action failed. Occlusion/invalid
  observations need an outer `unknown` check before using this helper.
- The model's verification is `not_configured`: it has no test tool. Acceptance
  comes from the independent evaluator, not a self-reported model success.
- One attempted `glob` call was denied by the proposal workspace's permissions;
  the model then read/edited `candidate.py` directly. The original denial remains
  in its transcript; this is not described as an error-free agent session.
- Point-count thresholds (150, 40, 0.2) are inherited from the development
  predicate, not universal sensor constants. Revalidate observability, sampling
  density and thresholds before using this example with another camera.

Local full evidence (not committed; large recordings remain outside git):
`/workspace/hanyu/roboworld-bench/runtime/robo-improve-dev.il4ICq/`.
The model transcript, proposal, output, baseline/candidate/suite hashes, per-case
decisions and Docker Proxy trace are retained there. Selected source SHA256:
`39dd8e73c2d81decb4daf03553ce216427085c2930721f2d79805c45c8a8792c`.

## Reproduce without a model

Use Python 3.11+ on Linux with Landlock and libseccomp. The exporter additionally
needs NumPy and Pillow; they are example-only dependencies. From the repository:

```bash
python examples/robo_improve/export_capx.py RECORDING_ROOT /tmp/seat-cases.json
python examples/robo_improve/run.py \
  --cases /tmp/seat-cases.json --output /tmp/seat-replay-001 \
  --candidate examples/robo_improve/seat_vacancy.py
```

`RECORDING_ROOT` is the original V4 `claude-opus-5` directory containing
`cube_restack/trial_01` and `cube_lifting/trial_01` through `trial_10`.
Paths above must not already exist; earlier evidence is never overwritten.
The generated 3.7 MB numerical dataset is not a checked-in fixture. The unit
tests use small, explicitly synthetic cases and run without the recordings.

For a fresh proposal omit `--candidate` and supply `--model-url`, `--model` and
`--api-key-env` for an already-authorized endpoint/proxy. Credentials are read
from the named environment variable, never passed as a CLI value. Example
defaults: one proposal, at most 8 responses, 40,000-token request-boundary
threshold, 180-second session budget, 4,096 output tokens per response. In-flight
requests can cross session thresholds. There is no automatic retry of a rejected
proposal; do not feed transfer failures back into repeated searches and continue
calling that set hidden validation.

The generated Python evaluator has a 15-second subprocess timeout, 3 CPU seconds,
512 MiB address-space limit and 1 MiB output-file limit. Existing Linux isolation
denies file contents, device access, networking and child processes. Unsupported
isolation fails closed. This is an offline runner, not a real-time controller or
a hardware safety certification.

## Effect checks and recovery: a convention, not another API

Use existing `robo.report` plus ordinary Python control flow:

- Record a stage, pass/fail/unknown, referenced observations and small selected
  measurements. Preconditions include entity/region, coordinate frame,
  observability and calibration; keep numerical arrays local.
- Treat seat vacancy and object retention as **different predicates**. Check
  retention independently before dependent transport/release actions.
- A recovery checkpoint can record `next_stage`, complete parameters and the
  last confirmed command ID within the existing report budget. It is a historical
  hint, not proof of present state or new motion authority.
- Resume only after fresh observation and prerequisite checks. Reconcile an
  uncertain command using its original ID; never replay a physical operation
  merely because a client lost its reply.

## Keep the library small

Prefer an existing helper when its contract fits. Promote a new version only
after the replay gate; identical behavior is not improvement. This example caps
each proposal at 8 KiB and 12 additional lines. Those limits are simple review
guards, not proofs of low complexity. Store evidence outside the core library,
keep only selected reusable source in the example, and require broader validation
before expanding its supported conditions or wiring it into a live controller.

This first gate measures correctness only. Behavior-equivalent speed/size
improvements need a separately declared and measured cost criterion; they are
not inherently useless, just outside this acceptance rule.

## Validation and commit scope

- 23 new tests passed in both the working tree and an exported **staged-only**
  source snapshot. That snapshot also reproduced the complete real-model replay
  report byte-for-byte, without relying on unrelated working-tree changes.
- Targeted Ruff checks passed and a wheel built from the staged snapshot.
- The wider Robo/CLI run initially had 352 passed, 4 skipped and 9 CLI failures:
  the cached Python lacked its installed console entry point. A temporary venv
  supplied the entry point; a second attempt exposed missing prompt-toolkit.
  After installing the declared dependency, all 9 CLI rechecks passed. Original
  XML failure reports remain retained. This is not an all-repository test claim.
- Mypy was unavailable in the original cached environment; no type-check pass
  is claimed. No global Python installation was modified.
- The change includes the two pre-existing self-contained Linux isolation and
  numerical wire helpers needed by the example. Other pending CaP-X/runtime
  modifications are deliberately outside this commit.
