"""Background worker that drains the persistent job queue at a steady, low
pressure pace.

Design principles (matches business profile of write-heavy / read-rare):

1. Single worker, never parallel. We trade throughput for stability — one
   ingest at a time means subprocess memory peaks and embedding API bursts
   are naturally serialized.

2. Sleep between jobs (default 10s). Even when the queue is full the worker
   refuses to thrash; the host other tenants (GitLab, sshd, dockerd) get
   breathing room.

3. Pre-flight system health check before claiming any job. If memory is low
   or load is high, the worker sleeps a longer interval and re-checks
   instead of running a job that would worsen the situation. This is
   what prevents the 2026-04-30 cascade from recurring: the queue keeps
   draining, just slower, never faster than the host can absorb.

4. Subprocess timeout enforced; on timeout the worker treats it as a failed
   attempt and lets the queue's exponential backoff handle retries.

Lifecycle: started by FastAPI lifespan() at app startup, stop_event signalled
during shutdown for graceful drain (worker finishes current job then exits).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

try:
    from job_queue import Job, JobQueue
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.job_queue import Job, JobQueue

try:
    from server_protection import BG_TRACKER, GATE, read_system_health
except ModuleNotFoundError:  # pragma: no cover
    from scripts.server_protection import BG_TRACKER, GATE, read_system_health

logger = logging.getLogger("transcendence-memory-server.worker")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


class JobWorker:
    """Single-threaded queue worker.

    Parameters are read from env vars at construction; tests override by
    constructing with explicit args.
    """

    def __init__(
        self,
        queue: JobQueue,
        command_resolver: Callable[[Job], list[str]],
        env_resolver: Callable[[Job], dict[str, str]] | None = None,
        idle_interval_sec: int | None = None,
        between_jobs_sec: int | None = None,
        pressure_backoff_sec: int | None = None,
        job_timeout_sec: int | None = None,
    ) -> None:
        self.queue = queue
        self.command_resolver = command_resolver
        self.env_resolver = env_resolver or (lambda _job: os.environ.copy())
        self.idle_interval_sec = idle_interval_sec or _env_int("TM_WORKER_IDLE_SEC", 5)
        self.between_jobs_sec = between_jobs_sec or _env_int("TM_WORKER_INTERVAL_SEC", 10)
        self.pressure_backoff_sec = pressure_backoff_sec or _env_int(
            "TM_WORKER_PRESSURE_BACKOFF_SEC", 60
        )
        self.job_timeout_sec = job_timeout_sec or _env_int("TM_WORKER_JOB_TIMEOUT_SEC", 1800)
        # Periodic queue compaction: drop terminal-state rows older than this
        # so SQLite doesn't grow forever. Cadence-checked once per N ticks.
        self.purge_interval_sec = _env_int("TM_QUEUE_PURGE_INTERVAL_SEC", 3600)
        self.purge_retention_sec = _env_int("TM_QUEUE_PURGE_RETENTION_SEC", 7 * 86400)
        self._last_purge_ts = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tm-job-worker", daemon=True)
        self._thread.start()
        logger.info(
            "worker started: idle=%ss between=%ss pressure_backoff=%ss timeout=%ss",
            self.idle_interval_sec,
            self.between_jobs_sec,
            self.pressure_backoff_sec,
            self.job_timeout_sec,
        )

    def stop(self, join_timeout: float = 30.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=join_timeout)
            if self._thread.is_alive():
                logger.warning("worker thread did not finish within %ss", join_timeout)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------- main loop ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # pragma: no cover - defensive top-level guard
                logger.exception("worker tick crashed: %s", exc)
                # Don't tight-loop on repeated failures
                self._sleep(self.between_jobs_sec)

    def _tick(self) -> None:
        # Periodic queue compaction (no-op when called within retention window).
        self._maybe_purge()

        # Pre-flight: refuse to claim a job if the host is under pressure.
        # The job stays in the queue; we'll check again next tick.
        snap = read_system_health()
        admit_ok, reason = GATE.check_admit(snap)
        if not admit_ok:
            logger.info("worker idle (system pressure): %s", reason)
            self._sleep(self.pressure_backoff_sec)
            return

        job = self.queue.claim_next()
        if job is None:
            self._sleep(self.idle_interval_sec)
            return

        logger.info(
            "worker running job id=%s op=%s container=%s attempt=%s/%s",
            job.id, job.op, job.container, job.attempts, job.max_attempts,
        )
        self._execute(job)
        # Steady-pace sleep between jobs — this is the lever that keeps
        # embedding API calls from bursting.
        self._sleep(self.between_jobs_sec)

    def _maybe_purge(self) -> None:
        if self.purge_interval_sec <= 0:
            return
        now = time.time()
        if (now - self._last_purge_ts) < self.purge_interval_sec:
            return
        self._last_purge_ts = now
        try:
            removed = self.queue.purge_done(older_than_sec=self.purge_retention_sec)
            if removed:
                logger.info("queue purge removed %d old rows", removed)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("queue purge failed: %s", exc)

    def _execute(self, job: Job) -> None:
        try:
            cmd = self.command_resolver(job)
        except Exception as exc:
            self.queue.mark_failed(job.id, f"command_resolver error: {exc}")
            return
        if not cmd:
            self.queue.mark_failed(job.id, "command_resolver returned empty command")
            return
        # Auto-prepend python interpreter for .py scripts (mirrors task_rag_server.run)
        real_cmd = [sys.executable, *cmd] if cmd[0].endswith(".py") else cmd
        env = self.env_resolver(job)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            proc = subprocess.Popen(
                real_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except Exception as exc:
            self.queue.mark_failed(job.id, f"failed to spawn subprocess: {exc}")
            return
        self.queue.attach_pid(job.id, proc.pid)
        # Register with the global tracker so admit-gate sees queue jobs too.
        BG_TRACKER.register(proc.pid, job.container, label=f"queue:{job.op}#{job.id}")
        try:
            stdout, stderr = proc.communicate(timeout=self.job_timeout_sec)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=10)
            except Exception:  # pragma: no cover - defensive
                pass
            BG_TRACKER.unregister(proc.pid)
            self.queue.mark_failed(
                job.id,
                f"timeout after {self.job_timeout_sec}s",
            )
            return
        finally:
            # Even on success path we want this; the unregister above for
            # timeout handles its own path. Re-call here is idempotent.
            BG_TRACKER.unregister(proc.pid)
        rc = proc.returncode
        if rc == 0:
            note = (stdout or "")[:512]
            self.queue.mark_done(job.id, result_code=0, note=note)
        else:
            tail = (stderr or stdout or "").strip()[-1024:]
            self.queue.mark_failed(
                job.id,
                f"exit {rc}: {tail}" if tail else f"exit {rc}",
            )

    def _sleep(self, seconds: int) -> None:
        # Use Event.wait for snappy shutdown response
        self._stop.wait(timeout=max(1, int(seconds)))


def default_command_resolver(scripts_dir: Path) -> Callable[[Job], list[str]]:
    """Build the default mapping from job.op to subprocess command.

    Knows about the three op types currently produced by the API:
    - 'embed' / 'ingest-memory' → task_rag_lancedb_ingest.py
    - 'ingest-structured' → task_rag_structured_ingest.py
    """
    scripts_dir = Path(scripts_dir)

    def resolve(job: Job) -> list[str]:
        if job.op in ("embed", "ingest-memory"):
            cmd = [
                str(scripts_dir / "task_rag_lancedb_ingest.py"),
                "--container", job.container,
            ]
            memory_dir = job.payload.get("memory_dir")
            archive_dir = job.payload.get("archive_dir")
            if memory_dir:
                cmd += ["--memory-dir", str(memory_dir)]
            if archive_dir:
                cmd += ["--archive-dir", str(archive_dir)]
            return cmd
        if job.op == "ingest-structured":
            cmd = [
                str(scripts_dir / "task_rag_structured_ingest.py"),
                "--container", job.container,
            ]
            input_path = job.payload.get("input_path")
            doc_type = job.payload.get("doc_type", "structured_json")
            doc_id = job.payload.get("doc_id")
            if input_path:
                cmd += ["--input", str(input_path)]
            cmd += ["--doc-type", doc_type]
            if doc_id:
                cmd += ["--doc-id", doc_id]
            return cmd
        raise ValueError(f"unknown op: {job.op}")

    return resolve
