import importlib.util
import sys
import types as py_types
import unittest
from pathlib import Path

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


def load_module_from_path(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFallbackAndLockout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        _install_streamlit_stub()
        cls.app = load_module_from_path("ple_app_module", REPO_ROOT / "app.py")
        harness_path = REPO_ROOT / "harness.py"
        if harness_path.exists():
            cls.harness_mod = load_module_from_path("ple_harness_module", harness_path)
            cls.engine_cls = cls.harness_mod.HarnessExecutionEngine
        else:
            cls.engine_cls = getattr(cls.app, "HarnessExecutionEngine", None)
        if cls.engine_cls is None:
            raise unittest.SkipTest("HarnessExecutionEngine not found")

    def _make_engine(self, **kwargs):
        config_path = REPO_ROOT / "submission.yaml"
        engine = self.engine_cls(config_path=str(config_path), **kwargs)
        engine.reset()
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
        self.assertEqual(result.get("status"), "verified")
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
        self.assertEqual(first_run.get("status"), "verification_retries_exhausted")
        self.assertEqual(engine.failure_count, 1)
        self.assertFalse(engine.locked)

        second_run = engine.run_with_verification("Second failing run", always_invalid, verifier)
        self.assertFalse(second_run.get("ok"))
        self.assertEqual(second_run.get("status"), "locked")
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

        generator_called = False

        def unexpected_gen(*_args, **_kwargs):
            nonlocal generator_called
            generator_called = True
            return '{"status": "ok"}'

        blocked = engine.run_with_verification("Blocked while locked", unexpected_gen, verifier)
        self.assertFalse(blocked.get("ok"))
        self.assertEqual(blocked.get("status"), "locked")
        self.assertFalse(generator_called)

        engine.reset()
        self.assertFalse(engine.locked)
        self.assertEqual(engine.failure_count, 0)

        recovered = engine.run_with_verification(
            "Recovered run", lambda *_: '{"status": "ok"}', verifier
        )
        self.assertTrue(recovered.get("ok"))
        self.assertEqual(recovered.get("status"), "verified")


if __name__ == "__main__":
    unittest.main(verbosity=2)
