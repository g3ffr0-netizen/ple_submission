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


def load_module_from_path(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_app():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    _install_streamlit_stub()
    return load_module_from_path("ple_app_module", REPO_ROOT / "app.py")


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

        bad_json, reason_json = verifier(
            '```json\n{"check": "passed", "note": "microgrid verified", invalid}\n```'
        )
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


class TestFallbackAndLockout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()
        harness_path = REPO_ROOT / "harness.py"
        if harness_path.exists():
            harness_mod = load_module_from_path("ple_harness_module", harness_path)
            cls.engine_cls = getattr(harness_mod, "HarnessExecutionEngine", None)
        else:
            cls.engine_cls = getattr(cls.app, "HarnessExecutionEngine", None)
        if cls.engine_cls is None:
            raise unittest.SkipTest("HarnessExecutionEngine not found")

    def _make_engine(self, **overrides):
        config_path = REPO_ROOT / "submission.yaml"
        engine = (
            self.engine_cls(config_path=str(config_path))
            if config_path.exists()
            else self.engine_cls()
        )
        engine.reset()
        for attr, value in overrides.items():
            setattr(engine, attr, value)
        return engine

    def test_rotates_to_fallback_model_when_primary_fails_verification(self):
        engine = self._make_engine(
            model="gemini-3.8-flash",
            fallback_models=["gemini-3.5-flash", "gemini-3.1-pro"],
            max_retries=3,
            max_failures=3,
        )
        verifier = self.app.make_verify_fn(
            min_chars=5, required_phrase="microgrid verified", require_json=True
        )
        models_called = []

        def fake_generate(_prompt, model, _tokens):
            models_called.append(model)
            if len(models_called) == 1:
                return '{"check": "failed", "note": "wrong phrase"}'
            return '{"check": "passed", "note": "microgrid verified"}'

        result = engine.run_with_verification("Verify microgrid", fake_generate, verifier)

        self.assertTrue(result.get("ok"))
        self.assertEqual(models_called[0], "gemini-3.8-flash")
        self.assertIn("gemini-3.5-flash", models_called)
        self.assertEqual(engine.failure_count, 0)
        self.assertFalse(engine.locked)

    def test_locks_circuit_breaker_after_max_consecutive_failures(self):
        engine = self._make_engine(
            model="gemini-3.8-flash",
            fallback_models=["gemini-3.5-flash"],
            max_retries=2,
            max_failures=2,
        )
        verifier = self.app.make_verify_fn(
            min_chars=10, required_phrase="microgrid verified", require_json=True
        )

        def always_invalid(_prompt, _model, _tokens):
            return "not valid json"

        first_run = engine.run_with_verification("First failing run", always_invalid, verifier)
        self.assertFalse(first_run.get("ok"))
        self.assertEqual(engine.failure_count, 1)
        self.assertFalse(engine.locked)

        second_run = engine.run_with_verification("Second failing run", always_invalid, verifier)
        self.assertFalse(second_run.get("ok"))
        self.assertEqual(engine.failure_count, 2)
        self.assertTrue(engine.locked)

    def test_locked_engine_blocks_execution_until_reset(self):
        engine = self._make_engine(
            model="gemini-3.8-flash",
            fallback_models=["gemini-3.5-flash"],
            max_retries=1,
            max_failures=1,
        )
        verifier = self.app.make_verify_fn(
            min_chars=5, required_phrase="ok", require_json=True
        )

        engine.run_with_verification("Trip lock", lambda *_: "bad", verifier)
        self.assertTrue(engine.locked)

        blocked = engine.run_with_verification(
            "Blocked while locked", lambda *_: '{"status": "ok"}', verifier
        )
        self.assertFalse(blocked.get("ok"))
        self.assertTrue(engine.locked)

        engine.reset()
        self.assertFalse(engine.locked)
        self.assertEqual(engine.failure_count, 0)

        recovered = engine.run_with_verification(
            "Recovered run", lambda *_: '{"status": "ok"}', verifier
        )
        self.assertTrue(recovered.get("ok"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
