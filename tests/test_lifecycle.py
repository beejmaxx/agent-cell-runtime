from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from conftest import cancel, complete, create, get, reconcile, row, running
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from agent_runtime.executions import TERMINAL, transition
from agent_runtime.failpoints import SimulatedCrash


def test_HAPPY_end_to_end(env):
    execution_id = create(env).json()["id"]
    assert get(env, execution_id).json()["status"] == "PENDING"
    reconcile(env)
    assert get(env, execution_id).json()["status"] == "RUNNING"
    result = {"answer": [1, "synthetic", {"html": "<script>untrusted</script>"}]}
    response = complete(env, execution_id, result)
    assert response.status_code == 200
    assert response.json()["result"] == result
    assert get(env, execution_id).json()["result"] == result
    reconcile(env)
    assert env.backend.get(f"exec-{execution_id}") is None


def test_LC_2_idempotency_sequential_and_concurrent(env):
    first = create(env, key="same")
    replay = create(env, key="same")
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert replay.headers["Idempotent-Replay"] == "true"
    assert create(env, key="same", input={"different": 1}).status_code == 422
    barrier = Barrier(20)
    grant_calls = env.issuer.grant_calls
    env.issuer.grant_delay = 0.1

    def submit(_):
        barrier.wait()
        return create(env, key="concurrent")

    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(pool.map(submit, range(20)))
    env.issuer.grant_delay = 0
    assert env.issuer.grant_calls == grant_calls + 1
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 1
    assert sum("Idempotent-Replay" not in r.headers for r in responses) == 1
    bob_grant = env.issuer.grant("bob")
    bob = create(env, token=env.issuer.token("bob"), key="same", grant=bob_grant)
    assert bob.status_code == 201 and bob.json()["id"] != first.json()["id"]
    with env.db.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(env.db.executions)) == 3
    env.issuer.unavailable = True
    assert create(env, key="same").json() == first.json()


def test_LC_2_key_expires_at_24_hours(env):
    first = create(env, key="expires")
    env.clock.advance(24 * 3600)
    env.grant["expires_at"] = (env.clock.now() + timedelta(hours=1)).isoformat()
    response = create(env, key="expires")
    assert response.status_code == 201
    assert response.json()["id"] != first.json()["id"]


@pytest.mark.parametrize("mode", ["removed", "absent"])
def test_LC_3c_unknown_launch_never_retried_across_restart(env, mode):
    execution_id = create(env).json()["id"]
    if mode == "removed":
        env.backend.lose_next_create_response()
    else:
        env.backend.fail_next_create_without_creating()
    reconcile(env)
    assert row(env, execution_id)["status"] == "PROVISIONING"
    env.backend.remove(f"exec-{execution_id}")
    reconcile(env, restart=True)
    assert row(env, execution_id)["failure_reason"] == "LAUNCH_UNKNOWN"
    for _ in range(3):
        reconcile(env)
    assert env.backend.create_calls == 1


def test_LC_3d_late_visible_workload_is_cleaned_without_authority(env):
    execution_id = create(env).json()["id"]
    name = f"exec-{execution_id}"
    env.backend.fail_next_create_without_creating()
    reconcile(env)
    credential = env.backend.specs[name].credential
    reconcile(env, restart=True)
    assert row(env, execution_id)["failure_reason"] == "LAUNCH_UNKNOWN"
    env.backend.appear(name, {"owner": "agent-runtime", "execution_id": execution_id})
    assert complete(env, execution_id, credential=credential).status_code == 401
    reconcile(env)
    assert env.backend.get(name) is None
    assert env.backend.create_calls == 1
    assert complete(env, running(env)).status_code == 200


def test_LC_3a_LC_4_LC_10_after_claim_commit_and_restart(env):
    execution_id = create(env).json()["id"]
    env.failpoints.arm("after_claim")
    with pytest.raises(SimulatedCrash):
        reconcile(env)
    # A separate connection sees the durable claim while no create was invoked.
    assert row(env, execution_id)["status"] == "PROVISIONING"
    assert row(env, execution_id)["launch_attempted_at"] == env.clock.now()
    assert env.backend.create_calls == 0
    assert env.backend.list_owned() == []
    reconcile(env, restart=True)
    assert row(env, execution_id)["failure_reason"] == "LAUNCH_UNKNOWN"
    assert env.backend.create_calls == 0
    assert row(env, running(env))["status"] == "RUNNING"


def test_LC_4_create_observes_durable_claim(env, monkeypatch):
    execution_id = create(env).json()["id"]
    original = env.backend.create

    def check(name, spec):
        assert row(env, execution_id)["status"] == "PROVISIONING"
        assert row(env, execution_id)["launch_attempted_at"] is not None
        return original(name, spec)

    monkeypatch.setattr(env.backend, "create", check)
    reconcile(env)
    assert row(env, execution_id)["status"] == "RUNNING"


@pytest.mark.parametrize("crash", [False, True])
def test_LC_3b_LC_5_LC_10_adopt_then_timeout_without_credential(env, crash):
    execution_id = create(env).json()["id"]
    if crash:
        env.failpoints.arm("after_create")
        with pytest.raises(SimulatedCrash):
            reconcile(env)
    else:
        env.backend.lose_next_create_response()
        reconcile(env)
    reconcile(env, restart=True)
    record = row(env, execution_id)
    assert record["status"] == "RUNNING" and record["workload_uid"]
    assert record["credential_hash"] is None
    assert len(env.backend.list_owned()) == 1
    assert env.backend.create_calls == 1
    assert complete(env, execution_id).status_code == 401
    env.clock.set(record["deadline_at"])
    reconcile(env)
    assert row(env, execution_id)["status"] == "TIMED_OUT"
    assert env.backend.list_owned() == []


@pytest.mark.parametrize(
    "labels", [{}, {"owner": "someone-else"}, {"owner": "agent-runtime", "execution_id": "wrong"}]
)
def test_LC_5_labels_required_for_adoption_and_cleanup(env, labels):
    execution_id = create(env).json()["id"]
    env.backend.fail_next_create_without_creating()
    reconcile(env)
    env.backend.appear(f"exec-{execution_id}", labels)
    reconcile(env, restart=True)
    assert row(env, execution_id)["failure_reason"] == "LAUNCH_UNKNOWN"
    assert env.backend.get(f"exec-{execution_id}") is not None
    assert env.backend.delete_calls == 0
    assert complete(env, running(env)).status_code == 200


@pytest.mark.parametrize("action", ["cancel", "complete"])
def test_LC_10_before_cleanup_recovers(env, action):
    execution_id = running(env)
    env.failpoints.arm("before_cleanup")
    with pytest.raises(SimulatedCrash):
        (cancel if action == "cancel" else complete)(env, execution_id)
    assert row(env, execution_id)["status"] in TERMINAL
    assert env.backend.get(f"exec-{execution_id}") is not None
    reconcile(env, restart=True)
    assert env.backend.get(f"exec-{execution_id}") is None
    assert env.backend.create_calls == 1


def test_LC_7_unavailable_backend_preserves_observation_but_not_authority(env):
    active = running(env)
    cancelled = running(env)
    timed_out = running(env)
    orphan_id = str(uuid4())
    env.backend.appear(f"exec-{orphan_id}", {"owner": "agent-runtime", "execution_id": orphan_id})
    calls = env.backend.create_calls
    env.backend.set_unavailable(True)
    reconcile(env)
    assert row(env, active)["status"] == "RUNNING"
    assert cancel(env, cancelled).status_code == 200
    env.clock.set(row(env, timed_out)["deadline_at"])
    reconcile(env)
    assert row(env, timed_out)["status"] == "TIMED_OUT"
    assert row(env, cancelled)["status"] == "CANCELLED"
    assert env.backend.delete_calls == 0 and env.backend.create_calls == calls
    env.backend.set_unavailable(False)
    reconcile(env)
    assert env.backend.list_owned() == []


@pytest.mark.parametrize("state", ["PENDING", "PROVISIONING", "RUNNING"])
def test_LC_8_deadline_in_each_nonterminal_state(env, state):
    execution_id = create(env).json()["id"]
    if state == "PROVISIONING":
        env.backend.fail_next_create_without_creating()
        reconcile(env)
    elif state == "RUNNING":
        reconcile(env)
    assert row(env, execution_id)["status"] == state
    env.clock.set(row(env, execution_id)["deadline_at"])
    reconcile(env)
    assert row(env, execution_id)["status"] == "TIMED_OUT"
    assert env.backend.list_owned() == []


@pytest.mark.parametrize("code", [0, 1])
def test_LC_9_exit_without_completion_fails(env, code):
    execution_id = running(env)
    env.backend.exit(f"exec-{execution_id}", code)
    reconcile(env)
    assert row(env, execution_id)["failure_reason"] == "EXITED_WITHOUT_COMPLETION"
    assert complete(env, execution_id).status_code == 409
    assert complete(env, running(env)).status_code == 200


def test_LC_9_completion_validation_replay_and_binding(env):
    execution_id = running(env)
    other = running(env)
    assert (
        complete(
            env, other, credential=env.backend.specs[f"exec-{execution_id}"].credential
        ).status_code
        == 401
    )
    assert complete(env, execution_id, credential="bad").status_code == 401
    assert (
        env.client.post(
            f"/api/v1/executions/{execution_id}/complete", json={"result": {}}
        ).status_code
        == 401
    )
    for result in ([1], "text", {"value": "é" * 32768}):
        response = complete(env, execution_id, result)
        assert response.status_code == 422
        assert row(env, execution_id)["status"] == "RUNNING"
        assert row(env, execution_id)["result"] is None
    result = {"b": 2, "a": 1}
    success = complete(env, execution_id, result)
    assert success.status_code == 200
    assert complete(env, execution_id, {"a": 1, "b": 2}).json() == success.json()
    assert complete(env, execution_id, {"a": 3}).status_code == 409
    assert cancel(env, execution_id).status_code == 409
    assert get(env, execution_id).json()["result"] == result


@pytest.mark.parametrize("winner", ["SUCCEEDED", "CANCELLED", "TIMED_OUT"])
def test_LC_1_LC_9_controlled_interleavings_atomic_result(env, winner):
    execution_id = running(env)
    record = row(env, execution_id)
    attempting = Event()
    loser_pid = None
    with env.db.engine.begin() as conn:
        values = {"result": {"ok": True}, "result_hash": "test"} if winner == "SUCCEEDED" else {}
        assert transition(env.db, conn, record, winner, env.clock.now(), **values)
        # The other connection cannot see the uncommitted result or status.
        assert row(env, execution_id)["status"] == "RUNNING"
        assert row(env, execution_id)["result"] is None

        def loser():
            nonlocal loser_pid
            with env.db.engine.begin() as other_conn:
                loser_pid = other_conn.scalar(text("SELECT pg_backend_pid()"))
                attempting.set()
                return transition(
                    env.db,
                    other_conn,
                    record,
                    "CANCELLED" if winner == "SUCCEEDED" else "SUCCEEDED",
                    env.clock.now(),
                    **(
                        {}
                        if winner == "SUCCEEDED"
                        else {"result": {"bad": True}, "result_hash": "bad"}
                    ),
                )

        with ThreadPoolExecutor() as pool:
            future = pool.submit(loser)
            try:
                assert attempting.wait(5)
                # AUTOCOMMIT avoids retaining a stale statistics snapshot while polling.
                with env.db.engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT"
                ) as observer:
                    until = monotonic() + 5
                    while monotonic() < until:
                        wait_type = observer.scalar(
                            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                            {"pid": loser_pid},
                        )
                        if wait_type == "Lock":
                            break
                        sleep(0.01)
                    else:
                        pytest.fail("Losing transition never blocked on the winner's lock")
                conn.commit()
            finally:
                # Release the loser even when the lock-observation assertion fails.
                if conn.in_transaction():
                    conn.rollback()
            assert future.result(timeout=5) is None
    final = row(env, execution_id)
    assert final["status"] == winner
    assert final["result"] == ({"ok": True} if winner == "SUCCEEDED" else None)


def test_LC_1_threaded_terminal_race(env):
    for _ in range(50):
        execution_id = running(env)
        deadline = row(env, execution_id)["deadline_at"]
        barrier = Barrier(3)

        def race(action, barrier=barrier, execution_id=execution_id, deadline=deadline):
            barrier.wait()
            if action == "complete":
                return complete(env, execution_id)
            if action == "cancel":
                return cancel(env, execution_id)
            env.app.state.reconciler.reconcile_once(deadline)

        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(pool.map(race, ["complete", "cancel", "deadline"]))
        final = row(env, execution_id)
        assert final["status"] in {"SUCCEEDED", "CANCELLED", "TIMED_OUT"}
        assert (final["result"] is not None) == (final["status"] == "SUCCEEDED")
        assert sum(r is not None and r.status_code == 200 for r in responses) == (
            final["status"] != "TIMED_OUT"
        )
        stored = dict(final)
        complete(env, execution_id)
        cancel(env, execution_id)
        reconcile(env)
        assert dict(row(env, execution_id)) == stored


@pytest.mark.parametrize("state", ["PENDING", "PROVISIONING"])
def test_LC_1_cancel_before_running_and_stale_reconciler(env, state):
    execution_id = create(env).json()["id"]
    if state == "PROVISIONING":
        env.backend.lose_next_create_response()
        reconcile(env)
    stale = row(env, execution_id)
    response = cancel(env, execution_id)
    assert response.status_code == 200
    assert cancel(env, execution_id).json() == response.json()
    with env.db.engine.begin() as conn:
        assert (
            transition(env.db, conn, stale, "RUNNING", env.clock.now(), workload_uid="stale")
            is None
        )
    reconcile(env)
    assert row(env, execution_id)["status"] == "CANCELLED"
    assert row(env, execution_id)["result"] is None


def test_LC_11_database_failure_and_owned_orphans(env, monkeypatch):
    owned_id = str(uuid4())
    name = f"exec-{owned_id}"
    env.backend.appear(name, {"owner": "agent-runtime", "execution_id": owned_id})
    env.backend.add_unowned("unowned")
    connect = env.db.engine.connect

    def unavailable():
        raise OperationalError("connection", {}, Exception("unavailable"))

    monkeypatch.setattr(env.db.engine, "connect", unavailable)
    with pytest.raises(OperationalError):
        reconcile(env)
    assert env.backend.delete_calls == 0
    monkeypatch.setattr(env.db.engine, "connect", connect)
    reconcile(env)
    assert env.backend.get(name) is None
    assert env.backend.get("unowned") is not None


def test_LC_3_rejected_create_and_running_loss(env):
    execution_id = create(env).json()["id"]
    env.backend.reject_next_create()
    reconcile(env)
    assert row(env, execution_id)["failure_reason"] == "LAUNCH_FAILED"
    active = running(env)
    calls = env.backend.create_calls
    env.backend.remove(f"exec-{active}")
    reconcile(env)
    assert row(env, active)["failure_reason"] == "POD_LOST"
    reconcile(env)
    assert env.backend.create_calls == calls


def test_LC_1_cancel_during_backend_create_blocks_stale_running_write(env, monkeypatch):
    execution_id = create(env).json()["id"]
    original_create = env.backend.create

    def cancel_during_create(name, spec):
        assert row(env, execution_id)["status"] == "PROVISIONING"
        assert cancel(env, execution_id).status_code == 200
        uid = original_create(name, spec)
        assert env.backend.get(name) is not None
        return uid

    monkeypatch.setattr(env.backend, "create", cancel_during_create)
    reconcile(env)
    record = row(env, execution_id)
    assert record["status"] == "CANCELLED"
    assert record["workload_uid"] is None
    assert record["credential_hash"] is None
    assert env.backend.create_calls == 1
    reconcile(env)
    assert env.backend.get(f"exec-{execution_id}") is None
    assert env.backend.create_calls == 1


def test_LC_11_cleanup_observes_workloads_not_terminal_history(env, monkeypatch):
    history = [create(env).json()["id"] for _ in range(20)]
    for execution_id in history:
        assert cancel(env, execution_id).status_code == 200
    active = running(env)
    late = history[0]
    orphan = str(uuid4())
    for execution_id in (late, orphan):
        env.backend.appear(
            f"exec-{execution_id}",
            {
                "owner": "agent-runtime",
                "execution_id": execution_id,
            },
        )
    observed = []
    lists = []
    original_get, original_list = env.backend.get, env.backend.list_owned

    def observe(name):
        observed.append(name)
        return original_get(name)

    def list_once():
        lists.append(True)
        return original_list()

    monkeypatch.setattr(env.backend, "get", observe)
    monkeypatch.setattr(env.backend, "list_owned", list_once)
    reconcile(env)
    assert observed == [f"exec-{active}"]
    assert len(lists) == 1
    assert original_get(f"exec-{late}") is None
    assert original_get(f"exec-{orphan}") is None
    assert original_get(f"exec-{active}") is not None


def test_LC_11_cleanup_lookup_failure_never_means_orphan(env, monkeypatch):
    orphan = str(uuid4())
    env.backend.appear(f"exec-{orphan}", {"owner": "agent-runtime", "execution_id": orphan})
    original_connect = env.db.engine.connect
    original_list = env.backend.list_owned

    def unavailable():
        raise OperationalError("connection", {}, Exception("unavailable"))

    def list_then_lose_database():
        workloads = original_list()
        monkeypatch.setattr(env.db.engine, "connect", unavailable)
        return workloads

    monkeypatch.setattr(env.backend, "list_owned", list_then_lose_database)
    with pytest.raises(OperationalError):
        reconcile(env)
    assert env.backend.delete_calls == 0
    assert env.backend.get(f"exec-{orphan}") is not None
    monkeypatch.setattr(env.db.engine, "connect", original_connect)
    monkeypatch.setattr(env.backend, "list_owned", original_list)
    reconcile(env)
    assert env.backend.get(f"exec-{orphan}") is None
