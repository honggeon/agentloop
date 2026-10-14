# DSH Integration Hardening and Release Readiness (M7)

- Date: 2026-10-14
- Baseline: `f5a001b` on `feat/agent-judge-reliability-dimensions`
- Scope: audit and harden the existing DSH Headless / ACP / Judge integration, plus
  isolation, verification and operations. No new protocol capability was added.
- Status: **ready to release** as an internal, opt-in feature; see §7 for the exact
  residual risks and the non-goals that were deliberately not implemented.

## 1. What was verified before and after

| Check | Baseline (`f5a001b`) | With M7 |
|---|---|---|
| Full deterministic pytest | 467 passed | **497 passed, 1 skipped** |
| Suite wall-clock (same machine) | ~456 s | **~8 s** |
| `cd frontend && npm run build` | pass | pass (`vue-tsc --noEmit` + `vite build`) |
| `python -m compileall auditrail backend` | pass | pass |
| `git diff --check` | clean | clean |
| Clean checkout (pristine worktree, no `.env`/`runs/`/`node_modules`) | DSH subset passes | **full suite passes** |
| Opt-in real-DSH smoke tests | did not exist | **6 passed** against dsh 0.2.0-rc.2 |

Nothing was pushed and no tagged release was cut.

## 2. How the audit was performed

Two read-only, evidence-first audits were run in parallel and independently
reviewed: one over the ACP transport (`auditrail/dsh_acp.py`), one over the test
corpus and the frontend contract. Both were required to cite `file:line`, separate
confirmed from suspected, and name what was already correct so it would not be
"fixed". Every high/medium finding below was then reproduced directly before a fix
was written.

The trust boundary was re-derived from the code rather than from documentation:
which values are server-owned, which are caller-selectable, and what a hostile or
buggy DSH peer can influence.

## 3. High and medium findings, and their fixes

All findings below are fixed and covered by tests unless marked otherwise.

### HIGH

| # | Finding | Fix |
|---|---|---|
| H1 | A user-cancelled ACP turn settled as a successful `end_turn`, so `runtime_manager` imported an **aborted turn as a durable, evaluated run**. Cancellation was derived only from the peer's `stopReason`, which folds several outcomes into `end_turn`. | `cancelled` now also honours the caller's cancel signal; a cancelled turn reports `stop_reason=cancelled`; the peer's own reason remains in `raw`. |
| H2 | JSON-RPC **id type confusion**: `True == 1` and `1.0 == 1` in Python, so a peer frame `{"id": true}` settled request 1 (the `initialize` handshake) with peer-chosen content. | `decode_frame` validates ids and rejects bool/float ids as a framing violation, so they never enter the pending-request table. |
| H3 | `_responded` grew without bound, and a **reused inbound id was answered with silence**, hanging the peer (including a permission request) forever. | The record is bounded and evicts oldest; a reused id now gets an explicit JSON-RPC error instead of being dropped. |
| H4 | `_permissions` grew without bound, accepted requests with **no `sessionId`** (unanswerable by any owner), and retained entries after the turn ended, so a later HTTP POST could answer a finished turn. | Empty-session requests are refused; the table is capped; a settled turn forgets its own permission requests; the client exposes `forget_permissions_for_session`. |
| H5 | `AcpTransport._write_timeout` was validated and stored but **never used**. A peer that stopped reading stdin blocked the writer forever while holding the single write lock, stalling every other writer. | Writes are bounded with `select` + non-blocking partial `os.write`, the fd flags are restored, and a partial frame invalidates the transport rather than being appended to. |

### MEDIUM

| # | Finding | Fix |
|---|---|---|
| M1 | A **cancelled** turn reported by the peer (`-32800`) became a generic `remote_error`, so it was recorded as failed rather than cancelled. | `-32800` maps to the cancelled outcome. |
| M2 | A response frame that also carried `method` was classified as a request, so the caller timed out **and** the transport answered the peer using our own request id. | Correlation requires a method-free frame. |
| M3 | `AcpRemoteError` carried unbounded, unredacted peer text and attached the raw `error.data`, contradicting its own docstring and amplifying up to a 4 MiB frame into an HTTP/SSE body. | Message is capped and redacted; `data` is redacted. |
| M4 | A frame-too-large, write timeout or reader failure closed the transport while the process kept running, and the manager reused that **poisoned client** because it only polled process liveness. | `is_alive()` includes the closed flag, and the manager restarts an unusable transport. |
| M5 | The permission option gate used **only peer-advertised** options, so an advertised `allow_always` was accepted. | The transport also enforces its own allowlist (`allow-once` / `reject-once`) — no allow-always, no automatic approval anywhere. |
| M6 | `encode_frame` sat outside every `try`, so a non-serializable caller value raised a bare `TypeError` out of `prompt`/`respond_permission`, bypassing the route's error mapping. | Serialization failures become a typed `AcpError`. |
| M7 | `AcpProcess.terminate()` used `os.getpgid(0)` when a wrapped object lacked `pid`, which signals **the backend's own process group**; the fallback could raise `AttributeError` out of `close()`. | `pid <= 0` falls back to closing stdin only; the direct-signal fallback swallows partial-handle errors. |
| M8 | An unhashable caller-supplied permission id raised `TypeError`. | Typed `DshValidationError`. |
| M9 | A write after close either misreported a deliberate local close as a wire failure or silently succeeded on a closed transport. | `_write` checks `_closed` and raises the close error. |
| M10 | An unbounded SSE operation queue let a stalled browser tab grow backend memory without limit. | The queue is bounded with a reserved headroom for terminal events; progress events are shed and counted, and `completed.dropped_events` reports it to the UI. |
| M11 | A malformed peer payload killed the backend's live-projection monitor thread silently, so SSE stopped with no error. | The monitor loop is exception-isolated. |

### Test-environment and hygiene findings

| # | Finding | Fix |
|---|---|---|
| T1 | `backend/app/config.py` loaded the project `.env` at import time with no way to disable it, so a developer's `.env` could enable the DSH runtime, repoint `AUDITRAIL_RUNS_DIR`, or load real credentials into the test process. | `AUDITRAIL_LOAD_DOTENV` gates loading; `tests/conftest.py` forces it off before collection. |
| T2 | `auditrail/llm_proxy.py` parsed the project `.env` directly, so importing a session with no LLM calls made the suite attempt a real `scp` to the host named in the local `.env`. | The same guard applies; a conftest `no_network_egress` fixture fails any test that shells out to `scp`/`ssh`/`rsync`/`curl`/`wget`. |
| T3 | Real `~/.dsh` was the only supported target for the new integration tests, mutating operator state and failing under a read-only sandbox (`EPERM`). | The integration tests derive an isolated DSH home under the pytest temp dir, copying small profile config and symlinking `node_modules`. |

### Deliberately not changed

- **Oversize-frame-is-terminal** (`frame_too_large` closes the long-lived
  transport): fail-closed by design and already asserted by a test. Documented, not
  weakened.
- **`_validate_remote_session_id` omitting the leading-`-` check**: remote ids
  never reach argv or a path, so this is a consistency nit, not a hole.
- **Single long-lived ACP process, one prompt per session, no cross-worker
  sharing**: an explicit non-goal.

## 4. Test-environment and `.env` isolation evidence

The isolation fix is the single largest behavioural change, so it is quantified
rather than asserted. Measured in a pristine `git worktree` at `f5a001b` with the
developer's real `.env` copied in:

- Baseline, `tests/test_backend_dsh.py` alone: **30.34 s for 6 tests.**
- With M7, same file and same `.env`: **0.81 s for 6 tests.**
- Direct instrumentation of the baseline showed exactly one outbound attempt:

  ```text
  subprocess attempts: [(['scp', '-q',
     'newuser@192.168.0.64:/home/newuser/agentloop/runs/proxy-llm-calls.jsonl',
     '/var/folders/.../auditrail-proxy-*.jsonl'], 30)]
  ```

  That is a real network copy with a 30 s timeout attempted by a unit test. With M7
  the configured source resolves to empty, and no subprocess is attempted at all.

The clean-checkout run (no `.env`, no `runs/`, no `.venv`, no `node_modules` in the
tree) passes the full suite, which is the actual release condition.

## 5. New and changed tests

30 tests were added; none were removed and no production contract was relaxed.

- `tests/test_dsh_acp.py`: id confusion, method-bearing response frames, reused
  inbound ids, cancelled-turn semantics (local and peer-reported), `allow_always`
  refusal, session-less permission refusal, permission eviction, unhashable ids,
  write-after-close, non-serializable frames, peer-error bounding/redaction,
  post-close liveness, bounded inbox, stalled-peer write timeout, fd-flag restore.
- `tests/test_backend_dsh_acp.py`: queue backpressure and reserved terminal
  headroom, `dropped_events` reporting, shutdown bounding a stuck worker, route
  shutdown idempotence, closed-manager reporting, `max_concurrency` validation.
- `tests/test_dsh_headless.py`: live-process cancellation terminates the process
  group; a pre-set cancel never spawns.
- `tests/test_backend_config.py` / `tests/test_llm_proxy.py`: the dotenv guard
  disables the fallback, and the documented behaviour still works when lifted.
- `tests/conftest.py` (new): forces the dotenv guard off and fails network egress.
- `tests/test_dsh_integration.py` (new): 6 opt-in real-binary smoke tests.

## 6. Opt-in integration smoke tests

Guarded by `AUDITRAIL_DSH_INTEGRATION=1` and skipped entirely otherwise, so the
default suite stays deterministic, offline and credential-free. Verified against
the real local install (`dsh 0.2.0-rc.2`, Node v22.23.0, Python 3.14.5):

```text
6 passed in 14.08s
```

Coverage: Headless NDJSON contract, durable recording import and round-trip,
timeout reported rather than hung, ACP handshake contract, ACP session+prompt
round-trip, ACP cancellation settling as cancelled. Each run uses an isolated DSH
home; the operator's `~/.dsh` mtime and `sessions/` listing were confirmed
unchanged afterwards.

## 7. Residual risks and non-goals

Residual risks, accepted for this release:

1. **`DSH_ACP_ENABLED` defaults to `true`.** ACP still cannot become reachable
   without `DSH_RUNTIME_ENABLED=true` and a full binary/profile/workspace
   allowlist, so there is no accidental exposure — but the default is permissive
   and relies on the master switch.
2. **Real-model behaviour is only sampled.** The integration tests prove the
   adapters drive a real process; they are not a model-quality or quota test.
3. **`AcpClient.cancel()` alone cannot settle a peer that ignores
   `session/cancel`.** The effective interrupt is the caller's cancel signal
   (`$/cancel_request` plus process-group termination), which the backend always
   sets; a peer that ignores both still runs to its deadline.
4. **One ACP process per worker.** Multiple workers do not share a process,
   session, or lock — deliberately out of scope.
5. **A frame over 4 MiB is terminal** for the long-lived process. This is
   fail-closed and documented in `docs/dsh-operations.md`.
6. **`python3` on this machine is 3.9.6** while the project requires `>=3.11`; the
   verified interpreter is the project virtualenv (3.14.5). Use `uv run pytest` or
   the venv interpreter.

Not implemented, as instructed: new protocol capability, allow-always, automatic
permission approval, multi-worker shared ACP process/session, distributed locks,
new judge schema, judge voting or automatic winner, Evidence MCP, ACP SDK
replacement, new UI framework, automatic deployment, and `push`.

Deterministic audit remains authoritative. Judge comparison still does not pick a
winner, and `unknown` still ranks below a real judgment.

## 8. Documentation

- `docs/dsh-operations.md` (new): trust boundary, enablement matrix, startup/
  health/shutdown order, `.env` isolation, the opt-in smoke tests, a
  symptom-to-cause troubleshooting table, the capacity/backpressure bounds, and a
  pre-release checklist.
- `README.md`: cancellation semantics, a pointer to the ops manual, the dotenv
  guard, and the integration-test command.
- `.env.example`: `AUDITRAIL_LOAD_DOTENV` and the integration-test variables.

## 9. Release decision

The deterministic suite, frontend build, compile check and whitespace check all
pass in the working tree and in a pristine clean checkout of the change set. The
high-severity defects that could have produced an incorrectly successful evaluated
run (H1), let a peer spoof protocol responses (H2), deadlock the peer (H3), grow
state without bound (H4, M10), or hang the writer (H5) are fixed and covered by
regression tests. Test isolation no longer depends on the absence of a local
`.env`.

**Recommendation: release the DSH integration as an opt-in, single-worker feature,
keeping `DSH_RUNTIME_ENABLED=false` by default.**
