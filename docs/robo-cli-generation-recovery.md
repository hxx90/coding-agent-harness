# Bounded generation in the real Robo CLI

The legacy CLI agent loop now rejects incomplete model generations before any
call in that batch can execute. This applies to streaming and non-streaming
responses, even when a `length`/`max_tokens` prefix contains valid JSON and valid
policy code. Malformed argument JSON no longer becomes an empty/default call.
Missing finish reasons remain compatible with providers declaring
`emits_finish_reason=false`; the bounded robot protocol always requires a finish
reason. Unknown finish reasons fail closed.

For normal Robo use without harness token quotas, keep session safety enabled:

```toml
[generation]
enabled = true
limit_tokens = false
recover_once = false
```

This disables input/output token reservations and per-call output caps. With no
explicit caller `max_tokens`, the current OpenAI-compatible gateway request omits
`max_tokens` entirely. The service still has its own defaults and model context
limits; this is not a promise of unlimited output. Other protocols may require a
provider default. Do not use the old experimental launcher: it installs its own
bounded configuration and a task prompt containing quotas.

Request/time/action limits, policy sandbox bounds, cancellation, completeness
checks and separate safety cleanup remain active. No model, thinking setting,
controller or H1 setting is changed. No line-count instruction is added. Audits
record actual reported usage and flag missing usage; token totals are descriptive,
not limits. An explicit caller limit remains explicit and is not silently ignored.
`limit_tokens=true` preserves the earlier bounded protocol below. This local
configuration field needs no Settings Manager or Le Chat Web allowlist update.

Automatic regeneration defaults off. For an opt-in Lift session, add this to a
trusted project TOML (keep the existing model/provider and MCP configuration):

```toml
[generation]
enabled = true
recover_once = false
max_calls = 16
max_input_tokens = 1200000
max_output_tokens = 24576
per_call_output_tokens = 4096
max_seconds = 480
max_actions = 80
audit_dir = "/absolute/new/trial-directory"
```

Set `recover_once=true` to allow at most one fresh regeneration in the session.
It replaces only a wholly undispatched generation, after fresh device observation.
It never concatenates partial programs/JSON, retries cancellation, or automatically
replays runtime failures. Both normal calls and regeneration share reservations,
request count and deadline. A policy-tool error terminates a bounded task;
independent scoring, stop ACK and lease release run in a separate 30-second
allowance, each operation limited to 10 seconds. Cleanup uses the Manifest default_verification when it names an advertised
criterion (Stack uses stack_objects); older manifests retain lift_object. Its success does not prove the model autonomously verified or
closed the session; inspect actor tool calls separately.

`enabled=false` (default) disables the opt-in session budget and regeneration,
not the universal pre-dispatch completeness guard. H1 remains independently
controlled by `ROBO_SIM_GEOMETRY_LIBRARY`; project default remains off.

Input accounting conservatively reserves serialized UTF-8 bytes plus framing;
existing local RGB evidence reserves raw image bytes plus at least one token per
pixel. This is not an exact gateway tokenizer. Missing usage consumes the full
reservation. A provider-reported overage fails before dispatch. Output caps are
sent to the backend; complete reported output refunds unused output reservation.
All calls in a bounded session disable backend HTTP retries. Titles must remain
off (`session_logging.generate_titles=false`); automatic compaction fails closed.
`--max-tokens` is the older post-call middleware limit and does not replace this
pre-request reservation protocol.

`generation.jsonl` records request messages/tools, model settings, capped outputs,
raw responses and finish reasons, rejections, recovery observations, budget stops,
and independent cleanup results. It excludes HTTP headers and configuration
credentials. Hardware traces/video are in `hardware-runs/`; full policy source and
execution artifacts remain in the normal session directory. Audit files contain
user/model content and should be kept local.

This is a new protocol, not the previous one-call backend smoke budget. Compare
recovery off/on under identical caps; do not compare these scores as H1 benefits.
New config is local harness configuration, not a Settings Manager or Le Chat Web
request field; no managed UI or backend allowlist change is needed.
