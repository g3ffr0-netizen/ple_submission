import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import HarnessExecutionEngine, extract_json_payload, make_verify_fn


def _make_engine(
    tmp_dir: str, max_retries: int = 3, max_failures: int = 3
) -> HarnessExecutionEngine:
    config_path = os.path.join(tmp_dir, "submission.yaml")
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(
            "model: gemini-4-hydrogen\n"
            "harness:\n"
            "  fallback_models:\n"
            "    - gemini-3.8-flash\n"
            "    - gemini-3.5-flash-lite\n"
            f"  max_retries: {max_retries}\n"
            f"  max_failures: {max_failures}\n"
            "  max_output_tokens: 4096\n"
        )
    return HarnessExecutionEngine(config_path=config_path)


class TestVerificationAndHarness(unittest.TestCase):
    def test_extract_json_payload_strips_fenced_block(self):
        raw = '```json\n{"status": "ok", "bus_kv": 1.0}\n```'
        parsed = json.loads(extract_json_payload(raw))
        self.assertEqual(parsed, {"status": "ok", "bus_kv": 1.0})

    def test_verify_rejects_malformed_json_even_with_required_phrase(self):
        verify = make_verify_fn(
            min_chars=10,
            required_phrase="microgrid verified",
            require_json=True,
        )
        ok, reason = verify("microgrid verified and operational {bad json")
        self.assertFalse(ok)
        self.assertIn("json", reason.lower())

    def test_verify_min_chars_exact_boundary(self):
        payload = '{"status":"ok","note":"microgrid verified and operational"}'
        verify_exact = make_verify_fn(
            min_chars=len(payload), required_phrase="", require_json=True
        )
        verify_over = make_verify_fn(
            min_chars=len(payload) + 1, required_phrase="", require_json=True
        )

        self.assertTrue(verify_exact(payload)[0])
        self.assertFalse(verify_over(payload)[0])

    def test_verify_phrase_case_insensitive_and_missing(self):
        verify = make_verify_fn(
            min_chars=1, required_phrase="Microgrid Verified", require_json=False
        )
        self.assertTrue(verify("prefix microgrid verified suffix")[0])
        self.assertFalse(verify("missing the target text")[0])

    def test_retry_prompt_carries_failure_reason_and_rotates_models(self):
        seen_calls = []

        def fake_generate(prompt: str, model: str, max_output_tokens: int) -> str:
            seen_calls.append((model, prompt, max_output_tokens))
            if len(seen_calls) == 1:
                return '{"status": "ok"}'
            return (
                '{"status": "ok", "detail": "padded response that easily exceeds '
                'the minimum length requirement of one hundred twenty characters"}'
            )

        verify = make_verify_fn(min_chars=120, required_phrase="", require_json=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            engine = _make_engine(tmp_dir, max_retries=3, max_failures=3)
            result = engine.run_with_verification("return json", fake_generate, verify)

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(seen_calls), 2)
        self.assertEqual(seen_calls[0][0], "gemini-4-hydrogen")
        self.assertEqual(seen_calls[1][0], "gemini-3.8-flash")
        self.assertIn("minimum required is 120", seen_calls[1][1].lower())

    def test_circuit_breaker_locks_after_max_failures_and_resets_cleanly(self):
        def always_bad(prompt: str, model: str, max_output_tokens: int) -> str:
            return "not json"

        verify = make_verify_fn(
            min_chars=1,
            required_phrase="impossible_phrase_xyz",
            require_json=True,
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            engine = _make_engine(tmp_dir, max_retries=1, max_failures=3)
            for _ in range(3):
                engine.run_with_verification("test", always_bad, verify)

            self.assertEqual(engine.failure_count, 3)
            self.assertTrue(engine.locked)

            engine.reset()
            self.assertFalse(engine.locked)
            self.assertEqual(engine.failure_count, 0)


if __name__ == "__main__":
    unittest.main()
