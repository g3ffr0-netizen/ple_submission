# ple_submission

**PLE Competition-Aligned Harness** — implements the official PLE Serve Protocol v1 agent server (`taskbridge.py` + `serve.py`), Plan-Work-Verify execution with dynamic MCP tool discovery, bounded retry & circuit-breaker protection (`harness.py`), offline batch evaluation (`eval/run.py`), and a local Streamlit inspection UI (`app.py`).

## Official PLE Competition Structure

| Path | Purpose |
| :--- | :--- |
| `submission.yaml` | Official PLE manifest (`name`, `model_api`, `install`, `serve`, quoted `env`). Default: `google-cloud/gemini-3.8-flash`. |
| `taskbridge.py` | Official PLE Serve Protocol v1 WebSocket bridge (`0.0.0.0:$PORT`, `max_size=16 MiB`, `ping_interval=None`, `task`, `exec`, `read_file`, `write_file`, `http`, `cancel`, `done`/`error`). |
| `serve.py` | PLE Protocol v1 Plan-Work-Verify harness with `edit_file`, container MCP client (`MCPServer`), lazy evaluator env resolution, and `CircuitBreaker` recovery (`--dry-run` supported offline). |
| `requirements-serve.txt` | Minimal runtime dependencies (`openai`, `websockets`, `pyyaml`) installed by `submission.yaml` into `/opt/harness/venv` for fast container builds. |
| `requirements.txt` | Full local/CI dependency list including Streamlit, Pandas, and `google-genai`. |
| `harness.py` | Core `CircuitBreaker`, `HarnessExecutionEngine`, `is_pass_verdict`, `make_verify_fn`, `extract_json_payload`, and dual PLE/legacy `submission.yaml` config loader. |
| `app.py` | Local Streamlit inspection & verification portal (`GEMINI_API_KEY` + optional `GOOGLE_GEMINI_BASE_URL`). |
| `eval/run.py` | Offline/live batch evaluation CLI (`--dry-run`, pass rate, `elapsed_ms`, lockouts, per-model usage). |
| `pipeline.py` | Offline contract test suite for `harness.py` and Protocol v1 `serve.py`. |
| `tests/` | Unit, live Protocol v1 loopback WebSocket, and adversarial stress test suites. |

## Model Selection Guidance (`submission.yaml`)

- **Default (`google-cloud/gemini-3.8-flash`):** Fast turnaround and low token cost for iterating across the 29 public dev tasks and smoke checks.
- **High-capability upgrade (`google-cloud/gemini-4-argon`):** Switch `model_api` in `submission.yaml` to `google-cloud/gemini-4-argon` before your final dev run & artifact generation (`artifacts/` must match the submitted `model_api`). `serve.py` reads `MODEL_NAME` automatically at container startup, so no code edits are needed when switching.

## Quickstart & Validation

### 1. Run Unit, Live Loopback Protocol v1 & Stress Tests

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python pipeline.py -v
```

### 2. Validate Against Official PLE Starter Kit (`ple_kit`)

On Linux/WSL2/macOS (with `C:\projects\ple_kit` on `PYTHONPATH`):

```bash
python -m kit validate .
python -m kit artifacts .
python -m kit check . --env-file .env
```

> **Windows Note:** `python -m kit check/build` requires Linux/macOS/WSL2 (and Docker) because `kit/build.py` imports Unix `fcntl`. On native Windows Python you can validate `submission.yaml` directly via `kit.spec.load('.')`.

### 3. Run Local Server in Offline `--dry-run` Mode

```bash
python serve.py --dry-run --host 127.0.0.1 --port 8765
```

In production containers, `submission.yaml` launches `serve.py` directly on `0.0.0.0:$PORT` and reads the evaluator-injected proxy variables (`MODEL_NAME`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `MODEL_BASE_URL`, `MODEL_API_KEY`, `GOOGLE_GEMINI_BASE_URL`, `GEMINI_API_KEY`).

### 4. Run Offline Evaluation & Local Streamlit UI

```bash
python eval/run.py --dry-run
streamlit run app.py
```
