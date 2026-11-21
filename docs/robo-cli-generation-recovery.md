# Continuous tasks and explicit evaluation budgets in Robo CLI

## Normal use: no execution quotas

Use the following for interactive robot work:

```toml
[generation]
enabled = true
enforce_limits = false
limit_tokens = false
recover_once = false
```

`enabled` keeps audit and safety cleanup active. `enforce_limits=false` disables
whole-task request, time, token and action quotas and allows normal compaction.
The default policy tool has no statement, iteration, action or duration quota.
All four policy caps remain optional for deliberately bounded evaluations.
Pure computation yields cooperatively so cancellation works even without tool calls.
A policy failure stops that policy/device and returns its error to the actor;
the normal conversation continues so the actor can observe, re-arm if necessary,
and correct its strategy. Completed actions are never automatically replayed.

A long policy persists all observation/action/output steps to `trace_uri` JSONL.
The response contains recent steps, stdout and receipts with `history_truncated`
and cumulative statement/action/iteration counts. These presentation windows do
not end execution. AST permissions, structured-value protection, per-command
motion convergence/failure reporting, device leases and stop ACK remain intact.
Clear authentication/service errors can still require user intervention; this
mode removes artificial task quotas, not backend/context or physical limitations.

Transient pre-output requests keep retrying until successful or cancelled, with
2s/4s exponential backoff capped at 60s (a longer Retry-After is respected).
Truncated/partially emitted responses still fail closed; `recover_once` remains
separate. Do not use old experiment launchers: they explicitly install quotas.

The material below describes optional **bounded evaluation mode**. Set
`enforce_limits=true` to enable those task caps. The default value is true for
compatibility with old evaluation TOMLs; `enabled=false` still disables the whole
budget/audit controller. The recommended local configuration above is continuous.
No managed service config allowlist change is needed for this local harness field.

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
enforce_limits = true
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
enforce_limits = true
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

## Accounted request retries

In bounded evaluation mode, an assistant turn may retry a transient failed model request at most twice, only
when that request has emitted no chunks and none of its tools have been dispatched.
The agent loop owns these retries; bounded sessions still disable backend retries
to avoid hidden calls. Each attempt goes through normal reservation, accounting,
audit and deadline checks. Default waits are 2s and 4s. A larger Retry-After delay
is honored; if it exceeds 60s or the remaining task deadline, fail instead of
retrying early. Waiting is cancellable. Request-budget exhaustion fails before
another call. Failed requests have unknown usage, not zero reported cost.

Bounded sessions cover transient HTTP statuses (including 429/503) and pre-output
network failures. For api-gateway.glm.ai, HTTP 403 with error.code=get_channel_failed
is also eligible if its message explicitly reports transient upstream throttling
or unavailability. Ordinary auth errors, pure quota/IP denials and invalid requests
remain terminal. This does not change credentials, routing or model settings.
For unbounded sessions, standard HTTP/network retry ownership stays in the backend;
the agent adds only the gateway's narrowly recognized 403 compatibility handling.

Once any chunk has been emitted, this mechanism does not restart the response.
The existing completeness guard still rejects truncation and partial tool JSON.
`recover_once=false` continues to disable fresh regeneration of incomplete output;
request retries are a separate transport failure mechanism. Runtime policy errors,
statement_limit and post-dispatch action failures never enter this retry block.
Previous tool results stay in history; successfully executed calls are not replayed.

Audit kinds `request_retry` and `request_retry_exhausted` record category, status,
delay and the accounted call number. Retry UI uses the existing typed notification.
No config field was added; Settings Manager / backend allowlists are unchanged.
