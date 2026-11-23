#!/usr/bin/env python3
"""Local offline post-mortem, trace correlator, and regression analyzer for PLE eval runs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_REPO = "g3ffr0-netizen/ple_submission"

CATEGORY_TITLES: Dict[str, str] = {
    "hard_constraint_h1": "Hard Constraint / Gate Misses ([h=1] & Mandatory Gates)",
    "ifc_geometry_clearance": "IFC / BIM Geometry, Connectivity, Proxy & Clearance Rules",
    "register_schema_reconciliation": "Register / Schema / Total Reconciliation Gaps",
    "rubric_coverage_gaps": "Rubric Coverage & Justification Gaps",
    "turn_budget_exhaustion": "Turn-Budget Exhaustion & Premature Truncation",
    "infra_api_faults": "Infra, Disk (ENOSPC), & API Quota/Server Faults (429/5xx)",
}

_H1_RE = re.compile(r"\[h\s*=\s*1\]|\bhard[_ -]?constraint\b|\bmandatory\b", re.I)
_HTTP_RE = re.compile(r'"HTTP/1\.[01]\s+(\d{3})[^"]*"')
_AUDIT_RE = re.compile(r"harness:\s*(audit(?:\(post-fix\))?):\s*(.*)")
_ROLE_TRUNC_RE = re.compile(
    r"harness:\s*(worker|verifier|fix):\s*truncated=(True|False)\b(.*)$",
    re.I,
)
_MAX_TURNS_RE = re.compile(r"step reached max_turns=(\d+)")


def find_gh_executable() -> Optional[str]:
    """Locate the GitHub CLI binary on PATH or standard Windows install location."""
    found = shutil.which("gh")
    if found:
        return found
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        cand = Path(local_app) / "Programs" / "gh" / "bin" / "gh.exe"
        if cand.is_file():
            return str(cand)
    return None


def download_run_artifacts(
    run_ids: Iterable[str],
    cache_dir: Path,
    *,
    repo: str = DEFAULT_REPO,
    artifact_filter: Optional[str] = None,
    force: bool = False,
) -> List[Path]:
    """Download GitHub Actions artifacts for one or more workflow run IDs."""
    gh_bin = find_gh_executable()
    if not gh_bin:
        raise FileNotFoundError(
            "GitHub CLI ('gh') was not found on PATH or %LOCALAPPDATA%\\Programs\\gh\\bin\\gh.exe"
        )
    cache_dir.mkdir(parents=True, exist_ok=True)
    downloaded_dirs: List[Path] = []
    for raw_id in run_ids:
        run_id = str(raw_id).strip()
        if not run_id:
            continue
        dest = cache_dir / f"run_{run_id}"
        has_files = dest.is_dir() and any(dest.rglob("*.json"))
        if force or not has_files:
            dest.mkdir(parents=True, exist_ok=True)
            cmd = [gh_bin, "run", "download", run_id, "--repo", repo, "-D", str(dest)]
            if artifact_filter:
                cmd.extend(["-n", artifact_filter])
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0 and not any(dest.rglob("*")):
                raise RuntimeError(
                    f"gh run download {run_id} failed ({proc.returncode}): "
                    f"{(proc.stderr or proc.stdout).strip()}"
                )
        downloaded_dirs.append(dest)
    return downloaded_dirs


def parse_json_objects(text: str) -> List[Any]:
    """Extract all valid JSON objects/arrays from potentially concatenated or noisy text."""
    if not text or not text.strip():
        return []
    stripped = text.strip()
    try:
        return [json.loads(stripped)]
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    objs: List[Any] = []
    idx = 0
    length = len(text)
    while idx < length:
        brace = text.find("{", idx)
        bracket = text.find("[", idx)
        starts = [pos for pos in (brace, bracket) if pos >= 0]
        if not starts:
            break
        start = min(starts)
        try:
            obj, end = decoder.raw_decode(text, start)
            objs.append(obj)
            idx = max(end, start + 1)
        except json.JSONDecodeError:
            idx = start + 1
    return objs


def _infer_run_id_from_path(path: Path) -> str:
    for part in (path.name, *[p.name for p in path.parents]):
        m = re.search(r"(?:^|[_-])(\d{8,})(?:$|[_-])", part)
        if m:
            return m.group(1)
    return path.name or "unknown"


def discover_task_bundles(dirs: Iterable[Path]) -> List[Tuple[str, str, Path]]:
    """Locate unique (run_id, task_id, task_root_path) tuples across artifact directories."""
    selected: Dict[Tuple[str, str], Tuple[int, Path]] = {}
    for root in dirs:
        root_path = Path(root)
        if not root_path.exists():
            continue
        run_hint = _infer_run_id_from_path(root_path)
        candidates: List[Tuple[str, str, int, Path]] = []

        # Direct single-task folder layout: <root>/results.json + <root>/logs/...
        if (root_path / "results.json").is_file() or any(root_path.glob("logs/*_verifier_*")):
            res_objs = parse_json_objects(
                (root_path / "results.json").read_text(encoding="utf-8", errors="replace")
            ) if (root_path / "results.json").is_file() else []
            tasks = (res_objs[0].get("tasks") or []) if res_objs and isinstance(res_objs[0], dict) else []
            if len(tasks) == 1 and isinstance(tasks[0], dict) and tasks[0].get("id"):
                candidates.append((run_hint, str(tasks[0]["id"]), 2, root_path))

        # Standard ple-task-<task_id>-<run_id> folders (both top-level and inside ple-dev-eval-<run>/logs/)
        for sub in sorted(root_path.rglob("ple-task-*")):
            if not sub.is_dir():
                continue
            m = re.match(r"^ple-task-(.+?)-(\d{6,})$", sub.name)
            if not m:
                continue
            tid, rid = m.group(1), m.group(2)
            # Prefer standalone ple-task-* over nested bundle copy
            priority = 1 if "ple-dev-eval-" in sub.as_posix() else 3
            if any(sub.rglob("*.json")) or any(sub.rglob("*.txt")) or any(sub.rglob("*.log")):
                candidates.append((rid, tid, priority, sub))

        for rid, tid, prio, cpath in candidates:
            key = (rid, tid)
            prev = selected.get(key)
            if prev is None or prio > prev[0]:
                selected[key] = (prio, cpath)

    return [(rid, tid, path) for (rid, tid), (_, path) in sorted(selected.items())]


def _collect_log_dirs(task_dir: Path, task_id: str, run_id: str) -> List[Path]:
    dirs: List[Path] = []
    if (task_dir / "logs").is_dir():
        dirs.append(task_dir / "logs")
    # Check sibling combined bundle if task_dir is inside ple-dev-eval-<run> or has peer combined logs
    dirs.append(task_dir)
    seen: set[Path] = set()
    unique: List[Path] = []
    for d in dirs:
        rp = d.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(d)
    _ = (task_id, run_id)
    return unique


def _parse_rubric_items(objs: List[Any], raw_texts: List[str]) -> Dict[str, Any]:
    """Extract per-criterion rubric deductions, [h=1] hard flags, and judge errors."""
    deductions: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    judge_errors: List[str] = []

    def _visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("grader_failed"):
                judge_errors.append(str(node["grader_failed"])[:300])
            for list_key in (
                "criteria",
                "rubric",
                "items",
                "results",
                "evaluations",
                "criterion_results",
                "scores",
            ):
                val = node.get(list_key)
                if isinstance(val, list):
                    for item in val:
                        if not isinstance(item, dict):
                            continue
                        label = str(
                            item.get("name")
                            or item.get("criterion")
                            or item.get("title")
                            or item.get("id")
                            or item.get("requirement")
                            or ""
                        ).strip()
                        if not label:
                            continue
                        score = item.get("score")
                        if score is None:
                            score = item.get("points")
                        if score is None:
                            score = item.get("awarded")
                        max_s = item.get("max_score")
                        if max_s is None:
                            max_s = item.get("weight")
                        if max_s is None:
                            max_s = item.get("possible")
                        passed = item.get("passed")
                        if passed is None:
                            passed = item.get("pass")
                        reason = str(
                            item.get("reason")
                            or item.get("rationale")
                            or item.get("explanation")
                            or item.get("feedback")
                            or item.get("justification")
                            or ""
                        ).strip()
                        is_hard = bool(
                            item.get("hard")
                            or item.get("is_hard")
                            or item.get("mandatory")
                            or _H1_RE.search(label + " " + reason)
                        )
                        is_deducted = False
                        if isinstance(passed, bool) and not passed:
                            is_deducted = True
                        elif isinstance(score, (int, float)) and not isinstance(score, bool):
                            if isinstance(max_s, (int, float)) and not isinstance(max_s, bool) and max_s > 0:
                                if float(score) < float(max_s) - 1e-6:
                                    is_deducted = True
                            elif float(score) < 1.0 - 1e-6:
                                is_deducted = True
                        if is_deducted and label not in seen_keys:
                            seen_keys.add(label)
                            deductions.append(
                                {
                                    "criterion": label[:180],
                                    "score": score,
                                    "max_score": max_s,
                                    "hard": is_hard,
                                    "reason": reason[:280],
                                }
                            )
            for v in node.values():
                _visit(v)
        elif isinstance(node, list):
            for el in node:
                _visit(el)

    for obj in objs:
        _visit(obj)

    for txt in raw_texts:
        for line in txt.splitlines():
            low = line.lower()
            if (
                ("rate_limit" in low or "quota exceeded" in low or "http 429" in low or '"code": 429' in low)
                and len(judge_errors) < 5
            ):
                clean = line.strip()[:260]
                if clean not in judge_errors:
                    judge_errors.append(clean)
            if _H1_RE.search(line) and any(tok in low for tok in ("fail", "miss", "0/", "0.0", "false", "deduct", "violat")):
                clean = line.strip()[:200]
                if clean and clean not in seen_keys:
                    seen_keys.add(clean)
                    deductions.append(
                        {
                            "criterion": clean,
                            "score": 0.0,
                            "max_score": 1.0,
                            "hard": True,
                            "reason": clean,
                        }
                    )

    return {
        "deductions": deductions,
        "judge_errors": judge_errors,
    }


def parse_agent_trace(log_files: List[Path], serve_logs: List[Path]) -> Dict[str, Any]:
    """Parse serve HTTP statuses and agent harness logs (audit, truncation, retries)."""
    http_counts: Dict[str, int] = defaultdict(int)
    for sl in serve_logs:
        for line in sl.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _HTTP_RE.search(line)
            if m:
                http_counts[m.group(1)] += 1

    audit_events: List[str] = []
    worker_truncated = False
    verifier_truncated = False
    fix_truncated = False
    max_turns_hits: List[int] = []
    model_failures: List[str] = []
    fallback_rotations: List[str] = []
    circuit_breaker_trips = 0

    for af in log_files:
        for line in af.read_text(encoding="utf-8", errors="replace").splitlines():
            am = _AUDIT_RE.search(line)
            if am:
                audit_events.append(f"{am.group(1)}: {am.group(2).strip()}"[:240])
            tm = _ROLE_TRUNC_RE.search(line)
            if tm:
                role, flag = tm.group(1).lower(), tm.group(2) == "True"
                if role == "worker" and flag:
                    worker_truncated = True
                elif role == "verifier" and flag:
                    verifier_truncated = True
                elif role == "fix" and flag:
                    fix_truncated = True
            mm = _MAX_TURNS_RE.search(line)
            if mm:
                max_turns_hits.append(int(mm.group(1)))
            if "tool circuit breaker tripped" in line.lower():
                circuit_breaker_trips += 1
            if "model call failed" in line.lower():
                model_failures.append(line.strip()[:200])
            if "rotating fallback model" in line.lower():
                fallback_rotations.append(line.strip()[:200])

    return {
        "http_counts": dict(sorted(http_counts.items())),
        "audit_events": audit_events,
        "worker_truncated": worker_truncated,
        "verifier_truncated": verifier_truncated,
        "fix_truncated": fix_truncated,
        "max_turns_hits": max_turns_hits,
        "circuit_breaker_trips": circuit_breaker_trips,
        "model_failures": model_failures[:10],
        "fallback_rotations": fallback_rotations[:10],
    }


def classify_deductions(record: Dict[str, Any]) -> List[Dict[str, str]]:
    """Classify every failure mode and deduction into generalizable root-cause classes."""
    findings: List[Dict[str, str]] = []
    gates = record.get("gates") or {}
    rule_counts = record.get("rule_counts") or {}
    failed_constraints = record.get("failed_constraints") or []
    rubric = record.get("rubric") or {}
    trace = record.get("trace") or {}
    exc_text = str(record.get("exception_text") or "")
    status = str(record.get("status") or "")

    # 1. Infra / API faults
    if "no space left on device" in exc_text.lower() or "enospc" in exc_text.lower():
        findings.append(
            {
                "category": "infra_api_faults",
                "rule": "ENOSPC",
                "detail": "Container build/eval ran out of disk space (ENOSPC).",
            }
        )
    elif exc_text or status.startswith("evaluator"):
        findings.append(
            {
                "category": "infra_api_faults",
                "rule": status or "evaluator_error",
                "detail": (exc_text or status)[:240],
            }
        )
    for jerr in rubric.get("judge_errors") or []:
        findings.append(
            {
                "category": "infra_api_faults",
                "rule": "judge_api_error",
                "detail": jerr[:240],
            }
        )
    http = trace.get("http_counts") or {}
    bad_http = {k: v for k, v in http.items() if k != "200" and v > 0}
    if bad_http or trace.get("model_failures"):
        findings.append(
            {
                "category": "infra_api_faults",
                "rule": "model_http_transient",
                "detail": f"HTTP non-200={bad_http}, failures={len(trace.get('model_failures') or [])}",
            }
        )

    # 2. Hard constraints / mandatory gates ([h=1] & checks gates)
    g1 = gates.get("gate_1_no_hard_clash")
    if g1 is False:
        findings.append(
            {
                "category": "hard_constraint_h1",
                "rule": "gate_1_no_hard_clash",
                "detail": f"Hard clash gate failed (clash_count={record.get('clash_count')}).",
            }
        )
    g2 = gates.get("gate_2_connectivity")
    if isinstance(g2, dict) and g2.get("passed") is False:
        findings.append(
            {
                "category": "hard_constraint_h1",
                "rule": "gate_2_connectivity",
                "detail": f"Connectivity gate failed: disconnected={g2.get('disconnected')}, "
                f"unserved={g2.get('unserved_rows')}, unreached={g2.get('unreached_consumers')}",
            }
        )
        findings.append(
            {
                "category": "ifc_geometry_clearance",
                "rule": "disconnected_service_network",
                "detail": f"Disconnected service networks / misclassified proxy drops: "
                f"{g2.get('disconnected')} (components={record.get('components_per_service')}, "
                f"segments={record.get('segments_per_service')})",
            }
        )
    for fc in failed_constraints:
        fc_txt = str(fc).strip()
        if fc_txt:
            findings.append(
                {
                    "category": "hard_constraint_h1",
                    "rule": "site_or_domain_constraint",
                    "detail": fc_txt[:240],
                }
            )
    if record.get("extraction_gate") is False:
        findings.append(
            {
                "category": "hard_constraint_h1",
                "rule": "extraction_gate",
                "detail": "Deliverable extraction gate failed (missing/invalid required extraction fields).",
            }
        )

    # 3. IFC geometry / clearance rules
    if (record.get("clash_count") or 0) > 0:
        findings.append(
            {
                "category": "ifc_geometry_clearance",
                "rule": "mep_hard_clash",
                "detail": f"{record['clash_count']} hard BIM/IFC clash(es) reported.",
            }
        )
    for rname, rcnt in sorted(rule_counts.items()):
        if rcnt and int(rcnt) > 0:
            findings.append(
                {
                    "category": "ifc_geometry_clearance",
                    "rule": rname,
                    "detail": f"{rname}: {rcnt} violation(s) (achieved_clearances={record.get('achieved_clearances')})",
                }
            )

    # 4. Register / schema / deliverable reconciliation gaps
    for pv in record.get("programmatic_verifiers") or []:
        if float(pv.get("score") if pv.get("score") is not None else 1.0) < 1.0 - 1e-6 or pv.get("verdict") == "fail":
            findings.append(
                {
                    "category": "register_schema_reconciliation",
                    "rule": f"programmatic_{pv.get('ver_id', 'check')}",
                    "detail": f"score={pv.get('score')}: {pv.get('detail', '')[:240]}",
                }
            )
    for err_txt in record.get("schema_errors") or []:
        findings.append(
            {
                "category": "register_schema_reconciliation",
                "rule": "schema_or_register_mismatch",
                "detail": str(err_txt)[:240],
            }
        )
    for ae in trace.get("audit_events") or []:
        if "errors=" in ae and "errors=0" not in ae:
            cat = (
                "ifc_geometry_clearance"
                if any(k in ae.lower() for k in ("ifc", "bvh", "clash", "busway", "tray", "road", "building"))
                else "register_schema_reconciliation"
            )
            findings.append(
                {
                    "category": cat,
                    "rule": "programmatic_audit_caught",
                    "detail": ae[:240],
                }
            )

    # 5. Rubric deductions ([h=1] vs general rubric coverage gaps)
    for ded in rubric.get("deductions") or []:
        cat = "hard_constraint_h1" if ded.get("hard") else "rubric_coverage_gaps"
        findings.append(
            {
                "category": cat,
                "rule": "[h=1]" if ded.get("hard") else "rubric_criterion",
                "detail": f"{ded['criterion']} (score={ded.get('score')}/{ded.get('max_score')}): {ded.get('reason', '')[:160]}",
            }
        )

    # 6. Turn-budget exhaustion (only when WORKER, VERIFIER, or FIX was truncated)
    if trace.get("worker_truncated") or trace.get("verifier_truncated") or trace.get("fix_truncated"):
        findings.append(
            {
                "category": "turn_budget_exhaustion",
                "rule": "step_turn_cap",
                "detail": (
                    f"worker_trunc={trace.get('worker_truncated')}, "
                    f"verifier_trunc={trace.get('verifier_truncated')}, "
                    f"fix_trunc={trace.get('fix_truncated')}, "
                    f"max_turns={trace.get('max_turns_hits')}"
                ),
            }
        )

    return findings


def analyze_task_bundle(run_id: str, task_id: str, task_dir: Path) -> Dict[str, Any]:
    """Parse all metrics, verifier logs, and harness traces for one task run."""
    record: Dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "path": str(task_dir),
        "model": "",
        "score": None,
        "status": "unknown",
        "seconds": None,
        "tokens": {},
        "clash_count": None,
        "violation_rate": None,
        "gates": {},
        "rule_counts": {},
        "segments_per_service": {},
        "components_per_service": {},
        "underfloor_overhead_split": {},
        "achieved_clearances": {},
        "failed_constraints": [],
        "extraction_gate": None,
        "schema_errors": [],
        "programmatic_verifiers": [],
        "exception_text": "",
        "rubric": {"deductions": [], "judge_errors": []},
        "trace": {},
        "findings": [],
    }

    res_file = task_dir / "results.json"
    if res_file.is_file():
        for obj in parse_json_objects(res_file.read_text(encoding="utf-8", errors="replace")):
            if not isinstance(obj, dict):
                continue
            if obj.get("model"):
                record["model"] = str(obj["model"])
            for t in obj.get("tasks") or []:
                if isinstance(t, dict) and (not t.get("id") or t.get("id") == task_id):
                    if t.get("score") is not None:
                        record["score"] = float(t["score"])
                    if t.get("status"):
                        record["status"] = str(t["status"])
                    if t.get("seconds") is not None:
                        record["seconds"] = t["seconds"]
                    if isinstance(t.get("tokens"), dict):
                        record["tokens"] = t["tokens"]

    search_dirs = _collect_log_dirs(task_dir, task_id, run_id)
    metrics_files: List[Path] = []
    verifier_text_files: List[Path] = []
    trial_result_files: List[Path] = []
    prog_ver_files: List[Path] = []
    serve_logs: List[Path] = []
    agent_logs: List[Path] = []

    for d in search_dirs:
        metrics_files.extend(sorted(d.glob("*_verifier_metrics.json")))
        metrics_files.extend(sorted(d.glob("*_verifier_eval.stdout.txt")))
        metrics_files.extend(sorted(d.glob("*_verifier_reward.json")))
        verifier_text_files.extend(sorted(d.glob("*_verifier_test-stdout.txt")))
        verifier_text_files.extend(sorted(d.glob("*_verifier_eval.stderr.txt")))
        verifier_text_files.extend(sorted(d.glob("*_verifier_grade.failed.json")))
        verifier_text_files.extend(sorted(d.glob("*_verifier_grade.log")))
        verifier_text_files.extend(sorted(d.glob("*_verifier_GRADER_FAILED.txt")))
        prog_ver_files.extend(sorted(d.glob("*_verifier_programmatic_ver_*.json")))
        trial_result_files.extend(sorted(d.glob("jobs_dev_*_result.json")))
        serve_logs.extend(sorted(d.glob("*_agent_serve.log")))
        agent_logs.extend(sorted(d.glob("*_agent_ple-submission.txt")))

    seen_prog_vers: set[str] = set()
    for pvf in prog_ver_files:
        m_vid = re.search(r"programmatic_(ver_[0-9a-f]+)\.json$", pvf.name, re.I)
        ver_id = m_vid.group(1) if m_vid else pvf.stem
        if ver_id in seen_prog_vers:
            continue
        seen_prog_vers.add(ver_id)
        pv_objs = parse_json_objects(pvf.read_text(encoding="utf-8", errors="replace"))
        pv_score: Optional[float] = None
        if pv_objs and isinstance(pv_objs[0], dict) and pv_objs[0].get("score") is not None:
            try:
                pv_score = float(pv_objs[0]["score"])
            except (TypeError, ValueError):
                pv_score = None
        log_path = pvf.with_suffix(".log")
        verdict = ""
        detail = ""
        if log_path.is_file():
            log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for ll in log_lines:
                if ll.lower().startswith("verdict:"):
                    verdict = ll.split(":", 1)[1].strip().lower()
                elif "[code_verifier]" in ll or (ll.strip() and not ll.lower().startswith("exit ")):
                    detail = ll.strip()
        record["programmatic_verifiers"].append(
            {
                "ver_id": ver_id,
                "score": pv_score,
                "verdict": verdict,
                "detail": detail,
            }
        )

    parsed_verifier_objs: List[Any] = []
    raw_verifier_texts: List[str] = []
    for p in metrics_files + verifier_text_files:
        txt = p.read_text(encoding="utf-8", errors="replace")
        raw_verifier_texts.append(txt)
        parsed_verifier_objs.extend(parse_json_objects(txt))

    for obj in parsed_verifier_objs:
        if not isinstance(obj, dict):
            continue
        if record["score"] is None:
            for sk in ("combined_score", "reward", "score"):
                val = obj.get(sk)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    record["score"] = float(val)
                    break
        if obj.get("exec_status"):
            record["status"] = f"{record['status']} ({obj['exec_status']})" if record["status"] not in ("unknown", "ok") else str(obj["exec_status"])
        if obj.get("clash_count") is not None:
            record["clash_count"] = obj["clash_count"]
        if obj.get("violation_rate") is not None:
            record["violation_rate"] = obj["violation_rate"]
        if isinstance(obj.get("gates"), dict) and obj["gates"]:
            record["gates"] = obj["gates"]
        if isinstance(obj.get("failed_constraints"), list):
            for fc in obj["failed_constraints"]:
                if fc not in record["failed_constraints"]:
                    record["failed_constraints"].append(fc)
        if obj.get("extraction_gate") is not None:
            record["extraction_gate"] = bool(obj["extraction_gate"])
        rep = obj.get("report")
        if isinstance(rep, dict):
            layers = rep.get("layers") or {}
            if isinstance(layers, dict):
                for lkey in ("1_schema", "2_geometry"):
                    ld = layers.get(lkey)
                    if isinstance(ld, dict) and ld.get("pass") is False:
                        for err in ld.get("errors") or []:
                            record["schema_errors"].append(err)
                l3 = layers.get("3_constraints")
                if isinstance(l3, dict):
                    for pc in l3.get("per_constraint") or []:
                        if isinstance(pc, dict) and pc.get("pass") is False:
                            desc = pc.get("nl") or pc.get("template") or pc.get("id") or "failed_constraint"
                            viols = pc.get("violations") or []
                            msg = f"{pc.get('id', '')} {desc}: {viols[:3]}".strip()
                            if msg not in record["failed_constraints"]:
                                record["failed_constraints"].append(msg)
        sec = obj.get("secondary")
        if isinstance(sec, dict):
            vd = sec.get("violation_detail") or {}
            if isinstance(vd, dict) and isinstance(vd.get("rule_counts"), dict):
                record["rule_counts"] = vd["rule_counts"]
            nc = sec.get("network_complexity") or {}
            if isinstance(nc, dict):
                if isinstance(nc.get("segments_per_service"), dict):
                    record["segments_per_service"] = nc["segments_per_service"]
                if isinstance(nc.get("components_per_service"), dict):
                    record["components_per_service"] = nc["components_per_service"]
            ext = sec.get("extended") or {}
            if isinstance(ext, dict):
                if isinstance(ext.get("achieved_clearances"), dict):
                    record["achieved_clearances"] = ext["achieved_clearances"]
                if isinstance(ext.get("underfloor_overhead_split"), dict):
                    record["underfloor_overhead_split"] = ext["underfloor_overhead_split"]

    for tr in trial_result_files:
        for obj in parse_json_objects(tr.read_text(encoding="utf-8", errors="replace")):
            if not isinstance(obj, dict):
                continue
            ei = obj.get("exception_info")
            if isinstance(ei, dict) and (ei.get("exception_type") or ei.get("exception_message")):
                et = ei.get("exception_type") or ""
                em = str(ei.get("exception_message") or "")
                snippet = f"{et}: {em[-300:] if len(em) > 300 else em}"
                if snippet not in record["exception_text"]:
                    record["exception_text"] = snippet

    record["rubric"] = _parse_rubric_items(parsed_verifier_objs, raw_verifier_texts)
    record["trace"] = parse_agent_trace(agent_logs, serve_logs)
    record["findings"] = classify_deductions(record)
    return record


def analyze_directories(dirs: Iterable[Path]) -> Dict[str, Any]:
    """Analyze all discovered tasks across `dirs` and compute aggregate root-cause breakdown."""
    bundles = discover_task_bundles(dirs)
    records = [analyze_task_bundle(rid, tid, path) for rid, tid, path in bundles]
    by_category: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for rec in records:
        for f in rec["findings"]:
            by_category[f["category"]].append(
                {
                    "run_id": rec["run_id"],
                    "task_id": rec["task_id"],
                    "rule": f["rule"],
                    "detail": f["detail"],
                }
            )
    return {
        "tasks": records,
        "by_category": dict(by_category),
    }


def build_comparison(
    records: List[Dict[str, Any]],
    run_a: str,
    run_b: str,
    task_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build side-by-side comparison records for tasks shared across `run_a` and `run_b`."""
    idx_a = {r["task_id"]: r for r in records if r["run_id"] == str(run_a)}
    idx_b = {r["task_id"]: r for r in records if r["run_id"] == str(run_b)}
    keys = [task_id] if (task_id and task_id in idx_a and task_id in idx_b) else sorted(set(idx_a) & set(idx_b))
    rows: List[Dict[str, Any]] = []
    for k in keys:
        ra, rb = idx_a[k], idx_b[k]
        rows.append(
            {
                "task_id": k,
                "run_a": run_a,
                "run_b": run_b,
                "score_a": ra.get("score"),
                "score_b": rb.get("score"),
                "status_a": ra.get("status"),
                "status_b": rb.get("status"),
                "clashes_a": ra.get("clash_count"),
                "clashes_b": rb.get("clash_count"),
                "components_a": ra.get("components_per_service"),
                "components_b": rb.get("components_per_service"),
                "segments_a": ra.get("segments_per_service"),
                "segments_b": rb.get("segments_per_service"),
                "rules_a": ra.get("rule_counts"),
                "rules_b": rb.get("rule_counts"),
                "audit_a": (ra.get("trace") or {}).get("audit_events"),
                "audit_b": (rb.get("trace") or {}).get("audit_events"),
                "clearances_a": ra.get("achieved_clearances"),
                "clearances_b": rb.get("achieved_clearances"),
            }
        )
    return rows


def format_markdown(
    report: Dict[str, Any],
    comparisons: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Format the analyzed post-mortem report as concise GitHub-flavored markdown."""
    tasks = report.get("tasks") or []
    by_cat = report.get("by_category") or {}
    lines: List[str] = ["# PLE Evaluation Post-Mortem & Upgrade Report", ""]

    if not tasks:
        lines.append("No evaluation task artifacts found.")
        return "\n".join(lines)

    lines.append("## 1. Run Summary")
    lines.append("")
    lines.append("| Run ID | Task | Score | Status | Time (s) | Clashes | Trace / Trunc | Root-Cause Classes |")
    lines.append("| :--- | :--- | ---: | :--- | ---: | ---: | :--- | :--- |")
    for t in tasks:
        sc = f"{t['score']:.3f}" if isinstance(t.get("score"), (int, float)) else "-"
        tm = str(t.get("seconds")) if t.get("seconds") is not None else "-"
        cl = str(t.get("clash_count")) if t.get("clash_count") is not None else "-"
        tr = t.get("trace") or {}
        trunc_flags = [
            f"{r}_trunc"
            for r in ("worker", "verifier", "fix")
            if tr.get(f"{r}_truncated")
        ]
        trace_col = ",".join(trunc_flags) if trunc_flags else "clean"
        cats = sorted({f["category"] for f in t.get("findings") or []})
        lines.append(
            f"| `{t['run_id']}` | `{t['task_id']}` | {sc} | `{t['status']}` | "
            f"{tm} | {cl} | `{trace_col}` | {', '.join(cats) or 'none'} |"
        )

    lines.extend(["", "## 2. Findings Grouped by Generalizable Root-Cause Class", ""])
    for cat_key, cat_title in CATEGORY_TITLES.items():
        items = by_cat.get(cat_key) or []
        if not items:
            continue
        lines.append(f"### {cat_title} (`{cat_key}`, {len(items)} finding(s))")
        for it in items:
            lines.append(f"- **`{it['run_id']}` / `{it['task_id']}`** (`{it['rule']}`): {it['detail']}")
        lines.append("")

    if comparisons:
        lines.extend(["## 3. Side-by-Side Regression Comparison", ""])
        for cmp_row in comparisons:
            lines.append(f"### Task `{cmp_row['task_id']}`: `{cmp_row['run_a']}` vs `{cmp_row['run_b']}`")
            lines.append("| Metric | Baseline (`" + str(cmp_row["run_a"]) + "`) | Candidate (`" + str(cmp_row["run_b"]) + "`) |")
            lines.append("| :--- | :--- | :--- |")
            lines.append(f"| Score | {cmp_row['score_a']} | {cmp_row['score_b']} |")
            lines.append(f"| Status | `{cmp_row['status_a']}` | `{cmp_row['status_b']}` |")
            lines.append(f"| Hard clashes | `{cmp_row['clashes_a']}` | `{cmp_row['clashes_b']}` |")
            lines.append(f"| Segments / service | `{cmp_row['segments_a']}` | `{cmp_row['segments_b']}` |")
            lines.append(f"| Components / service | `{cmp_row['components_a']}` | `{cmp_row['components_b']}` |")
            lines.append(f"| Violating rule counts | `{cmp_row['rules_a']}` | `{cmp_row['rules_b']}` |")
            lines.append(f"| Achieved clearances | `{cmp_row['clearances_a']}` | `{cmp_row['clearances_b']}` |")
            lines.append(f"| Audit / verifier logs | `{cmp_row['audit_a']}` | `{cmp_row['audit_b']}` |")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments for analyze_eval.py."""
    parser = argparse.ArgumentParser(description="Analyze PLE eval artifacts and harness traces.")
    parser.add_argument("--dirs", nargs="*", default=[], help="Local directories holding downloaded run artifacts.")
    parser.add_argument("--runs", nargs="*", default=[], help="GitHub Actions run IDs to analyze (use with --download).")
    parser.add_argument("--download", action="store_true", help="Download --runs artifacts via gh run download.")
    parser.add_argument("--force", action="store_true", help="Re-download artifacts even if cache directory exists.")
    parser.add_argument("--cache-dir", default=".eval_cache", help="Directory where downloaded runs are stored.")
    parser.add_argument("--artifact-filter", default="", help="Optional specific artifact name for gh run download.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository slug (owner/name).")
    parser.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"), help="Compare two run IDs side by side.")
    parser.add_argument("--task", default="", help="Optional task ID filter for --compare.")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format.")
    parser.add_argument("--out", default="", help="Optional output file path.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv)
    dirs: List[Path] = [Path(d) for d in args.dirs]
    if args.runs:
        cache_dir = Path(args.cache_dir)
        if args.download:
            dirs.extend(
                download_run_artifacts(
                    args.runs,
                    cache_dir,
                    repo=args.repo,
                    artifact_filter=args.artifact_filter or None,
                    force=args.force,
                )
            )
        else:
            for rid in args.runs:
                dirs.append(cache_dir / f"run_{rid}")

    report = analyze_directories(dirs)
    comparisons = (
        build_comparison(report["tasks"], args.compare[0], args.compare[1], args.task or None)
        if args.compare
        else []
    )

    if args.format == "json":
        rendered = json.dumps({"report": report, "comparisons": comparisons}, indent=2)
    else:
        rendered = format_markdown(report, comparisons=comparisons)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
