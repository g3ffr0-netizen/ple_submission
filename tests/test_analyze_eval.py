"""Unit tests for the local PLE evaluation post-mortem analyzer (tools/analyze_eval.py)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import analyze_eval


class TestAnalyzeEval(unittest.TestCase):
    def test_checks_connectivity_noise_json_and_regression_compare(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Run A: 0.908 PASS
            dir_a = root / "run_45204910959" / "ple-task-data-hall-mep-routing-offset-45204910959"
            (dir_a / "logs").mkdir(parents=True)
            (dir_a / "results.json").write_text(
                json.dumps({"model": "google-cloud/gemini-4-argon", "tasks": [{"id": "data-hall-mep-routing-offset", "score": 0.908, "status": "ok", "seconds": 2800}]}),
                encoding="utf-8",
            )
            (dir_a / "logs" / "dev_verifier_metrics.json").write_text(
                json.dumps(
                    {
                        "combined_score": 0.908,
                        "exec_status": "OK",
                        "clash_count": 0,
                        "gates": {"gate_1_no_hard_clash": True, "gate_2_connectivity": {"passed": True}},
                        "secondary": {
                            "violation_detail": {"rule_counts": {"clear_height": 10}},
                            "network_complexity": {
                                "segments_per_service": {"cable_tray": 4, "busway": 4, "chw_pipe": 5},
                                "components_per_service": {"cable_tray": 1, "busway": 1, "chw_pipe": 1},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            (dir_a / "logs" / "dev_agent_serve.log").write_text(
                'INFO: HTTP Request: POST http://host/v1 "HTTP/1.1 200 OK"\n',
                encoding="utf-8",
            )
            (dir_a / "logs" / "dev_agent_ple-submission.txt").write_text(
                "harness: audit(post-fix): errors=0 warnings=10\nharness: verifier: truncated=False audit_errors=0 PASS\n",
                encoding="utf-8",
            )

            # Run B: 0.000 ERR_CONNECTIVITY + concatenated stdout JSON
            dir_b = root / "run_45221745380" / "ple-task-data-hall-mep-routing-offset-45221745380"
            (dir_b / "logs").mkdir(parents=True)
            (dir_b / "results.json").write_text(
                json.dumps({"model": "google-cloud/gemini-4-argon", "tasks": [{"id": "data-hall-mep-routing-offset", "score": 0.0, "status": "ok", "seconds": 2670}]}),
                encoding="utf-8",
            )
            payload_b = {
                "combined_score": 0.0,
                "exec_status": "ERR_CONNECTIVITY",
                "clash_count": 0,
                "gates": {
                    "gate_1_no_hard_clash": True,
                    "gate_2_connectivity": {
                        "passed": False,
                        "disconnected": {"cable_tray": "9 separate networks", "busway": "9 separate networks"},
                        "unserved_rows": {},
                        "unreached_consumers": [],
                    },
                },
                "secondary": {
                    "violation_detail": {"rule_counts": {"clear_height": 10, "tray_stack_headroom": 11, "power_telecom_separation": 1}},
                    "network_complexity": {
                        "segments_per_service": {"cable_tray": 14, "busway": 14, "chw_pipe": 7},
                        "components_per_service": {"cable_tray": 9, "busway": 9, "chw_pipe": 1},
                    },
                    "extended": {"achieved_clearances": {"min_tray_busway_separation_mm": 266.5}},
                },
            }
            (dir_b / "logs" / "dev_verifier_eval.stdout.txt").write_text(
                json.dumps(payload_b) + "\nWARNING trailing text\n{\"log\": true}\n",
                encoding="utf-8",
            )
            (dir_b / "logs" / "dev_agent_ple-submission.txt").write_text(
                "harness: audit: errors=0 warnings=17\nharness: verifier: truncated=False audit_errors=0 PASS\n",
                encoding="utf-8",
            )

            report = analyze_eval.analyze_directories([root / "run_45204910959", root / "run_45221745380"])
            self.assertEqual(len(report["tasks"]), 2)
            cats = report["by_category"]
            self.assertIn("hard_constraint_h1", cats)
            self.assertIn("ifc_geometry_clearance", cats)
            self.assertTrue(any("gate_2_connectivity" in x["rule"] for x in cats["hard_constraint_h1"]))
            self.assertTrue(any("disconnected_service_network" in x["rule"] for x in cats["ifc_geometry_clearance"]))

            cmps = analyze_eval.build_comparison(report["tasks"], "45204910959", "45221745380", "data-hall-mep-routing-offset")
            self.assertEqual(len(cmps), 1)
            md = analyze_eval.format_markdown(report, comparisons=cmps)
            self.assertIn("Side-by-Side Regression Comparison", md)
            self.assertIn("0.908", md)
            self.assertIn("ERR_CONNECTIVITY", md)

    def test_rubric_h1_deduction_judge_429_and_turn_truncation_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run_45233521493" / "ple-task-solar-witness-test-package-45233521493"
            (root / "logs").mkdir(parents=True)
            (root / "results.json").write_text(
                json.dumps({"model": "google-cloud/gemini-4-argon", "tasks": [{"id": "solar-witness-test-package", "score": 0.625, "status": "ok", "seconds": 1800}]}),
                encoding="utf-8",
            )
            (root / "logs" / "dev_verifier_eval.stdout.txt").write_text(
                json.dumps(
                    {
                        "combined_score": 0.625,
                        "criteria": [
                            {"name": "[h=1] Include witnessed test date table", "score": 0.0, "max_score": 1.0, "passed": False, "reason": "Missing signed date column."},
                            {"name": "Provide voltage tolerance discussion", "score": 0.5, "max_score": 1.0, "passed": False, "reason": "Incomplete feeder explanation."},
                            {"name": "Executive summary present", "score": 1.0, "max_score": 1.0, "passed": True, "reason": "Good"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "logs" / "dev_verifier_grade.failed.json").write_text(
                json.dumps({"grader_failed": "judge failed: HTTP 429 RESOURCE_EXHAUSTED"}),
                encoding="utf-8",
            )
            (root / "logs" / "dev_verifier_programmatic_ver_deadbeef.json").write_text(
                json.dumps({"score": 0.0, "status": "valid"}),
                encoding="utf-8",
            )
            (root / "logs" / "dev_verifier_programmatic_ver_deadbeef.log").write_text(
                "exit 0\nverdict: fail\n[code_verifier] disagreeing with the package on ['darnwick']\n",
                encoding="utf-8",
            )
            (root / "logs" / "dev_agent_serve.log").write_text(
                'POST "HTTP/1.1 200 OK"\nPOST "HTTP/1.1 429 Too Many Requests"\nPOST "HTTP/1.1 503 Service Unavailable"\n',
                encoding="utf-8",
            )
            (root / "logs" / "dev_agent_ple-submission.txt").write_text(
                "step reached max_turns=55\n"
                "harness: worker: truncated=True turns=55\n"
                "harness: audit: errors=1 missing/placeholder IDs\n"
                "harness: verifier: truncated=True audit_errors=1\n"
                "model call failed (429); retrying in 5 s\n",
                encoding="utf-8",
            )

            report = analyze_eval.analyze_directories([Path(tmp) / "run_45233521493"])
            self.assertEqual(len(report["tasks"]), 1)
            rec = report["tasks"][0]
            self.assertEqual(rec["trace"]["http_counts"], {"200": 1, "429": 1, "503": 1})
            self.assertTrue(rec["trace"]["worker_truncated"])
            self.assertTrue(rec["trace"]["verifier_truncated"])

            cats = report["by_category"]
            self.assertIn("hard_constraint_h1", cats)
            self.assertIn("rubric_coverage_gaps", cats)
            self.assertIn("turn_budget_exhaustion", cats)
            self.assertIn("infra_api_faults", cats)
            self.assertIn("register_schema_reconciliation", cats)
            self.assertTrue(any("programmatic_ver_deadbeef" in x["rule"] for x in cats["register_schema_reconciliation"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
