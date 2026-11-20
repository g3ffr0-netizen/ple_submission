import json
import unittest


class TestHarnessBasics(unittest.TestCase):
    def test_json_verification_payload(self):
        sample = '{"check": "passed", "note": "microgrid verified"}'
        parsed = json.loads(sample)
        self.assertEqual(parsed["check"], "passed")
        self.assertIn("microgrid", parsed["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
