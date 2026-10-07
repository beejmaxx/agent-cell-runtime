# R1 specification: local execution lifecycle

**Status:** approved for implementation. It implements the R1 scope in [invariants.md](invariants.md): LC-1, LC-2, LC-3, LC-4, LC-5, LC-7, LC-8 (R1 part), LC-9 (R1 part), LC-10, ID-5, ID-6, and ID-11. Design rationale is in [execution-lifecycle.md](execution-lifecycle.md) and [identity-flow.md](identity-flow.md). Where this spec and those documents disagree, ask.

**Not in R1:** Kubernetes, the gateway, agent operations against Mock Workday, isolation claims, AWS, leader election, row-level security, event sourcing, and generic fault or plugin frameworks.

## 0. Stack and layout

| Item | Choice |
|---|---|
| Language | Python 3.12 with `uv`, project at the repository root |
| Web | FastAPI, synchronous handlers |
| Database | PostgreSQL via SQLAlchemy 2 Core and psycopg 3; plain `schema.sql` |
| Tests | pytest with a throwaway Postgres cluster per session (same pattern as Mock Workday: `initdb` plus `pg_ctl`, no Docker) |
| HTTP client to Mock Workday | `httpx` |

```text
src/agent_runtime/
  app.py           FastAPI app and a background reconcile loop (disabled in tests)
  config.py        settings from environment
  clock.py         controllable clock (moves only via set/advance in tests)
  db.py            engine, transactions, advisory locks
  schema.sql
  seed.py          tenants and agent definitions
  auth.py          verify Mock Workday-issued user tokens via JWKS
  mockworkday.py   minimal client: JWKS, list the caller's delegation grants
  executions.py    state transitions (conditional SQL)
  api.py           HTTP routes
  reconciler.py    reconcile_once(now)
  backend.py       WorkloadBackend contract plus FakeBackend
  failpoints.py    named, test-armed crash points
tests/
```

`make test` runs everything without external services. `make test-integration` runs the tests marked `integration` against a live local Mock Workday (§9).

## 1. Tenancy and caller identity

- **Tenants:** the runtime has its own `tenants` table, seeded with `acme` and `globex`. Each row has the Mock Workday issuer (`https://<slug>.mockworkday.local`), the Mock Workday base URL, and the `Host` value used to reach it.
- **Authentication [lab simplification]:** callers present a Mock Workday human access token (`typ=human`).
  - The runtime verifies it against Mock Workday's JWKS (cached by `kid`, re-fetched on an unknown `kid`), using an RS256 allowlist, expiry by the runtime clock, and `iss` matched to a known tenant.
  - The token's audience is Mock Workday's API, not the runtime. A real system would issue a runtime-audience token through single sign-on; this is documented in [identity-flow.md](identity-flow.md).
  - Delegated and integration tokens are rejected.
- **Principal:** `(tenant_id, account_id = token sub)`.
- **Scoping (ID-11):** every execution route filters by tenant and principal. Another principal's or another tenant's execution returns **404**, the same as a nonexistent one.

## 2. Agent definitions and operations

`agents(tenant_id, name, client_id, operations text[])` is seeded per tenant. `hr-assistant` uses Mock Workday client `hr-assistant` with operations:

- `read_worker`
- `read_compensation`
- `request_time_off`
- `approve_time_off`
- `read_document`

The operation catalog, with the Mock Workday scope each needs:

| Operation | Scope |
|---|---|
| `read_worker` | `staffing` |
| `read_compensation` | `compensation` |
| `request_time_off`, `approve_time_off` | `absence` |
| `read_document` | `documents` |

R1 only stores and validates the operations; R3 and R4 enforce them.

## 3. Data model

```text
tenants(id uuid pk, slug unique, mw_issuer, mw_base_url, mw_host)
agents(tenant_id, name, client_id, operations text[], pk(tenant_id, name))
executions(
  id uuid pk, tenant_id, principal_account_id, agent, grant_id,
  operations text[], input jsonb,
  status text, failure_reason text null,
  workload_name text,                -- 'exec-' || id
  workload_uid text null,
  launch_attempted_at timestamptz null,
  deadline_at timestamptz,
  credential_hash text null,         -- per-execution completion credential (§6)
  result jsonb null, result_hash text null,
  created_at, finished_at null)
idempotency_records(
  tenant_id, principal_account_id, idem_key, request_hash, execution_id, created_at,
  pk(tenant_id, principal_account_id, idem_key))
```

- **Status values:** `PENDING`, `PROVISIONING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT`.
- **Failure reasons:**
  - `LAUNCH_FAILED`: the backend definitively rejected the create.
  - `LAUNCH_UNKNOWN`
  - `POD_LOST` (the name is kept for continuity with R2)
  - `EXITED_WITHOUT_COMPLETION`
- **Explicit tenant predicates** appear in every query. Row-level security is not used in R1; it was learned in Mock Workday.

## 4. Execution API (`/api/v1`)

| Route | Behavior |
|---|---|
| `POST /executions` | Creates an execution (§5). `Idempotency-Key` required. Returns 201 with the execution. |
| `GET /executions/{id}` | The owner's view, including `result` |
| `POST /executions/{id}/cancel` | Conditional transition from any non-terminal state to CANCELLED. Already CANCELLED: 200 with the same execution. Other terminal state: 409 `INVALID_STATE`. |
| `POST /executions/{id}/complete` | Called by the workload, not the user (§6) |

- **Execution shape:** `{id, agent, status, failure_reason, operations, input, deadline_at, created_at, finished_at, result}`.
- **Errors** use Mock Workday's format: `{"error": {"code", "message", "request_id"}}`.

## 5. Creating an execution

**Request:**

```json
{"agent": "hr-assistant", "grant_id": "...", "operations": [...],
 "input": {...}, "timeout_seconds": 600}
```

- `input` is a JSON object of at most 16 KiB.
- `timeout_seconds` ranges from 30 to 3600.

**Processing order (one transaction):**

1. Authenticate the caller (§1).
2. **Idempotency (LC-2):**
   - Take an advisory lock on `(tenant, principal, key)`.
   - If an unexpired record exists with the same request hash, return the stored execution, status 201, with `Idempotent-Replay: true`.
   - A different hash returns 422 `IDEMPOTENCY_KEY_REUSED`.
   - Records are kept for 24 hours.
3. **Validate the agent and operations:** the agent exists in this tenant, and each requested operation is non-empty and within the agent's operations (422).
4. **Grant checks (ID-5, ID-6)**, using the caller's own token against their tenant's Mock Workday: `GET /api/v1/delegation-grants`, which returns only the caller's grants.
   - Grant not in the list → 403 `GRANT_NOT_OWNED`. This covers grants that are nonexistent or belong to someone else.
   - Revoked, or `expires_at <= now` → 403 `GRANT_INACTIVE`.
   - `grant.client_id != agent.client_id` → 403 `GRANT_CLIENT_MISMATCH`.
   - Any requested operation's scope not in `grant.scopes` → 403 `OPERATION_NOT_GRANTED`.
   - `now + timeout_seconds > grant.expires_at` → 422 `DEADLINE_EXCEEDS_GRANT`.
   - **Fails closed:** Mock Workday unreachable or malformed returns 503 `GRANT_CHECK_UNAVAILABLE`, and nothing is created.
5. Insert the execution (`PENDING`, `deadline_at = now + timeout`) and the idempotency record. Commit.

## 6. Completion (LC-9)

- **Launch credential:** when the reconciler launches a workload, it generates a random per-execution credential, stores its SHA-256 hash, and passes the plaintext to the backend as part of the workload spec. This stands in for R3's projected token.
- **Request:** `POST /executions/{id}/complete` with `Authorization: Execution <credential>` and body `{"result": <JSON object, at most 64 KiB>}`. A bad or missing credential returns 401.

**Transitions (conditional SQL):**

| State | Result |
|---|---|
| RUNNING | SUCCEEDED, storing the result and its canonical hash, in one transaction |
| SUCCEEDED, same result hash | 200 with the existing execution (replay) |
| SUCCEEDED, different hash | 409 `COMPLETION_CONFLICT` |
| Any other state | 409 `INVALID_STATE` |

The result is untrusted data, stored and returned as JSON, never interpreted.

## 7. Workload backend

**Contract** (the smallest set the reconciler needs; R2's Kubernetes backend implements the same):

```python
class WorkloadBackend(Protocol):
    def create(self, name: str, spec: WorkloadSpec) -> str: ...   # returns uid
    def get(self, name: str) -> Workload | None: ...              # None = definitely absent
    def delete(self, name: str) -> None: ...                       # idempotent
    def list_owned(self) -> list[Workload]: ...                    # runtime-labeled only
```

- `Workload` has `name`, `uid`, and `phase` (`RUNNING`, `SUCCEEDED`, `FAILED`, or `LOST`).
- **Errors:**
  - `BackendUnavailable`: cannot observe or act; never interpreted as absence.
  - `CreateRejected`: definite failure; the workload does not exist.
  - `CreateOutcomeUnknown`: timeout or lost response; it may exist.

**`FakeBackend`** is in-memory and deterministic. Test controls:

- `lose_next_create_response()`: the create happens but raises `CreateOutcomeUnknown`.
- `fail_next_create_without_creating()`: raises `CreateOutcomeUnknown` and creates nothing.
- `reject_next_create()`
- `set_unavailable(bool)`
- `exit(name, code)`: the workload ends without calling complete.
- `remove(name)`: the workload vanishes externally.
- `add_unowned(name)`: a workload without the runtime label.
- `create_calls`: the count of create invocations.

A fake workload does not execute code. Tests drive completion through the API using the credential from the spec the backend received.

## 8. Reconciler

`reconcile_once(now)` performs one pass. The app runs it in a loop every second; tests call it directly.

1. **PENDING:**
   - Claim it conditionally (`PENDING → PROVISIONING`, set `launch_attempted_at`, commit).
   - Failpoint **`after_claim`**.
   - Generate the credential and call `create(workload_name, spec)`:
     - success → failpoint **`after_create`** → record `workload_uid` and the credential hash, move to RUNNING;
     - `CreateRejected` → `FAILED (LAUNCH_FAILED)`;
     - `CreateOutcomeUnknown` or `BackendUnavailable` → stay PROVISIONING.
2. **PROVISIONING** (launch already attempted): `get(name)`.
   - Exists → adopt: record the uid and move to RUNNING. A credential that was never persisted means completion is impossible, so the execution will time out. That is acceptable, and the case is covered by the failpoint test.
   - `None` → `FAILED (LAUNCH_UNKNOWN)`. **Never call `create` again for this execution.**
   - Unavailable → no change.
3. **RUNNING:** `get(name)`.
   - `None` or `LOST` → `FAILED (POD_LOST)`.
   - Phase SUCCEEDED or FAILED without a completion → `FAILED (EXITED_WITHOUT_COMPLETION)`.
   - Unavailable → no change.
4. **Deadlines:** any non-terminal execution with `now >= deadline_at` → `TIMED_OUT`. This is database-only and applies even when the backend is unavailable (LC-7).
5. **Cleanup:** terminal execution whose workload exists → `delete`. Failpoint **`before_cleanup`** sits between the terminal transition and cleanup in the cancel and complete paths.
6. **Orphans (part of LC-11 in R1):** only if the database query for executions succeeded in this pass. For each `list_owned()` workload with no execution row → `delete`. Unowned workloads are never touched. No grace period.

- **Conditional transitions everywhere:** every transition is `UPDATE … WHERE id = :id AND status = :expected`. Zero rows updated means another transition won, and the reconciler moves on.
- **Failpoints:** `failpoints.arm("after_claim")` makes the next hit raise `SimulatedCrash`. The next `reconcile_once` must converge (LC-10).

## 9. Tests (named by invariant ID)

**Without external services:**

- Mock Workday is faked with a small test JWKS issuer and an in-memory grant list behind the same `mockworkday.py` contract.
- Tests use the controllable clock and `FakeBackend`.

| ID | What to test |
|---|---|
| LC-1 | 50 iterations racing complete, cancel, and deadline with threads |
| LC-2 | Sequential replay and a different body; 20 concurrent identical requests → exactly one execution; the same key value under another principal → a separate execution |
| LC-3 | Lost create response followed by `remove`, and `fail_next_create_without_creating` → `LAUNCH_UNKNOWN` with `create_calls == 1` |
| LC-4 | Failpoint `after_claim`: no workload exists before the claim commit, and no create happens before it |
| LC-5 | Lost create response while the workload exists → adopted, one workload, RUNNING |
| LC-7 | Backend unavailable: no POD_LOST, no relaunch, no orphan deletion; cancel and deadline still apply |
| LC-8 | An overdue execution in each non-terminal state becomes `TIMED_OUT` |
| LC-9 | Exit without completing; replay of the same result; a different result; oversized and non-object results; bad credential |
| LC-10 | Failpoints `after_claim`, `after_create`, and `before_cleanup` each converge |
| ID-5 | Another user's valid grant in the same tenant → 403; the owner with the same grant → 201 |
| ID-6 | Deadline beyond grant expiry → 422; an operation outside the grant's scopes → 403; a revoked or expired grant → 403; Mock Workday unreachable → 503 with nothing created |
| ID-11 | Real executions owned by Alice (Acme) and Dave (Globex): cross-principal and cross-tenant GET and cancel → 404; the owner → 200 |

**Integration** (`make test-integration`, against Mock Workday at `MW_BASE_URL`, default `http://127.0.0.1:18080`, started with `MW_TEST_ADMIN=1` from the mock-workday repository): ID-5 and ID-6 with real grants created through Mock Workday's API; creating executions; cancel; complete. Requests to Mock Workday must bypass HTTP proxies (`Host` selects the tenant).

## 10. Done when

- All tests above pass under `make test`, and the integration tests pass against local Mock Workday.
- The README documents how to run both.
- No code exists for Kubernetes, the gateway, or AWS.
