"""Turn a manifest into a Problem the runner can load, using the shared disk cache.

Directory layout (hard links into the blob cache, so tests exist on disk once):
  problems/<manifest sha>/problem.yaml     built from the manifest
  problems/<manifest sha>/manifest.json    kept to restore the test -> database id mapping
  problems/<manifest sha>/tests/<g>/<t>.in|.out
  problems/<manifest sha>/checker/<name>, interactor/<name>   jury sources
The runner reads `problem.yaml`; the test list is then replaced by the manifest's (database) order,
so names never have to be unique across files and the order is exactly the database order.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import yaml

from protocol.manifest import ManifestError, manifest_digest, validate_manifest
from runner.package import Problem, TestFile

from .cache import DiskCache

log = logging.getLogger("judge.materialize")
QUIZ_TYPES = ("choice", "text_answer")


@dataclass
class LoadedProblem:
    problem: Problem
    manifest_sha256: str
    test_ids: list  # database test_case id for every test, in runner order (index - 1)


def _safe_name(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/")) or "file"
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:80]


def _link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def build_problem_yaml(manifest: dict) -> dict:
    """The problem.yaml the runner expects, assembled from database data."""
    data = dict(manifest["config"])
    data.update(slug=manifest["slug"], type=manifest["type"], title=manifest["titles"] or {"ru": manifest["slug"]})
    data.setdefault("format_version", 1)
    data["test_groups"] = [{
        "name": g["name"], "points": g["points"], "scoring": g["scoring"], "visibility": g["visibility"],
        "depends_on": g["depends_on"], "feedback": g["feedback"], "show_in_statement": g["show_in_statement"],
        "tests": [],  # filled from the manifest after loading
    } for g in manifest["groups"]]
    programs = {p["role"]: p for p in manifest["programs"]}
    if "checker" in data and data["checker"].get("type") == "custom":
        prog = programs.get("checker")
        if prog is None:
            raise ManifestError("custom checker declared but no checker program in the manifest")
        data["checker"] = dict(data["checker"], path=f"checker/{_safe_name(prog['name'])}", language=prog["language"])
    if manifest["type"] == "interactive":
        prog = programs.get("interactor")
        if prog is None:
            raise ManifestError("interactive problem without an interactor program")
        data["interactor"] = {"path": f"interactor/{_safe_name(prog['name'])}", "language": prog["language"]}
    return data


class ProblemStore:
    def __init__(self, cache: DiskCache, client, lru_size: int = 8) -> None:
        self.cache, self.client = cache, client
        self._lru: OrderedDict = OrderedDict()
        self._lru_size = lru_size

    def get(self, version_id: int, manifest_sha256: str) -> LoadedProblem:
        if manifest_sha256 in self._lru:
            self._lru.move_to_end(manifest_sha256)
            self.cache.touch(self.cache.problem_dir(manifest_sha256))
            self.cache.counters.add("problem_hits")
            return self._lru[manifest_sha256]
        final = self.cache.problem_dir(manifest_sha256)
        if not final.is_dir():
            with self.cache.lock(f"problem-{manifest_sha256[:16]}"):
                if not final.is_dir():
                    self.cache.counters.add("problem_misses")
                    manifest_sha256 = self._build(version_id, manifest_sha256)
                    final = self.cache.problem_dir(manifest_sha256)
        else:
            self.cache.counters.add("problem_hits")
        self.cache.touch(final)
        loaded = self._load(final, manifest_sha256)
        self._lru[manifest_sha256] = loaded
        while len(self._lru) > self._lru_size:
            self._lru.popitem(last=False)
        return loaded

    def touch(self, loaded: LoadedProblem) -> None:
        """Called from the heartbeat: keeps the directory away from eviction while a job uses it."""
        self.cache.touch(self.cache.problem_dir(loaded.manifest_sha256))

    # ---------------------------------------------------------------- building
    def _fetch(self, sha: str) -> Path:
        return self.cache.get_blob(sha, lambda write, sha=sha: self.client.download_blob(sha, write))

    def _build(self, version_id: int, expected_sha: str) -> str:
        manifest = self.client.manifest(version_id)["manifest"]
        validate_manifest(manifest)
        actual = manifest_digest(manifest)
        if actual != expected_sha:
            log.warning("manifest of version %s changed since the claim (%s -> %s)", version_id, expected_sha[:12], actual[:12])
        final = self.cache.problem_dir(actual)
        if final.is_dir():
            return actual
        data = build_problem_yaml(manifest)
        tmp = self.cache.new_problem_tmp()
        try:
            (tmp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            (tmp / "problem.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            for p in manifest["programs"]:
                folder = "checker" if p["role"] == "checker" else "interactor"
                _link(self._fetch(p["source_sha256"]), tmp / folder / _safe_name(p["name"]))
            for g in manifest["groups"]:
                for t in g["tests"]:
                    _link(self._fetch(t["input_sha256"]), tmp / "tests" / str(g["position"]) / f"{t['position']}.in")
                    if t["answer_sha256"]:
                        _link(self._fetch(t["answer_sha256"]), tmp / "tests" / str(g["position"]) / f"{t['position']}.out")
            self.cache.publish_problem(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return actual

    def _load(self, root: Path, manifest_sha256: str) -> LoadedProblem:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        problem = Problem.load(root)
        by_name = {g.name: g for g in problem.groups}
        ids: list = []
        for g in manifest["groups"]:
            group = by_name[g["name"]]
            group.tests = []
            for t in g["tests"]:
                base = root / "tests" / str(g["position"]) / str(t["position"])
                answer = base.with_suffix(".out")
                group.tests.append(TestFile(group.name, t["name"], base.with_suffix(".in"),
                                            answer if t["answer_sha256"] else None))
                ids.append(t["id"])
        return LoadedProblem(problem, manifest_sha256, ids)
