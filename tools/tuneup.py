#!/usr/bin/env python3
"""PLE Tune-Up Shop: zero-API offline failure miner, token/loop profiler, golden replay & A/B gate."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import analyze_eval  # noqa: E402

ROOT_CAUSES: Tuple[str, ...] = (
    "missing_deliverable",
    "calc_or_unit_error",
    "citation_gap",
    "tool_or_context_loop",
)

ROOT_CAUSE_TITLES: Dict[str, str] = {
    "missing_deliverable": "Missing Deliverable / Section / Register Item",
    "calc_or_unit_error": "Calculation, Sizing, Formula, Margin & Unit Errors",
    "citation_gap": "Citation, Cross-Reference, UFC Prohibition & Formatting Gaps",
    "tool_or_context_loop": "Tool Loops, API Retries, Context Bloat & Turn Truncation",
}

DEFAULT_INPUT_PER_1K = 0.00045
DEFAULT_CACHED_PER_1K = 0.000045
DEFAULT_OUTPUT_PER_1K = 0.0036

_SECTION_NUM_RE = re.compile(r"^\d{2}\s+\d{2}\s+\d{2}(?:\.\d{2}\s+\d{2})?$")
_HTTP_STATUS_RE = re.compile(r'"HTTP/1\.[01]\s+(\d{3})[^"]*"')
_VERIFIER_LINE_RE = re.compile(
    r"^harness:\s*verifier:\s*truncated=(True|False)\b"
    r"(?:\s+audit_errors=(\d+))?(?:\s+prefilter_errors=(\d+))?(?:\s+readback_errors=(\d+))?\s*(.*)$",
    re.I,
)
_PASS_TAIL_RE = re.compile(r"^\s*\**\s*(?:\{\"verdict\"\s*:\s*\"PASS\"\}|PASS(?:ED)?)\s*\**\s*\.?\s*$", re.I)
_EXEC_STATUS_RE = re.compile(r"exit_code\s*=\s*(-?\d+)", re.I)

_TOKEN_EXTRACT_RE = re.compile(r"\b(?:[A-Za-z0-9_.-]{6,}|\d{4,})\b")


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return default
    try:
        v = float(value)
        return v
    except (TypeError, ValueError):
        return default


def _normalize_path(raw: str) -> str:
    s = raw.strip().strip("\"'").replace("\\", "/")
    if not s:
        return ""
    if s.startswith("./"):
        s = s[2:]
    try:
        norm = str(PurePosixPath(s))
        return "" if norm == "." else norm
    except Exception:
        return s


def _sha256_short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Run & Task Artifact Discovery + Truncation-Resilient JSON Parser
# ---------------------------------------------------------------------------


def discover_run_dirs(cache_dir: Path) -> List[Tuple[str, Path]]:
    """Return sorted list of (run_id, run_dir) under `cache_dir` ordered numerically."""
    if not cache_dir.is_dir():
        return []
    found: List[Tuple[int, str, Path]] = []
    for entry in cache_dir.iterdir():
        if not entry.is_dir():
            continue
        m = re.match(r"^run_(\d+)$", entry.name)
        if m:
            found.append((int(m.group(1)), m.group(1), entry))
    found.sort(key=lambda item: item[0])
    return [(rid, p) for _, rid, p in found]


def resolve_run_dir(
    cache_dir: Path,
    *,
    run_dir: Optional[str] = None,
    run_id: Optional[str] = None,
    offset_from_latest: int = 0,
) -> Optional[Path]:
    """Resolve an explicit run directory/ID or auto-pick from `cache_dir`."""
    if run_dir:
        p = Path(run_dir)
        return p if p.exists() else None
    if run_id:
        cand = cache_dir / f"run_{str(run_id).strip()}"
        return cand if cand.exists() else None
    runs = discover_run_dirs(cache_dir)
    if not runs:
        return None
    idx = len(runs) - 1 - offset_from_latest
    if 0 <= idx < len(runs):
        return runs[idx][1]
    return None


def load_run_summary(run_dir: Path) -> Dict[str, Any]:
    """Load run-level summary.json or reconstruct it from task results.json files."""
    run_id = analyze_eval._infer_run_id_from_path(run_dir)
    candidates = [
        run_dir / f"ple-dev-eval-{run_id}" / "summary.json",
        run_dir / "summary.json",
        *sorted(run_dir.rglob("summary.json")),
    ]
    for cand in candidates:
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8", errors="replace"))
                if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                    return {
                        "run_id": run_id,
                        "run_dir": str(run_dir),
                        "model": data.get("model", ""),
                        "judge_model": data.get("judge_model", ""),
                        "overall_score": _safe_float(data.get("overall_score")),
                        "passed_tasks": int(data.get("passed_tasks") or 0),
                        "measured_tasks": int(data.get("measured_tasks") or len(data["tasks"])),
                        "total_tasks": int(data.get("total_tasks") or len(data["tasks"])),
                        "tokens": dict(data.get("tokens") or {}),
                        "estimated_cost_usd": _safe_float(data.get("estimated_cost_usd"), 0.0) or 0.0,
                        "tasks": data["tasks"],
                    }
            except (OSError, json.JSONDecodeError):
                continue

    # Fallback: reconstruct from task results.json bundles
    tasks: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    tot_in = tot_cache = tot_out = 0
    model = ""
    for res_p in sorted(run_dir.rglob("results.json")):
        objs = analyze_eval.parse_json_objects(res_p.read_text(encoding="utf-8", errors="replace"))
        for ob in objs:
            if not isinstance(ob, dict):
                continue
            if ob.get("model") and not model:
                model = str(ob["model"])
            for t in ob.get("tasks") or []:
                if not isinstance(t, dict) or not t.get("id"):
                    continue
                tid = str(t["id"])
                if tid in seen_ids:
                    continue
                seen_ids.add(tid)
                tok = dict(t.get("tokens") or {})
                tot_in += int(tok.get("input") or 0)
                tot_cache += int(tok.get("cache") or 0)
                tot_out += int(tok.get("output") or 0)
                sc = _safe_float(t.get("score"))
                tasks.append(
                    {
                        "id": tid,
                        "verdict": t.get("verdict") or ("PASS" if (sc is not None and sc >= 0.5) else "FAIL"),
                        "score": sc,
                        "measured": sc is not None,
                        "status": t.get("status", "ok"),
                        "seconds": _safe_float(t.get("seconds")),
                        "tokens": tok,
                        "estimated_cost_usd": _safe_float(t.get("estimated_cost_usd"), 0.0) or 0.0,
                    }
                )
    scores = [float(t["score"]) for t in tasks if isinstance(t.get("score"), (int, float))]
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "model": model,
        "judge_model": "",
        "overall_score": (sum(scores) / len(scores)) if scores else None,
        "passed_tasks": sum(1 for t in tasks if t.get("verdict") == "PASS"),
        "measured_tasks": len(scores),
        "total_tasks": len(tasks),
        "tokens": {"input": tot_in, "cache": tot_cache, "output": tot_out},
        "estimated_cost_usd": sum(float(t.get("estimated_cost_usd") or 0.0) for t in tasks),
        "tasks": tasks,
    }


def discover_run_tasks(run_dir: Path) -> List[Dict[str, Any]]:
    """Discover per-task bundles, log files, verifier files, and trajectory paths in `run_dir`."""
    run_id = analyze_eval._infer_run_id_from_path(run_dir)
    bundles = analyze_eval.discover_task_bundles([run_dir])
    summary = load_run_summary(run_dir)
    summary_by_id = {str(t["id"]): t for t in summary.get("tasks") or [] if isinstance(t, dict) and t.get("id")}

    # Map combined-bundle fallback artifact paths if standalone ple-task-* is missing artifacts
    combined_art_root = run_dir / f"ple-dev-eval-{run_id}" / "artifacts"
    if not combined_art_root.is_dir():
        found_arts = sorted(run_dir.glob("ple-dev-eval-*/artifacts"))
        combined_art_root = found_arts[0] if found_arts else (run_dir / "artifacts")

    seen: set[str] = set()
    records: List[Dict[str, Any]] = []

    def _build_task_entry(rid: str, tid: str, task_path: Path) -> Dict[str, Any]:
        search_dirs = analyze_eval._collect_log_dirs(task_path, tid, rid)
        verifier_files: List[Path] = []
        serve_logs: List[Path] = []
        agent_logs: List[Path] = []
        prog_ver_files: List[Path] = []
        for d in search_dirs:
            verifier_files.extend(sorted(d.glob("*_verifier_metrics.json")))
            verifier_files.extend(sorted(d.glob("*_verifier_artifacts.json")))
            verifier_files.extend(sorted(d.glob("*_verifier_eval.stdout.txt")))
            verifier_files.extend(sorted(d.glob("*_verifier_reward.json")))
            verifier_files.extend(sorted(d.glob("*_verifier_paper_score.json")))
            verifier_files.extend(sorted(d.glob("*_verifier_test-stdout.txt")))
            prog_ver_files.extend(sorted(d.glob("*_verifier_programmatic_ver_*.json")))
            serve_logs.extend(sorted(d.glob("*_agent_serve.log")))
            agent_logs.extend(sorted(d.glob("*_agent_ple-submission.txt")))

        # Locate trajectory.json and final_message.md
        traj_cands = [
            task_path / "artifacts" / tid / "trajectory.json",
            task_path / "trajectory.json",
            combined_art_root / tid / "trajectory.json",
        ]
        traj_path = next((p for p in traj_cands if p.is_file()), None)
        if traj_path is None:
            rglob_t = list(task_path.rglob("trajectory.json"))
            traj_path = rglob_t[0] if rglob_t else None

        fmsg_cands = [
            task_path / "artifacts" / tid / "final_message.md",
            task_path / "final_message.md",
            combined_art_root / tid / "final_message.md",
        ]
        fmsg_path = next((p for p in fmsg_cands if p.is_file()), None)

        return {
            "run_id": rid,
            "task_id": tid,
            "task_dir": task_path,
            "summary": summary_by_id.get(tid, {}),
            "verifier_files": _dedupe_paths(verifier_files),
            "prog_ver_files": _dedupe_paths(prog_ver_files),
            "serve_logs": _dedupe_paths(serve_logs),
            "agent_logs": _dedupe_paths(agent_logs),
            "trajectory_path": traj_path,
            "final_message_path": fmsg_path,
        }

    for rid, tid, tpath in bundles:
        if tid in seen:
            continue
        seen.add(tid)
        records.append(_build_task_entry(rid, tid, tpath))

    # Also catch tasks present in direct subdirs (including <6-digit synthetic run IDs)
    for tid in sorted(summary_by_id):
        if tid not in seen:
            cands = [
                run_dir / f"ple-task-{tid}-{run_id}",
                *sorted(run_dir.glob(f"ple-task-{tid}-*")),
                run_dir / tid,
            ]
            chosen = next((c for c in cands if c.is_dir()), None)
            if chosen is not None:
                seen.add(tid)
                records.append(_build_task_entry(run_id, tid, chosen))

    return records


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    out: List[Path] = []
    seen: set[Path] = set()
    for p in paths:
        rp = p.resolve()
        if rp not in seen and p.is_file():
            seen.add(rp)
            out.append(p)
    return out


def parse_verifier_json_resilient(text: str) -> List[Dict[str, Any]]:
    """Parse standard, concatenated, or middle-truncated verifier JSON objects."""
    if not text or not text.strip():
        return []
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return [loaded]
    except json.JSONDecodeError:
        pass

    recovered: Dict[str, Any] = {}
    decoder = json.JSONDecoder()

    # 1. Extract intact top-level "key": <value> blocks even if mid-file has omitted lines
    for m in re.finditer(r'^  "([^"]+)":\s*', text, re.M):
        key = m.group(1)
        pos = m.end()
        try:
            val, _ = decoder.raw_decode(text, pos)
            recovered[key] = val
        except json.JSONDecodeError:
            continue

    # 2. Recover intact per-item family check objects inside truncated "per_item" sections
    recovered_items: Dict[str, Dict[str, Any]] = {}
    section_spans: List[Tuple[int, str]] = []
    for sm in re.finditer(r'^    "([^"]+)":\s*\{', text, re.M):
        cand_sec = sm.group(1).strip()
        if _SECTION_NUM_RE.match(cand_sec) or len(cand_sec) <= 32:
            section_spans.append((sm.start(), cand_sec))

    sec_idx = 0
    current_section = "_partial_section"
    for fm in re.finditer(r'^        "([A-Z][A-Z0-9_]{2,})":\s*\{', text, re.M):
        pos = fm.start()
        while sec_idx < len(section_spans) and section_spans[sec_idx][0] < pos:
            current_section = section_spans[sec_idx][1]
            sec_idx += 1
        brace_pos = text.find("{", pos)
        if brace_pos < 0:
            continue
        try:
            obj, _ = decoder.raw_decode(text, brace_pos)
            if isinstance(obj, dict) and "ok" in obj:
                recovered_items.setdefault(current_section, {})[fm.group(1)] = obj
        except json.JSONDecodeError:
            continue

    if recovered_items:
        recovered["_recovered_item_checks"] = recovered_items

    if recovered:
        return [recovered]

    # 3. Fallback to multi-object stream extractor
    return [ob for ob in analyze_eval.parse_json_objects(text) if isinstance(ob, dict)]


# ---------------------------------------------------------------------------
# Module 1: Rubric Failure Miner (`mine`)
# ---------------------------------------------------------------------------


def classify_root_cause(check_id: str, detail: str = "") -> str:
    """Classify a failed/partial rubric check into one of the 4 canonical root causes."""
    hay = f"{check_id} {detail}".lower()

    if any(
        tok in hay
        for tok in (
            "truncated",
            "max_turns",
            "turn_budget",
            "read_loop",
            "retry_storm",
            "tool_error",
            "timeout",
            "http 429",
            "http 503",
            "enospc",
            "grader_failed",
            "parse_ok=false",
            "parse_ok: false",
            "loop",
        )
    ):
        return "tool_or_context_loop"

    if any(
        tok in hay
        for tok in (
            "fmt_references",
            "fmt_xref",
            "fmt_no_ufc",
            "fmt_structure",
            "listed but not cited",
            "cited but not listed",
            "cross-reference",
            "cross reference",
            "xref",
            "citation",
            "cited",
            "ufc",
            "submittal",
            "justification",
            "reference",
            "basis",
            "clause",
            "standard",
        )
    ):
        return "citation_gap"

    if any(
        tok in hay
        for tok in (
            "register_missing",
            "missing_hard",
            "missing_deliverable",
            "missing ",
            "unregistered",
            "not in the register",
            "scope_exclusions",
            "studies_scope",
            "section_set",
            "extraction_gate",
            "not found",
            "absent",
            "omitted",
            "deliverable",
            "unlisted",
        )
    ):
        return "missing_deliverable"

    if any(
        tok in hay
        for tok in (
            "calc",
            "unit",
            "agreement",
            "wall_forces",
            "water_allocation",
            "net_savings",
            "margin",
            "plf",
            "kip",
            "kwh",
            "kw ",
            "gal",
            "ampacity",
            "voltage",
            "flow",
            "recomputed",
            "declared",
            "sccr",
            "tolerance",
            "mismatch",
            "disagree",
            "seismic_restraint",
            "conductors",
            "panelboards",
            "generator",
            "lighting_performance",
            "emergency_lighting",
            "spd",
            "service_tailoring",
            "lighting_controls",
            "testing_scope",
            "drift",
            "torsion",
            "deflection",
            "anchorage",
            "shear",
        )
    ) or re.search(r"\d+(?:\.\d+)?\s+vs\s+\d+(?:\.\d+)?", hay):
        return "calc_or_unit_error"

    return "calc_or_unit_error"


def _format_num(val: Any) -> str:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return f"{val:.4g}"
    return str(val)


def extract_task_rubric_checks(task_info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract normalized failed/partial rubric occurrences from one task's verifier artifacts."""
    run_id = task_info["run_id"]
    task_id = task_info["task_id"]
    occurrences: List[Dict[str, Any]] = []
    seen_occ: set[Tuple[str, str]] = set()

    def _add_occ(
        check_id: str,
        item_id: str,
        *,
        hard: bool,
        weight: float,
        pass_fraction: float,
        detail: str,
        source_file: str,
    ) -> None:
        key = (check_id, item_id)
        if key in seen_occ:
            return
        seen_occ.add(key)
        pf = max(0.0, min(1.0, float(pass_fraction)))
        occurrences.append(
            {
                "run_id": run_id,
                "task_id": task_id,
                "check_id": check_id,
                "item_id": item_id,
                "hard": bool(hard),
                "weight": round(float(weight), 4),
                "pass_fraction": round(pf, 4),
                "deficit": round(1.0 - pf, 4),
                "detail": detail.strip()[:280],
                "root_cause": classify_root_cause(check_id, detail),
                "source_file": source_file,
            }
        )

    for vfile in task_info.get("verifier_files") or []:
        txt = vfile.read_text(encoding="utf-8", errors="replace")
        objs = parse_verifier_json_resilient(txt)
        for obj in objs:
            src = vfile.name

            # 1. Division-26 style: per_item + _recovered_item_checks + family_pass_fraction
            seen_d26_fams: set[str] = set()
            for container_key in ("per_item", "_recovered_item_checks"):
                container = obj.get(container_key)
                if not isinstance(container, dict):
                    continue
                for sec_id, sec_data in container.items():
                    if not isinstance(sec_data, dict):
                        continue
                    fams = sec_data.get("families") if container_key == "per_item" else sec_data
                    if not isinstance(fams, dict):
                        continue
                    for fam, meta in fams.items():
                        if not isinstance(meta, dict):
                            continue
                        if meta.get("ok") is False:
                            seen_d26_fams.add(fam)
                            tag = str(meta.get("tag") or "soft").lower()
                            w = _safe_float(meta.get("weight"), 1.0) or 1.0
                            is_hard = tag == "hard"
                            eff_w = max(w, 1.5) if is_hard else w
                            detail = str(meta.get("detail") or f"{fam} failed in section {sec_id}")
                            _add_occ(
                                fam,
                                str(sec_id),
                                hard=is_hard,
                                weight=eff_w,
                                pass_fraction=0.0,
                                detail=detail,
                                source_file=src,
                            )

            fam_frac = obj.get("family_pass_fraction")
            fam_sat = obj.get("families_satisfied") or {}
            if isinstance(fam_frac, dict):
                for fam, frac_raw in fam_frac.items():
                    frac = _safe_float(frac_raw, 1.0)
                    if frac is None:
                        continue
                    if (frac < 1.0 - 1e-6 or fam_sat.get(fam) is False) and fam not in seen_d26_fams:
                        _add_occ(
                            str(fam),
                            "_aggregate",
                            hard=False,
                            weight=1.0,
                            pass_fraction=frac,
                            detail=f"aggregate family pass fraction={frac:.3f} (satisfied={fam_sat.get(fam)})",
                            source_file=src,
                        )

            reg_missing_hard = obj.get("register_missing_hard")
            if isinstance(reg_missing_hard, list) and reg_missing_hard:
                _add_occ(
                    "REGISTER_MISSING_HARD",
                    "_register",
                    hard=True,
                    weight=2.5,
                    pass_fraction=_safe_float(obj.get("register_recall_hard"), 0.0) or 0.0,
                    detail=f"missing {len(reg_missing_hard)} hard register IDs: {reg_missing_hard}",
                    source_file=src,
                )

            # 2. Heat-rejection style: items[item].families + margins + notes + register
            items_val = obj.get("items")
            if isinstance(items_val, dict):
                for item_id, item_data in items_val.items():
                    if not isinstance(item_data, dict):
                        continue
                    notes = item_data.get("notes") or {}
                    fams = item_data.get("families") or {}
                    if isinstance(fams, dict):
                        for fam, passed in fams.items():
                            if passed is False:
                                code_prefix = str(fam).split("_", 1)[0]
                                note = notes.get(code_prefix) or notes.get(fam) or f"{fam} failed on {item_id}"
                                is_hard = bool(re.match(r"^H\d+", str(fam)))
                                w = 2.5 if is_hard else 1.0
                                _add_occ(
                                    str(fam),
                                    str(item_id),
                                    hard=is_hard,
                                    weight=w,
                                    pass_fraction=0.0,
                                    detail=f"[{item_id}] {note}",
                                    source_file=src,
                                )
                    margins = item_data.get("margins") or {}
                    if isinstance(margins, dict):
                        for mname, mval in margins.items():
                            mv = _safe_float(mval)
                            if mv is not None and mv < 0:
                                _add_occ(
                                    f"MARGIN_{re.sub(r'[^A-Za-z0-9]+', '_', mname).upper()}",
                                    str(item_id),
                                    hard=True,
                                    weight=2.0,
                                    pass_fraction=0.0,
                                    detail=f"[{item_id}] negative margin {mname}={mv:.6f}",
                                    source_file=src,
                                )

            # 3. Seismic-diaphragm style: hard_families + unengineered[item][fam] + unregistered_families + register
            hard_fams_set = set(obj.get("hard_families") or []) if isinstance(obj.get("hard_families"), list) else set()
            uneng = obj.get("unengineered")
            if isinstance(uneng, dict):
                for item_id, fam_map in uneng.items():
                    if not isinstance(fam_map, dict):
                        continue
                    for fam, finfo in fam_map.items():
                        if not isinstance(finfo, dict):
                            continue
                        ok_flag = finfo.get("ok")
                        eng_flag = finfo.get("engineered")
                        if ok_flag is False or eng_flag is False:
                            is_hard = fam in hard_fams_set or bool(re.match(r"^(?:G|F)\d+", str(fam)))
                            w = 2.5 if is_hard else 1.0
                            whys = finfo.get("why") or []
                            why_str = "; ".join(str(x) for x in whys[:3]) if isinstance(whys, list) else str(whys)
                            _add_occ(
                                str(fam),
                                str(item_id),
                                hard=is_hard,
                                weight=w,
                                pass_fraction=0.0,
                                detail=f"[{item_id}] ok={ok_flag} engineered={eng_flag}: {why_str}",
                                source_file=src,
                            )

            reg_dict = obj.get("register")
            if isinstance(reg_dict, dict):
                missing_ids = reg_dict.get("missing_hard_binding") or reg_dict.get("missing") or []
                if isinstance(missing_ids, list) and missing_ids:
                    rec_val = _safe_float(reg_dict.get("hard_recall") or reg_dict.get("recall_hard"), 0.0) or 0.0
                    _add_occ(
                        "REGISTER_MISSING_HARD",
                        "_register",
                        hard=True,
                        weight=2.5,
                        pass_fraction=rec_val,
                        detail=f"missing {len(missing_ids)} register IDs: {missing_ids}",
                        source_file=src,
                    )

            # 4. Generic Opus LLM Judge rubric list (criteria / rubric / items / evaluations / scores)
            for lkey in ("criteria", "rubric", "items", "evaluations", "criterion_results", "scores"):
                lst = obj.get(lkey)
                if not isinstance(lst, list):
                    continue
                for idx_c, item in enumerate(lst):
                    if not isinstance(item, dict):
                        continue
                    label = str(
                        item.get("id")
                        or item.get("name")
                        or item.get("criterion")
                        or item.get("title")
                        or item.get("requirement")
                        or ""
                    ).strip()
                    if not label:
                        continue
                    sc = _safe_float(
                        item.get("score") if item.get("score") is not None else (
                            item.get("points") if item.get("points") is not None else item.get("awarded")
                        )
                    )
                    max_s = _safe_float(
                        item.get("max_score") if item.get("max_score") is not None else (
                            item.get("weight") if item.get("weight") is not None else item.get("possible")
                        ),
                        1.0,
                    )
                    if max_s is None or max_s <= 0:
                        max_s = 1.0
                    passed = item.get("passed") if item.get("passed") is not None else item.get("pass")
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
                        or "[h=1]" in label.lower()
                    )
                    failed = (passed is False) or (sc is not None and sc < max_s - 1e-6)
                    if failed:
                        pf = (sc / max_s) if (sc is not None and max_s > 0) else 0.0
                        w = max(max_s, 2.5 if is_hard else 1.0) if max_s == 1.0 and is_hard else max_s
                        _add_occ(
                            label[:120],
                            f"criterion_{idx_c}",
                            hard=is_hard,
                            weight=w,
                            pass_fraction=pf,
                            detail=reason or f"{label} scored {_format_num(sc)}/{_format_num(max_s)}",
                            source_file=src,
                        )

            if obj.get("parse_ok") is False:
                _add_occ(
                    "VERIFIER_PARSE_ERROR",
                    "_package",
                    hard=True,
                    weight=3.0,
                    pass_fraction=0.0,
                    detail="parse_ok=False in verifier metrics",
                    source_file=src,
                )

    # 5. Programmatic verifiers (.json + .log)
    seen_pvs: set[str] = set()
    for pvf in task_info.get("prog_ver_files") or []:
        m_vid = re.search(r"programmatic_(ver_[0-9a-f]+)\.json$", pvf.name, re.I)
        ver_id = m_vid.group(1) if m_vid else pvf.stem
        if ver_id in seen_pvs:
            continue
        seen_pvs.add(ver_id)
        objs = parse_verifier_json_resilient(pvf.read_text(encoding="utf-8", errors="replace"))
        pv_score = _safe_float(objs[0].get("score")) if objs else None
        verdict = ""
        detail = ""
        log_p = pvf.with_suffix(".log")
        if log_p.is_file():
            for line in log_p.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("verdict:"):
                    verdict = line.split(":", 1)[1].strip().lower()
                elif line.strip() and not line.lower().startswith("exit "):
                    detail = line.strip()
        if verdict == "fail" or (pv_score is not None and pv_score < 1.0 - 1e-6):
            _add_occ(
                f"PROGRAMMATIC_{ver_id}",
                "_programmatic",
                hard=True,
                weight=2.5,
                pass_fraction=pv_score or 0.0,
                detail=detail or f"verdict={verdict} score={pv_score}",
                source_file=pvf.name,
            )

    return occurrences


def mine_rubric_failures(run_dir: Path, report_out: Optional[Path] = None) -> Dict[str, Any]:
    """Parse all task verifier artifacts in `run_dir`, rank failures by weight*freq, and emit gap_report.md."""
    tasks = discover_run_tasks(run_dir)
    run_id = analyze_eval._infer_run_id_from_path(run_dir)
    all_occurrences: List[Dict[str, Any]] = []
    task_summaries: List[Dict[str, Any]] = []

    for tinfo in tasks:
        occs = extract_task_rubric_checks(tinfo)
        all_occurrences.extend(occs)
        rc_counts = Counter(o["root_cause"] for o in occs)
        top_rc = rc_counts.most_common(1)[0][0] if rc_counts else "none"
        task_summaries.append(
            {
                "task_id": tinfo["task_id"],
                "score": _safe_float((tinfo.get("summary") or {}).get("score")),
                "failed_occurrences": len(occs),
                "distinct_checks": len({o["check_id"] for o in occs}),
                "hard_failures": sum(1 for o in occs if o["hard"]),
                "top_root_cause": top_rc,
                "root_cause_counts": dict(rc_counts),
            }
        )

    # Group by (task_id, check_id) preserving item-level frequency and impact = weight * frequency
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for occ in all_occurrences:
        gk = (occ["task_id"], occ["check_id"])
        entry = grouped.get(gk)
        if entry is None:
            entry = {
                "task_id": occ["task_id"],
                "check_id": occ["check_id"],
                "root_cause": occ["root_cause"],
                "hard": occ["hard"],
                "weight": occ["weight"],
                "frequency": 0,
                "total_deficit": 0.0,
                "min_pass_fraction": occ["pass_fraction"],
                "affected_items": [],
                "details": [],
                "source_files": set(),
            }
            grouped[gk] = entry
        entry["frequency"] += 1
        entry["total_deficit"] += occ["deficit"]
        entry["weight"] = max(entry["weight"], occ["weight"])
        entry["hard"] = entry["hard"] or occ["hard"]
        entry["min_pass_fraction"] = min(entry["min_pass_fraction"], occ["pass_fraction"])
        if occ["item_id"] not in entry["affected_items"]:
            entry["affected_items"].append(occ["item_id"])
        if occ["detail"] and occ["detail"] not in entry["details"] and len(entry["details"]) < 5:
            entry["details"].append(occ["detail"])
        entry["source_files"].add(occ["source_file"])

    ranked_checks: List[Dict[str, Any]] = []
    for entry in grouped.values():
        wxf = round(entry["weight"] * entry["frequency"], 3)
        wdef = round(entry["weight"] * entry["total_deficit"], 3)
        ranked_checks.append(
            {
                "task_id": entry["task_id"],
                "check_id": entry["check_id"],
                "root_cause": entry["root_cause"],
                "hard": entry["hard"],
                "weight": round(entry["weight"], 3),
                "frequency": entry["frequency"],
                "weight_x_frequency": wxf,
                "weighted_deficit": wdef,
                "min_pass_fraction": entry["min_pass_fraction"],
                "affected_items": sorted(entry["affected_items"]),
                "details": entry["details"],
                "source_files": sorted(entry["source_files"]),
            }
        )

    ranked_checks.sort(
        key=lambda r: (-r["weight_x_frequency"], -r["weight"], -r["frequency"], r["task_id"], r["check_id"])
    )

    by_root_cause: Dict[str, Dict[str, Any]] = {}
    for rc in ROOT_CAUSES:
        rc_checks = [c for c in ranked_checks if c["root_cause"] == rc]
        by_root_cause[rc] = {
            "distinct_checks": len(rc_checks),
            "total_occurrences": sum(c["frequency"] for c in rc_checks),
            "total_weight_x_frequency": round(sum(c["weight_x_frequency"] for c in rc_checks), 3),
            "checks": rc_checks,
        }

    result = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "task_count": len(tasks),
        "total_failed_occurrences": len(all_occurrences),
        "distinct_failed_checks": len(ranked_checks),
        "tasks": task_summaries,
        "ranked_checks": ranked_checks,
        "by_root_cause": by_root_cause,
    }

    if report_out is not None:
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(render_gap_report(result), encoding="utf-8", newline="\n")

    return result


def render_gap_report(mined: Dict[str, Any]) -> str:
    """Render `gap_report.md` summarizing ranked rubric failures by weight*frequency & root cause."""
    lines: List[str] = [
        f"# PLE Rubric Gap Report — Run `{mined['run_id']}`",
        "",
        f"- **Tasks analyzed:** {mined['task_count']}",
        f"- **Distinct failed/partial checks:** {mined['distinct_failed_checks']}",
        f"- **Total failed/partial item occurrences:** {mined['total_failed_occurrences']}",
        "",
        "## 1. Per-Task Failure Overview",
        "",
        "| Task | Score | Failed Occurrences | Distinct Checks | Hard Failures | Dominant Root Cause |",
        "| :--- | ---: | ---: | ---: | ---: | :--- |",
    ]
    for t in mined.get("tasks") or []:
        sc = f"{t['score']:.4f}" if isinstance(t.get("score"), (int, float)) else "-"
        lines.append(
            f"| `{t['task_id']}` | {sc} | {t['failed_occurrences']} | "
            f"{t['distinct_checks']} | {t['hard_failures']} | `{t['top_root_cause']}` |"
        )

    lines.extend(
        [
            "",
            "## 2. Root-Cause Breakdown",
            "",
            "| Root Cause | Distinct Checks | Occurrences | Total Weight × Freq |",
            "| :--- | ---: | ---: | ---: |",
        ]
    )
    for rc in ROOT_CAUSES:
        bucket = (mined.get("by_root_cause") or {}).get(rc) or {}
        lines.append(
            f"| `{rc}` | {bucket.get('distinct_checks', 0)} | "
            f"{bucket.get('total_occurrences', 0)} | {bucket.get('total_weight_x_frequency', 0.0):.2f} |"
        )

    lines.extend(
        [
            "",
            "## 3. Ranked Failed / Partial Rubric Checks (`weight × frequency`)",
            "",
            "| Rank | Task | Check ID | Hard | Weight | Freq | Weight × Freq | Root Cause | Evidence |",
            "| ---: | :--- | :--- | :---: | ---: | ---: | ---: | :--- | :--- |",
        ]
    )
    for idx, chk in enumerate((mined.get("ranked_checks") or [])[:35], 1):
        ev = "; ".join(chk.get("details") or [])[:150].replace("|", "/").replace("\n", " ")
        hard_str = "Y" if chk["hard"] else "N"
        lines.append(
            f"| {idx} | `{chk['task_id']}` | `{chk['check_id']}` | {hard_str} | "
            f"{chk['weight']:.2f} | {chk['frequency']} | **{chk['weight_x_frequency']:.2f}** | "
            f"`{chk['root_cause']}` | {ev} |"
        )

    lines.extend(["", "## 4. Failures Grouped by Root Cause", ""])
    for rc in ROOT_CAUSES:
        bucket = (mined.get("by_root_cause") or {}).get(rc) or {}
        checks = bucket.get("checks") or []
        if not checks:
            continue
        lines.append(
            f"### `{rc}` — {ROOT_CAUSE_TITLES.get(rc, rc)} "
            f"({bucket['distinct_checks']} check(s), weight×freq={bucket['total_weight_x_frequency']:.2f})"
        )
        for chk in checks[:12]:
            items_s = ",".join(chk["affected_items"][:6])
            ev = "; ".join(chk["details"][:2]).replace("\n", " ")
            lines.append(
                f"- **`{chk['task_id']}` / `{chk['check_id']}`** "
                f"(weight={chk['weight']}, freq={chk['frequency']}, W×F={chk['weight_x_frequency']:.2f}, "
                f"items=[{items_s}]): {ev[:220]}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Module 2: Token & Loop Profiler (`profile`) — All 4 Precision Refinements
# ---------------------------------------------------------------------------


def _extract_exec_paths(cmd: str) -> Tuple[List[str], List[str]]:
    """Heuristically extract unambiguous single-file reads and file writes from an exec command."""
    reads: List[str] = []
    writes: List[str] = []

    # 1. File mutations in shell commands or embedded Python scripts
    for m in re.finditer(r"(?:>>?|\btee\s+(?:-a\s+)?)\s*[\"']?([^\s\"'|;&]+)[\"']?", cmd):
        cand = _normalize_path(m.group(1))
        if cand and not cand.startswith("&") and cand != "/dev/null":
            writes.append(cand)
    for m in re.finditer(
        r"open\(\s*[\"']([^\"']+)[\"']\s*,\s*[\"'][wa][b+]*[\"']",
        cmd,
    ):
        writes.append(_normalize_path(m.group(1)))
    for m in re.finditer(
        r"\.(?:to_csv|to_excel|savefig|save|write_text|write_bytes)\(\s*[\"']([^\"']+)[\"']",
        cmd,
    ):
        writes.append(_normalize_path(m.group(1)))
    for m in re.finditer(r"\bsed\s+-i[^\n]*?\s+[\"']?([^\s\"';&]+)[\"']?(?:\s|$)", cmd):
        writes.append(_normalize_path(m.group(1)))

    # 2. Pure shell single-file read commands (cat/head/tail/pdftotext)
    stripped = cmd.strip()
    m_cat = re.match(
        r"^(?:cat|head(?:\s+-n\s+\d+)?|tail(?:\s+-n\s+\d+)?)\s+[\"']?([^\s\"'|;&><]+)[\"']?\s*$",
        stripped,
    )
    if m_cat:
        p = _normalize_path(m_cat.group(1))
        if p:
            reads.append(p)

    return reads, writes


def _cluster_rejection_reason(text: str) -> str:
    low = text.lower()
    if "cross-reference" in low or "cross reference" in low or "disallowed" in low or "ufc" in low or "not part of the project manual" in low:
        return "disallowed_cross_reference"
    if "[____" in low or "placeholder" in low or "bracket" in low or "as indicated" in low or "unwrapped" in low:
        return "unresolved_placeholders_or_brackets"
    if "missing" in low or "register" in low or "absent" in low or "not generated" in low or "omitted" in low:
        return "missing_deliverable_or_register"
    if any(tok in low for tok in ("mismatch", "calc", "disagree", "unit", "tolerance", "plf", "kip", "kw", "amp", "formula")):
        return "numeric_or_calc_mismatch"
    if any(tok in low for tok in ("format", "comma", "schema", "column", "header", "style")):
        return "format_or_schema_violation"
    if "truncated=true" in low or "max_turns" in low:
        return "verifier_truncated_timeout"
    return "other"


def _diagnose_rejection_cluster(cluster: str) -> str:
    if cluster in ("disallowed_cross_reference", "format_or_schema_violation"):
        return "worker_prompt_gap_or_strict_verifier_format_rule"
    if cluster == "verifier_truncated_timeout":
        return "turn_budget_truncation"
    return "worker_prompt_or_audit_gap"


def _parse_verifier_rejections(agent_logs: List[Path], task_id: str) -> Dict[str, Any]:
    """Parse `harness: verifier:` events from agent logs to compute rejection rate & clustered reasons."""
    calls: List[Dict[str, Any]] = []
    for af in agent_logs:
        lines = af.read_text(encoding="utf-8", errors="replace").splitlines()
        i = 0
        while i < len(lines):
            m = _VERIFIER_LINE_RE.match(lines[i].strip())
            if m:
                truncated = m.group(1).lower() == "true"
                audit_errs = int(m.group(2) or 0)
                prefilter_errs = int(m.group(3) or 0)
                readback_errs = int(m.group(4) or 0)
                tail = m.group(5).strip()
                body_lines = [tail] if tail else []
                j = i + 1
                while j < len(lines):
                    nxt = lines[j].strip()
                    if (
                        nxt.startswith("harness:")
                        or nxt.startswith("exec ")
                        or nxt.startswith("done after ")
                        or nxt.startswith("serve status=")
                        or nxt.startswith('{"type":')
                    ):
                        break
                    if nxt:
                        body_lines.append(nxt)
                    j += 1
                full_text = "\n".join(body_lines).strip()
                is_pass = bool(_PASS_TAIL_RE.match(full_text)) and audit_errs == 0 and prefilter_errs == 0 and readback_errs == 0
                if full_text.upper().endswith("PASS") and len(full_text) <= 20 and audit_errs == 0:
                    is_pass = True
                calls.append(
                    {
                        "line": i + 1,
                        "truncated": truncated,
                        "passed": is_pass,
                        "audit_errors": audit_errs,
                        "prefilter_errors": prefilter_errs,
                        "readback_errors": readback_errs,
                        "text": full_text,
                    }
                )
                i = j
            else:
                i += 1

    rejections = [c for c in calls if not c["passed"]]
    reason_items: List[Dict[str, str]] = []
    for rej in rejections:
        txt = rej["text"]
        if not txt and rej["truncated"]:
            reason_items.append({"cluster": "verifier_truncated_timeout", "excerpt": "Verifier output empty with truncated=True"})
            continue
        # Split numbered/bulleted critique lines
        chunks = re.split(r"(?:^|\n)\s*(?:\d+\.|[-*])\s+", txt)
        chunks = [ch.strip() for ch in chunks if ch.strip()]
        if not chunks:
            chunks = [txt]
        for ch in chunks:
            cl = _cluster_rejection_reason(ch)
            reason_items.append({"cluster": cl, "excerpt": ch[:220]})

    return {
        "task_id": task_id,
        "verifier_calls": len(calls),
        "verifier_passes": len(calls) - len(rejections),
        "verifier_rejections": len(rejections),
        "verifier_rejection_rate": round(len(rejections) / len(calls), 4) if calls else 0.0,
        "calls": calls,
        "reason_items": reason_items,
    }


def profile_task(task_info: Dict[str, Any]) -> Dict[str, Any]:
    """Profile one task for READ_LOOP_SUSPECTED, RETRY_STORM, CONTEXT_BLOAT, and verifier rejections."""
    task_id = task_info["task_id"]
    summary = task_info.get("summary") or {}
    traj_path: Optional[Path] = task_info.get("trajectory_path")
    traj: Dict[str, Any] = {}
    if traj_path and traj_path.is_file():
        try:
            loaded = json.loads(traj_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(loaded, dict):
                traj = loaded
        except json.JSONDecodeError:
            traj = {}

    steps: List[Dict[str, Any]] = traj.get("steps") or []
    final_metrics: Dict[str, Any] = traj.get("final_metrics") or {}
    sum_tokens: Dict[str, Any] = summary.get("tokens") or {}

    total_prompt_tokens = int(
        final_metrics.get("total_prompt_tokens")
        or sum_tokens.get("input")
        or 0
    )
    total_output_tokens = int(
        final_metrics.get("total_completion_tokens")
        or sum_tokens.get("output")
        or 0
    )
    total_cached_tokens = int(
        final_metrics.get("total_cached_tokens")
        or sum_tokens.get("cache")
        or 0
    )
    reported_cost = _safe_float(summary.get("estimated_cost_usd"))

    # Effective per-token cost rates calibrated to actual summary cost if present
    if reported_cost and reported_cost > 0 and (total_prompt_tokens + total_output_tokens) > 0:
        uncached_in = max(0, total_prompt_tokens - total_cached_tokens)
        list_est = (
            (uncached_in / 1000.0) * DEFAULT_INPUT_PER_1K
            + (total_cached_tokens / 1000.0) * DEFAULT_CACHED_PER_1K
            + (total_output_tokens / 1000.0) * DEFAULT_OUTPUT_PER_1K
        )
        cost_scale = (reported_cost / list_est) if list_est > 0 else 1.0
    else:
        cost_scale = 1.0

    def _estimate_cost(in_toks: int, out_toks: int, cached_toks: int = 0) -> float:
        unc = max(0, in_toks - cached_toks)
        usd = (
            (unc / 1000.0) * DEFAULT_INPUT_PER_1K
            + (cached_toks / 1000.0) * DEFAULT_CACHED_PER_1K
            + (out_toks / 1000.0) * DEFAULT_OUTPUT_PER_1K
        ) * cost_scale
        return round(usd, 6)

    # Parse HTTP requests & API retries from serve logs
    http_total_calls = 0
    http_non200_retries = 0
    serve_retry_lines = 0
    for sl in task_info.get("serve_logs") or []:
        for line in sl.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _HTTP_STATUS_RE.search(line)
            if m:
                http_total_calls += 1
                if m.group(1) != "200":
                    http_non200_retries += 1
            elif "retrying request" in line.lower():
                serve_retry_lines += 1

    log_retry_mentions = 0
    fallback_rotations = 0
    for af in task_info.get("agent_logs") or []:
        for line in af.read_text(encoding="utf-8", errors="replace").splitlines():
            low = line.lower()
            if "model call failed" in low or "retrying in " in low:
                log_retry_mentions += 1
            if "rotating fallback model" in low:
                fallback_rotations += 1

    # Walk trajectory chronologically for:
    # 1) READ_LOOP_SUSPECTED (with write/edit invalidation & content hash check)
    # 2) Failed tool retries
    # 3) Turn-by-turn context bloat + suggested_prune
    last_read_state: Dict[str, Dict[str, Any]] = {}  # path -> {"hash": str, "step_id": int, "dirty": bool}
    read_loops: List[Dict[str, Any]] = []
    verify_reads_suppressed = 0

    tool_retry_steps: List[Dict[str, Any]] = []
    prev_failed_cmd: Optional[Tuple[int, str, str]] = None

    total_steps = len(steps)
    obs_records: List[Dict[str, Any]] = []
    step_texts: List[str] = []
    raw_turn_cum_chars: List[int] = []
    cum_chars = 0

    for idx_s, st in enumerate(steps):
        sid = int(st.get("step_id") or (idx_s + 1))
        msg = str(st.get("message") or "")
        cum_chars += len(msg)
        tcalls = st.get("tool_calls") or []
        obs_results = (st.get("observation") or {}).get("results") or []
        res_by_id = {str(r.get("source_call_id")): str(r.get("content") or "") for r in obs_results}

        parts_for_ref = [msg]
        for tc in tcalls:
            fn = str(tc.get("function_name") or "")
            args = tc.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            parts_for_ref.append(json.dumps(args, ensure_ascii=False))
            cum_chars += len(json.dumps(args, ensure_ascii=False))
            call_id = str(tc.get("tool_call_id") or f"step_{sid}")
            obs_txt = res_by_id.get(call_id, "")
            if not obs_txt and len(obs_results) == 1 and len(tcalls) == 1:
                obs_txt = str(obs_results[0].get("content") or "")
            cum_chars += len(obs_txt)

            # Check writes BEFORE reads so edit/write marks dirty
            if fn in ("write_file", "edit_file", "multi_edit", "patch_file"):
                wpath = _normalize_path(str(args.get("path") or args.get("file_path") or ""))
                if wpath and wpath in last_read_state:
                    last_read_state[wpath]["dirty"] = True
            elif fn in ("exec", "bash", "run_command"):
                cmd_str = str(args.get("command") or args.get("cmd") or "")
                ex_reads, ex_writes = _extract_exec_paths(cmd_str)
                for wp in ex_writes:
                    if wp in last_read_state:
                        last_read_state[wp]["dirty"] = True
                for rp in ex_reads:
                    h = _sha256_short(obs_txt)
                    prev = last_read_state.get(rp)
                    if prev is not None:
                        if prev["dirty"] or prev["hash"] != h:
                            verify_reads_suppressed += 1
                        else:
                            read_loops.append(
                                {
                                    "step_id": sid,
                                    "prev_step_id": prev["step_id"],
                                    "tool_call_id": call_id,
                                    "function_name": fn,
                                    "path": rp,
                                    "content_hash": h,
                                    "obs_chars": len(obs_txt),
                                }
                            )
                    last_read_state[rp] = {"hash": h, "step_id": sid, "dirty": False}

                # Track failed exec retries
                m_ec = _EXEC_STATUS_RE.search(obs_txt)
                ec = int(m_ec.group(1)) if m_ec else 0
                cmd_head = " ".join(cmd_str.split())[:120]
                if prev_failed_cmd is not None:
                    pf_sid, _pf_head, _ = prev_failed_cmd
                    if sid - pf_sid <= 2:
                        tool_retry_steps.append(
                            {
                                "step_id": sid,
                                "failed_step_id": pf_sid,
                                "exit_code_prev": prev_failed_cmd[2],
                                "command_preview": cmd_head,
                                "metrics": st.get("metrics"),
                            }
                        )
                    prev_failed_cmd = None
                if ec != 0 or obs_txt.lower().startswith("error:"):
                    prev_failed_cmd = (sid, cmd_head, f"exit_code={ec}")

            elif fn in ("read_file", "view_file"):
                rp = _normalize_path(str(args.get("path") or args.get("file_path") or ""))
                if rp:
                    h = _sha256_short(obs_txt)
                    prev = last_read_state.get(rp)
                    if prev is not None:
                        if prev["dirty"] or prev["hash"] != h:
                            verify_reads_suppressed += 1
                        else:
                            read_loops.append(
                                {
                                    "step_id": sid,
                                    "prev_step_id": prev["step_id"],
                                    "tool_call_id": call_id,
                                    "function_name": fn,
                                    "path": rp,
                                    "content_hash": h,
                                    "obs_chars": len(obs_txt),
                                }
                            )
                    last_read_state[rp] = {"hash": h, "step_id": sid, "dirty": False}

            if obs_txt:
                obs_records.append(
                    {
                        "step_idx": idx_s,
                        "step_id": sid,
                        "tool_call_id": call_id,
                        "function_name": fn,
                        "obs_chars": len(obs_txt),
                        "est_tokens": max(1, len(obs_txt) // 4),
                        "age_steps": max(0, total_steps - idx_s - 1),
                        "text": obs_txt,
                        "metrics": st.get("metrics"),
                    }
                )

        step_texts.append("\n".join(parts_for_ref))
        raw_turn_cum_chars.append(cum_chars)

    # Calibrate per-turn context tokens to final_metrics.total_prompt_tokens
    raw_est_sum = sum(max(1, c // 4) for c in raw_turn_cum_chars) or 1
    agent_turns = max(1, sum(1 for s in steps if s.get("source") == "agent"))
    if total_prompt_tokens > 0 and raw_turn_cum_chars:
        scale_ctx = total_prompt_tokens / float(raw_est_sum)
    else:
        scale_ctx = 1.0

    turn_context_est = [
        {
            "step_id": int((steps[i]).get("step_id") or (i + 1)),
            "est_context_tokens": int((raw_turn_cum_chars[i] // 4) * scale_ctx),
            "obs_chars_this_step": sum(
                len(str(r.get("content") or ""))
                for r in ((steps[i].get("observation") or {}).get("results") or [])
            ),
        }
        for i in range(len(steps))
    ]

    # Exact or calibrated RETRY_STORM accounting
    api_retries = max(http_non200_retries, serve_retry_lines, log_retry_mentions)
    tool_retries = len(tool_retry_steps)
    total_retries = api_retries + tool_retries

    avg_in_per_call = (
        total_prompt_tokens / float(max(1, http_total_calls or agent_turns))
        if total_prompt_tokens > 0
        else 25_000.0
    )
    avg_out_per_call = (
        total_output_tokens / float(max(1, http_total_calls or agent_turns))
        if total_output_tokens > 0
        else 1_200.0
    )
    avg_cache_per_call = (
        total_cached_tokens / float(max(1, http_total_calls or agent_turns))
        if total_cached_tokens > 0
        else 0.0
    )

    retry_in = 0
    retry_out = 0
    retry_cache = 0
    if api_retries > 0:
        retry_in += int(round(api_retries * avg_in_per_call))
        retry_out += int(round(api_retries * avg_out_per_call))
        retry_cache += int(round(api_retries * avg_cache_per_call))
    for tr in tool_retry_steps:
        m_dict = tr.get("metrics")
        if isinstance(m_dict, dict) and (m_dict.get("prompt_tokens") or m_dict.get("input_tokens")):
            retry_in += int(m_dict.get("prompt_tokens") or m_dict.get("input_tokens") or 0)
            retry_out += int(m_dict.get("completion_tokens") or m_dict.get("output_tokens") or 0)
            retry_cache += int(m_dict.get("cached_tokens") or 0)
        else:
            retry_in += int(round(avg_in_per_call))
            retry_out += int(round(avg_out_per_call))
            retry_cache += int(round(avg_cache_per_call))

    retry_token_waste = retry_in + retry_out
    retry_cost_waste_usd = _estimate_cost(retry_in, retry_out, retry_cache)
    retry_storm_flagged = total_retries >= 3 or retry_token_waste >= 250_000

    # Context bloat & suggested_prune ranking (oldest, largest, least-referenced observations)
    later_token_sets: List[set[str]] = [
        set(_TOKEN_EXTRACT_RE.findall(st_txt)) for st_txt in step_texts
    ]
    suggested_prune: List[Dict[str, Any]] = []
    for rec in obs_records:
        obs_tokens = set(_TOKEN_EXTRACT_RE.findall(rec["text"]))
        # Sample top distinctive tokens for downstream reference detection
        sig_tokens = sorted(obs_tokens, key=lambda t: (-len(t), t))[:24]
        downstream_refs = 0
        if sig_tokens:
            for nxt_idx in range(rec["step_idx"] + 1, total_steps):
                if len(later_token_sets[nxt_idx].intersection(sig_tokens)) >= 2:
                    downstream_refs += 1
        est_tok = rec["est_tokens"]
        age = rec["age_steps"]
        est_prompt_savings = est_tok * age
        prune_score = (est_tok * (1 + age)) / (1.0 + downstream_refs * 3.0)
        if est_tok >= 500 or age >= 5:
            suggested_prune.append(
                {
                    "tool_call_id": rec["tool_call_id"],
                    "step_id": rec["step_id"],
                    "function_name": rec["function_name"],
                    "obs_chars": rec["obs_chars"],
                    "est_tokens": est_tok,
                    "age_steps": age,
                    "downstream_refs": downstream_refs,
                    "est_prompt_token_savings": est_prompt_savings,
                    "prune_score": round(prune_score, 1),
                    "preview": rec["text"][:100].replace("\n", " "),
                }
            )

    suggested_prune.sort(
        key=lambda x: (-x["prune_score"], -x["obs_chars"], -x["age_steps"], x["step_id"])
    )

    oversized_turns = [
        t for t in turn_context_est
        if t["est_context_tokens"] >= 60_000 or t["obs_chars_this_step"] >= 12_000
    ]
    peak_turn_tokens = max((t["est_context_tokens"] for t in turn_context_est), default=0)
    context_bloat_flagged = bool(oversized_turns) or total_prompt_tokens >= 5_000_000

    # Verifier rejection rate & reason clustering
    ver_info = _parse_verifier_rejections(task_info.get("agent_logs") or [], task_id)

    flags: List[str] = []
    if read_loops:
        flags.append("READ_LOOP_SUSPECTED")
    if retry_storm_flagged:
        flags.append("RETRY_STORM")
    if context_bloat_flagged:
        flags.append("CONTEXT_BLOAT")

    return {
        "task_id": task_id,
        "steps": total_steps,
        "http_calls": http_total_calls,
        "tokens": {
            "input": total_prompt_tokens,
            "cache": total_cached_tokens,
            "output": total_output_tokens,
        },
        "estimated_cost_usd": reported_cost if reported_cost is not None else _estimate_cost(
            total_prompt_tokens, total_output_tokens, total_cached_tokens
        ),
        "flags": flags,
        "read_loop": {
            "flagged": bool(read_loops),
            "count": len(read_loops),
            "verify_reads_suppressed": verify_reads_suppressed,
            "events": read_loops[:10],
        },
        "retry_storm": {
            "flagged": retry_storm_flagged,
            "retry_count": total_retries,
            "api_retries": api_retries,
            "tool_retries": tool_retries,
            "fallback_rotations": fallback_rotations,
            "retry_input_tokens": retry_in,
            "retry_output_tokens": retry_out,
            "retry_cached_tokens": retry_cache,
            "retry_token_waste": retry_token_waste,
            "retry_cost_waste_usd": retry_cost_waste_usd,
        },
        "context_bloat": {
            "flagged": context_bloat_flagged,
            "peak_turn_est_tokens": peak_turn_tokens,
            "oversized_turn_count": len(oversized_turns),
            "total_obs_chars": sum(r["obs_chars"] for r in obs_records),
            "suggested_prune": suggested_prune[:10],
        },
        "verifier": ver_info,
    }


def profile_run(run_dir: Path, out_path: Optional[Path] = None) -> Dict[str, Any]:
    """Run Token & Loop Profiler across all tasks in `run_dir`."""
    run_id = analyze_eval._infer_run_id_from_path(run_dir)
    tasks = discover_run_tasks(run_dir)
    task_profiles = [profile_task(t) for t in tasks]

    tot_ver_calls = sum(tp["verifier"]["verifier_calls"] for tp in task_profiles)
    tot_ver_rej = sum(tp["verifier"]["verifier_rejections"] for tp in task_profiles)
    reason_buckets: Dict[str, Dict[str, Any]] = {}
    for tp in task_profiles:
        tid = tp["task_id"]
        for item in tp["verifier"]["reason_items"]:
            cl = item["cluster"]
            b = reason_buckets.get(cl)
            if b is None:
                b = {
                    "reason_cluster": cl,
                    "count": 0,
                    "tasks": set(),
                    "diagnosis": _diagnose_rejection_cluster(cl),
                    "sample_excerpts": [],
                }
                reason_buckets[cl] = b
            b["count"] += 1
            b["tasks"].add(tid)
            if len(b["sample_excerpts"]) < 3 and item["excerpt"] not in b["sample_excerpts"]:
                b["sample_excerpts"].append(item["excerpt"])

    top_rejection_reasons = [
        {
            "reason_cluster": b["reason_cluster"],
            "count": b["count"],
            "tasks": sorted(b["tasks"]),
            "diagnosis": b["diagnosis"],
            "sample_excerpts": b["sample_excerpts"],
        }
        for b in sorted(reason_buckets.values(), key=lambda x: (-x["count"], x["reason_cluster"]))
    ]

    report = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "task_count": len(task_profiles),
        "totals": {
            "input_tokens": sum(tp["tokens"]["input"] for tp in task_profiles),
            "cached_tokens": sum(tp["tokens"]["cache"] for tp in task_profiles),
            "output_tokens": sum(tp["tokens"]["output"] for tp in task_profiles),
            "estimated_cost_usd": round(sum(tp["estimated_cost_usd"] for tp in task_profiles), 6),
            "read_loops": sum(tp["read_loop"]["count"] for tp in task_profiles),
            "verify_reads_suppressed": sum(tp["read_loop"]["verify_reads_suppressed"] for tp in task_profiles),
            "retry_count": sum(tp["retry_storm"]["retry_count"] for tp in task_profiles),
            "retry_token_waste": sum(tp["retry_storm"]["retry_token_waste"] for tp in task_profiles),
            "retry_cost_waste_usd": round(sum(tp["retry_storm"]["retry_cost_waste_usd"] for tp in task_profiles), 6),
            "verifier_calls": tot_ver_calls,
            "verifier_rejections": tot_ver_rej,
            "verifier_rejection_rate": round(tot_ver_rej / tot_ver_calls, 4) if tot_ver_calls else 0.0,
        },
        "top_rejection_reasons": top_rejection_reasons,
        "tasks": task_profiles,
    }

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")

    return report


# ---------------------------------------------------------------------------
# Module 3: Offline Golden-Replay Regression (`replay`)
# ---------------------------------------------------------------------------


def _build_regression_assertions(check: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build deterministic offline assertions from a mined rubric check failure."""
    cid = check["check_id"]
    cid_low = cid.lower()
    details_txt = " ".join(check.get("details") or [])
    assertions: List[Dict[str, Any]] = []

    if cid_low in ("register_missing_hard", "register_missing"):
        ids = re.findall(r"\bR[-_A-Za-z0-9]{1,16}\b", details_txt)
        filtered = [x for x in ids if x != "R" and any(ch.isdigit() for ch in x) or x.startswith("R-")]
        if filtered:
            assertions.append({"type": "required_substrings", "values": sorted(set(filtered))})

    if "fmt_no_ufc" in cid_low:
        assertions.append(
            {
                "type": "forbidden_substrings",
                "values": ["UFC ", "Unified Facilities Criteria", "[_____]"],
            }
        )
    elif "fmt_references" in cid_low:
        m = re.search(r"listed but not cited:\s*(\[[^\]]+\])", details_txt)
        assertions.append(
            {
                "type": "citation_consistency",
                "rule": "Every standard in References/Applicable Publications must be cited cleanly in body text.",
                "observed_mismatch": m.group(1) if m else details_txt[:160],
            }
        )
    elif "fmt_xref" in cid_low:
        assertions.append(
            {
                "type": "regex_forbidden",
                "pattern": r"Section\s+\d{2}\s+\d{2}\s+\d{2},\s+[A-Z]",
                "description": "No comma between section number and section title in cross-references.",
            }
        )

    if not assertions:
        assertions.append(
            {
                "type": "check_rubric_coverage",
                "root_cause": check["root_cause"],
                "expected_pass_fraction": 1.0,
                "observed_evidence": details_txt[:220],
            }
        )
    return assertions


def extract_golden_replays(
    run_dir: Path,
    out_dir: Path,
    *,
    top_k: int = 15,
    mined: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Extract top failed rubric checks into deterministic offline regression snapshots."""
    mined_data = mined if mined is not None else mine_rubric_failures(run_dir)
    tasks_by_id = {t["task_id"]: t for t in discover_run_tasks(run_dir)}
    out_dir.mkdir(parents=True, exist_ok=True)

    snapshots: List[Dict[str, Any]] = []
    for idx, chk in enumerate((mined_data.get("ranked_checks") or [])[:top_k], 1):
        tid = chk["task_id"]
        cid = chk["check_id"]
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{tid}__{cid}") + ".json"
        tinfo = tasks_by_id.get(tid) or {}
        fmsg = ""
        fmsg_path = tinfo.get("final_message_path")
        if isinstance(fmsg_path, Path) and fmsg_path.is_file():
            fmsg = fmsg_path.read_text(encoding="utf-8", errors="replace")[:800]

        snap = {
            "snapshot_id": f"{tid}__{cid}",
            "rank": idx,
            "source_run_id": mined_data["run_id"],
            "task_id": tid,
            "check_id": cid,
            "root_cause": chk["root_cause"],
            "hard": chk["hard"],
            "weight": chk["weight"],
            "frequency": chk["frequency"],
            "impact_score": chk["weight_x_frequency"],
            "affected_items": chk["affected_items"],
            "failure_details": chk["details"],
            "regression_assertions": _build_regression_assertions(chk),
            "deliverable_excerpt": fmsg,
        }
        snap_path = out_dir / safe_name
        snap_path.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        snapshots.append(
            {
                "snapshot_id": snap["snapshot_id"],
                "file": safe_name,
                "task_id": tid,
                "check_id": cid,
                "root_cause": chk["root_cause"],
                "impact_score": chk["weight_x_frequency"],
            }
        )

    manifest = {
        "schema_version": "golden-replay-v1",
        "source_run_id": mined_data["run_id"],
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def evaluate_golden_replays(replays_dir: Path, candidate_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Validate golden replay snapshots and optionally run deterministic checks on `candidate_dir`."""
    manifest_p = replays_dir / "manifest.json"
    if not manifest_p.is_file():
        raise FileNotFoundError(f"Missing manifest.json in {replays_dir}")
    manifest = json.loads(manifest_p.read_text(encoding="utf-8", errors="replace"))

    results: List[Dict[str, Any]] = []
    for entry in manifest.get("snapshots") or []:
        snap_p = replays_dir / entry["file"]
        snap = json.loads(snap_p.read_text(encoding="utf-8", errors="replace"))
        if candidate_dir is None:
            results.append(
                {
                    "snapshot_id": snap["snapshot_id"],
                    "status": "loaded",
                    "passed": True,
                    "failures": [],
                }
            )
            continue

        # Gather candidate text for the snapshot's task_id (or whole candidate_dir)
        task_root = candidate_dir / snap["task_id"]
        search_root = task_root if task_root.is_dir() else candidate_dir
        corpus_parts: List[str] = []
        for ext in ("*.md", "*.txt", "*.csv", "*.json", "*.yaml", "*.yml"):
            for fp in sorted(search_root.rglob(ext)):
                corpus_parts.append(fp.read_text(encoding="utf-8", errors="replace"))
        corpus = "\n".join(corpus_parts)

        failures: List[str] = []
        for asrt in snap.get("regression_assertions") or []:
            atype = asrt.get("type")
            if atype == "required_substrings":
                for val in asrt.get("values") or []:
                    if val not in corpus:
                        failures.append(f"Missing required token: {val}")
            elif atype == "forbidden_substrings":
                for val in asrt.get("values") or []:
                    if val in corpus:
                        failures.append(f"Forbidden token present: {val}")
            elif atype == "regex_forbidden":
                pat = asrt.get("pattern") or ""
                if pat and re.search(pat, corpus):
                    failures.append(f"Forbidden pattern matched: {pat}")

        results.append(
            {
                "snapshot_id": snap["snapshot_id"],
                "status": "PASS" if not failures else "FAIL",
                "passed": not failures,
                "failures": failures,
            }
        )

    passed_cnt = sum(1 for r in results if r["passed"])
    return {
        "replays_dir": str(replays_dir),
        "candidate_dir": str(candidate_dir) if candidate_dir else None,
        "total": len(results),
        "passed": passed_cnt,
        "failed": len(results) - passed_cnt,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Module 4: A/B Scoreboard & Diff Gate (`compare`)
# ---------------------------------------------------------------------------


def _summarize_task_subset(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not tasks:
        return {
            "count": 0,
            "mean_score": None,
            "input_tokens_per_task": 0.0,
            "wall_clock_min_per_task": 0.0,
            "cost_per_task_usd": 0.0,
        }
    scores = [float(t["score"]) for t in tasks if isinstance(t.get("score"), (int, float))]
    in_toks = [int((t.get("tokens") or {}).get("input") or 0) for t in tasks]
    secs = [float(t.get("seconds") or 0.0) for t in tasks]
    costs = [float(t.get("estimated_cost_usd") or 0.0) for t in tasks]
    n = len(tasks)
    return {
        "count": n,
        "mean_score": round(sum(scores) / len(scores), 6) if scores else None,
        "input_tokens_per_task": round(sum(in_toks) / n, 1),
        "wall_clock_min_per_task": round((sum(secs) / n) / 60.0, 2),
        "cost_per_task_usd": round(sum(costs) / n, 4),
    }


def compare_runs(
    baseline_dir: Path,
    candidate_dir: Path,
    out_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compare Run N (candidate) vs Run N-1 (baseline) and emit PROMOTE / HOLD gate."""
    base_sum = load_run_summary(baseline_dir)
    cand_sum = load_run_summary(candidate_dir)

    base_map = {str(t["id"]): t for t in base_sum.get("tasks") or [] if isinstance(t, dict) and t.get("id")}
    cand_map = {str(t["id"]): t for t in cand_sum.get("tasks") or [] if isinstance(t, dict) and t.get("id")}
    shared_ids = sorted(set(base_map) & set(cand_map))

    base_all = _summarize_task_subset(list(base_map.values()))
    cand_all = _summarize_task_subset(list(cand_map.values()))

    base_shared = _summarize_task_subset([base_map[k] for k in shared_ids])
    cand_shared = _summarize_task_subset([cand_map[k] for k in shared_ids])

    # Evaluate gate on shared tasks when overlap exists, otherwise overall
    eval_base = base_shared if shared_ids else base_all
    eval_cand = cand_shared if shared_ids else cand_all

    sb = eval_base["mean_score"]
    sc = eval_cand["mean_score"]
    delta_score = round(sc - sb, 6) if (sb is not None and sc is not None) else None
    ib = eval_base["input_tokens_per_task"]
    ic = eval_cand["input_tokens_per_task"]
    delta_in = round(ic - ib, 1)
    delta_in_pct = round((100.0 * delta_in / ib), 2) if ib > 0 else 0.0
    mb = eval_base["wall_clock_min_per_task"]
    mc = eval_cand["wall_clock_min_per_task"]
    delta_min = round(mc - mb, 2)

    if delta_score is None:
        gate = "HOLD"
        rationale = "Insufficient scored tasks to evaluate promotion gate."
    elif delta_score > 1e-6:
        gate = "PROMOTE"
        rationale = f"Mean score improved ({sb:.4f} -> {sc:.4f}, {delta_score:+.4f})."
    elif delta_score >= -1e-6 and delta_in < -1.0:
        gate = "PROMOTE"
        rationale = (
            f"Zero score regression ({sb:.4f} -> {sc:.4f}) while input tokens/task "
            f"dropped ({delta_in:+,.0f}, {delta_in_pct:+.1f}%)."
        )
    elif delta_score < -1e-6:
        gate = "HOLD"
        rationale = f"Score regressed ({sb:.4f} -> {sc:.4f}, {delta_score:+.4f})."
    else:
        gate = "HOLD"
        rationale = (
            f"Score flat ({sb:.4f} -> {sc:.4f}) and input tokens/task did not decrease "
            f"({delta_in:+,.0f}, {delta_in_pct:+.1f}%)."
        )

    per_task = []
    for tid in sorted(set(base_map) | set(cand_map)):
        tb = base_map.get(tid) or {}
        tc = cand_map.get(tid) or {}
        per_task.append(
            {
                "task_id": tid,
                "shared": tid in shared_ids,
                "score_base": _safe_float(tb.get("score")),
                "score_cand": _safe_float(tc.get("score")),
                "input_tokens_base": int((tb.get("tokens") or {}).get("input") or 0) if tb else None,
                "input_tokens_cand": int((tc.get("tokens") or {}).get("input") or 0) if tc else None,
                "min_base": round(float(tb["seconds"]) / 60.0, 2) if tb.get("seconds") is not None else None,
                "min_cand": round(float(tc["seconds"]) / 60.0, 2) if tc.get("seconds") is not None else None,
            }
        )

    comparison = {
        "baseline_run_id": base_sum["run_id"],
        "candidate_run_id": cand_sum["run_id"],
        "shared_task_ids": shared_ids,
        "gate": gate,
        "rationale": rationale,
        "shared_metrics": {
            "baseline": base_shared,
            "candidate": cand_shared,
            "delta_score": delta_score,
            "delta_input_tokens_per_task": delta_in,
            "delta_input_tokens_pct": delta_in_pct,
            "delta_wall_clock_min_per_task": delta_min,
        },
        "overall_metrics": {
            "baseline": base_all,
            "candidate": cand_all,
        },
        "per_task": per_task,
    }

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_comparison_markdown(comparison), encoding="utf-8", newline="\n")

    return comparison


def render_comparison_markdown(cmp_data: Dict[str, Any]) -> str:
    """Render side-by-side Run N vs Run N-1 A/B scoreboard as markdown."""
    sh = cmp_data["shared_metrics"]
    sb = sh["baseline"]
    sc = sh["candidate"]
    ov = cmp_data["overall_metrics"]
    ob = ov["baseline"]
    oc = ov["candidate"]

    lines: List[str] = [
        f"# PLE A/B Scoreboard: Run `{cmp_data['candidate_run_id']}` vs Baseline Run `{cmp_data['baseline_run_id']}`",
        "",
        f"## Gate Decision: **{cmp_data['gate']}**",
        f"> {cmp_data['rationale']}",
        "",
        f"## 1. Apples-to-Apples Shared Tasks ({len(cmp_data['shared_task_ids'])} task(s))",
        "",
        "| Metric | Baseline (`" + str(cmp_data["baseline_run_id"]) + "`) | Candidate (`" + str(cmp_data["candidate_run_id"]) + "`) | Delta |",
        "| :--- | ---: | ---: | ---: |",
        f"| Mean Rubric Score | {sb['mean_score']:.4f} | {sc['mean_score']:.4f} | {sh['delta_score']:+.4f} |"
        if sb["mean_score"] is not None and sc["mean_score"] is not None
        else "| Mean Rubric Score | - | - | - |",
        f"| Input Tokens / Task | {sb['input_tokens_per_task']:,.0f} | {sc['input_tokens_per_task']:,.0f} | {sh['delta_input_tokens_per_task']:+,.0f} ({sh['delta_input_tokens_pct']:+.1f}%) |",
        f"| Wall-Clock Min / Task | {sb['wall_clock_min_per_task']:.2f}m | {sc['wall_clock_min_per_task']:.2f}m | {sh['delta_wall_clock_min_per_task']:+.2f}m |",
        f"| Est. Cost / Task (USD) | ${sb['cost_per_task_usd']:.2f} | ${sc['cost_per_task_usd']:.2f} | ${sc['cost_per_task_usd'] - sb['cost_per_task_usd']:+.2f} |",
        "",
        f"## 2. Full-Run Averages (Baseline n={ob['count']}, Candidate n={oc['count']})",
        "",
        f"- **Mean Score:** {ob['mean_score']:.4f} -> {oc['mean_score']:.4f}"
        if ob["mean_score"] is not None and oc["mean_score"] is not None
        else "- **Mean Score:** -",
        f"- **Input Tokens / Task:** {ob['input_tokens_per_task']:,.0f} -> {oc['input_tokens_per_task']:,.0f}",
        f"- **Wall-Clock Min / Task:** {ob['wall_clock_min_per_task']:.2f}m -> {oc['wall_clock_min_per_task']:.2f}m",
        "",
        "## 3. Per-Task Breakdown",
        "",
        "| Task | Score (Base -> Cand) | Input Tokens / Task (Base -> Cand) | Wall Min (Base -> Cand) |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for row in cmp_data.get("per_task") or []:
        s_b = f"{row['score_base']:.4f}" if row["score_base"] is not None else "-"
        s_c = f"{row['score_cand']:.4f}" if row["score_cand"] is not None else "-"
        i_b = f"{row['input_tokens_base']:,}" if row["input_tokens_base"] is not None else "-"
        i_c = f"{row['input_tokens_cand']:,}" if row["input_tokens_cand"] is not None else "-"
        m_b = f"{row['min_base']:.1f}m" if row["min_base"] is not None else "-"
        m_c = f"{row['min_cand']:.1f}m" if row["min_cand"] is not None else "-"
        lines.append(f"| `{row['task_id']}` | {s_b} -> {s_c} | {i_b} -> {i_c} | {m_b} -> {m_c} |")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CLI Entrypoint (`mine`, `profile`, `replay`, `compare`, `all`)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PLE Tune-Up Shop: zero-API offline failure miner, token profiler, golden replay, and A/B gate."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--cache-dir", default=str(REPO_ROOT / ".eval_cache"), help="Eval artifact cache root.")
        p.add_argument("--run-dir", default="", help="Explicit run artifact directory.")
        p.add_argument("--run-id", default="", help="Run ID under --cache-dir (defaults to latest run).")

    p_mine = sub.add_parser("mine", help="Mine ranked rubric failures and write gap_report.md.")
    _add_common(p_mine)
    p_mine.add_argument("--out", default=str(REPO_ROOT / "tools" / "gap_report.md"), help="Output gap_report.md path.")

    p_prof = sub.add_parser("profile", help="Profile token waste, read loops, retry storms, and verifier rejections.")
    _add_common(p_prof)
    p_prof.add_argument("--out", default=str(REPO_ROOT / "tools" / "profile_report.json"), help="Output profile JSON path.")

    p_rep = sub.add_parser("replay", help="Extract or evaluate deterministic golden replay snapshots.")
    _add_common(p_rep)
    p_rep.add_argument("--out-dir", default=str(REPO_ROOT / "tools" / "golden_replays"), help="Golden replays directory.")
    p_rep.add_argument("--top-k", type=int, default=15, help="Number of top checks to snapshot.")
    p_rep.add_argument("--verify", default="", help="Optional candidate deliverable directory to verify offline.")

    p_cmp = sub.add_parser("compare", help="Compare Run N vs Run N-1 with PROMOTE/HOLD gate.")
    p_cmp.add_argument("--cache-dir", default=str(REPO_ROOT / ".eval_cache"), help="Eval artifact cache root.")
    p_cmp.add_argument("--baseline-dir", default="", help="Baseline Run N-1 directory.")
    p_cmp.add_argument("--candidate-dir", default="", help="Candidate Run N directory.")
    p_cmp.add_argument("--baseline-id", default="", help="Baseline run ID under --cache-dir.")
    p_cmp.add_argument("--candidate-id", default="", help="Candidate run ID under --cache-dir.")
    p_cmp.add_argument("--out", default=str(REPO_ROOT / "tools" / "compare_report.md"), help="Comparison markdown output.")

    p_all = sub.add_parser("all", help="Run mine + profile + replay + compare end-to-end locally.")
    _add_common(p_all)
    p_all.add_argument("--baseline-dir", default="", help="Optional explicit baseline Run N-1 directory.")
    p_all.add_argument("--baseline-id", default="", help="Optional baseline run ID under --cache-dir.")
    p_all.add_argument("--tools-dir", default=str(REPO_ROOT / "tools"), help="Directory for output artifacts.")
    p_all.add_argument("--top-k", type=int, default=15, help="Number of golden replay snapshots to extract.")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cache_dir = Path(args.cache_dir)

    if args.command == "mine":
        rdir = resolve_run_dir(cache_dir, run_dir=args.run_dir or None, run_id=args.run_id or None)
        if rdir is None:
            sys.stderr.write("error: could not locate run directory\n")
            return 2
        res = mine_rubric_failures(rdir, report_out=Path(args.out))
        print(
            f"[mine] run={res['run_id']} tasks={res['task_count']} "
            f"distinct_failed={res['distinct_failed_checks']} occ={res['total_failed_occurrences']} -> {args.out}"
        )
        return 0

    if args.command == "profile":
        rdir = resolve_run_dir(cache_dir, run_dir=args.run_dir or None, run_id=args.run_id or None)
        if rdir is None:
            sys.stderr.write("error: could not locate run directory\n")
            return 2
        res = profile_run(rdir, out_path=Path(args.out))
        tot = res["totals"]
        print(
            f"[profile] run={res['run_id']} read_loops={tot['read_loops']} "
            f"verify_suppressed={tot['verify_reads_suppressed']} retries={tot['retry_count']} "
            f"retry_waste={tot['retry_token_waste']:,}tok (${tot['retry_cost_waste_usd']:.2f}) "
            f"verifier_rej_rate={tot['verifier_rejection_rate']:.1%} -> {args.out}"
        )
        return 0

    if args.command == "replay":
        out_dir = Path(args.out_dir)
        if args.verify:
            eval_res = evaluate_golden_replays(out_dir, candidate_dir=Path(args.verify))
            print(f"[replay --verify] passed={eval_res['passed']}/{eval_res['total']} failed={eval_res['failed']}")
            return 0 if eval_res["failed"] == 0 else 1
        rdir = resolve_run_dir(cache_dir, run_dir=args.run_dir or None, run_id=args.run_id or None)
        if rdir is None:
            sys.stderr.write("error: could not locate run directory\n")
            return 2
        man = extract_golden_replays(rdir, out_dir=out_dir, top_k=args.top_k)
        print(f"[replay] extracted {man['snapshot_count']} golden snapshots -> {out_dir}")
        return 0

    if args.command == "compare":
        cand_dir = resolve_run_dir(cache_dir, run_dir=args.candidate_dir or None, run_id=args.candidate_id or None)
        base_dir = resolve_run_dir(
            cache_dir,
            run_dir=args.baseline_dir or None,
            run_id=args.baseline_id or None,
            offset_from_latest=1,
        )
        if cand_dir is None or base_dir is None:
            sys.stderr.write("error: need both baseline and candidate run directories\n")
            return 2
        cmp_res = compare_runs(base_dir, cand_dir, out_path=Path(args.out))
        print(f"[compare] {cmp_res['baseline_run_id']} -> {cmp_res['candidate_run_id']} GATE={cmp_res['gate']} | {cmp_res['rationale']}")
        return 0

    if args.command == "all":
        tools_dir = Path(args.tools_dir)
        cand_dir = resolve_run_dir(cache_dir, run_dir=args.run_dir or None, run_id=args.run_id or None)
        if cand_dir is None:
            sys.stderr.write("error: could not locate candidate run directory\n")
            return 2
        mined = mine_rubric_failures(cand_dir, report_out=tools_dir / "gap_report.md")
        prof = profile_run(cand_dir, out_path=tools_dir / "profile_report.json")
        man = extract_golden_replays(
            cand_dir, out_dir=tools_dir / "golden_replays", top_k=args.top_k, mined=mined
        )
        tot = prof["totals"]
        print(
            f"[mine] run={mined['run_id']} tasks={mined['task_count']} "
            f"distinct={mined['distinct_failed_checks']} occ={mined['total_failed_occurrences']} -> {tools_dir / 'gap_report.md'}"
        )
        print(
            f"[profile] read_loops={tot['read_loops']} verify_suppressed={tot['verify_reads_suppressed']} "
            f"retries={tot['retry_count']} retry_waste={tot['retry_token_waste']:,} (${tot['retry_cost_waste_usd']:.2f}) "
            f"ver_rej_rate={tot['verifier_rejection_rate']:.1%} -> {tools_dir / 'profile_report.json'}"
        )
        print(f"[replay] snapshots={man['snapshot_count']} -> {tools_dir / 'golden_replays'}")

        base_dir = resolve_run_dir(
            cache_dir,
            run_dir=args.baseline_dir or None,
            run_id=args.baseline_id or None,
            offset_from_latest=1,
        )
        if base_dir is not None and base_dir.resolve() != cand_dir.resolve():
            cmp_res = compare_runs(base_dir, cand_dir, out_path=tools_dir / "compare_report.md")
            print(
                f"[compare] {cmp_res['baseline_run_id']} -> {cmp_res['candidate_run_id']} "
                f"GATE={cmp_res['gate']} | {cmp_res['rationale']} -> {tools_dir / 'compare_report.md'}"
            )
        else:
            print("[compare] skipped (no baseline run found)")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
