# ple_submission

A Streamlit portal for running Gemini-backed tasks through the bounded execution harness.

## Configure your Gemini API key

Never put a real API key in `app.py`, commit it to Git, or paste it into a public issue.
The app checks Streamlit secrets first and then the `GEMINI_API_KEY` environment variable.

### Local setup

1. Install the dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

2. Configure your local key (either option):
   - Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and replace the placeholder value.
   - Or export `GEMINI_API_KEY` in your shell environment.

3. Launch the Streamlit portal:

   ```bash
   streamlit run app.py
   ```

### Running tests & offline evaluation

```bash
python -m unittest discover -s tests -v
python pipeline.py
python eval/run.py --dry-run
```
