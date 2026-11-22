# ple_submission

**Sovereign Harness Execution Portal** — a bounded Gemini execution harness with verification-driven retries, automatic fallback model rotation, circuit-breaker lockout protection, a Streamlit web portal, an async WebSocket server, and an offline/live evaluation runner.

## Repository Architecture

| Path | Purpose |
| :--- | :--- |
| `harness.py` | Core `HarnessExecutionEngine`, `make_verify_fn`, `extract_json_payload`, config loading, fallback model rotation, and circuit breaker. |
| `submission.yaml` | Primary model, fallback model list, retry count, max consecutive failures, and output token budget. |
| `app.py` | Streamlit UI displaying engine status, circuit-breaker controls, fallback usage, elapsed latency, and per-attempt history. |
| `serve.py` | Async WebSocket protocol handler (`handle_ws_message`) with per-engine `asyncio.Lock` plus runnable `websockets` server CLI (`start_ws_server` / `run_server`). |
| `eval/run.py` | Batch evaluation CLI (`--dry-run` or live) computing pass rate, average `elapsed_ms`, lockouts, and per-model usage. |
| `eval/dev_tasks.json` | Sample evaluation tasks for offline `--dry-run` checks. |
| `pipeline.py` | Offline contract & regression test suite for `harness.py` and `serve.py`. |
| `tests/` | Unit, live loopback WebSocket, and adversarial stress test suites discovered by CI. |

## Configure Your Gemini API Key

Never put a real API key in `app.py`, commit it to Git, or paste it into an issue.  
The app reads Streamlit secrets first and falls back to the `GEMINI_API_KEY` environment variable; `serve.py` and `eval/run.py` read `GEMINI_API_KEY` from the environment (unless `--dry-run` is passed).

### Local Setup

```bash
python -m pip install -r requirements.txt
```

Optional key configuration for live runs:
- Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and replace the placeholder value, **or**
- Export `GEMINI_API_KEY` in your shell environment.

## Quickstart Commands

### 1. Run All Tests

```bash
python -m unittest discover -s tests -v
python pipeline.py -v
```

### 2. Run Offline Batch Evaluation (`--dry-run`)

```bash
python eval/run.py --dry-run
```

Write a custom tasks file and save metrics to `eval/results.json`:

```bash
python eval/run.py --tasks eval/dev_tasks.json --out eval/results.json --dry-run
```

### 3. Run the WebSocket Server (`serve.py`)

Start locally in offline `--dry-run` mode on `ws://127.0.0.1:8765`:

```bash
python serve.py --dry-run --host 127.0.0.1 --port 8765
```

Supported JSON message frames (both `"action"` and legacy `"type"` keys accepted):
- `{"action": "ping", "request_id": "1"}`
- `{"action": "status", "request_id": "2"}`
- `{"action": "execute", "prompt": "Confirm readiness", "required_phrase": "ok", "require_json": true, "request_id": "3"}`
- `{"action": "reset", "request_id": "4"}`

### 4. Launch the Streamlit Portal (`app.py`)

```bash
streamlit run app.py
```
