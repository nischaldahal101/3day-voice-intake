# Voice Intake

Turn a sales-call transcript into a structured customer + job record in
FieldPulse. A rep records an intake call, the transcript is sent to this
service, Claude extracts the structured fields, and the result is written to
FieldPulse as a customer, a job, a note (summary + flags + quotes + the full
transcript), and follow-up subtasks.

This repo runs entirely against a **local mock** of FieldPulse, so you can build
and test the whole pipeline without touching a real account.

> Audio → text (transcription) happens elsewhere (an iPhone Shortcut, or Whisper
> if added later). This service only deals with text transcripts.

## Pipeline

```
transcript ──▶ extractor.py ──▶ fieldpulse_writer.py ──▶ mock FieldPulse
   (text)       (Claude)          (REST writes)            (localhost:5000)
                   │                    │
                   ▼                    ▼
            structured dict     customer + job + note + subtasks
```

`orchestrator.py` is the HTTP front door that wires the two halves together.

## Components

| File | Role |
|---|---|
| `voice-intake/extractor.py` | `extract_intake(transcript) -> dict`. Calls Claude (`claude-sonnet-4-6`, temperature 0.0) with the locked-in prompt in `extraction_prompt.txt`, parses the JSON response. Raises `ExtractionError`. |
| `voice-intake/fieldpulse_writer.py` | `write_intake_to_fieldpulse(extraction, original_transcript=None) -> dict`. Maps the extraction to FieldPulse and creates customer → job → note → subtasks. Raises `WriterError`. |
| `voice-intake/orchestrator.py` | Flask app (port 5100) exposing the HTTP endpoints. Ties extractor + writer together, times the run, handles failures. |
| `mock_fieldpulse.py` | Local stand-in for the FieldPulse API (port 5000) with a browser admin UI. See [MOCK_FIELDPULSE.md](MOCK_FIELDPULSE.md). |
| `voice-intake/extraction_prompt.txt` | The validated system prompt Claude uses for extraction. |
| `voice-intake/test_*.py` | Standalone tests for each layer (extractor, writer, orchestrator). |

## Setup

Requires Python 3.9+ and an Anthropic API key.

```bash
cd voice-intake
python3 -m venv venv
venv/bin/pip install anthropic python-dotenv requests flask

# Configure environment
cp .env.example .env
# then edit .env and set a real ANTHROPIC_API_KEY
```

### Environment variables (`voice-intake/.env`)

| Variable | Default | Used by |
|---|---|---|
| `ANTHROPIC_API_KEY` | — (required) | extractor |
| `FIELDPULSE_BASE_URL` | `http://localhost:5000` | writer |
| `FIELDPULSE_API_KEY` | `dev-test-key-12345` | writer |
| `INTAKE_API_KEY` | `dev-intake-key` | orchestrator (`x-intake-key` header) |

`.env` is gitignored — it never gets committed.

## Running

Two servers, in separate terminals. The mock must be up before the orchestrator
can write to it.

```bash
# Terminal 1 — mock FieldPulse (port 5000)
python mock_fieldpulse.py

# Terminal 2 — orchestrator (port 5100)
cd voice-intake && venv/bin/python orchestrator.py
```

## Endpoints (orchestrator, port 5100)

All except `/health` require the header `x-intake-key: <INTAKE_API_KEY>`.

| Method | Path | Description |
|---|---|---|
| GET  | `/health` | Liveness check, no auth. |
| POST | `/process-transcript` | Body `{"transcript": "...", "source": "..."}`. Runs the full pipeline. |
| POST | `/test-with-sample` | No body. Runs the pipeline against a baked-in sample transcript. |

**Success** returns `{status: "success", extraction: {...}, fieldpulse: {...}, elapsed_seconds: N}`.
**Failures:** `422` extraction failed, `502` writer failed (includes the partial
`extraction`), `500` unexpected, `401` bad/missing auth.

```bash
curl -X POST http://localhost:5100/process-transcript \
  -H "x-intake-key: dev-intake-key" \
  -H "Content-Type: application/json" \
  -d '{"transcript": "REP: Hi... PROSPECT: ...", "source": "manual-test"}'
```

## Testing

With both servers running:

```bash
cd voice-intake
venv/bin/python test_extractor.py      # extractor only (calls Claude)
venv/bin/python test_writer.py         # writer only (needs mock on :5000)
venv/bin/python test_orchestrator.py   # full pipeline via HTTP (needs both servers)
```

Each test prints a `fieldpulse_url` — open it, or browse all records at
**http://localhost:5000/admin**. Repeated test runs accumulate records; clear
them with the reset button at http://localhost:5000/admin/reset.

## Switching to real FieldPulse

Most code is unchanged — point `FIELDPULSE_BASE_URL` and `FIELDPULSE_API_KEY`
at the real API. See [MOCK_FIELDPULSE.md](MOCK_FIELDPULSE.md) for the caveats
(field-name guesses, no webhooks, no rate limiting in the mock).
