# Deploying to Render

This repo ships **two** Python web services. Each one is a separate Render
service (different "Root Directory", different build/start commands, different
env vars). Both use **gunicorn** as the production WSGI server.

The application code is unchanged from local dev — `python <script>.py`
still works for the Flask dev server, and `gunicorn <module>:app` runs the
same Flask `app` object in production. Each service's startup work (DB pool
open, schema creation, seed) happens at module import time, so gunicorn
workers initialize correctly without any extra hook.

---

## Service 1 — Mock FieldPulse (`mockfieldpulse/`)

The mock CRM backed by Postgres. Render's managed Postgres provides a
`DATABASE_URL` with `?sslmode=require`; psycopg honors that automatically.

| Setting | Value |
|---|---|
| **Root Directory** | `/` *(repo root — `mock_fieldpulse.py` is here)* |
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn mock_fieldpulse:app --bind 0.0.0.0:$PORT` |
| **Health Check Path** | `/health` |

### Environment variables

| Name | Notes |
|---|---|
| `DATABASE_URL` | Provided by the Render Postgres add-on. Comes with `?sslmode=require` baked in. |

That's the only required env var — the API key (`x-api-key`) check in the
mock just verifies the header is present (any non-empty value works), and
the seed data is hardcoded in `_seed_data()`.

---

## Service 2 — Voice Intake Orchestrator (`voice-intake/`)

The Flask app that ties Deepgram + Anthropic + the mock together. Calls the
mock service over HTTPS once deployed.

| Setting | Value |
|---|---|
| **Root Directory** | `voice-intake` |
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn orchestrator:app --bind 0.0.0.0:$PORT` |
| **Health Check Path** | `/health` |

### Environment variables

| Name | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key for the extractor. |
| `DEEPGRAM_API_KEY` | Deepgram API key for `transcriber.py`. |
| `FIELDPULSE_BASE_URL` | **Set this to the deployed mock's Render URL** (e.g. `https://mock-fieldpulse.onrender.com`). Locally it defaults to `http://localhost:5000`. |
| `FIELDPULSE_API_KEY` | Any non-empty string; the mock just checks for the header's presence. Default `dev-test-key-12345` works. |
| `INTAKE_API_KEY` | The shared secret the React app sends as `x-intake-key`. Pick a real value here when going to production; locally defaults to `dev-intake-key`. |

> **Deploy the mock first**, copy its `https://...onrender.com` URL into the
> orchestrator's `FIELDPULSE_BASE_URL`, then deploy the orchestrator.

---

## Local development (unchanged)

Both services still run with the Flask dev server exactly as before. The
`$PORT` env var falls back to the original local defaults (`5000` for the
mock, `5100` for the orchestrator).

```bash
# Terminal 1 — mock CRM
cd mockfieldpulse
venv/bin/python mock_fieldpulse.py

# Terminal 2 — orchestrator
cd mockfieldpulse/voice-intake
venv/bin/python orchestrator.py
```

To smoke-test the production server locally before pushing, swap `python …`
for `gunicorn …` with the same `--bind` flag Render uses:

```bash
# Mock under gunicorn (production-equivalent)
cd mockfieldpulse
venv/bin/gunicorn mock_fieldpulse:app --bind 0.0.0.0:5000

# Orchestrator under gunicorn
cd mockfieldpulse/voice-intake
venv/bin/gunicorn orchestrator:app --bind 0.0.0.0:5100
```

---

## What was changed for Render-readiness

* Added `requirements.txt` to **mockfieldpulse/** and **voice-intake/**, each
  pinned to the versions currently in that service's venv.
* Added `gunicorn==23.0.0` to both files (and to both venvs).
* Moved the mock's connection-pool open + `init_db()` out of
  `if __name__ == "__main__":` into a module-level `_bootstrap()` call, so
  gunicorn workers run it on import.
* Both services' `app.run()` now reads `PORT` from the environment, with
  the original ports as the local-dev defaults. (Under gunicorn, `app.run()`
  is never called — gunicorn binds via `--bind 0.0.0.0:$PORT`.)
* No routes, request/response shapes, auth, or business logic changed.
