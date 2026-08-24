"""Repositories.

Tenant isolation lives here and nowhere else: every repository is constructed with an ``org_id``
and injects that filter into every read and write. A service cannot "forget" the filter, because
it never writes the query.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, TypeVar, cast

from sqlalchemy import CursorResult, Select, and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from gdap.core.enums import JobState
from gdap.core.errors import ConflictError, NotFoundError
from gdap.observability.logging import get_logger
from gdap.storage import models as m

log = get_logger(__name__)

T = TypeVar("T", bound=m.Base)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Repository(Generic[T]):
    """Tenant-scoped CRUD over one model."""

    model: type[T]

    def __init__(self, session: Session, org_id: str) -> None:
        self.session = session
        self.org_id = org_id

    # ------------------------------------------------------------------ helpers
    def _scoped(self, statement: Select[Any]) -> Select[Any]:
        if hasattr(self.model, "org_id"):
            return statement.where(self.model.org_id == self.org_id)  # type: ignore[attr-defined]
        return statement

    def get(self, entity_id: str) -> T | None:
        statement = self._scoped(select(self.model).where(self.model.id == entity_id))  # type: ignore[attr-defined]
        return self.session.execute(statement).scalar_one_or_none()

    def get_or_raise(self, entity_id: str) -> T:
        found = self.get(entity_id)
        if found is None:
            raise NotFoundError(
                f"{self.model.__name__.lower()} '{entity_id}' not found",
                details={"resource": self.model.__name__, "id": entity_id},
            )
        return found

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "created_at",
        descending: bool = True,
        **filters: Any,
    ) -> list[T]:
        statement = self._scoped(select(self.model))
        for key, value in filters.items():
            if value is None or not hasattr(self.model, key):
                continue
            column = getattr(self.model, key)
            statement = statement.where(
                column.in_(value) if isinstance(value, list | tuple) else column == value
            )
        if hasattr(self.model, order_by):
            column = getattr(self.model, order_by)
            statement = statement.order_by(column.desc() if descending else column.asc())
        statement = statement.limit(min(limit, 1000)).offset(offset)
        return list(self.session.execute(statement).scalars().all())

    def count(self, **filters: Any) -> int:
        statement = self._scoped(select(func.count()).select_from(self.model))
        for key, value in filters.items():
            if value is not None and hasattr(self.model, key):
                statement = statement.where(getattr(self.model, key) == value)
        return int(self.session.execute(statement).scalar_one())

    def create(self, **values: Any) -> T:
        if hasattr(self.model, "org_id"):
            values.setdefault("org_id", self.org_id)
        entity = self.model(**values)
        self.session.add(entity)
        self.session.flush()
        return entity

    def save(self, entity: T) -> T:
        self.session.add(entity)
        self.session.flush()
        return entity

    def delete(self, entity_id: str) -> None:
        entity = self.get_or_raise(entity_id)
        self.session.delete(entity)
        self.session.flush()


# ────────────────────────────────────────── catalog ────────────────────────────────────────


class SourceRepository(Repository[m.Source]):
    model = m.Source

    def by_name(self, name: str) -> m.Source | None:
        statement = self._scoped(select(m.Source).where(m.Source.name == name))
        return self.session.execute(statement).scalar_one_or_none()

    def require_by_name_or_id(self, reference: str) -> m.Source:
        found = self.by_name(reference) or self.get(reference)
        if found is None:
            raise NotFoundError(f"source '{reference}' not found")
        return found


class DatasetRepository(Repository[m.Dataset]):
    model = m.Dataset

    def by_name(self, name: str) -> m.Dataset | None:
        statement = self._scoped(select(m.Dataset).where(m.Dataset.name == name))
        return self.session.execute(statement).scalar_one_or_none()

    def require_by_name_or_id(self, reference: str) -> m.Dataset:
        found = self.by_name(reference) or self.get(reference)
        if found is None:
            raise NotFoundError(
                f"dataset '{reference}' not found",
                details={"hint": "list datasets with GET /api/v1/datasets"},
            )
        return found

    def get_or_create(self, name: str, **values: Any) -> m.Dataset:
        existing = self.by_name(name)
        if existing:
            return existing
        return self.create(name=name, **values)

    # -- versions -------------------------------------------------------------
    def add_version(self, dataset: m.Dataset, **values: Any) -> m.DatasetVersion:
        version = m.DatasetVersion(org_id=self.org_id, dataset_id=dataset.id, **values)
        self.session.add(version)
        dataset.current_version = version.version
        dataset.row_count = values.get("row_count", dataset.row_count)
        self.session.flush()
        return version

    def latest_version(self, dataset_id: str) -> m.DatasetVersion | None:
        statement = (
            select(m.DatasetVersion)
            .where(
                and_(
                    m.DatasetVersion.dataset_id == dataset_id,
                    m.DatasetVersion.org_id == self.org_id,
                )
            )
            .order_by(m.DatasetVersion.version.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def version(self, dataset_id: str, version: int) -> m.DatasetVersion | None:
        statement = select(m.DatasetVersion).where(
            and_(
                m.DatasetVersion.dataset_id == dataset_id,
                m.DatasetVersion.org_id == self.org_id,
                m.DatasetVersion.version == version,
            )
        )
        return self.session.execute(statement).scalar_one_or_none()

    def versions(self, dataset_id: str, *, limit: int = 50) -> list[m.DatasetVersion]:
        statement = (
            select(m.DatasetVersion)
            .where(
                and_(
                    m.DatasetVersion.dataset_id == dataset_id,
                    m.DatasetVersion.org_id == self.org_id,
                )
            )
            .order_by(m.DatasetVersion.version.desc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars().all())

    def resolve_version(self, dataset: m.Dataset, version: int | None) -> m.DatasetVersion:
        found = (
            self.version(dataset.id, version)
            if version is not None
            else self.latest_version(dataset.id)
        )
        if found is None:
            raise NotFoundError(
                f"dataset '{dataset.name}' has no {'version ' + str(version) if version else 'data yet'}",
                details={"dataset_id": dataset.id},
            )
        return found


class IngestionRepository(Repository[m.Ingestion]):
    model = m.Ingestion


class ProfileRepository(Repository[m.Profile]):
    model = m.Profile

    def latest_for_version(self, dataset_version_id: str) -> m.Profile | None:
        statement = self._scoped(
            select(m.Profile)
            .where(m.Profile.dataset_version_id == dataset_version_id)
            .order_by(m.Profile.created_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()


class QualityRepository(Repository[m.QualityReportRow]):
    model = m.QualityReportRow

    def latest_for_dataset(self, dataset_id: str) -> m.QualityReportRow | None:
        statement = self._scoped(
            select(m.QualityReportRow)
            .where(m.QualityReportRow.dataset_id == dataset_id)
            .order_by(m.QualityReportRow.created_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def history(self, dataset_id: str, *, limit: int = 30) -> list[m.QualityReportRow]:
        statement = self._scoped(
            select(m.QualityReportRow)
            .where(m.QualityReportRow.dataset_id == dataset_id)
            .order_by(m.QualityReportRow.created_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars().all())


# ────────────────────────────────────────── automation ─────────────────────────────────────


class PipelineRepository(Repository[m.Pipeline]):
    model = m.Pipeline

    def by_name(self, name: str) -> m.Pipeline | None:
        statement = self._scoped(select(m.Pipeline).where(m.Pipeline.name == name))
        return self.session.execute(statement).scalar_one_or_none()

    def require_by_name_or_id(self, reference: str) -> m.Pipeline:
        found = self.by_name(reference) or self.get(reference)
        if found is None:
            raise NotFoundError(f"pipeline '{reference}' not found")
        return found

    def add_version(
        self, pipeline: m.Pipeline, spec: dict[str, Any], actor: str
    ) -> m.PipelineVersion:
        version = m.PipelineVersion(
            org_id=self.org_id,
            pipeline_id=pipeline.id,
            version=pipeline.version,
            spec=spec,
            fingerprint=pipeline.fingerprint,
            created_by=actor,
        )
        self.session.add(version)
        self.session.flush()
        return version

    def due(self, now: datetime | None = None, *, limit: int = 50) -> list[m.Pipeline]:
        """Schedules that should fire. Not tenant-scoped: the scheduler is a system actor."""
        moment = now or utcnow()
        statement = (
            select(m.Pipeline)
            .where(
                and_(
                    m.Pipeline.enabled.is_(True),
                    m.Pipeline.next_run_at.is_not(None),
                    m.Pipeline.next_run_at <= moment,
                )
            )
            .order_by(m.Pipeline.next_run_at.asc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars().all())


class JobRepository(Repository[m.Job]):
    model = m.Job

    def steps(self, job_id: str) -> list[m.JobStep]:
        statement = (
            select(m.JobStep)
            .where(m.JobStep.job_id == job_id)
            .order_by(m.JobStep.attempt.asc(), m.JobStep.index.asc())
        )
        return list(self.session.execute(statement).scalars().all())

    def clear_steps(self, job_id: str, attempt: int) -> int:
        """Drop the step rows of an attempt that is about to be re-run.

        A run resumed after an approval is the *same* attempt continuing, so its earlier partial
        step rows would otherwise appear twice. The superseded history stays in the job result
        payload and in the audit trail.
        """
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                delete(m.JobStep).where(
                    and_(m.JobStep.job_id == job_id, m.JobStep.attempt == attempt)
                )
            ),
        )
        return int(result.rowcount or 0)

    def add_step(self, job_id: str, **values: Any) -> m.JobStep:
        step = m.JobStep(job_id=job_id, **values)
        self.session.add(step)
        self.session.flush()
        return step

    def transition(self, job: m.Job, state: JobState, **values: Any) -> m.Job:
        """Guarded state machine — terminal states are final (§20)."""
        current = JobState(job.state)
        if current.is_terminal and state != current:
            raise ConflictError(
                f"job {job.id} is already {current.value}",
                details={"attempted": state.value},
            )
        job.state = state.value
        for key, value in values.items():
            setattr(job, key, value)
        if state is JobState.RUNNING and job.started_at is None:
            job.started_at = utcnow()
        if state.is_terminal:
            job.finished_at = utcnow()
            job.lease_until = None
        self.session.flush()
        return job

    # -- queue primitives (see ADR-004) ---------------------------------------
    def claim_next(
        self, worker_id: str, *, lease_seconds: int, now: datetime | None = None
    ) -> m.Job | None:
        """Atomically lease one runnable job. Safe across processes: the UPDATE is the lock."""
        moment = now or utcnow()
        candidates = (
            select(m.Job.id)
            .where(
                and_(
                    m.Job.scheduled_for <= moment,
                    or_(
                        m.Job.state == JobState.PENDING.value,
                        m.Job.state == JobState.RETRYING.value,
                        and_(
                            m.Job.state == JobState.RUNNING.value,
                            m.Job.lease_until.is_not(None),
                            m.Job.lease_until < moment,  # crashed worker: lease expired
                        ),
                    ),
                )
            )
            .order_by(m.Job.priority.desc(), m.Job.scheduled_for.asc())
            .limit(1)
        )

        job_id = self.session.execute(candidates).scalar_one_or_none()
        if job_id is None:
            return None

        claimed = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(m.Job)
                .where(
                    and_(
                        m.Job.id == job_id,
                        or_(
                            m.Job.state.in_([JobState.PENDING.value, JobState.RETRYING.value]),
                            and_(
                                m.Job.state == JobState.RUNNING.value,
                                m.Job.lease_until < moment,
                            ),
                        ),
                    )
                )
                .values(
                    state=JobState.RUNNING.value,
                    worker_id=worker_id,
                    lease_until=moment + timedelta(seconds=lease_seconds),
                    started_at=m.Job.started_at,
                    attempt=m.Job.attempt + 1,
                )
            ),
        )
        if claimed.rowcount != 1:
            return None  # lost the race to another worker
        self.session.flush()
        return self.session.get(m.Job, job_id)

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int) -> bool:
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(m.Job)
                .where(and_(m.Job.id == job_id, m.Job.worker_id == worker_id))
                .values(lease_until=utcnow() + timedelta(seconds=lease_seconds))
            ),
        )
        return result.rowcount == 1

    def pending_count(self) -> int:
        statement = (
            select(func.count())
            .select_from(m.Job)
            .where(m.Job.state.in_([JobState.PENDING.value, JobState.RETRYING.value]))
        )
        return int(self.session.execute(statement).scalar_one())

    def recent(self, *, limit: int = 20) -> list[m.Job]:
        return self.list(limit=limit, order_by="created_at")


class ApprovalRepository(Repository[m.ApprovalRequest]):
    model = m.ApprovalRequest

    def pending_for_job(self, job_id: str) -> m.ApprovalRequest | None:
        statement = self._scoped(
            select(m.ApprovalRequest).where(
                and_(m.ApprovalRequest.job_id == job_id, m.ApprovalRequest.status == "pending")
            )
        )
        return self.session.execute(statement).scalar_one_or_none()


# ─────────────────────────────────── analysis, reports, alerts ─────────────────────────────


class AnalysisRepository(Repository[m.Analysis]):
    model = m.Analysis


class ReportRepository(Repository[m.Report]):
    model = m.Report


class AlertRepository(Repository[m.Alert]):
    model = m.Alert

    def find_open_duplicate(self, dedupe_key: str, *, within_minutes: int = 60) -> m.Alert | None:
        cutoff = utcnow() - timedelta(minutes=within_minutes)
        statement = self._scoped(
            select(m.Alert)
            .where(
                and_(
                    m.Alert.dedupe_key == dedupe_key,
                    m.Alert.status == "open",
                    m.Alert.created_at >= cutoff,
                )
            )
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()


class AlertRuleRepository(Repository[m.AlertRule]):
    model = m.AlertRule

    def enabled_rules(self, kind: str | None = None) -> list[m.AlertRule]:
        statement = self._scoped(select(m.AlertRule).where(m.AlertRule.enabled.is_(True)))
        if kind:
            statement = statement.where(m.AlertRule.kind == kind)
        return list(self.session.execute(statement).scalars().all())


class ModelRepository(Repository[m.ModelRecord]):
    model = m.ModelRecord

    def latest(self, name: str) -> m.ModelRecord | None:
        statement = self._scoped(
            select(m.ModelRecord)
            .where(m.ModelRecord.name == name)
            .order_by(m.ModelRecord.version.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def next_version(self, name: str) -> int:
        latest = self.latest(name)
        return (latest.version + 1) if latest else 1


# ────────────────────────────────────────── governance ─────────────────────────────────────


class AuditRepository(Repository[m.AuditEventRow]):
    """Append-only: there is deliberately no update or delete path."""

    model = m.AuditEventRow

    def append(self, **values: Any) -> m.AuditEventRow:
        return self.create(**values)

    def delete(self, entity_id: str) -> None:  # noqa: D102 - intentional override
        raise ConflictError("audit events are immutable")

    def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[m.AuditEventRow]:
        statement = self._scoped(select(m.AuditEventRow))
        if actor:
            statement = statement.where(m.AuditEventRow.actor == actor)
        if action:
            statement = statement.where(m.AuditEventRow.action == action)
        if resource_type:
            statement = statement.where(m.AuditEventRow.resource_type == resource_type)
        if resource_id:
            statement = statement.where(m.AuditEventRow.resource_id == resource_id)
        if since:
            statement = statement.where(m.AuditEventRow.at >= since)
        statement = (
            statement.order_by(m.AuditEventRow.at.desc()).limit(min(limit, 1000)).offset(offset)
        )
        return list(self.session.execute(statement).scalars().all())


class LineageRepository(Repository[m.LineageEdgeRow]):
    model = m.LineageEdgeRow

    def upstream(self, node_type: str, node_id: str) -> list[m.LineageEdgeRow]:
        statement = self._scoped(
            select(m.LineageEdgeRow).where(
                and_(
                    m.LineageEdgeRow.downstream_type == node_type,
                    m.LineageEdgeRow.downstream_id == node_id,
                )
            )
        )
        return list(self.session.execute(statement).scalars().all())

    def downstream(self, node_type: str, node_id: str) -> list[m.LineageEdgeRow]:
        statement = self._scoped(
            select(m.LineageEdgeRow).where(
                and_(
                    m.LineageEdgeRow.upstream_type == node_type,
                    m.LineageEdgeRow.upstream_id == node_id,
                )
            )
        )
        return list(self.session.execute(statement).scalars().all())

    def for_job(self, job_id: str) -> list[m.LineageEdgeRow]:
        statement = self._scoped(select(m.LineageEdgeRow).where(m.LineageEdgeRow.job_id == job_id))
        return list(self.session.execute(statement).scalars().all())


# ──────────────────────────────────── identity (cross-tenant) ──────────────────────────────


class OrganizationRepository:
    """Not tenant-scoped by definition — used only by bootstrap and admin flows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def by_slug(self, slug: str) -> m.Organization | None:
        return self.session.execute(
            select(m.Organization).where(m.Organization.slug == slug)
        ).scalar_one_or_none()

    def get(self, org_id: str) -> m.Organization | None:
        return self.session.get(m.Organization, org_id)

    def create(self, *, slug: str, name: str, **values: Any) -> m.Organization:
        if self.by_slug(slug):
            raise ConflictError(f"organization '{slug}' already exists")
        org = m.Organization(slug=slug, name=name, **values)
        self.session.add(org)
        self.session.flush()
        return org

    def list(self, *, limit: int = 100) -> Sequence[m.Organization]:
        return list(self.session.execute(select(m.Organization).limit(limit)).scalars().all())


class UserRepository(Repository[m.User]):
    model = m.User

    def by_email(self, email: str) -> m.User | None:
        statement = self._scoped(select(m.User).where(m.User.email == email))
        return self.session.execute(statement).scalar_one_or_none()


class ApiKeyRepository(Repository[m.ApiKey]):
    model = m.ApiKey

    def by_prefix(self, prefix: str) -> m.ApiKey | None:
        """Cross-tenant lookup: the key itself identifies the tenant."""
        statement = select(m.ApiKey).where(
            and_(m.ApiKey.prefix == prefix, m.ApiKey.revoked_at.is_(None))
        )
        return self.session.execute(statement).scalar_one_or_none()

    def revoke(self, key_id: str) -> None:
        key = self.get_or_raise(key_id)
        key.revoked_at = utcnow()
        self.session.flush()

    def purge_expired(self) -> int:
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                delete(m.ApiKey).where(
                    and_(m.ApiKey.expires_at.is_not(None), m.ApiKey.expires_at < utcnow())
                )
            ),
        )
        return int(result.rowcount or 0)
