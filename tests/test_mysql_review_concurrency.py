from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from judicial_ai.config import get_settings
from judicial_ai.db import Base
from judicial_ai.errors import ApiError
from judicial_ai.ids import new_id
from judicial_ai.models import (
    AnnotationRecord,
    AnnotationTask,
    Dataset,
    DatasetItem,
    DatasetVersion,
)
from judicial_ai.schemas import AnnotationRecordCreate, AnnotationTaskCreate
from judicial_ai.security import Principal
from judicial_ai.services import governance

MYSQL_URL = os.getenv("JAI_TEST_MYSQL_URL")
pytestmark = pytest.mark.skipif(not MYSQL_URL, reason="isolated MySQL test database not configured")


@pytest.fixture
def mysql_case(monkeypatch):
    engine = create_engine(MYSQL_URL, pool_pre_ping=True)
    if engine.dialect.name != "mysql":
        pytest.fail("JAI_TEST_MYSQL_URL must refer to an isolated MySQL database")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    tenant_id, dataset_id, version_id, item_id = (new_id() for _ in range(4))
    monkeypatch.setattr(get_settings(), "app_env", "dev")
    monkeypatch.setattr(governance, "_check_regression_item_access", lambda *args: None)
    monkeypatch.setattr(governance, "_validate_regression_label", lambda *args: [])
    monkeypatch.setattr(governance, "record_event", lambda *args, **kwargs: None)
    with sessions() as db:
        db.add_all([
            Dataset(id=dataset_id, tenant_id=tenant_id, code=f"race-{uuid.uuid4().hex}",
                    name="concurrency test", type="RETRIEVAL_EVAL"),
            DatasetVersion(id=version_id, dataset_id=dataset_id, version="v1", status="DRAFT",
                           source_snapshot={"source": "retrieval_feedback"}),
            DatasetItem(id=item_id, dataset_version_id=version_id, item_type="retrieval_feedback",
                        input={"query": "test", "kb_ids": []}, output={}, label={},
                        review_status="NEEDS_REVIEW", split="test"),
        ])
        db.commit()
    yield sessions, tenant_id, version_id, item_id
    engine.dispose()


def _parallel(count, action):
    barrier = threading.Barrier(count)

    def run(index):
        barrier.wait(timeout=15)
        try:
            return action(index)
        except ApiError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def test_mysql_serializes_task_creation(mysql_case):
    sessions, tenant_id, version_id, _ = mysql_case
    payload = AnnotationTaskCreate(
        name="race", task_type="RETRIEVAL", dataset_version_id=version_id, required_reviewers=2
    )

    def create(index):
        with sessions() as db:
            principal = Principal(kind="app", tenant_id=tenant_id, app_id=index + 1)
            governance.create_annotation_task(db, principal, payload)
            return 201

    assert sorted(_parallel(2, create)) == [201, 409]
    with sessions() as db:
        assert db.query(AnnotationTask).filter_by(dataset_version_id=version_id).count() == 1


def test_mysql_serializes_concurrent_reviews(mysql_case):
    sessions, tenant_id, version_id, item_id = mysql_case
    with sessions() as db:
        task = AnnotationTask(
            id=new_id(), tenant_id=tenant_id, dataset_version_id=version_id,
            name="race", task_type="RETRIEVAL", required_reviewers=2, status="PENDING",
        )
        db.add(task)
        db.commit()
        task_id = task.id
    payload = AnnotationRecordCreate(item_id=item_id, decision="REJECTED")

    def review(index):
        with sessions() as db:
            principal = Principal(kind="app", tenant_id=tenant_id, app_id=index + 1)
            governance.submit_annotation_record(db, principal, task_id, payload)
            return 201

    assert _parallel(2, review) == [201, 201]
    with sessions() as db:
        assert db.get(DatasetItem, item_id).review_status == "REJECTED"
        assert db.get(AnnotationTask, task_id).status == "COMPLETED"
        assert db.query(AnnotationRecord).filter_by(task_id=task_id, item_id=item_id).count() == 2


def test_mysql_rejects_duplicate_concurrent_review(mysql_case):
    sessions, tenant_id, version_id, item_id = mysql_case
    with sessions() as db:
        task = AnnotationTask(
            id=new_id(), tenant_id=tenant_id, dataset_version_id=version_id,
            name="duplicate", task_type="RETRIEVAL", required_reviewers=2, status="PENDING",
        )
        db.add(task)
        db.commit()
        task_id = task.id
    payload = AnnotationRecordCreate(item_id=item_id, decision="REJECTED")

    def review(_index):
        with sessions() as db:
            principal = Principal(kind="app", tenant_id=tenant_id, app_id=1)
            governance.submit_annotation_record(db, principal, task_id, payload)
            return 201

    assert sorted(_parallel(2, review)) == [201, 409]
    with sessions() as db:
        assert db.get(DatasetItem, item_id).review_status == "IN_REVIEW"
        assert db.query(AnnotationRecord).filter_by(task_id=task_id, item_id=item_id).count() == 1


def test_mysql_publish_cannot_overtake_review(mysql_case):
    sessions, tenant_id, version_id, item_id = mysql_case
    with sessions() as db:
        task = AnnotationTask(
            id=new_id(), tenant_id=tenant_id, dataset_version_id=version_id,
            name="publish-race", task_type="RETRIEVAL", required_reviewers=1, status="PENDING",
        )
        db.add(task)
        db.commit()
        task_id = task.id
    payload = AnnotationRecordCreate(
        item_id=item_id, decision="APPROVED",
        label={"relevant_chunk_ids": [], "expect_empty": True},
    )

    def review_or_publish(index):
        with sessions() as db:
            principal = Principal(kind="app", tenant_id=tenant_id, app_id=index + 1)
            if index == 0:
                governance.submit_annotation_record(db, principal, task_id, payload)
                return 201
            governance.publish_dataset_version(db, principal, version_id)
            return 200

    results = _parallel(2, review_or_publish)
    assert results[0] == 201
    assert results[1] in {200, 409}
    with sessions() as db:
        assert db.get(DatasetItem, item_id).review_status == "APPROVED"
        version = db.get(DatasetVersion, version_id)
        assert version.status == ("PUBLISHED" if results[1] == 200 else "DRAFT")
