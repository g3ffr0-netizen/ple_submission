import importlib.util
import sys
import types as py_types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent


def _install_streamlit_stub() -> py_types.ModuleType:
    """Stub streamlit so importing app.py does not require a live runtime."""

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
    spec = importlib.util.spec_from_file_location("ple_app_fallback_module", str(app_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFallbackAndLockout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app()
        cls.engine_cls = getattr(cls.app, "HarnessExecutionEngine", None)
        if cls.engine_cls is None:
            raise unittest.SkipTest("HarnessExecutionEngine not found on app module")

    def _make_engine(self, **overrides):
        config_path = REPO_ROOT / "submission.yaml"
        engine = self.engine_cls(config_path=str(config_path)) if config_path.exists() else self.engine_cls()
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

        result = engine.run_with_verification("Return valid JSON", always_invalid, verifier)

        self.assertFalse(result.get("ok"))
        self.assertTrue(engine.locked)
        self.assertGreaterEqual(engine.failure_count, 2)

    def test_locked_engine_blocks_execution_until_reset(self):
        engine = self._make_engine(
            model="gemini-3.8-flash",
            fallback_models=["gemini-3.5-flash"],
            max_retries=2,
            max_failures=1,
        )
        verifier = self.app.make_verify_fn(
            min_chars=5, required_phrase="ok", require_json=True
        )

        engine.run_with_verification("First run", lambda *_: "bad", verifier)
        self.assertTrue(engine.locked)

        with patch.object(
            self.app,
            "make_generate_fn",
            side_effect=AssertionError("Generator should not run while locked"),
        ):
            blocked_result = engine.run_with_verification(
                "Blocked run", lambda *_: '{"status": "ok"}', verifier
            )
            self.assertFalse(blocked_result.get("ok"))
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
