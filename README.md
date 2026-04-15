# Gov Proposal Monitor

Single-file Streamlit app that scans government procurement databases for space industry opportunities using Claude AI.

## Files

```
app.py            ← entire application (edit this)
requirements.txt  ← pip dependencies
railway.json      ← Railway deployment config
.gitignore
```

## Run locally

```bash
pip install -r requirements.txt

# Set your API key
export ANTHROPIC_API_KEY=sk-ant-...

streamlit run app.py
```

## Deploy to Railway

1. Push repo to GitHub
2. New project → Deploy from GitHub repo
3. Add environment variable: `ANTHROPIC_API_KEY = sk-ant-...`
4. Railway reads `railway.json` and deploys automatically

Every `git push` to `main` triggers a redeploy.

## Secrets (alternative to env vars)

Create `.streamlit/secrets.toml` locally (gitignored):

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
```

On Railway, set the same key under **Variables**.

## Tweaking

Everything lives in `app.py`. Key sections are clearly labeled:

- `CONSTANTS` — model, default keywords, agencies, colors
- `DATABASE` — SQLite schema and queries
- `SCANNER` — Anthropic API call and JSON parsing
- `SCHEDULER` — APScheduler auto-scan setup
- `HOLIDAYS` — federal holiday calculation
- `MAIN APP` — Streamlit UI

Change `MODEL` at the top to swap Claude models.
