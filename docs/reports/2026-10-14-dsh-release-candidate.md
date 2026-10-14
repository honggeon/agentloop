# DSH ACP Secure-Default Patch — Release Candidate Acceptance Report

- Date: 2026-10-14
- Baseline: `29a2153` on `feat/agent-judge-reliability-dimensions` (M0–M7 complete)
- Change set: one local commit on top of `29a2153`; no push, no tag, no release
- Scope: a single security-default change plus its tests, templates and docs
- Status: **release candidate accepted** for the opt-in, single-worker DSH integration

## 1. What changed, and why

The M7 readiness report listed exactly one permissive default as an accepted
residual risk (`docs/reports/2026-10-14-dsh-release-readiness.md` §7.1):
`DSH_ACP_ENABLED` defaulted to `true`. ACP could not become *reachable* without
the master switch and a full allowlist, so there was no accidental exposure — but
the safe default should not depend on a second switch being left alone.

This patch makes ACP opt-in:

| File | Change |
|---|---|
| `backend/app/config.py:64` | `Settings.dsh_acp_enabled` default `True` → `False`, with a comment stating the two-switch rule |
| `backend/app/config.py:94` | `_env_bool("DSH_ACP_ENABLED", False)` |

No routing logic changed. The existing gates already AND the switches together and
were left untouched:

- `backend/app/routes/dsh_acp.py:48` — `get_acp_runtime_manager()` raises 503 unless both switches are on.
- `backend/app/routes/dsh_acp.py:81` — `GET /api/dsh/acp/options` reports `enabled: false`.
- `backend/app/routes/dsh.py:73` — `dsh.acp.enabled` is `enabled and settings.dsh_acp_enabled`.

This is a **default value only**: nothing in the enablement model, the ACP
protocol, the permission policy, or the run-artifact contract was altered.

## 2. Resulting enablement model

Three capabilities share one master switch; ACP adds one opt-in on top of it.

| Capability | `DSH_RUNTIME_ENABLED` | Dedicated switch | Other required |
|---|---|---|---|
| Headless single-turn | `true` | none | `DSH_BINARY`, `DSH_ALLOWED_PROFILES`, `DSH_ALLOWED_WORKSPACE_ROOTS` |
| ACP interactive runtime | `true` | `DSH_ACP_ENABLED=true` (**default `false`**) | same allowlist (profile fixed to `acp`) |
| DSH judge | `true` | none | all of the above + `DSH_JUDGE_PROFILE` + `DSH_JUDGE_WORKSPACE` |

Truth table, verified against the code above:

| `DSH_RUNTIME_ENABLED` | `DSH_ACP_ENABLED` | Headless | ACP |
|---|---|---|---|
| unset / `false` | any | off | off |
| `true` | unset / `false` | available | **off** (no implicit enable) |
| `true` | `true` | available | available (if the allowlist validates) |
| `false` | `true` | off | off (the ACP switch alone cannot open it) |

Backward compatibility: operators who already set `DSH_ACP_ENABLED=true` keep
exactly the behaviour they had. Only the implicit default flipped, so an existing
deployment that relied on the default must now opt in explicitly — the intended
effect of a secure default. Enablement is still discoverable at runtime through
`GET /api/dsh/options` → `dsh.acp.enabled`.

## 3. Requirement 4: the master switch alone must not enable ACP

Verified empirically against the **real** settings loader and the **real** FastAPI
app (no monkeypatched `get_settings`), with a valid Headless allowlist, an absolute
`DSH_BINARY`, permitted profiles `headless,acp`, and `DSH_ACP_ENABLED` unset. The
ACP process factory was replaced with a tripwire that fails the check if called.

```text
settings.dsh_runtime_enabled = True
settings.dsh_acp_enabled     = False
GET /api/dsh/options -> dsh.enabled        = True
GET /api/dsh/options -> dsh.acp.enabled    = False
GET /api/dsh/acp/options -> 200 {'enabled': False, 'disabled_reason': 'ACP runtime is disabled', 'capabilities': {}}
GET /api/dsh/acp/sessions -> 503
POST /api/dsh/acp/session/new -> 503

OK: master switch alone leaves ACP disabled and spawns no process
```

Headless discovery still works (`dsh.enabled = true`), the ACP manifest honestly
reports `false` so the workbench can render a disabled state, every ACP route
returns 503, and **no ACP process was constructed or spawned**.

The mirror-image check confirms the opt-in path still works:

```text
settings.dsh_acp_enabled     = True
GET /api/dsh/options -> dsh.acp.enabled    = True

OK: explicit DSH_ACP_ENABLED=true opts ACP in (backward compatible)
```

## 4. Regression guards

Three tests were added (497 → 500 collected) and two existing ones updated so the
ACP suite opts in explicitly rather than passing by accident of the default.

| Test | Guards |
|---|---|
| `tests/test_backend_config.py:49` | `Settings()` defaults: runtime off, **ACP off** |
| `tests/test_backend_config.py:91` | unset → off, `true` → on, `false` → off, invalid → `ValueError` |
| `tests/test_backend_config.py:123` | master on + allowlist valid → runtime on, ACP off, Headless policy builds; explicit `true` still enables |
| `tests/test_backend_routes.py:620` | `GET /api/dsh/options`: `dsh.enabled` true while `dsh.acp.enabled` false, manifest otherwise intact |
| `tests/test_backend_dsh_acp.py:861` | master on + ACP off: options disabled, sessions/new/operation/cancel all 503 |
| `tests/test_backend_dsh_acp.py` `_settings()` | ACP tests now set `dsh_acp_enabled=True` explicitly |

**Mutation check.** With the old default temporarily restored, the new guards fail
as intended, so they are genuine regression tests rather than tautologies:

```text
FAILED tests/test_backend_config.py::test_dsh_runtime_defaults_do_not_enable_local_execution
FAILED tests/test_backend_config.py::test_get_settings_reads_the_acp_runtime_switch
FAILED tests/test_backend_config.py::test_master_switch_alone_does_not_enable_acp
FAILED tests/test_backend_routes.py::test_dsh_options_master_switch_alone_does_not_enable_acp
4 failed, 73 passed
```

The default was restored immediately afterwards.

## 5. Release acceptance results

Working tree, project interpreter (Python 3.14.5 in `.venv`):

| Check | Result |
|---|---|
| Full deterministic pytest | **500 passed, 1 skipped** (8.89 s) |
| `cd frontend && npm run build` | pass (`vue-tsc --noEmit` + `vite build`, 594 modules, 2.23 s) |
| `python3 -m compileall -q auditrail backend` | pass |
| `git diff --check` | clean |
| Real-DSH opt-in smoke tests | 6 passed at the `29a2153` baseline (unchanged by this patch) |

Pristine checkout of exactly this change set: a detached `git worktree` at
`29a2153` with the patch applied and the frontend dependencies symlinked (no
`.env`, no `runs/`, no build output in-tree):

| Check | Result |
|---|---|
| Full deterministic pytest | **500 passed, 1 skipped** (9.08 s) |
| `cd frontend && npm run build` | pass |
| `python3 -m compileall -q auditrail backend` | pass |
| `git diff --check` | clean |

The editable `auditrail` install exposes a `MetaPathFinder` appended *after* the
standard `PathFinder`, and the run was confirmed to import `auditrail` and
`backend.app.config` from the pristine worktree, not from the developer checkout.

## 6. Documentation updated

- `.env.example` — the ACP block now states the default is off, that both switches
  are required, and that the master switch never implicitly enables ACP.
- `README.md` (Local DSH ACP runtime) — both switches required, either alone leaves
  ACP off, and `/api/dsh/options` reports `dsh.acp.enabled`.
- `docs/dsh-operations.md` — enablement matrix (`默认关`), the two-switch rule, and
  the 503 troubleshooting row now points at the default-off cause.
- `docs/reports/2026-10-14-dsh-release-readiness.md` — residual risk §7.1 annotated
  as closed by this patch, with a pointer here.
- This report.

The frontend needed no change: `LiveEvalView.vue` already derives
`acpEnabled` from `dshEnabled && acpManifest.enabled`, so the ACP provider now
renders as disabled by default without a code edit.

## 7. Confirmed out of scope

Nothing in the following was implemented, matching the task boundary: new
features; ACP protocol changes; cancel refactor; per-session ACP process;
multi-worker ACP; automatic permission approval; allow-always; new judge features;
`push`; tag; release. The trust boundary is unchanged — the binary path, profile,
workspace, timeout and concurrency stay server-owned, and the permission policy
remains `allow-once` / `reject-once` with `allow_always: false`.

Git hygiene: the only unrelated entries in the working tree remain the two
pre-existing untracked items `hello.txt` and `my-testing/`, which were neither
modified, deleted, nor committed.

## 8. Residual risks

1. **Operators relying on the old implicit default must opt in.** After upgrading,
   ACP is off until `DSH_ACP_ENABLED=true` is set. This is the intended secure
   default and is documented in `.env.example`, `README.md` and
   `docs/dsh-operations.md`; the 503 message and `dsh.acp.enabled` make it
   observable rather than silent.
2. Items 2–6 of the M7 readiness report (§7) still apply unchanged: sampled
   real-model behaviour, a peer that ignores `session/cancel`, one ACP process per
   worker, fail-closed 4 MiB frames, and the machine's `python3` being 3.9.6 while
   the verified interpreter is the project `.venv` (3.14.5).

## 9. Release decision

The default-value change is covered by five focused tests that fail without it,
verified end-to-end against the real app in both directions, and confirmed in a
pristine checkout. Full pytest, the frontend build, `compileall` and
`git diff --check` all pass in the working tree and in the pristine tree, and the
two unrelated untracked items are untouched.

**Recommendation: accept this change set as the release candidate.** Ship the DSH
integration as an opt-in, single-worker feature with `DSH_RUNTIME_ENABLED=false`
and `DSH_ACP_ENABLED=false` by default. This work is a local commit only — no push
and no tag were performed, so tagging and release remain a separate, explicit step.
