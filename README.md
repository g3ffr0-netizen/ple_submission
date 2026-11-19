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

2. Create the local secrets file from the safe template:

   ```bash
   mkdir -p .streamlit
   cp .streamlit/secrets.toml.example .streamlit/secrets.toml
   ```

3. Edit `.streamlit/secrets.toml` and replace the placeholder with your key, keeping the quotes:

   ```toml
   GEMINI_API_KEY = "your-real-gemini-api-key"
   ```

4. Start the app:

   ```bash
   streamlit run app.py
   ```

`.streamlit/secrets.toml` is excluded by `.gitignore`; do not force-add it.

Alternatively, set `GEMINI_API_KEY` in your shell environment before running Streamlit.

### Streamlit Community Cloud

Open the deployed app's **Settings → Secrets** and add:

```toml
GEMINI_API_KEY = "your-real-gemini-api-key"
```

Save the setting and restart/redeploy the app if prompted. Do not put the key in the repository or in the app's source code.

## Run the contract tests

```bash
python -m unittest -v pipeline.py
```
