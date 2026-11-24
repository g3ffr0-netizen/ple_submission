"""Unit tests for the PLE Tune-Up Shop (tools/tuneup.py)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import tuneup


class TestTuneupShop(unittest.TestCase):
    def _make_task_bundle(
        self,
        root: Path,
        run_id: str,
        task_id: str,
        *,
        score: float = 0.4,
        seconds: int = 1200,
        tokens: dict | None = None,
        cost: float = 4.0,
    ) -> Path:
        task_dir = root / f"run_{run_id}" / f"ple-task-{task_id}-{run_id}"
        (task_dir / "logs").mkdir(parents=True, exist_ok=True)
        (task_dir / "artifacts" / task_id).mkdir(parents=True, exist_ok=True)
        tok = tokens or {"input": 100_000, "cache": 50_000, "output": 2_000}
        (task_dir / "results.json").write_text(
            json.dumps(
                {
                    "model": "google-cloud/gemini-4-argon",
                    "tasks": [
                        {
                            "id": task_id,
                            "score": score,
                            "status": "ok",
                            "seconds": seconds,
                            "tokens": tok,
                            "estimated_cost_usd": cost,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return task_dir

    def test_mine_handles_truncated_json_and_all_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run_90000001"

            # Task A: Division-26 truncated JSON + register missing hard
            t_a = self._make_task_bundle(root, "90000001", "div26-task", score=0.30)
            truncated_json = """{
  "combined_score": 0.30,
  "parse_ok": true,
  "register_missing_hard": ["R-260573-02", "R-ILLUM"],
  "register_recall_hard": 0.75,
  "families_satisfied": {
    "FMT_REFERENCES": false,
    "LIGHTING_PERFORMANCE": false
  },
  "family_pass_fraction": {
    "FMT_REFERENCES": 0.5,
    "LIGHTING_PERFORMANCE": 0.0
  },
  "per_item": {
    "26 05 73": {
      "present": true,
      "families": {
        "FMT_REFERENCES": {
          "ok": false,
          "tag": "soft",
          "weight": 0.4,
          "detail": "listed but not cited: ['IEEE C2']"
        }
      }
    },
    "26 20 00": {
      "families": {
... [omitted 335 line(s)] ...
        "FMT_REFERENCES": {
          "ok": false,
          "tag": "soft",
          "weight": 0.4,
          "detail": "listed but not cited: ['NETA ATS']"
        }
      }
    }
  }
}"""
            (t_a / "logs" / "dev_verifier_metrics.json").write_text(truncated_json, encoding="utf-8")

            # Task B: Heat-rejection artifacts schema + generic criteria + tool truncation
            t_b = self._make_task_bundle(root, "90000001", "heat-task", score=0.38)
            (t_b / "logs" / "dev_verifier_artifacts.json").write_text(
                json.dumps(
                    {
                        "combined_score": 0.38,
                        "items": {
                            "ANNEX": {
                                "families": {"H07_water_allocation": False, "H01_design_wetbulb": True},
                                "notes": {"H07": "annual 12608 kgal vs cap 12600 kgal"},
                                "margins": {"water allocation": -0.00067},
                            },
                            "LAB": {
                                "families": {"H07_water_allocation": False},
                                "notes": {"H07": "peak day makeup exceeded"},
                            },
                        },
                        "criteria": [
                            {
                                "id": "TOOL_LOOP_GUARD",
                                "score": 0.0,
                                "max_score": 1.0,
                                "passed": False,
                                "reason": "worker truncated=True at max_turns",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report_path = root / "gap_report.md"
            mined = tuneup.mine_rubric_failures(run_dir, report_out=report_path)
            self.assertEqual(mined["task_count"], 2)
            checks = {(c["task_id"], c["check_id"]): c for c in mined["ranked_checks"]}

            # Recovered truncated second section FMT_REFERENCES + first section => freq == 2
            self.assertIn(("div26-task", "FMT_REFERENCES"), checks)
            self.assertEqual(checks[("div26-task", "FMT_REFERENCES")]["frequency"], 2)
            self.assertEqual(checks[("div26-task", "FMT_REFERENCES")]["root_cause"], "citation_gap")

            # Aggregate family not seen in per_item captured
            self.assertIn(("div26-task", "LIGHTING_PERFORMANCE"), checks)
            self.assertEqual(checks[("div26-task", "LIGHTING_PERFORMANCE")]["root_cause"], "calc_or_unit_error")

            # Register missing hard captured
            self.assertEqual(checks[("div26-task", "REGISTER_MISSING_HARD")]["root_cause"], "missing_deliverable")

            # Heat rejection freq=2, hard weight=2.5 => impact 5.0 sorted at top
            self.assertIn(("heat-task", "H07_water_allocation"), checks)
            self.assertEqual(checks[("heat-task", "H07_water_allocation")]["weight_x_frequency"], 5.0)
            self.assertEqual(mined["ranked_checks"][0]["check_id"], "H07_water_allocation")

            # Tool loop bucket populated
            self.assertIn(("heat-task", "TOOL_LOOP_GUARD"), checks)
            self.assertEqual(checks[("heat-task", "TOOL_LOOP_GUARD")]["root_cause"], "tool_or_context_loop")

            md = report_path.read_text(encoding="utf-8")
            self.assertIn("H07_water_allocation", md)
            self.assertIn("missing_deliverable", md)
            self.assertIn("calc_or_unit_error", md)
            self.assertIn("citation_gap", md)
            self.assertIn("tool_or_context_loop", md)

    def test_read_loop_not_triggered_after_edit_or_write_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t_dir = self._make_task_bundle(root, "90000002", "loop-test")
            same_text = "alpha = 1\nbeta = 2\n" * 50
            traj = {
                "schema_version": "ATIF-v1.7",
                "steps": [
                    # a.py: read -> edit_file -> verify read (IDENTICAL hash) => MUST NOT flag!
                    {
                        "step_id": 1,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "c1", "function_name": "read_file", "arguments": {"path": "/app/a.py"}}],
                        "observation": {"results": [{"source_call_id": "c1", "content": same_text}]},
                    },
                    {
                        "step_id": 2,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "c2", "function_name": "edit_file", "arguments": {"path": "/app/a.py"}}],
                        "observation": {"results": [{"source_call_id": "c2", "content": "edited"}]},
                    },
                    {
                        "step_id": 3,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "c3", "function_name": "read_file", "arguments": {"path": "/app/a.py"}}],
                        "observation": {"results": [{"source_call_id": "c3", "content": same_text}]},
                    },
                    # b.py: read -> write_file -> verify read (IDENTICAL hash) => MUST NOT flag!
                    {
                        "step_id": 4,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "c4", "function_name": "read_file", "arguments": {"path": "/app/b.py"}}],
                        "observation": {"results": [{"source_call_id": "c4", "content": same_text}]},
                    },
                    {
                        "step_id": 5,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "c5", "function_name": "write_file", "arguments": {"path": "/app/b.py"}}],
                        "observation": {"results": [{"source_call_id": "c5", "content": "wrote"}]},
                    },
                    {
                        "step_id": 6,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "c6", "function_name": "read_file", "arguments": {"path": "/app/b.py"}}],
                        "observation": {"results": [{"source_call_id": "c6", "content": same_text}]},
                    },
                    # d.py: read -> read with DIFFERENT hash => MUST NOT flag!
                    {
                        "step_id": 7,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "c7", "function_name": "read_file", "arguments": {"path": "/app/d.py"}}],
                        "observation": {"results": [{"source_call_id": "c7", "content": "v1"}]},
                    },
                    {
                        "step_id": 8,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "c8", "function_name": "read_file", "arguments": {"path": "/app/d.py"}}],
                        "observation": {"results": [{"source_call_id": "c8", "content": "v2 changed"}]},
                    },
                    # c.py: read -> read with IDENTICAL hash and NO write/edit => MUST flag READ_LOOP_SUSPECTED!
                    {
                        "step_id": 9,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "c9", "function_name": "read_file", "arguments": {"path": "/app/c.py"}}],
                        "observation": {"results": [{"source_call_id": "c9", "content": same_text}]},
                    },
                    {
                        "step_id": 10,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "c10", "function_name": "read_file", "arguments": {"path": "/app/c.py"}}],
                        "observation": {"results": [{"source_call_id": "c10", "content": same_text}]},
                    },
                ],
                "final_metrics": {"total_prompt_tokens": 50_000, "total_completion_tokens": 1_000, "total_cached_tokens": 0},
            }
            (t_dir / "artifacts" / "loop-test" / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")

            prof = tuneup.profile_run(root / "run_90000002")
            tp = prof["tasks"][0]
            self.assertIn("READ_LOOP_SUSPECTED", tp["flags"])
            self.assertEqual(tp["read_loop"]["count"], 1)
            self.assertEqual(tp["read_loop"]["events"][0]["path"], "/app/c.py")
            self.assertEqual(tp["read_loop"]["verify_reads_suppressed"], 3)

            # Remove c.py duplicate read -> READ_LOOP_SUSPECTED must clear completely
            traj["steps"] = traj["steps"][:8]
            (t_dir / "artifacts" / "loop-test" / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
            prof_clean = tuneup.profile_run(root / "run_90000002")
            self.assertNotIn("READ_LOOP_SUSPECTED", prof_clean["tasks"][0]["flags"])
            self.assertEqual(prof_clean["tasks"][0]["read_loop"]["count"], 0)
            self.assertEqual(prof_clean["tasks"][0]["read_loop"]["verify_reads_suppressed"], 3)

    def test_retry_storm_context_bloat_and_verifier_clustering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t_dir = self._make_task_bundle(
                root,
                "90000003",
                "profile-rich",
                tokens={"input": 10_000_000, "cache": 5_000_000, "output": 100_000},
                cost=6.0,
            )
            (t_dir / "logs" / "dev_agent_serve.log").write_text(
                "\n".join(
                    [
                        'POST "HTTP/1.1 200 OK"',
                        'POST "HTTP/1.1 429 Too Many Requests"',
                        'Retrying request in 0.4s',
                        'POST "HTTP/1.1 503 Service Unavailable"',
                        'POST "HTTP/1.1 200 OK"',
                    ]
                    + ['POST "HTTP/1.1 200 OK"'] * 15
                ),
                encoding="utf-8",
            )
            (t_dir / "logs" / "dev_agent_ple-submission.txt").write_text(
                "\n".join(
                    [
                        "harness: verifier: truncated=False audit_errors=0 1. **Disallowed cross-reference in specs/26 24 13.docx:** Section 26 27 14.00 20 is not part of the project manual.",
                        "2. **Unresolved `[_____]` blank placeholder** turns into `as indicated`.",
                        "exec echo ok",
                        "harness: verifier: truncated=True audit_errors=0 1. **Missing deliverable table** in summary.",
                        "harness: verifier: truncated=False audit_errors=0 prefilter_errors=0 readback_errors=0 PASS",
                    ]
                ),
                encoding="utf-8",
            )
            huge_unused_dump = ("IRRELEVANT_LOG_NOISE_998877 " * 1200)  # ~30k chars, never referenced later
            used_later_txt = "CRITICAL_TABLE_TOKEN_ABC123 value=42.5"
            traj = {
                "schema_version": "ATIF-v1.7",
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "huge_old_1", "function_name": "exec", "arguments": {"command": "dump"}}],
                        "observation": {"results": [{"source_call_id": "huge_old_1", "content": huge_unused_dump}]},
                    },
                    {
                        "step_id": 2,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "fail_2", "function_name": "exec", "arguments": {"command": "run_calc.py"}}],
                        "observation": {"results": [{"source_call_id": "fail_2", "content": "exit_code=1\nTraceback..."}]},
                    },
                    {
                        "step_id": 3,
                        "source": "agent",
                        "message": "",
                        "tool_calls": [{"tool_call_id": "retry_3", "function_name": "exec", "arguments": {"command": "run_calc.py"}}],
                        "observation": {"results": [{"source_call_id": "retry_3", "content": used_later_txt}]},
                    },
                    {
                        "step_id": 4,
                        "source": "agent",
                        "message": "Using CRITICAL_TABLE_TOKEN_ABC123 in final calc.",
                        "tool_calls": [{"tool_call_id": "ok_4", "function_name": "exec", "arguments": {"command": "echo done"}}],
                        "observation": {"results": [{"source_call_id": "ok_4", "content": "CRITICAL_TABLE_TOKEN_ABC123 ok"}]},
                    },
                ],
                "final_metrics": {"total_prompt_tokens": 10_000_000, "total_completion_tokens": 100_000, "total_cached_tokens": 5_000_000},
            }
            (t_dir / "artifacts" / "profile-rich" / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")

            prof = tuneup.profile_run(root / "run_90000003")
            tp = prof["tasks"][0]
            self.assertIn("RETRY_STORM", tp["flags"])
            self.assertEqual(tp["retry_storm"]["api_retries"], 2)
            self.assertEqual(tp["retry_storm"]["tool_retries"], 1)
            self.assertEqual(tp["retry_storm"]["retry_count"], 3)
            self.assertGreater(tp["retry_storm"]["retry_token_waste"], 1_000_000)
            self.assertGreater(tp["retry_storm"]["retry_cost_waste_usd"], 0.0)

            self.assertIn("CONTEXT_BLOAT", tp["flags"])
            prune_list = tp["context_bloat"]["suggested_prune"]
            self.assertTrue(prune_list)
            self.assertEqual(prune_list[0]["tool_call_id"], "huge_old_1")

            self.assertEqual(tp["verifier"]["verifier_calls"], 3)
            self.assertEqual(tp["verifier"]["verifier_rejections"], 2)
            self.assertAlmostEqual(tp["verifier"]["verifier_rejection_rate"], 0.6667, places=3)
            clusters = {r["reason_cluster"] for r in prof["top_rejection_reasons"]}
            self.assertIn("disallowed_cross_reference", clusters)
            self.assertIn("unresolved_placeholders_or_brackets", clusters)

    def test_golden_replay_extraction_and_offline_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run_90000004"
            t_dir = self._make_task_bundle(root, "90000004", "spec-task", score=0.3)
            (t_dir / "logs" / "dev_verifier_metrics.json").write_text(
                json.dumps(
                    {
                        "register_missing_hard": ["R-260573-02"],
                        "per_item": {
                            "26 05 73": {
                                "families": {
                                    "FMT_NO_UFC": {"ok": False, "tag": "hard", "weight": 0.4, "detail": "Found UFC 3-520-01"},
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (t_dir / "artifacts" / "spec-task" / "final_message.md").write_text("Spec draft summary", encoding="utf-8")

            replays_dir = root / "golden_replays"
            man = tuneup.extract_golden_replays(run_dir, replays_dir, top_k=5)
            self.assertEqual(man["snapshot_count"], 2)
            self.assertTrue((replays_dir / "manifest.json").is_file())

            # Bad candidate: missing R-260573-02 and still contains "UFC 3-520-01"
            cand_bad = root / "cand_bad" / "spec-task"
            cand_bad.mkdir(parents=True)
            (cand_bad / "out.txt").write_text("Per UFC 3-520-01 criteria only.", encoding="utf-8")
            eval_bad = tuneup.evaluate_golden_replays(replays_dir, candidate_dir=root / "cand_bad")
            self.assertEqual(eval_bad["failed"], 2)

            # Good candidate: includes R-260573-02 and removes UFC references
            cand_good = root / "cand_good" / "spec-task"
            cand_good.mkdir(parents=True)
            (cand_good / "out.txt").write_text("Satisfies R-260573-02 per IES-26.", encoding="utf-8")
            eval_good = tuneup.evaluate_golden_replays(replays_dir, candidate_dir=root / "cand_good")
            self.assertEqual(eval_good["failed"], 0)
            self.assertEqual(eval_good["passed"], 2)

    def test_compare_promote_and_hold_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Baseline Run 10 has 2 tasks (extra task t_extra + shared t1)
            b_dir = root / "run_10" / "ple-dev-eval-10"
            b_dir.mkdir(parents=True)
            (b_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "tasks": [
                            {"id": "t1", "score": 0.40, "seconds": 600, "tokens": {"input": 1000, "cache": 0, "output": 100}, "estimated_cost_usd": 1.0},
                            {"id": "t_extra", "score": 0.90, "seconds": 600, "tokens": {"input": 1000, "cache": 0, "output": 100}, "estimated_cost_usd": 1.0},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            def _write_cand(name: str, score: float, inp: int) -> Path:
                c = root / name / f"ple-dev-eval-{name}"
                c.mkdir(parents=True, exist_ok=True)
                (c / "summary.json").write_text(
                    json.dumps(
                        {
                            "tasks": [
                                {"id": "t1", "score": score, "seconds": 540, "tokens": {"input": inp, "cache": 0, "output": 100}, "estimated_cost_usd": 0.8}
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                return root / name

            # 1. Score improves -> PROMOTE
            c_up = _write_cand("run_11", 0.45, 1200)
            res_up = tuneup.compare_runs(root / "run_10", c_up)
            self.assertEqual(res_up["gate"], "PROMOTE")
            self.assertEqual(res_up["shared_task_ids"], ["t1"])

            # 2. Same score, lower input tokens -> PROMOTE
            c_tok = _write_cand("run_12", 0.40, 800)
            self.assertEqual(tuneup.compare_runs(root / "run_10", c_tok)["gate"], "PROMOTE")

            # 3. Score regresses even with lower tokens -> HOLD
            c_reg = _write_cand("run_13", 0.35, 500)
            self.assertEqual(tuneup.compare_runs(root / "run_10", c_reg)["gate"], "HOLD")

            # 4. Same score, higher input tokens -> HOLD
            c_worse_tok = _write_cand("run_14", 0.40, 1200)
            self.assertEqual(tuneup.compare_runs(root / "run_10", c_worse_tok)["gate"], "HOLD")

    def test_cli_all_end_to_end_on_synthetic_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / ".eval_cache"
            t1 = self._make_task_bundle(cache, "1001", "t1", score=0.30, seconds=1200)
            t2 = self._make_task_bundle(cache, "1002", "t1", score=0.45, seconds=900)
            (t1 / "logs" / "dev_verifier_metrics.json").write_text('{"register_missing_hard": ["R1"]}', encoding="utf-8")
            (t2 / "logs" / "dev_verifier_metrics.json").write_text('{"register_missing_hard": ["R2"]}', encoding="utf-8")
            (t2 / "artifacts" / "t1" / "trajectory.json").write_text('{"steps": [], "final_metrics": {}}', encoding="utf-8")

            tools_out = root / "tools_out"
            rc = tuneup.main(
                [
                    "all",
                    "--cache-dir",
                    str(cache),
                    "--tools-dir",
                    str(tools_out),
                    "--top-k",
                    "5",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue((tools_out / "gap_report.md").is_file())
            self.assertTrue((tools_out / "profile_report.json").is_file())
            self.assertTrue((tools_out / "golden_replays" / "manifest.json").is_file())
            self.assertTrue((tools_out / "compare_report.md").is_file())
            self.assertIn("REGISTER_MISSING_HARD", (tools_out / "gap_report.md").read_text(encoding="utf-8"))
            self.assertIn("PROMOTE", (tools_out / "compare_report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
