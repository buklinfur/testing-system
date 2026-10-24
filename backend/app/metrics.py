"""Prometheus text format for the queue snapshot (also readable by a human with curl)."""
from __future__ import annotations


def _line(name: str, value, labels: dict | None = None) -> str:
    if labels:
        inner = ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for k, v in sorted(labels.items()))
        return f"{name}{{{inner}}} {value}"
    return f"{name} {value}"


def render_prometheus(snap: dict) -> str:
    """snap: output of queue.collect_metrics()."""
    out = []

    def metric(name: str, help_text: str, kind: str = "gauge") -> None:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")

    metric("judge_queue_length", "Judgements waiting for a worker")
    out.append(_line("judge_queue_length", snap["queue_length"]))
    metric("judge_queue_oldest_age_seconds", "Age of the oldest queued judgement")
    out.append(_line("judge_queue_oldest_age_seconds", round(snap["oldest_queued_age_s"], 1)))
    metric("judge_in_flight", "Judgements being compiled or run")
    out.append(_line("judge_in_flight", snap["in_flight"]))
    metric("judge_workers", "Workers by state (online = heartbeat is fresh)")
    for state, n in sorted(snap["workers_by_state"].items()):
        out.append(_line("judge_workers", n, {"state": state}))
    metric("judge_typical_judge_seconds", "Median judging time of recent judgements")
    out.append(_line("judge_typical_judge_seconds", round(snap["typical_seconds"], 2)))
    metric("judge_queue_wait_seconds", "Median time a recent judgement waited in the queue")
    out.append(_line("judge_queue_wait_seconds", round(snap["median_wait_s"], 2)))
    metric("judge_finished_total", "Finished judgements by verdict (all time)", "counter")
    for verdict, n in sorted(snap["finished_by_verdict"].items()):
        out.append(_line("judge_finished_total", n, {"verdict": verdict}))
    metric("judge_requeued_total", "Judgements returned to the queue after a lost worker or internal error", "counter")
    out.append(_line("judge_requeued_total", snap["requeued_total"]))
    return "\n".join(out) + "\n"
