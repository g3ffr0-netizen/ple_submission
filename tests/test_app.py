import importlib.util
import json
import os
import sys
import types as py_types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent


def _install_streamlit_stub() -> py_types.ModuleType:
    """Create a fresh Streamlit module stub so importing app.py does not require a live runtime."""

    class _SessionState(dict):
        def __getattr__(self, key):
            try:
                return self[key]
            except KeyError as exc:
                raise AttributeError(key) from exc

        def __setattr__(self, key, value):
            self[key] = value

    class _Block:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __getattr__(self, _name):
            return lambda *a, **kw: None

    st_stub = py_types.ModuleType("streamlit")
    st_stub.secrets = {}
    st_stub.session_state = _SessionState()
    for fn_name in (
        "title",
        "caption",
        "header",
        "markdown",
        "write",
        "warning",
        "error",
        "subheader",
        "success",
        "dataframe",
    ):
        setattr(st_stub, fn_name, lambda *a, **kw: None)

    st_stub.set_page_config = lambda *a, **kw: None
    st_stub.button = lambda *a, **kw: False
    st_stub.rerun = lambda *a, **kw: None
    st_stub.text_area = lambda *a, **kw: ""
    st_stub.text_input = lambda *a, **kw: ""
    st_stub.number_input = lambda *a, **kw: kw.get("value", 1)
    st_stub.checkbox = lambda *a, **kw: False
    st_stub.spinner = lambda *a, **kw: _Block()
    st_stub.expander = lambda *a, **kw: _Block()
    st_stub.sidebar = _Block()

    errors_mod = py_types.ModuleType("streamlit.errors")

    class StreamlitSecretNotFoundError(Exception):
        pass

    errors_mod.StreamlitSecretNotFoundError = StreamlitSecretNotFoundError
    sys.modules["streamlit"] = st_stub
    sys.modules["streamlit.errors"] = errors_mod
    return st_stub


def load_app():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    _install_streamlit_stub()
    app_path = REPO_ROOT / "app.py"
    spec = importlib.util.spec_from_file_location("ple_app_module", str(app_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSovereignHarnessApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()

    def test_json_verification_payload(self):
        sample = '{"check": "passed", "note": "microgrid verified"}'
        parsed = json.loads(sample)
        self.assertEqual(parsed["check"], "passed")
        self.assertIn("microgrid", parsed["note"])

    def test_get_gemini_api_key_prefers_secrets_then_env(self):
        with patch.object(self.app, "st") as mock_st, patch.dict(
            os.environ, {"GEMINI_API_KEY": "env-fallback-key"}, clear=False
        ):
            mock_st.secrets = {"GEMINI_API_KEY": "  secret-key  "}
            self.assertEqual(self.app.get_gemini_api_key(), "secret-key")

            mock_st.secrets = {}
            self.assertEqual(self.app.get_gemini_api_key(), "env-fallback-key")

    def test_verify_fn_checks_empty_length_phrase_and_fenced_json(self):
        verifier = self.app.make_verify_fn(
            min_chars=10, required_phrase="Microgrid Verified", require_json=True
        )

        empty_ok, empty_reason = verifier("   \n  ")
        self.assertFalse(empty_ok)
        self.assertIn("empty", empty_reason.lower())

        ok, _ = verifier('```json\n{"check": "passed", "note": "microgrid verified"}\n```')
        self.assertTrue(ok)

        bad_json, reason_json = verifier('```json\n{"check": "passed", invalid}\n```')
        self.assertFalse(bad_json)
        self.assertIn("JSON", reason_json)

        bad_phrase, reason_phrase = verifier('{"check": "passed", "note": "other text"}')
        self.assertFalse(bad_phrase)
        self.assertIn("microgrid verified", reason_phrase.lower())

        bad_len, reason_len = verifier('{"a":1}')
        self.assertFalse(bad_len)
        self.assertIn("short", reason_len.lower())

    def test_generate_fn_calls_google_genai_client(self):
        mock_response = MagicMock()
        mock_response.text = "  trimmed model output  "
        mock_models = MagicMock()
        mock_models.generate_content.return_value = mock_response
        mock_client = MagicMock()
        mock_client.models = mock_models
        mock_client_cls = MagicMock(return_value=mock_client)
        mock_types = MagicMock()
        mock_types.GenerateContentConfig.return_value = "mock-config"

        with patch.object(
            self.app, "genai", MagicMock(Client=mock_client_cls)
        ), patch.object(self.app, "types", mock_types):
            gen_fn = self.app.make_generate_fn("test-api-key")
            output = gen_fn("Test prompt", "gemini-3.8-flash", 512)

        self.assertEqual(output, "trimmed model output")
        mock_client_cls.assert_called_once_with(api_key="test-api-key", vertexai=False)
        mock_models.generate_content.assert_called_once_with(
            model="gemini-3.8-flash",
            contents="Test prompt",
            config="mock-config",
        )

    def test_harness_retries_and_circuit_breaker_reset(self):
        engine_cls = getattr(self.app, "HarnessExecutionEngine", None)
        if engine_cls is None:
            self.skipTest("HarnessExecutionEngine not found on app module")

        config_path = REPO_ROOT / "submission.yaml"
        if not config_path.exists():
            self.skipTest("submission.yaml not found in repository root")

        engine = engine_cls(config_path=str(config_path))
        engine.reset()
        verifier = self.app.make_verify_fn(
            min_chars=5, required_phrase="ok", require_json=True
        )
        responses = iter(["not json", '{"status": "ok"}'])

        result = engine.run_with_verification(
            "Return JSON status ok",
            lambda _prompt, _model, _tokens: next(responses),
            verifier,
        )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("attempt"), 2)
        self.assertEqual(engine.failure_count, 0)
        self.assertFalse(engine.locked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
