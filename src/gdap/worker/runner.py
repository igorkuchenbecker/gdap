"""Job runner.

Leases jobs from the metadata store, executes them, and heartbeats the lease while they run. This
is the piece that makes execution *survivable*: if a worker dies mid-job, its lease expires and
another worker picks the job up (at-least-once, which is why step writes are versioned rather than
in-place — ADR-004).

Concurrency is thread-based on purpose: the heavy lifting happens inside Polars/DuckDB, which
release the GIL. Scaling past one machine is a matter of running more workers against the same
database — no code change.
"""

from __future__ import annotations

import signal
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gdap.core.container import Platform
from gdap.core.contracts import Principal
from gdap.core.enums import JobState
from gdap.core.errors import GdapError
from gdap.observability.logging import get_logger, log_context
from gdap.observability.metrics import METRICS
from gdap.storage import models as m

log = get_logger(__name__)


@dataclass(slots=True)
class WorkerConfig:
    worker_id: str
    concurrency: int = 2
    poll_interval_s: float = 1.0
    lease_seconds: int = 300
    heartbeat_seconds: int = 30
    scheduler_enabled: bool = True
    max_jobs: int | None = None  # stop after N jobs (used by tests and one-shot runs)


class JobRunner:
    def __init__(self, platform: Platform, config: WorkerConfig | None = None) -> None:
        self.platform = platform
        settings = platform.settings.worker
        self.config = config or WorkerConfig(
            worker_id=f"worker-{_hostname()}-{threading.get_ident()}",
            concurrency=settings.concurrency,
            poll_interval_s=settings.poll_interval_s,
            lease_seconds=settings.lease_seconds,
            heartbeat_seconds=settings.heartbeat_seconds,
            scheduler_enabled=settings.scheduler_enabled,
        )
        self._stop = threading.Event()
        self._processed = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def request_stop(self, *_signal_args: Any) -> None:
        """Graceful shutdown: finish in-flight jobs, stop leasing new ones."""
        if not self._stop.is_set():
            log.info("worker_stopping", worker_id=self.config.worker_id)
        self._stop.set()

    def install_signal_handlers(self) -> None:
        for signal_name in ("SIGINT", "SIGTERM"):
            handler = getattr(signal, signal_name, None)
            if handler is not None:
                signal.signal(handler, self.request_stop)

    def run_forever(self) -> int:
        """Main loop. Returns the number of jobs processed."""
        log.info(
            "worker_started",
            worker_id=self.config.worker_id,
            concurrency=self.config.concurrency,
            scheduler=self.config.scheduler_enabled,
        )
        scheduler = None
        if self.config.scheduler_enabled:
            from gdap.worker.scheduler import Scheduler

            scheduler = Scheduler(self.platform)

        threads = [
            threading.Thread(target=self._worker_loop, name=f"gdap-worker-{index}", daemon=True)
            for index in range(max(1, self.config.concurrency))
        ]
        for thread in threads:
            thread.start()

        try:
            while not self._stop.is_set():
                if scheduler is not None:
                    try:
                        queued = scheduler.tick()
                        if queued:
                            log.info("scheduler_queued", jobs=queued)
                    except Exception as exc:  # the scheduler must never kill the worker
                        log.error("scheduler_tick_failed", error=str(exc))
                self._stop.wait(max(self.config.poll_interval_s, 1.0))
        finally:
            self.request_stop()
            for thread in threads:
                thread.join(timeout=self.platform.settings.worker.max_job_runtime_s)
            log.info("worker_stopped", worker_id=self.config.worker_id, processed=self._processed)
        return self._processed

    def run_once(self) -> bool:
        """Lease and execute a single job. Returns False when the queue is empty."""
        job_id = self._lease()
        if job_id is None:
            return False
        self._execute(job_id)
        return True

    def drain(self, *, max_jobs: int = 100) -> int:
        """Run queued jobs until the queue is empty (used by the CLI and by tests)."""
        processed = 0
        while processed < max_jobs and self.run_once():
            processed += 1
        return processed

    # ------------------------------------------------------------------ internals
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.run_once():
                    self._stop.wait(self.config.poll_interval_s)
            except Exception as exc:  # a bad job must not take the worker down
                log.exception("worker_loop_error", error=str(exc))
                self._stop.wait(self.config.poll_interval_s)
            if self.config.max_jobs and self._processed >= self.config.max_jobs:
                self.request_stop()

    def _lease(self) -> str | None:
        with self.platform.db.session() as session:
            repo_context = self.platform.context(session, Principal.system("system", "worker"))
            job = repo_context.jobs.repo.claim_next(
                self.config.worker_id, lease_seconds=self.config.lease_seconds
            )
            if job is None:
                return None
            log.info(
                "job_leased",
                job_id=job.id,
                pipeline=job.pipeline_name,
                attempt=job.attempt,
                worker_id=self.config.worker_id,
            )
            return job.id

    def _execute(self, job_id: str) -> None:
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, args=(job_id, heartbeat_stop), daemon=True
        )
        heartbeat.start()
        started = time.perf_counter()

        try:
            with self.platform.db.session() as session:
                job = session.get(m.Job, job_id)
                if job is None:
                    log.error("job_vanished", job_id=job_id)
                    return
                principal = _principal_for_job(self.platform, session, job)
                context = self.platform.context(session, principal)
                with log_context(worker_id=self.config.worker_id, job_id=job_id):
                    result = context.jobs.execute(job)
                METRICS.observe(
                    "job_duration_s", time.perf_counter() - started, pipeline=job.pipeline_name
                )
                log.info(
                    "job_finished",
                    job_id=job_id,
                    state=result.state.value,
                    duration_s=round(time.perf_counter() - started, 3),
                )
        except GdapError as exc:
            log.error("job_execution_error", job_id=job_id, code=exc.code, error=exc.message)
            self._mark_failed(job_id, f"{exc.code}: {exc.message}")
        except Exception as exc:
            log.exception("job_execution_crashed", job_id=job_id, error=str(exc))
            self._mark_failed(job_id, f"{type(exc).__name__}: {exc}")
        finally:
            heartbeat_stop.set()
            with self._lock:
                self._processed += 1

    def _heartbeat_loop(self, job_id: str, stop: threading.Event) -> None:
        while not stop.wait(self.config.heartbeat_seconds):
            try:
                with self.platform.db.session() as session:
                    context = self.platform.context(session, Principal.system("system", "worker"))
                    alive = context.jobs.repo.heartbeat(
                        job_id, self.config.worker_id, lease_seconds=self.config.lease_seconds
                    )
                if not alive:
                    log.warning("heartbeat_lost", job_id=job_id, worker_id=self.config.worker_id)
                    return
            except Exception as exc:  # pragma: no cover - transient db issue
                log.warning("heartbeat_failed", job_id=job_id, error=str(exc))

    def _mark_failed(self, job_id: str, error: str) -> None:
        """Last-resort transition so a crashed execution never leaves a job RUNNING forever."""
        try:
            with self.platform.db.session() as session:
                job = session.get(m.Job, job_id)
                if job is None or JobState(job.state).is_terminal:
                    return
                job.state = JobState.FAILED.value
                job.error = error[:2000]
                job.error_code = "GDAP-5002"
                job.finished_at = datetime.now(UTC)
                job.lease_until = None
        except Exception as exc:  # pragma: no cover
            log.error("job_failure_record_failed", job_id=job_id, error=str(exc))


def _principal_for_job(platform: Platform, session: Any, job: m.Job) -> Principal:
    """Run a job as its creator when possible, so RBAC and audit stay honest."""
    from gdap.security.rbac import permissions_for
    from gdap.storage.repositories import UserRepository

    if job.created_by and not job.created_by.startswith("system:"):
        user = UserRepository(session, job.org_id).get(job.created_by)
        if user is not None:
            from gdap.core.enums import Role

            return Principal(
                org_id=job.org_id,
                user_id=user.id,
                email=user.email,
                role=Role(user.role),
                permissions=permissions_for(Role(user.role)),
            )
    return Principal.system(job.org_id, "worker")


def _hostname() -> str:
    import socket

    try:
        return socket.gethostname()[:24]
    except Exception:  # pragma: no cover
        return "unknown"
