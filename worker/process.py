"""One worker process: claim -> fetch -> judge -> report, with a heartbeat thread and graceful stop.

Exit codes (the supervisor reads them):
  0   normal stop
  75  superseded: another process registered under the same worker name
  78  configuration / authentication error (token revoked or wrong): retrying fast is pointless
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import resource
import signal
import subprocess
import threading
import time
from typing import Optional

from protocol.manifest import ManifestError
from runner.config import RunnerConfig
from runner.judge import Judge, JudgeCancelled
from runner.languages import get_profile

from .cache import DiskCache, DiskJuryCache, IntegrityError
from .client import ApiAuthError, ApiClient, ApiConflict, ApiError, ApiNotFound, ApiTransient
from .config import WorkerConfig
from .materialize import QUIZ_TYPES, ProblemStore
from .results import code_result, internal_error_result, quiz_result
from .topology import WorkerSlot, apply_affinity, format_cpu_list

log = logging.getLogger("judge.worker")
EXIT_OK, EXIT_SUPERSEDED, EXIT_FATAL = 0, 75, 78
HEARTBEAT_GIVE_UP_S = 60.0  # no successful heartbeat this long: the server has surely requeued the job


def toolchain_id() -> str:
    """Changes whenever the compilers change, so cached checkers are rebuilt."""
    parts = []
    for tool in ("/usr/bin/g++", "/usr/bin/gcc"):
        try:
            out = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        parts.append(out.splitlines()[0] if out else "")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class WorkerProcess:
    def __init__(self, cfg: WorkerConfig, slot: WorkerSlot, client: Optional[ApiClient] = None,
                 hb_client: Optional[ApiClient] = None, judge: Optional[Judge] = None,
                 cache: Optional[DiskCache] = None) -> None:
        self.cfg, self.slot = cfg, slot
        self.name = f"{cfg.machine}-w{slot.index}"
        self.client = client or ApiClient(cfg.api_url, cfg.token, cfg.request_timeout_s)
        self.hb_client = hb_client or ApiClient(cfg.api_url, cfg.token, cfg.request_timeout_s)
        self.cache = cache or DiskCache(cfg.cache_dir)
        self.judge = judge or Judge(
            RunnerConfig(backend=cfg.backend, isolate_bin=cfg.isolate_bin, box_id_base=slot.box_base,
                         toolchain_id=toolchain_id()),
            jury_cache=DiskJuryCache(self.cache))
        self.problems = ProblemStore(self.cache, self.client)
        self.stop = threading.Event()  # finish the current job, then exit
        self.abort = threading.Event()  # give the current job back now
        self.lease_lost = threading.Event()  # the server no longer considers the job ours
        self.fatal: Optional[int] = None  # exit code requested by the heartbeat thread
        self._lock = threading.Lock()
        self._state = {"state": "idle", "judgement_id": None, "attempt": None, "phase": None, "done": None, "total": None}
        self._loaded = None
        self._stats = {"jobs_done": 0, "jobs_failed": 0, "jobs_released": 0, "last_job_ms": None, "last_error": None}
        self._last_hb_ok = time.monotonic()
        self._hb_stop = threading.Event()

    # ------------------------------------------------------------------ state shared with the heartbeat
    def _set(self, **changes) -> None:
        with self._lock:
            self._state.update(changes)

    def _snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state)
        stats = dict(self._stats, cache=self.cache.counters.snapshot(),
                     rss_kb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return {"state": state, "stats": stats}

    def _progress(self, phase: str, done: int, total: int) -> None:
        self._set(phase=phase, done=done, total=total)

    # ------------------------------------------------------------------ heartbeat thread
    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.wait(self.cfg.heartbeat_interval_s):
            snap = self._snapshot()
            st = snap["state"]
            try:
                reply = self.hb_client.heartbeat(st["state"], st["judgement_id"], st["attempt"], st["phase"],
                                                 st["done"], st["total"], snap["stats"])
                self._last_hb_ok = time.monotonic()
                if reply.get("cancel"):
                    log.warning("server cancelled judgement %s (lease lost)", st["judgement_id"])
                    self.lease_lost.set()
                if self._loaded is not None and st["judgement_id"] is not None:
                    self.problems.touch(self._loaded)
            except ApiConflict as exc:
                if exc.code == "superseded":
                    log.error("superseded by another process with the same name: stopping")
                    self.fatal = EXIT_SUPERSEDED
                    self.stop.set()
                    self.abort.set()
                else:
                    self.lease_lost.set()
            except ApiAuthError as exc:
                log.error("authentication failed: %s", exc)
                self.fatal = EXIT_FATAL
                self.stop.set()
                self.abort.set()
            except ApiError as exc:
                log.warning("heartbeat failed: %s", exc)
                if st["judgement_id"] is not None and time.monotonic() - self._last_hb_ok > HEARTBEAT_GIVE_UP_S:
                    log.error("no heartbeat acknowledged for %.0fs: dropping the job", HEARTBEAT_GIVE_UP_S)
                    self.lease_lost.set()

    # ------------------------------------------------------------------ main loop
    def run(self) -> int:
        if self.cfg.pin_cpus and self.slot.cpus:
            apply_affinity(self.slot.cpus)
        info = {"pid": os.getpid(), "cpus": format_cpu_list(self.slot.cpus), "boxes": list(self.slot.boxes),
                "backend": self.cfg.backend, "machine": self.cfg.machine}
        try:
            self.client.register(self.name, list(self.cfg.capabilities), info)
        except ApiAuthError as exc:
            log.error("cannot register: %s", exc)
            return EXIT_FATAL
        except ApiError as exc:
            log.error("cannot register: %s", exc)
            return 1
        self.hb_client.worker_id, self.hb_client.instance = self.client.worker_id, self.client.instance
        log.info("registered as %s (id %s), cpus=%s boxes=%s", self.name, self.client.worker_id, info["cpus"], info["boxes"])
        threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        threading.Thread(target=self._drain_watchdog, name="drain", daemon=True).start()
        delay = 1.0
        try:
            while not self.stop.is_set():
                try:
                    job = self.client.claim()
                except ApiAuthError as exc:
                    log.error("authentication failed: %s", exc)
                    return EXIT_FATAL
                except ApiConflict as exc:
                    log.error("superseded: %s", exc)
                    return EXIT_SUPERSEDED
                except ApiError as exc:
                    log.warning("claim failed: %s (retry in %.0fs)", exc, delay)
                    self.stop.wait(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                delay = 1.0
                if job is None:
                    self.stop.wait(self.cfg.poll_interval_s * (0.75 + random.random() / 2))
                    continue
                self.run_job(job)
        finally:
            self._hb_stop.set()
            self._set(state="stopped", judgement_id=None)
            try:
                self.client.deregister()
            except ApiError:
                pass
            log.info("stopped")
        return self.fatal if self.fatal is not None else EXIT_OK

    def _drain_watchdog(self) -> None:
        self.stop.wait()
        if self._hb_stop.wait(self.cfg.drain_timeout_s):
            return
        log.warning("drain timeout (%.0fs): giving the current job back", self.cfg.drain_timeout_s)
        self.abort.set()

    # ------------------------------------------------------------------ one job
    def run_job(self, job: dict) -> None:
        jid, attempt = job["judgement_id"], job["attempt"]
        started = time.monotonic()
        deadline = started + min(self.cfg.job_deadline_s, float(job.get("deadline_s") or self.cfg.job_deadline_s))
        self.lease_lost.clear()
        self._set(state="busy", judgement_id=jid, attempt=attempt, phase="compiling", done=0, total=None)
        timed_out = threading.Event()

        def cancel() -> bool:
            if time.monotonic() > deadline:
                timed_out.set()
            return self.abort.is_set() or self.lease_lost.is_set() or timed_out.is_set()

        log.info("claimed judgement=%s attempt=%s submission=%s", jid, attempt, job.get("submission_id"))
        fetch_ms = judge_ms = 0
        try:
            t0 = time.monotonic()
            loaded = self.problems.get(job["problem_version_id"], job["manifest_sha256"])
            self._loaded = loaded
            problem = loaded.problem
            if problem.type in QUIZ_TYPES:
                payload = quiz_result(problem, job.get("answers"))
                fetch_ms = int((time.monotonic() - t0) * 1000)
            else:
                data = self.client.blob_bytes(job["source_sha256"])
                fetch_ms = int((time.monotonic() - t0) * 1000)
                t1 = time.monotonic()
                language = job["language"]
                get_profile(language)  # unknown language -> ValueError -> IE below
                report = self.judge.judge_submission(problem, language, data, is_archive=bool(job.get("is_archive")),
                                                     entry=job.get("entry_file"), cancel=cancel, progress=self._progress)
                judge_ms = int((time.monotonic() - t1) * 1000)
                payload = code_result(report, problem, language, loaded.test_ids)
        except JudgeCancelled:
            self._finish_cancelled(job, timed_out.is_set())
            return
        except ApiTransient as exc:
            log.warning("judgement %s: backend unreachable while preparing (%s): giving it back", jid, exc)
            self._release(job, "backend_unreachable")
            self.stop.wait(2.0)
            return
        except ApiConflict:
            log.warning("judgement %s: lease lost while preparing", jid)
            return
        except (ManifestError, IntegrityError, ApiNotFound) as exc:
            log.error("judgement %s: problem data is broken: %s", jid, exc)
            payload = internal_error_result(f"problem data cannot be loaded: {exc}")
        except Exception as exc:  # noqa: BLE001 - a bug must not lose the job silently
            log.exception("judgement %s: unexpected failure", jid)
            payload = internal_error_result(f"{type(exc).__name__}: {exc}")
        finally:
            self._loaded = None

        payload.update(attempt=attempt, timing={"fetch_ms": fetch_ms, "judge_ms": judge_ms})
        self._deliver(job, payload, started)

    def _deliver(self, job: dict, payload: dict, started: float) -> None:
        jid = job["judgement_id"]
        try:
            self.client.submit_result(jid, payload, retry_for_s=self.cfg.result_retry_s)
            self._stats["jobs_done"] += 1
            self._stats["last_job_ms"] = int((time.monotonic() - started) * 1000)
            log.info("done judgement=%s verdict=%s score=%s tests=%d fetch_ms=%s judge_ms=%s total_ms=%s", jid,
                     payload["verdict"], payload["score"], len(payload["tests"]), payload["timing"]["fetch_ms"],
                     payload["timing"]["judge_ms"], self._stats["last_job_ms"])
        except ApiConflict as exc:
            log.warning("result of judgement %s rejected (%s): somebody else owns it now", jid, exc)
            self._stats["jobs_failed"] += 1
        except ApiError as exc:
            log.error("result of judgement %s could not be delivered: %s", jid, exc)
            self._stats["jobs_failed"] += 1
            self._stats["last_error"] = str(exc)[:200]
        finally:
            self._set(state="draining" if self.stop.is_set() else "idle", judgement_id=None, attempt=None,
                      phase=None, done=None, total=None)

    def _finish_cancelled(self, job: dict, timed_out: bool) -> None:
        jid = job["judgement_id"]
        if self.lease_lost.is_set():
            log.warning("judgement %s abandoned: the server took the job back", jid)
        elif timed_out:
            log.error("judgement %s exceeded the deadline (%.0fs): reporting an internal error", jid, self.cfg.job_deadline_s)
            payload = internal_error_result(f"judging exceeded the worker deadline of {self.cfg.job_deadline_s:.0f}s")
            payload.update(attempt=job["attempt"], timing={})
            self._deliver(job, payload, time.monotonic())
            return
        else:
            log.warning("judgement %s given back (worker is stopping)", jid)
            self._release(job, "worker_stopping")
        self._set(state="draining" if self.stop.is_set() else "idle", judgement_id=None, attempt=None,
                  phase=None, done=None, total=None)

    def _release(self, job: dict, reason: str) -> None:
        try:
            self.client.release(job["judgement_id"], job["attempt"], reason)
            self._stats["jobs_released"] += 1
        except ApiError as exc:
            log.warning("release of judgement %s failed (%s): the server will requeue it by timeout", job["judgement_id"], exc)
        self._set(state="draining" if self.stop.is_set() else "idle", judgement_id=None, attempt=None,
                  phase=None, done=None, total=None)

    # ------------------------------------------------------------------ signals
    def install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            if self.stop.is_set():
                log.warning("second signal: aborting the current job")
                self.abort.set()
            else:
                log.info("signal %s: finishing the current job, then exiting", signum)
                self.stop.set()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
