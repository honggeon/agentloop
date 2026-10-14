# DSH ACP Protocol Spike (M6A)

## Scope

M6A verifies the ACP surface actually shipped by the installed DSH and fixes the
transport contract that M6B will build on. It produces the low-level transport
only: no backend route, no SSE, no frontend, no session manager, no model call.

Verified against the installed package, not against documentation:

| Item | Value |
|---|---|
| DSH version | `0.2.0-rc.2` |
| Install root | `/Users/chen/.local/lib/node_modules/@deepseek-ai/dsh/` |
| ACP app bundle | `@deepseek-ai/dsh-acp-app` `0.2.0-rc.2` |
| ACP protocol plugin | `@deepseek-ai/dsh-acp` `0.2.0-rc.2` |
| ACP SDK | `@agentclientprotocol/sdk` `1.4.0` |
| Entry point | `dsh --profile acp` |
| Protocol version | `1` |

Every claim below cites the file it was read from. Runtime code and tests remain
the source of truth; this document is a point-in-time record.

## 1. Transport and framing

ACP over stdio is **newline-delimited JSON** (NDJSON), not LSP-style
`Content-Length` headers.

`@agentclientprotocol/sdk/dist/stream.js` encodes each message as
`JSON.stringify(message) + "\n"` and decodes by buffering until `\n`, trimming,
skipping blank lines, and `JSON.parse`-ing the remainder. A line that is not
valid JSON does **not** kill the stream: the SDK replies with a parse-error
response and keeps reading.

Consequences for the Python transport:

- stdout and stdin are line-oriented UTF-8 in both directions and fully
  symmetric. The client writes requests, notifications, **and responses**;
  it reads responses, notifications, **and requests**.
- Frames are unbounded by the protocol, so the reader must enforce its own byte
  limit and surface oversize frames as an explicit protocol error.
- Nothing but protocol traffic may reach stdout. `dsh-acp-app` states this
  directly and binds stdin EOF to a bounded shutdown
  (`@deepseek-ai/dsh-acp-app/lib/index.js`): closing the client's stdin is the
  documented way to ask the server to exit.

## 2. JSON-RPC envelope

Standard JSON-RPC 2.0 (`@agentclientprotocol/sdk/dist/jsonrpc.js`):

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","method":"session/update","params":{}}
{"jsonrpc":"2.0","id":1,"result":{}}
{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"...","data":{}}}
```

- `id` is a string or number; the transport issues positive integers and accepts
  either on the wire.
- A payload with `id` **and** `method` is a request; `method` without `id` is a
  notification; `id` without `method` is a response carrying exactly one of
  `result` or `error`.
- Error codes are the JSON-RPC set: `-32700` parse, `-32600` invalid request,
  `-32601` method not found, `-32602` invalid params, `-32603` internal,
  `-32800` request cancelled, `-32000` auth required, `-32002` resource not
  found.

## 3. Method surface

Agent-side methods the client calls
(`@agentclientprotocol/sdk/dist/schema/index.js`, `AGENT_METHODS`; DSH handler
registrations in `@deepseek-ai/dsh-acp/lib/index.js`):

| Method | Kind | Status in DSH 0.2.0-rc.2 |
|---|---|---|
| `initialize` | request | supported |
| `authenticate` | request | supported, always succeeds |
| `session/new` | request | supported |
| `session/list` | request | supported, capability-gated |
| `session/resume` | request | supported, capability-gated |
| `session/close` | request | supported, capability-gated |
| `session/set_config_option` | request | supported |
| `session/prompt` | request | supported, one in flight per session |
| `session/cancel` | **notification** | supported |
| `$/cancel_request` | **notification** | SDK-level request cancellation |
| `session/load`, `session/delete`, `session/fork` | request | **not mounted**; expect `-32601` |
| `fs/*`, `terminal/*`, `elicitation/*` | client method | DSH never calls these |

The last row matters: DSH's ACP surface intentionally omits client filesystem,
terminal, and elicitation calls, so a Python client never has to implement them.
It advertises no `fs` or `terminal` capability requirement.

`$`-prefixed methods are protocol-level and are deliberately not declared in
`AgentRequestHandlersByMethod`; `$/cancel_request` takes `{"requestId": <id>}`.

## 4. Initialization and capability negotiation

Request (`InitializeRequest`):

```json
{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"...","version":"..."}}
```

Response (`InitializeResponse`), as DSH actually returns it — read from the
`initialize` handler in `@deepseek-ai/dsh-acp/lib/index.js`:

```json
{
  "protocolVersion": 1,
  "agentInfo": {"name": "deepseek-harness-acp", "version": "0.0.1"},
  "agentCapabilities": {
    "mcpCapabilities": {"http": true},
    "promptCapabilities": {"image": false, "audio": false, "embeddedContext": false},
    "sessionCapabilities": {"close": {}, "list": {}, "resume": {}}
  },
  "authMethods": []
}
```

Notes that shape the client:

- `protocolVersion` is the **only** negotiated field. The client must reject a
  mismatched version rather than guess.
- `promptCapabilities.image` is resolved per connection from the live LLM
  service catalog, so it is `false` unless an exact image-capable route is
  configured. The client must send image prompts only when it observed `true`.
- `sessionCapabilities` is where `list`, `resume`, and `close` are advertised.
  A client must not call those methods on an agent that did not advertise them.
- `authMethods` is `[]` because no authentication is required; `authenticate` is
  a no-op that can still be called.
- `agentInfo.version` is hard-coded to `0.0.1` and is **not** the DSH version.
  Do not record it as `dsh_version`.

Client capabilities sent by the Python transport are empty: the transport
implements no ACP client extension and must not claim one.

## 5. Session lifecycle

### Create

`session/new` takes `{"cwd": "<absolute>", "mcpServers": []}`; `cwd` must be
absolute or the server returns invalid params (DSH
`validateWorkspaceParams`). `additionalDirectories` is **rejected** by DSH, so
the client must omit it or send an empty list. Response:
`{"sessionId": "<uuid>", "configOptions": [...]}`.

The returned session id is a branded random UUID. It is opaque and must never be
interpolated into a filesystem path, but it is **not** validated by the same
rules as the Headless `session-...` id: it contains no `/`, `\`, `..`, or
control characters, so the transport enforces exactly those properties instead
of reusing `validate_session_id`.

### Resume

`session/resume` takes `{"sessionId": "<uuid>", "cwd": "<absolute>"}`. DSH
rejects a session that is already active in this process and verifies that the
request `cwd` matches the persisted canonical workspace. Response carries
`configOptions` only — **there is no `sessionId` echo**. The client must
therefore correlate the resume response with the id it asked for. DSH restores
the log without replaying historic updates, so a resumed session produces no
retroactive `session/update` traffic.

### List

`session/list` takes `{"cwd"?: "<absolute>", "cursor"?: "<opaque>"}` and returns
newest-first pages with an opaque `nextCursor`.

### Close

`session/close` takes `{"sessionId": "<uuid>"}`. DSH performs quiescent
teardown: cancel in-flight work, drain committed updates, dispose descendants,
flush persistence, and dispose only that session's Agent scope. Closing one
session must not affect another. The persisted session stays listed and
resumable, so close is not deletion.

### Connection teardown

Client disconnect (stdin EOF) makes `dsh-acp-app` exit with a bounded shutdown.
The ACP bridge treats connection close exactly like an explicit close for every
owned session.

## 6. Session updates (notifications)

Agent → client notifications use method `session/update` with
(`@agentclientprotocol/sdk/dist/schema/types.gen.d.ts` `SessionNotification`):

```json
{"sessionId":"<uuid>","update":{"sessionUpdate":"<variant>", ...}}
```

Variants DSH actually emits — from the projections in
`@deepseek-ai/dsh-acp/lib/index.js`:

| `sessionUpdate` | Payload | Meaning |
|---|---|---|
| `agent_message_chunk` | `content` (usually `{type:"text",text}`, optional `messageId`) | committed assistant text |
| `agent_thought_chunk` | `content` (text, optional `messageId`) | committed reasoning |
| `tool_call` | `toolCallId`, `title`, `kind`, `status:"in_progress"`, `rawInput` | tool lifecycle start |
| `tool_call_update` | `toolCallId`, `status`, `content[]` | tool settled or progressed |
| `usage_update` | `used`, `size` | context occupancy, only when usage **and** capacity are known |
| `config_option_update` | `configOptions` | topology change (model/reasoning options) |

The standard also defines `user_message_chunk`, `plan`, `plan_update`,
`plan_removed`, `available_commands_update`, `current_mode_update`,
`session_info_update`, `compaction_update`, `compaction_summary_chunk`. DSH
mounts none of the features that would emit them, but the transport must
tolerate **unknown** variants by preserving their `sessionUpdate` discriminator
and redacted payload instead of failing the stream.

Update payload details that the projection layer must respect:

- `tool_call.title` is the tool **name**, and `kind` is always `"other"` in DSH.
  The programmatic `name` field is intentionally left unset, so a normalized
  event should take the tool name from `title`.
- `tool_call.rawInput` is the parsed tool arguments; DSH deliberately preserves
  malformed model JSON as an opaque string. The normalizer must therefore accept
  both an object and a string.
- `tool_call_update.status` is `"completed"` or `"failed"`; `content` is a list
  of `{"type":"content","content":<ContentBlock>}` entries (diffs and terminals
  are not produced by DSH). Text has to be extracted from nested content blocks.
- `messageId` groups chunks of one message; a change of `messageId` starts a new
  message. Without it, consecutive chunks belong to the same message.

Updates are serialized per session. A client that receives an update for an
unknown or closed session must drop it rather than crash.

## 7. Prompt turn and settlement

`session/prompt` takes (`PromptRequest`):

```json
{"sessionId":"<uuid>","prompt":[{"type":"text","text":"..."}]}
```

- `prompt` is an ordered `ContentBlock[]`. DSH accepts text and resource links
  always; images only when `initialize` reported `image: true`; audio never.
- The server validates the whole batch, snapshots the model route, and only then
  queues the message. A cancellation that wins admission never enqueues a late
  turn.
- **One prompt in flight per session.** A second concurrent prompt is a protocol
  error from the client's perspective; the transport enforces it locally so the
  mistake is a local, explicit failure rather than an ambiguous server response.

Response (`PromptResponse`):

```json
{"stopReason":"end_turn","usage":{...}}
```

| `stopReason` | DSH source reason (`@deepseek-ai/dsh-acp/lib/index.js`) |
|---|---|
| `end_turn` | `completed`, `aborted`, `blocked`, `error`, or a seed-only variant |
| `max_tokens` | `max-tokens` |
| `cancelled` | `interrupted` |
| `refusal` | not produced by the current mapping |
| `max_turn_requests` | not produced by the current mapping |

This mapping is lossy and must be treated as such: `end_turn` covers both
success and failure, so **`stopReason` alone does not prove success**. The
authoritative signal stays the durable session recording, exactly as with
Headless. The transport reports `stopReason` faithfully and does not invent a
success flag from it.

Settlement order is explicit: cancellation, then committed-output failure, then
interval-wide Agent failure, then the correlated turn ending.

## 8. Permission requests

The agent asks the client for authorization with a **request** (not a
notification), so the client must reply with a response frame. DSH's call site
(`@deepseek-ai/dsh-acp/lib/index.js`, `approval/request` handler) always sends
exactly two options:

```json
{
  "sessionId": "<uuid>",
  "toolCall": {"toolCallId": "<id>"},
  "options": [
    {"optionId": "allow-once",  "name": "Allow once", "kind": "allow_once"},
    {"optionId": "reject-once", "name": "Reject",     "kind": "reject_once"}
  ]
}
```

The response is `RequestPermissionResponse`:

```json
{"outcome": {"outcome": "selected", "optionId": "allow-once"}}
{"outcome": {"outcome": "cancelled"}}
```

DSH folds the outcome back into its approval system as
`allow-once → "allowed-once"`, anything else selected → `"rejected"`, and
`cancelled → "cancelled"`. DSH waits for this answer while the prompt is in
flight, so the transport must be able to handle an inbound request **while a
`session/prompt` call is blocked**. A client that can only answer after the
prompt returns self-deadlocks; this is the single most important structural
constraint on the transport.

`allow_always` / `reject_always` are valid ACP kinds but DSH never offers them,
and the transport must not synthesize them.

## 9. Cancellation

Two distinct mechanisms:

- `session/cancel` (notification, `{"sessionId": ...}`) is the prompt-owned
  cancellation path. Without an ACP prompt in flight it cancels autonomous work.
  An unknown session id is a **no-op**, not an error.
- `$/cancel_request` (notification, `{"requestId": <id>}`) is the SDK's generic
  JSON-RPC request cancellation. A method handler that observes its signal
  aborted replies with `-32800`.

After `session/cancel`, the in-flight `session/prompt` still settles normally
with `stopReason: "cancelled"`. Cancellation is therefore reported as a
cancelled outcome, never as success, and never by dropping the pending request.

## 10. Process lifecycle and error semantics

Observed from `@deepseek-ai/dsh-acp-app/lib/index.js` and
`@deepseek-ai/dsh-acp/lib/index.js`:

- `dsh --profile acp` takes **no arguments**. `--help` parses, publishes no
  startup service, and starts no transport.
- stdout belongs exclusively to ACP. A launcher that captures stdout for
  logging will corrupt the stream.
- stdin EOF → bounded shutdown, then process exit. This is the supported
  shutdown signal.
- The ACP plugin quiesces sessions on connection close and logs teardown
  failures; a teardown failure must not be reported as a successful turn.
- The process may exit at any time. Every pending request must then fail with an
  explicit error; a client must never leave a promise hanging or infer success
  from silence.

Error semantics the transport implements:

| Condition | Reported as |
|---|---|
| malformed/oversize inbound frame | explicit protocol error, connection marked broken |
| peer JSON-RPC error response | `AcpRemoteError` carrying `code`, `message`, `data` |
| process exit with a request in flight | explicit `closed` error naming the exit code |
| request timeout | explicit `timeout` error, `$/cancel_request` sent first |
| local cancel requested | `$/cancel_request` sent, settles as cancelled |
| unknown inbound request method | JSON-RPC `-32601` response |
| handler raised | JSON-RPC `-32603` response, detail bounded and redacted |
| write failure | explicit `write_failed`; connection becomes unusable |

## 11. Decision: dependency versus owned transport

**Decision: implement a small owned Python transport. Add no ACP dependency.**

Reasons, in order of weight:

1. There is no maintained Python ACP SDK. The reference implementations are the
   TypeScript SDK (`@agentclientprotocol/sdk`) and Rust. A resolution check
   against the configured package index on this machine found no
   `agent-client-protocol` distribution at all, and the bare name `acp` resolves
   to an empty `0.0.0` stub with no code. Introducing one would add an unvetted
   dependency to a core module for a protocol that is, at the wire level, NDJSON
   plus JSON-RPC 2.0.
2. The wire format is small and stable. `ndJsonStream` is ~90 lines of
   TypeScript; the Python equivalent is a bounded line reader and a locked
   writer. This is well inside the "small owned transport" the design document
   already anticipated.
3. DSH uses only a subset: no client `fs/*`, no terminal, no elicitation, no
   session load/delete/fork. A general SDK would carry surface we must then
   prove is unreachable, whereas an owned transport exposes only what is
   verified.
4. Repo policy forbids adding toolchain and runtime dependencies without a clear
   justification, and M6A is explicitly a spike. The dependency question is
   answered by evidence, and the evidence says the dependency is not needed.
5. An owned transport makes the deadlock-sensitive behaviour — answering an
   inbound `session/request_permission` while `session/prompt` is blocked —
   explicit and testable with fake pipes, with no third-party scheduler in the
   way.

Revisit if any of these change: DSH starts using client filesystem/terminal
capabilities, the client must support more than one transport (HTTP/WebSocket),
or a maintained Python SDK reaches parity.

## 11a. Live handshake evidence

`scripts/probe_acp_handshake.py` performs an opt-in, model-free handshake against
the real installation. It forces `DSH_TELEMETRY_MODE=DISABLED`, sends only
`initialize`, and closes stdin to request shutdown. This is what DSH actually
returned on 2026-10-14:

```text
argv: ('/Users/chen/.local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js', '--profile', 'acp')
```

```json
{
  "protocolVersion": 1,
  "agentInfo": {"name": "deepseek-harness-acp", "version": "0.0.1"},
  "agentCapabilities": {
    "mcpCapabilities": {"http": true},
    "promptCapabilities": {"image": false, "audio": false, "embeddedContext": false},
    "sessionCapabilities": {"close": {}, "list": {}, "resume": {}}
  },
  "authMethods": []
}
```

This confirms, against the running binary rather than a type declaration:

- the single-line JSON framing works with a plain `write` plus newline, with no
  headers and no handshake preamble;
- protocol version `1` is negotiated as documented;
- `sessionCapabilities` advertises exactly `close`, `list`, and `resume`, and
  `session/load` is absent;
- `promptCapabilities.image` is `false` for the shipped `acp` profile, matching
  the "only when an exact image-capable route is configured" rule;
- `agentInfo.version` is the hard-coded `0.0.1`, not the DSH version.

Two operational facts worth carrying into M6B:

- the `dsh` on `PATH` is a launcher whose real entry point is
  `.../@deepseek-ai/dsh/lib/bin.js`, so `DSH_BINARY` must stay an absolute path
  resolved by the backend, never a bare `dsh`;
- the process writes proxy and telemetry notices to **stderr**, which is why
  stdout must remain protocol-only and stderr must keep being drained and
  bounded.

## 12. Stable interface delivered to M6B

`auditrail/dsh_acp.py` (core package; imports no `backend`, FastAPI, or SSE):

```text
Protocol constants
  ACP_PROTOCOL_VERSION, ACP_CLIENT_NAME
  AgentMethod, ClientMethod, AcpStopReason, ClientRequestOutcome

Framing
  encode_frame(message) -> bytes
  AcpFrameReader(stream, max_bytes=).read() -> AcpFrameRead   # stateful, bounded
  read_frame_bytes(stream, max_bytes=) -> AcpFrameRead        # convenience
  decode_frame(data) -> AcpMessage                     # raises on malformed JSON
  AcpMessage.is_request / is_notification / is_response / is_error
  AcpPeerError                                         # JSON-RPC error payload

Process boundary (injectable; fakes need no subprocess)
  AcpProcess  (stdin/stdout/stderr/poll/wait/close_stdin/terminate)
  spawn_acp_process(argv, cwd=, env=, extra_env=) -> AcpProcess
  build_acp_argv(config, profile) -> ("<binary>", "--profile", profile)
  AcpProcessFactory (callable: argv, cwd -> AcpProcess)

Transport
  AcpTransport(process, max_frame_bytes=, write_timeout_seconds=)
    .start() / .close() / .terminate() / .is_closed / .close_error
    .send_request(method, params, *, timeout_seconds, deadline, cancellable, cancel_event) -> result
    .send_notification(method, params)
    .send_response / .send_error_response
    .handle_request(method, handler)                   # inbound agent -> client
    .take_notification(timeout=) / .drain_inbox() / .requeue(message)
    .stderr_diagnostics / .framing_errors

Client
  AcpClient(transport, config, profile=)
    .initialize(timeout_seconds=)  -> AcpInitializeResult
    .new_session(workspace)        -> AcpSession
    .resume_session(session_id, workspace) -> AcpSession
    .list_sessions(cwd=, cursor=)  -> AcpSessionPage
    .set_config_option(session_id, config_id, value) -> tuple[option, ...]
    .close_session(session_id)
    .prompt(session_id, text, *, timeout_seconds, cancel_event) -> AcpPromptOutcome
    .cancel(session_id)
    .take_notifications(timeout=) -> tuple[DshRuntimeEvent, ...]
    .pending_permissions() / .respond_permission(id, option_id) / .cancel_permission(id)
    .session(session_id) / .initialize_result

Events and outcomes
  normalize_session_update(params, max_text_bytes=) -> DshRuntimeEvent | None
  AcpRuntimeEvent  -> normalized DshRuntimeEvent (same shape as Headless)
  AcpPromptOutcome (session_id, stop_reason, cancelled, usage, updates, text, truncated)
  AcpSessionPage / AcpSessionSummary / AcpPermissionRequest / AcpInitializeResult
```

Errors are always explicit: `AcpError` carries a stable `code` (`malformed_frame`,
`frame_too_large`, `timeout`, `cancelled`, `closed`, `write_failed`,
`malformed_result`, `unsupported_protocol_version`, `unsupported_capability`,
`not_initialized`, `invalid_session_id`, `unknown_session`, `prompt_in_flight`,
`unknown_permission_request`) and `AcpRemoteError` adds the peer's numeric
`code_number`, message, and data.

Normalized event mapping shared with Headless (`DshRuntimeEvent`):

| ACP source | normalized `type` |
|---|---|
| `agent_message_chunk` text | `text` |
| `agent_thought_chunk` text | `thinking` |
| `tool_call` | `tool_call` |
| `tool_call_update` | `tool_result` |
| `usage_update` | `status` with `phase="usage"` |
| `config_option_update` | `status` with `phase="config"` |
| unknown variant | `unknown` with `source_type` |
| `session/prompt` response | `final` (appended to the outcome's ordered events) |

Permission requests are deliberately **not** an event in that stream. They are
inbound JSON-RPC *requests* that need a correlated response, so they are surfaced
through `AcpClient.pending_permissions()` and answered with
`respond_permission` / `cancel_permission`. M6B turns one into the additive SSE
`permission_requested` event, which the design document already lists as an
ACP-phase event. Headless never produces one.

Out of scope for M6A, left to M6B: the in-memory operation registry, SSE
projection, durable import, multi-session scheduling, and any HTTP surface.
