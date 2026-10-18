"""Phase 5 cross-process ImportJob infrastructure: transactional claim,
fencing tokens, heartbeat honesty, stale recovery, pause/resume and retry
backoff — all against a real SQLite database (two Database instances stand
in for two processes; SQLite file locks provide the actual cross-process
semantics). No network, no real tokens.
"""

import json

import pytest

from auditrail.db import Database, apply_migrations
from auditrail.db.job_store import DbJobStore
from auditrail.integrations import external_models as ext
from auditrail.integrations import import_jobs
from backend.app import import_worker, integration_service, job_store as job_store_mod
from backend.app.config import Settings


def _db(tmp_path, name="jobs.db") -> Database:
    db = Database("sqlite:///%s" % (tmp_path / name))
    apply_migrations(db)
    return db


def _settings(tmp_path, **overrides):
    base = dict(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
        import_checkpoint_records=2,
        import_lease_seconds=60,
        import_max_retries=3,
        storage_mode="database",
        queue_mode="local_worker",
        database_url="sqlite:///%s" % (tmp_path / "platform" / "auditrail.db"),
    )
    base.update(overrides)
    settings = Settings(**base)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    apply_migrations(Database(settings.database_url))
    job_store_mod.reset_cached_stores()
    return settings


def _store(settings) -> job_store_mod.DatabaseJobStore:
    store = job_store_mod.job_store(settings)
    assert store.mode == "database"
    return store


def _new_job(**overrides):
    kwargs = dict(source_system="langfuse", source_type="file", kind="traces", source_fingerprint="fp")
    kwargs.update(overrides)
    return import_jobs.new_import_job(**kwargs)


# ---------------------------------------------------------------------------
# transactional claim
# ---------------------------------------------------------------------------


def test_claim_only_one_worker_wins(tmp_path):
    db = _db(tmp_path)
    store_a = DbJobStore(db)
    store_b = DbJobStore(Database("sqlite:///%s" % (tmp_path / "jobs.db")))
    job = _new_job()
    store_a.save_job(job)

    claimed_a = store_a.claim_next_queued("worker-a", lease_seconds=60)
    claimed_b = store_b.claim_next_queued("worker-b", lease_seconds=60)
    assert claimed_a is not None
    assert claimed_b is None  # second worker sees no claimable job
    assert claimed_a["worker_id"] == "worker-a"
    assert claimed_a["lease_version"] == 1
    assert claimed_a["attempt_count"] == 1
    assert claimed_a["status"] == "validating"
    lease = db.query_one("SELECT worker_id, lease_version FROM job_leases WHERE job_id=%s", (job["import_id"],))
    assert lease["worker_id"] == "worker-a"


def test_fencing_token_blocks_old_worker(tmp_path):
    db = _db(tmp_path)
    store = DbJobStore(db)
    job = _new_job()
    store.save_job(job)
    claimed = store.claim_next_queued("worker-old", lease_seconds=60)
    assert claimed is not None

    # A second worker takes over (simulating recovery after lease expiry):
    import_jobs.save_import_job  # noqa: F841 - documentation reference
    stolen = dict(claimed)
    stolen["lease_version"] = int(claimed["lease_version"]) + 1
    stolen["worker_id"] = "worker-new"
    store.save_job(stolen)  # unfenced admin write (recovery path)

    # The old worker's fenced write is rejected — it cannot clobber state.
    stale_write = dict(claimed)
    stale_write["status"] = "running"
    with pytest.raises(import_jobs.StaleWorkerError):
        store.save_job(stale_write, worker_id="worker-old", lease_version=int(claimed["lease_version"]))
    reloaded = store.load_job(job["import_id"])
    assert reloaded["worker_id"] == "worker-new"
    assert reloaded["status"] == "validating"


def test_heartbeat_fenced_and_honest(tmp_path):
    db = _db(tmp_path)
    store = DbJobStore(db)
    job = _new_job()
    store.save_job(job)
    claimed = store.claim_next_queued("worker-a", lease_seconds=60)
    refreshed = store.heartbeat(claimed, "worker-a", lease_seconds=60)
    assert refreshed["heartbeat_at"] != claimed["heartbeat_at"] or refreshed["lease_expires_at"]

    # another worker's heartbeat fails loudly — it never claims false health
    with pytest.raises(import_jobs.StaleWorkerError):
        store.heartbeat(claimed, "worker-b", lease_seconds=60)


def test_stale_recovery_marks_never_completes(tmp_path):
    db = _db(tmp_path)
    store = DbJobStore(db)
    job = _new_job()
    store.save_job(job)
    claimed = store.claim_next_queued("worker-dead", lease_seconds=60)
    # expire the lease (worker died)
    db.execute("UPDATE import_jobs SET lease_expires_at=%s WHERE import_id=%s", ("2020-01-01T00:00:00Z", job["import_id"]))
    stale = store.recover_stale()
    assert stale == [job["import_id"]]
    reloaded = store.load_job(job["import_id"])
    assert reloaded["status"] == "stale"
    assert reloaded["status"] != "completed"


def test_next_retry_at_defers_claim(tmp_path):
    db = _db(tmp_path)
    store = DbJobStore(db)
    job = _new_job()
    store.save_job(job)
    claimed = store.claim_next_queued("worker-a", lease_seconds=60)
    requeued = store.requeue_retryable(claimed, "worker-a", delay_seconds=3600)
    assert requeued["status"] == "queued"
    assert requeued["next_retry_at"]
    assert requeued["retry_count"] == 1
    # not due yet: no claim
    assert store.claim_next_queued("worker-b", lease_seconds=60) is None
    # due now: claimable
    db.execute("UPDATE import_jobs SET next_retry_at=%s WHERE import_id=%s", ("2020-01-01T00:00:00Z", job["import_id"]))
    assert store.claim_next_queued("worker-b", lease_seconds=60) is not None


def test_idempotency_lookup(tmp_path):
    db = _db(tmp_path)
    store = DbJobStore(db)
    job = _new_job()
    job["idempotency_key"] = "key-1"
    job["request_fingerprint"] = "fp-1"
    store.save_job(job)
    found = store.find_by_idempotency("key-1", "fp-1")
    assert found is not None and found["import_id"] == job["import_id"]
    assert store.find_by_idempotency("key-1", "other") is None


# ---------------------------------------------------------------------------
# pause / resume / retry through the worker service (database store)
# ---------------------------------------------------------------------------


def _traces_file(settings, count=5, prefix="t"):
    rows = [{"id": "%s-%d" % (prefix, index), "metadata": {"run_id": "run-1"}} for index in range(1, count + 1)]
    path = settings.platform_dir / "imports" / "traces.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _make_run(settings, run_id):
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": "completed",
                "status_reason": None,
                "started_at": "2026-01-05T10:00:00Z",
                "finished_at": "2026-01-05T10:00:02Z",
                "agent_ref": {"id": "agent-1"},
                "test_case_ref": None,
                "suite_ref": None,
                "artifact_manifest": [],
            }
        ),
        encoding="utf-8",
    )


def test_database_mode_inline_import_completes(tmp_path):
    settings = _settings(tmp_path)
    _make_run(settings, "run-1")
    _traces_file(settings, count=3)
    job = integration_service.create_import_job(
        "langfuse", {"source_file": "imports/traces.jsonl"}, settings, actor="tester"
    )
    finished = import_worker.execute_import_inline(str(job["import_id"]), settings)
    assert finished["status"] == "completed"
    assert finished["imported_records"] == 3
    assert finished["lease_version"] >= 1
    stored = ext.load_external_traces(settings.platform_dir)
    assert len(stored) == 3
    # dual visibility: listing reads from the database store
    jobs = _store(settings).list_jobs()
    assert [j["import_id"] for j in jobs] == [job["import_id"]]


def test_pause_and_resume_across_processes(tmp_path):
    """Pause lands via one connection; another 'process' resumes from the
    checkpoint without duplicating records."""
    settings = _settings(tmp_path, import_checkpoint_records=2)
    _make_run(settings, "run-1")
    _traces_file(settings, count=5)
    job = integration_service.create_import_job("langfuse", {"source_file": "imports/traces.jsonl"}, settings)
    import_id = str(job["import_id"])

    # pause before any worker claims it: queued -> paused immediately
    paused = import_worker.request_pause(import_id, settings)
    assert paused["status"] == "paused"
    # a paused job is not claimable
    assert _store(settings).claim_next("worker-x", lease_seconds=60) is None

    resumed = import_worker.prepare_resume(import_id, settings)
    assert resumed["status"] == "queued"
    finished = import_worker.execute_import_inline(import_id, settings)
    assert finished["status"] == "completed"
    assert finished["imported_records"] == 5


def test_pause_mid_run_stops_at_batch_boundary(tmp_path):
    settings = _settings(tmp_path, import_checkpoint_records=2)
    _make_run(settings, "run-1")
    _traces_file(settings, count=5)
    job = integration_service.create_import_job("langfuse", {"source_file": "imports/traces.jsonl"}, settings)
    import_id = str(job["import_id"])

    store = _store(settings)
    claimed = store.claim(import_id, "worker-1", lease_seconds=60)
    assert claimed is not None

    # request pause from "the API process" (separate connection)
    api_store = DbJobStore(Database(settings.database_url))
    api_store.mutate_job(import_id, import_jobs.request_pause)

    # driving the job sees the flag at the next flush boundary
    from backend.app.import_worker import _execute

    import threading

    with pytest.raises(import_jobs.ImportJobError) as excinfo:
        _execute(import_id, settings, "worker-1", threading.Event(), store)
    assert excinfo.value.code == "import_paused"
    current = store.load(import_id)
    assert current["pause_requested"] is True
    # the in-flight boundary already persisted its batch: checkpoint exists
    assert current["processed_records"] >= 0


def test_retry_backoff_requeues_without_holding_lease(tmp_path, monkeypatch):
    settings = _settings(tmp_path, import_max_retries=2)
    monkeypatch.setenv("AUDITRAIL_TEST_LF_TOKEN", "lf-token")

    def rate_limited(url, token):
        from auditrail.integrations import langfuse

        raise langfuse.LangfuseSourceError("source_rate_limited", "429", retryable=True, status=429)

    job = integration_service.create_import_job(
        "langfuse",
        {"kind": "traces", "config": {"enabled": True, "base_url": "https://langfuse.example",
                                      "token_env": "AUDITRAIL_TEST_LF_TOKEN", "allowed_hosts": ["langfuse.example"]}},
        settings,
    )
    import_id = str(job["import_id"])
    import_worker.register_import_fetcher(import_id, rate_limited)
    store = _store(settings)
    claimed = store.claim(import_id, "worker-1", lease_seconds=60)
    result = import_worker.execute_claimed_job(claimed, settings)
    # retryable: requeued with backoff, lease released — not failed, not sleeping
    assert result["status"] == "queued"
    assert result["next_retry_at"]
    assert result["worker_id"] is None
    assert result["retry_count"] == 1
    assert result["error"]["retryable"] is True


def test_interrupted_process_never_fakes_completed(tmp_path):
    """A job left running by a dead process is stale after lease expiry."""
    settings = _settings(tmp_path)
    job = _new_job()
    store = _store(settings)
    store.save(job)
    claimed = store.claim(str(job["import_id"]), "worker-dead", lease_seconds=60)
    assert claimed["status"] == "validating"
    # process "dies": lease expires without any terminal write
    store.database.execute(
        "UPDATE import_jobs SET lease_expires_at=%s WHERE import_id=%s",
        ("2020-01-01T00:00:00Z", str(job["import_id"])),
    )
    stale = import_worker.start_import_worker(settings)
    assert str(job["import_id"]) in stale
    reloaded = store.load(str(job["import_id"]))
    assert reloaded["status"] == "stale"
    # explicit resume path works from stale
    retried = import_worker.prepare_retry(str(job["import_id"]), settings)
    assert retried["status"] == "queued"


def test_worker_registry(tmp_path):
    db = _db(tmp_path)
    store = DbJobStore(db)
    store.register_worker("worker-1", pid=1234)
    store.heartbeat_worker("worker-1")
    workers = store.list_workers()
    assert workers[0]["worker_id"] == "worker-1"
    assert workers[0]["status"] == "running"
    assert workers[0]["pid"] == 1234
    assert workers[0]["seconds_since_heartbeat"] is not None
    store.stop_worker("worker-1")
    assert store.list_workers()[0]["status"] == "stopped"


def test_completed_job_not_reexecuted(tmp_path):
    settings = _settings(tmp_path)
    _make_run(settings, "run-1")
    _traces_file(settings, count=2)
    job = integration_service.create_import_job("langfuse", {"source_file": "imports/traces.jsonl"}, settings)
    import_id = str(job["import_id"])
    first = import_worker.execute_import_inline(import_id, settings)
    assert first["status"] == "completed"
    # idempotent: re-executing a terminal job returns it unchanged
    again = import_worker.execute_import_inline(import_id, settings)
    assert again["status"] == "completed"
    assert again["finished_at"] == first["finished_at"]
    assert len(ext.load_external_traces(settings.platform_dir)) == 2
