from __future__ import annotations

import threading
import time
from datetime import timedelta

from judicial_ai.config import get_settings
from judicial_ai.db import SessionLocal
from judicial_ai.models import Job, utcnow
from judicial_ai.services.jobs import JOB_HANDLERS, enqueue_job, recover_stale_jobs, run_worker_loop


def test_stale_job_fails_without_automatic_retry(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_mode", "worker")
    with SessionLocal() as db:
        job = enqueue_job(db, "retry", {}, tenant_id=1)
        job.status = "RUNNING"
        job.attempts = 1
        job.started_at = utcnow() - timedelta(minutes=30)
        job.heartbeat_at = job.started_at
        db.commit()
        assert recover_stale_jobs(db, stale_after_seconds=10) == 1
        db.refresh(job)
        assert job.status == "FAILED"
        assert "outcome uncertain" in job.error
        assert recover_stale_jobs(db, stale_after_seconds=10) == 0


def test_active_job_heartbeat_prevents_recovery(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "job_mode", "worker")
    monkeypatch.setattr(settings, "worker_heartbeat_seconds", 0.1)
    monkeypatch.setattr(settings, "worker_stale_seconds", 1)
    started = threading.Event()
    release = threading.Event()

    def handler(db, job):
        started.set()
        assert release.wait(10)
        return {"done": True}

    monkeypatch.setitem(JOB_HANDLERS, "heartbeat-test", handler)
    with SessionLocal() as db:
        job_id = enqueue_job(db, "heartbeat-test", {}, tenant_id=1).id
    worker = threading.Thread(target=lambda: run_worker_loop(once=True, batch_size=1))
    worker.start()
    try:
        assert started.wait(10)
        time.sleep(1.5)
        with SessionLocal() as db:
            assert recover_stale_jobs(db, stale_after_seconds=1) == 0
            assert db.get(Job, job_id).status == "RUNNING"
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == "SUCCEEDED"


def test_running_job_cannot_be_cancelled(client, admin_headers):
    with SessionLocal() as db:
        job = Job(
            id="job-running-cancel-test",
            job_type="retry",
            tenant_id=1,
            payload={},
            status="RUNNING",
            attempts=1,
            started_at=utcnow(),
        )
        db.add(job)
        db.commit()
    response = client.post(f"/api/v1/jobs/{job.id}/cancel", headers=admin_headers)
    assert response.status_code == 409
