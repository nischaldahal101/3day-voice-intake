# Mock FieldPulse API

A local fake of FieldPulse you can develop against without touching anyone's
real account. Lives on your laptop. Persists data to a JSON file. Resets in
one command.

## Run it

```bash
pip install flask
python mock_fieldpulse.py
```

That's it. The server runs on `http://localhost:5000`. Your integration code
should point at that URL instead of FieldPulse's real one
(`https://ywe3crmpll.execute-api.us-east-2.amazonaws.com/stage`).

When you eventually switch to real FieldPulse, only **two things change** in
your integration code:

1. The base URL
2. The API key

Everything else — headers, JSON shapes, response structures — works identically.

## What's in it

The mock matches the real FieldPulse API's shape:

- **Auth:** `x-api-key` header (any non-empty value works in the mock)
- **Format:** REST + JSON
- **Pagination:** `?page=1&per_page=20` query params; responses include
  a `meta` block with `current_page`, `per_page`, `total`, `total_pages`
- **Status codes:** 200/201 for success, 401 for missing auth, 404 for missing records

## Endpoints

### Customers
| Method | Path | What it does |
|---|---|---|
| GET    | `/customers`        | List all customers (paginated) |
| GET    | `/customers/<id>`    | Get one customer |
| POST   | `/customers`        | Create a customer |
| PATCH  | `/customers/<id>`    | Update a customer |

### Jobs
| Method | Path | What it does |
|---|---|---|
| GET    | `/jobs?customer_id=cust_001` | List jobs (optionally filtered by customer) |
| GET    | `/jobs/<id>`                  | Get one job |
| POST   | `/jobs`                      | Create a job |
| PATCH  | `/jobs/<id>`                  | Update a job — **custom_fields merge, not replace** |

### Notes on a job
| Method | Path | What it does |
|---|---|---|
| GET    | `/jobs/<id>/notes`     | List notes on a job |
| POST   | `/jobs/<id>/notes`     | Add a note |

### Subtasks
| Method | Path | What it does |
|---|---|---|
| GET    | `/jobs/<id>/subtasks`  | List subtasks on a job |
| POST   | `/jobs/<id>/subtasks`  | Create a subtask |
| PATCH  | `/subtasks/<id>`        | Update a subtask (mark complete, change due date) |

### Custom field schema
| Method | Path | What it does |
|---|---|---|
| GET    | `/custom_fields`       | List available custom field definitions |

### Dev-only
| Method | Path | What it does |
|---|---|---|
| GET    | `/health`              | Health check (no auth needed) |
| POST   | `/_reset`              | Wipe data and reseed. Useful during dev. |

## Seed data

When you first run it, the mock creates three customers (`Client XYZ`,
`Client ABC`, `Client DEF`) and one in-progress kitchen/bath job for each.
That gives your integration something to read on day one without having to
seed it yourself.

To reset to original seed data:

```bash
curl -X POST http://localhost:5000/_reset -H "x-api-key: anything"
# or just delete mock_data.json and restart the server
rm mock_data.json && python mock_fieldpulse.py
```

## Example: simulate a voice memo's worth of writes

```bash
H="x-api-key: dev"
BASE=http://localhost:5000

# 1. Add a note (summary of what the client said)
curl -X POST $BASE/jobs/job_001/notes -H "$H" -H "Content-Type: application/json" \
  -d '{"body":"Client switching cabinets to navy. Brass hardware. Install moved to May 22.","source":"voice_memo"}'

# 2. Create the follow-up subtasks
curl -X POST $BASE/jobs/job_001/subtasks -H "$H" -H "Content-Type: application/json" \
  -d '{"title":"Reorder cabinets in navy","priority":"high","due_date":"2026-05-22"}'

# 3. Update the job's spec (custom fields merge — other fields preserved)
curl -X PATCH $BASE/jobs/job_001 -H "$H" -H "Content-Type: application/json" \
  -d '{"custom_fields":{"cabinet_finish":"navy blue","hardware_finish":"brass"},"scheduled_start":"2026-05-22"}'

# 4. Read the job back to confirm
curl $BASE/jobs/job_001 -H "$H"
```

That's exactly the sequence your real integration will run after every voice
memo. Build and test that flow against the mock; switch the URL when you have
real FieldPulse access.

## Things this mock does NOT do

Be honest with yourself about what's faked:

- **No rate limiting.** Real FieldPulse limits to 50 req/sec. You won't hit it,
  but your code should still handle 429 responses gracefully.
- **No webhooks.** Real FieldPulse has webhooks for job status changes; the
  mock doesn't fire them. If your tool depends on webhooks, you'll need to
  poll until you're on the real API.
- **No field validation.** Real FieldPulse will reject malformed data; the
  mock accepts almost anything. Don't take that as permission to skip
  validation in your code.
- **No multi-tenancy.** Real FieldPulse isolates data per account. The mock
  has one global tenant.
- **Field names are educated guesses.** I used reasonable conventions
  (`display_name`, `customer_id`, `custom_fields`), but real FieldPulse may
  use slightly different names. Verify against their docs when you get API
  access, and adjust your client code's mapping layer accordingly.

## When to switch to real FieldPulse

Use this mock for **weeks 1-2** of building. Once your voice memo →
transcription → Claude extraction → mock-write flow is working end-to-end,
that's the right time to swap in real FieldPulse on your own trial account.
Most of your code won't change. The few places where real FieldPulse's
field names differ will fail loudly and clearly, which is exactly what you
want.
