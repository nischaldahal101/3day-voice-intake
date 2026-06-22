"""
Mock FieldPulse API server
==========================

Pretends to be FieldPulse so you can develop your integration without
touching a real account. Runs locally on http://localhost:5000.

Endpoints implemented (matches FieldPulse's public API surface):

  GET    /customers              list customers (paginated)
  GET    /customers/<id>          get one customer
  POST   /customers              create a customer
  PATCH  /customers/<id>          update a customer
  DELETE /customers/<id>          delete a customer + their jobs/notes/subtasks

  GET    /jobs                   list jobs (paginated, filterable by customer_id)
  GET    /jobs/<id>               get one job
  POST   /jobs                   create a job
  PATCH  /jobs/<id>               update a job (status, schedule, custom fields)

  GET    /jobs/<id>/notes         list notes on a job
  POST   /jobs/<id>/notes         add a note to a job

  GET    /jobs/<id>/subtasks      list subtasks on a job
  POST   /jobs/<id>/subtasks      create a subtask
  PATCH  /subtasks/<id>           update a subtask (mark complete, change due date)

  GET    /custom_fields           list available custom field definitions

Auth:
  Same as real FieldPulse — every request must include the header
    x-api-key: dev-test-key-12345
  (We don't actually validate it as real, just that it's present.)

Run:
  python mock_fieldpulse.py
  Then point your integration at http://localhost:5000

Data:
  Lives in mock_data.json in the same folder. Resets if you delete that file.
  All writes are persisted so the mock behaves like a real system across
  restarts. Perfect for "dry-run" testing your tool over a few days.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, abort, render_template_string

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────
# Storage — single JSON file. Simple. Easy to inspect with `cat`.
# ─────────────────────────────────────────────────────────────────────
DATA_FILE = os.path.join(os.path.dirname(__file__), "mock_data.json")

def _now():
    return datetime.now(timezone.utc).isoformat()

def _seed_data():
    """Initial fake data — a few customers and jobs that look like
    what Mike would have in real FieldPulse."""
    return {
        "customers": [
            {
                "id": "cust_001",
                "display_name": "Whitaker, Daniel",
                "first_name": "Daniel",
                "last_name": "Whitaker",
                "email": "dan.whitaker@gmail.com",
                "phone": "618-555-0143",
                "address": {
                    "street": "412 Cambridge Court",
                    "city": "O'Fallon",
                    "state": "IL",
                    "postal_code": "62269"
                },
                "created_at": _now(),
                "updated_at": _now(),
            },
            {
                "id": "cust_002",
                "display_name": "Reinhardt, Karen",
                "first_name": "Karen",
                "last_name": "Reinhardt",
                "email": "kreinhardt@yahoo.com",
                "phone": "618-555-0188",
                "address": {
                    "street": "27 Lindenwood Drive",
                    "city": "Belleville",
                    "state": "IL",
                    "postal_code": "62221"
                },
                "created_at": _now(),
                "updated_at": _now(),
            },
            {
                "id": "cust_003",
                "display_name": "Maddox, Anthony",
                "first_name": "Anthony",
                "last_name": "Maddox",
                "email": "tmaddox@outlook.com",
                "phone": "618-555-0207",
                "address": {
                    "street": "1530 Montclaire Avenue",
                    "city": "Edwardsville",
                    "state": "IL",
                    "postal_code": "62025"
                },
                "created_at": _now(),
                "updated_at": _now(),
            },
            {
                "id": "cust_004",
                "display_name": "Brennan, Lisa",
                "first_name": "Lisa",
                "last_name": "Brennan",
                "email": "lisa.brennan@gmail.com",
                "phone": "618-555-0162",
                "address": {
                    "street": "905 Ashland Meadows Lane",
                    "city": "Fairview Heights",
                    "state": "IL",
                    "postal_code": "62208"
                },
                "created_at": _now(),
                "updated_at": _now(),
            },
        ],
        "jobs": [
            {
                "id": "job_001",
                "customer_id": "cust_001",
                "title": "Kitchen Remodel — Cabinet & Countertop Refresh",
                "status": "design",
                "scheduled_start": "2026-06-15",
                "scheduled_end": "2026-07-24",
                "contract_value": 32500.00,
                "custom_fields": {
                    "cabinet_brand": "KraftMaid",
                    "cabinet_finish": "white shaker",
                    "countertop": "quartz — Calacatta look",
                    "hardware_finish": "brushed nickel",
                    "year_built": "1998",
                },
                "created_at": _now(),
                "updated_at": _now(),
            },
            {
                "id": "job_002",
                "customer_id": "cust_002",
                "title": "Primary Bath Renovation",
                "status": "sourcing",
                "scheduled_start": "2026-07-06",
                "scheduled_end": "2026-08-21",
                "contract_value": 41800.00,
                "custom_fields": {
                    "vanity_finish": "natural oak",
                    "tile": "marble-look porcelain",
                    "fixtures": "matte black",
                    "year_built": "1985",
                    "lead_source": "referral",
                },
                "created_at": _now(),
                "updated_at": _now(),
            },
            {
                "id": "job_003",
                "customer_id": "cust_003",
                "title": "Full Kitchen Tear-Out",
                "status": "in_progress",
                "scheduled_start": "2026-05-11",
                "scheduled_end": "2026-07-02",
                "contract_value": 78400.00,
                "custom_fields": {
                    "cabinet_brand": "Medallion — premium line",
                    "cabinet_finish": "navy blue island, white perimeter",
                    "countertop": "quartz",
                    "hardware_finish": "brass",
                    "lead_source": "web",
                },
                "created_at": _now(),
                "updated_at": _now(),
            },
            {
                "id": "job_004",
                "customer_id": "cust_004",
                "title": "Powder Room Update",
                "status": "consultation_scheduled",
                "scheduled_start": "2026-08-10",
                "scheduled_end": "2026-08-29",
                "contract_value": 18900.00,
                "custom_fields": {
                    "fixtures": "polished chrome",
                    "vanity_finish": "white",
                    "tile": "hexagon mosaic floor",
                    "lead_source": "radio",
                },
                "created_at": _now(),
                "updated_at": _now(),
            },
        ],
        "notes": {
            "job_001": [
                {
                    "id": "note_seed01a",
                    "job_id": "job_001",
                    "body": (
                        "Daniel Whitaker wants to refresh his kitchen — keep the "
                        "layout but reface or replace the cabinets and swap the "
                        "laminate counters for quartz. 1998 home in O'Fallon, lived "
                        "there about 11 years. Found us through a Facebook ad. "
                        "Hoping to wrap before he hosts family in July.\n"
                        "\n"
                        "FLAGS:\n"
                        "- spouse not mentioned — confirm both decision-makers\n"
                        "- budget not discussed on the call\n"
                        "\n"
                        "RAW QUOTES:\n"
                        "> The cabinets are solid, we mostly just hate the laminate counters\n"
                        "> We'd like it done before we host in July"
                    ),
                    "source": "voice_intake_ai",
                    "created_at": _now(),
                },
                {
                    "id": "note_seed01b",
                    "job_id": "job_001",
                    "body": "Cabinet door samples (white shaker + maple) dropped off 5/14. Daniel leaning shaker. Holding the order until he confirms.",
                    "source": "manual",
                    "created_at": _now(),
                },
                {
                    "id": "note_seed01c",
                    "job_id": "job_001",
                    "body": "Daniel asked whether we can reuse the existing hardwood toe-kick to save cost. Need to check with the install crew.",
                    "source": "manual",
                    "created_at": _now(),
                },
            ],
            "job_002": [
                {
                    "id": "note_seed02a",
                    "job_id": "job_002",
                    "body": (
                        "Karen Reinhardt is renovating her primary bath — full tile "
                        "surround, new vanity, and updated fixtures. Belleville home "
                        "built in 1985. Referred by a past client (the Doyles). "
                        "Flexible on timing but prefers late summer.\n"
                        "\n"
                        "FLAGS:\n"
                        "- exact tile selection still undecided\n"
                        "- phone on file is a mobile — confirm best contact time\n"
                        "\n"
                        "RAW QUOTES:\n"
                        "> I saw what you did at the Doyles' place and I want something similar\n"
                        "> The layout is fine, it just needs to be brought into this decade"
                    ),
                    "source": "voice_intake_ai",
                    "created_at": _now(),
                },
                {
                    "id": "note_seed02b",
                    "job_id": "job_002",
                    "body": "Tile vendor backordered on the marble-look porcelain until mid-June. Flagged to Karen — she's fine waiting since she wanted a late-summer start anyway.",
                    "source": "manual",
                    "created_at": _now(),
                },
            ],
            "job_003": [
                {
                    "id": "note_seed03a",
                    "job_id": "job_003",
                    "body": (
                        "Anthony Maddox is doing a full kitchen tear-out — new layout "
                        "with an island, premium cabinets, and quartz throughout. "
                        "Edwardsville home from 2009. Came in from a Google search. "
                        "Wants the island to seat four.\n"
                        "\n"
                        "FLAGS:\n"
                        "- HOA may require approval for exterior vent relocation\n"
                        "- timeline is tight around a planned August trip\n"
                        "\n"
                        "RAW QUOTES:\n"
                        "> We cook every night, so the kitchen has to actually work\n"
                        "> The island is the whole reason we're doing this"
                    ),
                    "source": "voice_intake_ai",
                    "created_at": _now(),
                },
                {
                    "id": "note_seed03b",
                    "job_id": "job_003",
                    "body": "Premium line cabinets confirmed with supplier — ship date 5/28. Quartz slab on hold at the yard pending Anthony's final edge profile pick.",
                    "source": "manual",
                    "created_at": _now(),
                },
                {
                    "id": "note_seed03c",
                    "job_id": "job_003",
                    "body": "Demo started 5/11. Found old galvanized plumbing behind the sink wall — added to scope, Anthony approved the change order ($1,850).",
                    "source": "manual",
                    "created_at": _now(),
                },
            ],
            "job_004": [
                {
                    "id": "note_seed04a",
                    "job_id": "job_004",
                    "body": (
                        "Lisa Brennan wants a quick powder room update — new vanity, "
                        "mirror, lighting, and fixtures. Fairview Heights home built "
                        "in 2002. Heard our radio spot. Consultation scheduled; she "
                        "and her husband both plan to attend.\n"
                        "\n"
                        "FLAGS:\n"
                        "- scope is small — confirm it meets project minimum\n"
                        "- spouse name not captured\n"
                        "\n"
                        "RAW QUOTES:\n"
                        "> It's just the half bath off the entry, nothing major\n"
                        "> We want it to feel less builder-grade"
                    ),
                    "source": "voice_intake_ai",
                    "created_at": _now(),
                },
                {
                    "id": "note_seed04b",
                    "job_id": "job_004",
                    "body": "Lisa asked about a wall-mount faucet option. Pricing it out before the consultation so we can show the upcharge.",
                    "source": "manual",
                    "created_at": _now(),
                },
            ],
        },
        "subtasks": {
            "job_001": [
                {
                    "id": "sub_seed01a",
                    "job_id": "job_001",
                    "title": "Order cabinets once finish is confirmed",
                    "due_date": "2026-05-30",
                    "priority": "high",
                    "status": "open",
                    "source": "manual",
                    "created_at": _now(),
                    "updated_at": _now(),
                },
                {
                    "id": "sub_seed01b",
                    "job_id": "job_001",
                    "title": "Send Daniel the countertop quote",
                    "due_date": "2026-05-16",
                    "priority": "med",
                    "status": "completed",
                    "source": "voice_intake_ai",
                    "created_at": _now(),
                    "updated_at": _now(),
                },
            ],
            "job_002": [
                {
                    "id": "sub_seed02a",
                    "job_id": "job_002",
                    "title": "Finalize tile selection with Karen",
                    "due_date": "2026-06-12",
                    "priority": "high",
                    "status": "open",
                    "source": "manual",
                    "created_at": _now(),
                    "updated_at": _now(),
                },
                {
                    "id": "sub_seed02b",
                    "job_id": "job_002",
                    "title": "Send pre-meeting info email",
                    "due_date": "2026-05-09",
                    "priority": "med",
                    "status": "completed",
                    "source": "voice_intake_ai",
                    "created_at": _now(),
                    "updated_at": _now(),
                },
            ],
            "job_003": [
                {
                    "id": "sub_seed03a",
                    "job_id": "job_003",
                    "title": "Schedule rough-in inspection",
                    "due_date": "2026-05-26",
                    "priority": "high",
                    "status": "open",
                    "source": "manual",
                    "created_at": _now(),
                    "updated_at": _now(),
                },
                {
                    "id": "sub_seed03b",
                    "job_id": "job_003",
                    "title": "Confirm quartz slab release with the yard",
                    "due_date": "2026-05-19",
                    "priority": "high",
                    "status": "completed",
                    "source": "manual",
                    "created_at": _now(),
                    "updated_at": _now(),
                },
            ],
            "job_004": [
                {
                    "id": "sub_seed04a",
                    "job_id": "job_004",
                    "title": "Confirm consultation with both decision-makers present",
                    "due_date": "2026-05-27",
                    "priority": "high",
                    "status": "open",
                    "source": "voice_intake_ai",
                    "created_at": _now(),
                    "updated_at": _now(),
                },
            ],
        },
        "custom_fields_schema": [
            {"key": "cabinet_brand",   "label": "Cabinet brand",     "type": "string"},
            {"key": "cabinet_finish",  "label": "Cabinet finish",    "type": "string"},
            {"key": "hardware_finish", "label": "Hardware finish",   "type": "string"},
            {"key": "countertop",      "label": "Countertop",        "type": "string"},
            {"key": "tile",            "label": "Tile selection",    "type": "string"},
            {"key": "fixtures",        "label": "Fixtures",          "type": "string"},
            {"key": "vanity_finish",   "label": "Vanity finish",     "type": "string"},
            {"key": "year_built",      "label": "Year built",        "type": "string"},
            {"key": "lead_source",     "label": "Lead source",       "type": "string"},
        ],
    }

def load_data():
    if not os.path.exists(DATA_FILE):
        save_data(_seed_data())
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ─────────────────────────────────────────────────────────────────────
# Auth check — every endpoint requires the x-api-key header,
# matching real FieldPulse behavior.
# ─────────────────────────────────────────────────────────────────────
@app.before_request
def check_api_key():
    if request.path == "/health":
        return
    if request.path.startswith("/admin"):
        return
    if not request.headers.get("x-api-key"):
        return jsonify({"error": "Missing x-api-key header"}), 401

# ─────────────────────────────────────────────────────────────────────
# Pagination helper — mirrors FieldPulse's paging style.
# ─────────────────────────────────────────────────────────────────────
def paginate(items):
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    start = (page - 1) * per_page
    end = start + per_page
    return {
        "data": items[start:end],
        "meta": {
            "current_page": page,
            "per_page": per_page,
            "total": len(items),
            "total_pages": max(1, (len(items) + per_page - 1) // per_page),
        }
    }

# ─────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "mock-fieldpulse"}

# ─────────────────────────────────────────────────────────────────────
# Customers
# ─────────────────────────────────────────────────────────────────────
@app.get("/customers")
def list_customers():
    data = load_data()
    return paginate(data["customers"])

@app.get("/customers/<customer_id>")
def get_customer(customer_id):
    data = load_data()
    cust = next((c for c in data["customers"] if c["id"] == customer_id), None)
    if not cust:
        return jsonify({"error": "Customer not found"}), 404
    return jsonify({"data": cust})

@app.post("/customers")
def create_customer():
    data = load_data()
    body = request.get_json() or {}
    new_cust = {
        "id": f"cust_{uuid.uuid4().hex[:8]}",
        "display_name": body.get("display_name", "New Customer"),
        "first_name": body.get("first_name", ""),
        "last_name": body.get("last_name", ""),
        "email": body.get("email", ""),
        "phone": body.get("phone", ""),
        "address": body.get("address", {}),
        "created_at": _now(),
        "updated_at": _now(),
    }
    data["customers"].append(new_cust)
    save_data(data)
    return jsonify({"data": new_cust}), 201

@app.patch("/customers/<customer_id>")
def update_customer(customer_id):
    data = load_data()
    cust = next((c for c in data["customers"] if c["id"] == customer_id), None)
    if not cust:
        return jsonify({"error": "Customer not found"}), 404
    body = request.get_json() or {}
    for k, v in body.items():
        if k not in ("id", "created_at"):
            cust[k] = v
    cust["updated_at"] = _now()
    save_data(data)
    return jsonify({"data": cust})

@app.delete("/customers/<customer_id>")
def delete_customer(customer_id):
    """Delete a customer and cascade-delete all of their data — every job
    belonging to them, plus the notes and subtasks on those jobs."""
    data = load_data()
    cust = next((c for c in data["customers"] if c["id"] == customer_id), None)
    if not cust:
        return jsonify({"error": "Customer not found"}), 404

    # Cascade: drop this customer's jobs and the notes/subtasks attached to them.
    job_ids = [j["id"] for j in data["jobs"] if j["customer_id"] == customer_id]
    for jid in job_ids:
        data["notes"].pop(jid, None)
        data["subtasks"].pop(jid, None)
    data["jobs"] = [j for j in data["jobs"] if j["customer_id"] != customer_id]
    data["customers"] = [c for c in data["customers"] if c["id"] != customer_id]
    save_data(data)
    return jsonify({"data": {
        "deleted_customer_id": customer_id,
        "deleted_job_ids": job_ids,
    }})

# ─────────────────────────────────────────────────────────────────────
# Jobs
# ─────────────────────────────────────────────────────────────────────
@app.get("/jobs")
def list_jobs():
    data = load_data()
    jobs = data["jobs"]
    # Optional filter by customer_id (real FieldPulse supports this)
    customer_id = request.args.get("customer_id")
    if customer_id:
        jobs = [j for j in jobs if j["customer_id"] == customer_id]
    return paginate(jobs)

@app.get("/jobs/<job_id>")
def get_job(job_id):
    data = load_data()
    job = next((j for j in data["jobs"] if j["id"] == job_id), None)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"data": job})

@app.post("/jobs")
def create_job():
    data = load_data()
    body = request.get_json() or {}
    new_job = {
        "id": f"job_{uuid.uuid4().hex[:8]}",
        "customer_id": body.get("customer_id"),
        "title": body.get("title", "New Job"),
        "status": body.get("status", "design"),
        "scheduled_start": body.get("scheduled_start"),
        "scheduled_end": body.get("scheduled_end"),
        "contract_value": body.get("contract_value"),
        "custom_fields": body.get("custom_fields", {}),
        "created_at": _now(),
        "updated_at": _now(),
    }
    data["jobs"].append(new_job)
    data["notes"][new_job["id"]] = []
    data["subtasks"][new_job["id"]] = []
    save_data(data)
    return jsonify({"data": new_job}), 201

@app.patch("/jobs/<job_id>")
def update_job(job_id):
    data = load_data()
    job = next((j for j in data["jobs"] if j["id"] == job_id), None)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    body = request.get_json() or {}
    # Custom fields merge instead of overwrite — important behavior
    if "custom_fields" in body:
        job["custom_fields"].update(body["custom_fields"])
        del body["custom_fields"]
    for k, v in body.items():
        if k not in ("id", "created_at"):
            job[k] = v
    job["updated_at"] = _now()
    save_data(data)
    return jsonify({"data": job})

# ─────────────────────────────────────────────────────────────────────
# Notes on a job
# ─────────────────────────────────────────────────────────────────────
@app.get("/jobs/<job_id>/notes")
def list_notes(job_id):
    data = load_data()
    if job_id not in data["notes"]:
        return jsonify({"error": "Job not found"}), 404
    return paginate(data["notes"][job_id])

@app.post("/jobs/<job_id>/notes")
def add_note(job_id):
    data = load_data()
    if job_id not in data["notes"]:
        data["notes"][job_id] = []
    body = request.get_json() or {}
    note = {
        "id": f"note_{uuid.uuid4().hex[:8]}",
        "job_id": job_id,
        "body": body.get("body", ""),
        "source": body.get("source", "manual"),  # 'manual' or 'voice_memo' etc.
        "created_at": _now(),
    }
    data["notes"][job_id].append(note)
    save_data(data)
    return jsonify({"data": note}), 201

# ─────────────────────────────────────────────────────────────────────
# Subtasks
# ─────────────────────────────────────────────────────────────────────
@app.get("/jobs/<job_id>/subtasks")
def list_subtasks(job_id):
    data = load_data()
    if job_id not in data["subtasks"]:
        return jsonify({"error": "Job not found"}), 404
    return paginate(data["subtasks"][job_id])

@app.post("/jobs/<job_id>/subtasks")
def create_subtask(job_id):
    data = load_data()
    if job_id not in data["subtasks"]:
        data["subtasks"][job_id] = []
    body = request.get_json() or {}
    subtask = {
        "id": f"sub_{uuid.uuid4().hex[:8]}",
        "job_id": job_id,
        "title": body.get("title", ""),
        "due_date": body.get("due_date"),
        "priority": body.get("priority", "med"),
        "status": body.get("status", "open"),
        "source": body.get("source", "manual"),
        "created_at": _now(),
        "updated_at": _now(),
    }
    data["subtasks"][job_id].append(subtask)
    save_data(data)
    return jsonify({"data": subtask}), 201

@app.patch("/subtasks/<subtask_id>")
def update_subtask(subtask_id):
    data = load_data()
    found = None
    found_job_id = None
    for jid, tasks in data["subtasks"].items():
        for t in tasks:
            if t["id"] == subtask_id:
                found = t
                found_job_id = jid
                break
        if found:
            break
    if not found:
        return jsonify({"error": "Subtask not found"}), 404
    body = request.get_json() or {}
    for k, v in body.items():
        if k not in ("id", "job_id", "created_at"):
            found[k] = v
    found["updated_at"] = _now()
    save_data(data)
    return jsonify({"data": found})

# ─────────────────────────────────────────────────────────────────────
# Custom field schema
# ─────────────────────────────────────────────────────────────────────
@app.get("/custom_fields")
def list_custom_fields():
    data = load_data()
    return jsonify({"data": data["custom_fields_schema"]})

# ─────────────────────────────────────────────────────────────────────
# Reset helper — handy during development
# ─────────────────────────────────────────────────────────────────────
@app.post("/_reset")
def reset_data():
    """Wipe everything and reseed. Not a real FieldPulse endpoint —
    just useful during dev. Underscore prefix marks it as internal."""
    save_data(_seed_data())
    return jsonify({"status": "reset", "message": "Data reseeded."})


# ─────────────────────────────────────────────────────────────────────
# Admin UI — local browser view for inspecting mock data while
# developing the integration. No auth (skipped in before_request).
# ─────────────────────────────────────────────────────────────────────
_ADMIN_CSS = """
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    color: #1f2933;
    background: #f8f9fa;
    margin: 0;
    padding: 0;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }
  .banner {
    background: #1f2933;
    color: #e9edf2;
    padding: 10px 28px;
    font-size: 13px;
    font-weight: 500;
    letter-spacing: 0.03em;
  }
  .container { max-width: 920px; margin: 0 auto; padding: 32px 24px 48px; }
  h1 { font-size: 24px; font-weight: 600; margin: 0 0 4px 0; letter-spacing: -0.01em; }
  h2 {
    font-size: 12px; font-weight: 600; margin: 32px 0 12px 0;
    color: #7b8794; text-transform: uppercase; letter-spacing: 0.07em;
  }
  .crumbs { font-size: 13px; color: #7b8794; margin-bottom: 24px; }
  .crumbs a { color: #52606d; }
  a { color: #2563b6; text-decoration: none; }
  a:hover { text-decoration: underline; }

  .card {
    background: #fff;
    border: 1px solid #e6e9ed;
    border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    padding: 20px 22px;
    margin-bottom: 16px;
  }

  table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    background: #fff;
    border: 1px solid #e6e9ed;
    border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    overflow: hidden;
    margin-bottom: 16px;
  }
  th, td {
    text-align: left;
    padding: 11px 16px;
    border-bottom: 1px solid #eef1f4;
    font-size: 14px;
    vertical-align: top;
  }
  th {
    background: #f4f6f8; font-weight: 600; color: #52606d;
    font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em;
  }
  tbody tr:nth-child(even) { background: #fafbfc; }
  tbody tr:hover { background: #f1f5f9; }
  tr:last-child td { border-bottom: none; }

  .mono { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 12px; color: #7b8794; }
  .empty { color: #9aa5b1; font-style: italic; padding: 12px 0; }

  /* status / source pills */
  .pill {
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: 12px; font-weight: 600; letter-spacing: 0.01em;
    background: #ebedef; color: #586069;
  }
  .status-new_lead { background: #e6f0fb; color: #1b4f8a; }
  .status-design { background: #efe8fb; color: #5a3aa3; }
  .status-sourcing { background: #fbf2cf; color: #856404; }
  .status-in_progress { background: #e4f6ea; color: #1e7a45; }
  .status-consultation_scheduled { background: #fdecd9; color: #a45c14; }
  .status-completed { background: #ebedef; color: #586069; }
  .status-open { background: #eaf1f6; color: #41566a; }
  .src-voice_intake_ai { background: #e3f1f1; color: #1f6f6b; }
  .src-manual { background: #eef1f4; color: #52606d; }

  /* contact / overview definition lists */
  dl { margin: 0; display: grid; grid-template-columns: 160px 1fr; gap: 8px 16px; }
  dt { font-size: 12px; color: #9aa5b1; text-transform: uppercase; letter-spacing: 0.04em; padding-top: 2px; }
  dd { margin: 0; font-size: 14px; }

  /* notes as cards */
  .note {
    background: #fff; border: 1px solid #e6e9ed; border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    padding: 16px 18px; margin-bottom: 12px;
  }
  .note-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; gap: 12px; }
  .note-time { font-size: 12px; color: #9aa5b1; font-family: "SF Mono", Menlo, Consolas, monospace; white-space: nowrap; }
  .note-summary { font-size: 14px; white-space: pre-wrap; }

  .box-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 6px; }
  .flags-box {
    margin-top: 14px; background: #fff8e6; border: 1px solid #f3e2b3;
    border-radius: 6px; padding: 10px 14px;
  }
  .flags-box .box-label { color: #946c00; }
  .flags-list { margin: 0; padding-left: 18px; }
  .flags-list li { font-size: 13px; color: #6b5410; margin: 3px 0; }

  .quotes-box { margin-top: 14px; }
  .quotes-box .box-label { color: #7b8794; }
  .quote {
    margin: 6px 0; padding: 6px 14px; border-left: 3px solid #cfd6dd;
    color: #52606d; font-style: italic; font-size: 13px; background: #fafbfc;
  }

  .transcript { margin-top: 14px; }
  .transcript summary { cursor: pointer; font-size: 11px; color: #7b8794; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }
  .transcript-body {
    margin-top: 8px; white-space: pre-wrap; font-size: 13px; color: #52606d;
    background: #f8f9fa; border: 1px solid #eef1f4; border-radius: 6px; padding: 12px;
  }

  .reset-btn {
    background: #d64545; color: #fff; border: none;
    padding: 10px 18px; font-size: 14px; font-weight: 500;
    cursor: pointer; border-radius: 6px;
  }
  .reset-btn:hover { background: #b83a3a; }

  /* consultation section on the job page */
  .consult-card dl { grid-template-columns: 210px 1fr; }
  .consult-sub {
    font-size: 11px; font-weight: 600; color: #7b8794;
    text-transform: uppercase; letter-spacing: 0.07em;
    margin: 22px 0 10px 0;
    padding-bottom: 6px; border-bottom: 1px solid #eef1f4;
  }
  .consult-sub:first-child { margin-top: 0; }
  .consult-mini {
    font-size: 11px; font-weight: 600; color: #9aa5b1;
    text-transform: uppercase; letter-spacing: 0.05em;
    margin: 16px 0 6px 0;
  }
  .consult-details {
    margin-top: 4px; padding: 6px 12px;
    font-size: 13px; color: #52606d;
    background: #fafbfc; border-left: 2px solid #e6e9ed; border-radius: 4px;
  }
  .consult-range {
    color: #a45c14; font-size: 13px; margin-left: 8px;
  }
"""

_ADMIN_BANNER = '<div class="banner">Mock FieldPulse · Development Admin</div>'
_ADMIN_HEAD = (
    '<meta http-equiv="refresh" content="10">'
    '<style>' + _ADMIN_CSS + '</style>'
)


_LOCAL_TZ = ZoneInfo("America/Chicago")


def _format_timestamp(value):
    """UTC ISO timestamp → 'May 22, 2026 at 10:18 PM' in America/Chicago local
    time. Returns '—' on empty input, or the raw value if it can't be parsed."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return value
    # created_at/updated_at are stored as UTC; treat any naive value as UTC too.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_LOCAL_TZ).strftime("%b %-d, %Y at %-I:%M %p")


def _format_date(value):
    """Date-only string (YYYY-MM-DD) → 'May 19, 2026'."""
    if not value:
        return "—"
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
        return dt.strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return value


# Presentation helper for the admin job page: turn the flat consult_* custom
# fields into a clean three-group structure with friendly labels and formatted
# values. Lives next to _format_timestamp/_format_date because it's display-only
# — the API still returns the raw custom_fields untouched.
def _consult_groups(custom_fields):
    """Return a grouped, display-ready view of any consult_* custom fields.

    Shape:
      {
        "has_any": bool,
        "scope":      [{"label", "value", "details"?}],
        "appliances": [{"label", "value"}],
        "timeline":   [{"label", "value"}],
        "sales":      [{"label", "value", "range"?}],
      }
    Returns {"has_any": False} when the job has no consult_* fields at all.
    """
    cf = custom_fields or {}
    if not any(k.startswith("consult_") for k in cf):
        return {"has_any": False}

    def _str(value):
        if value is None:
            return ""
        return str(value).strip()

    def _is_blank(value):
        s = _str(value)
        return s == "" or s.lower() == "unknown"

    def _yn(value):
        if isinstance(value, bool):
            return "Yes" if value else "No"
        s = _str(value).lower()
        return {
            "yes": "Yes", "no": "No", "unknown": "Unknown",
            "true": "Yes", "false": "No",
        }.get(s, _str(value))

    def _money(value):
        try:
            return f"${int(round(float(value))):,}"
        except (TypeError, ValueError):
            return None

    def _add(rows, label, value):
        if not _is_blank(value):
            rows.append({"label": label, "value": _str(value)})

    # ---- Scope ----------------------------------------------------------
    scope = []
    _add(scope, "Project type", cf.get("consult_project_type"))
    _add(scope, "Project vision", cf.get("consult_project_vision"))
    for key, label in (
        ("consult_structural", "Structural changes"),
        ("consult_plumbing", "Plumbing changes"),
        ("consult_electrical", "Electrical changes"),
        ("consult_lighting", "Lighting changes"),
    ):
        ans = cf.get(key)
        if _is_blank(ans):
            continue
        row = {"label": label, "value": _yn(ans)}
        details = cf.get(f"{key}_details")
        if not _is_blank(details):
            row["details"] = _str(details)
        scope.append(row)
    _add(scope, "Style", cf.get("consult_style"))

    # ---- Appliances (sub-group within Scope) ----------------------------
    appliances = []
    for key, label in (
        ("consult_appliance_refrigerator", "Refrigerator"),
        ("consult_appliance_range", "Range"),
        ("consult_appliance_dishwasher", "Dishwasher"),
        ("consult_appliance_microwave", "Microwave"),
        ("consult_appliance_hood", "Hood"),
        ("consult_appliance_other", "Other"),
    ):
        _add(appliances, label, cf.get(key))

    # ---- Timeline & Readiness ------------------------------------------
    timeline = []
    _add(timeline, "Time frame", cf.get("consult_time_frame"))
    _add(timeline, "Thinking duration", cf.get("consult_thinking_duration"))
    _add(timeline, "Work done so far", cf.get("consult_work_done"))

    # ---- Sales ----------------------------------------------------------
    sales = []
    budget_raw = cf.get("consult_budget")
    lo = _money(cf.get("consult_budget_low"))
    hi = _money(cf.get("consult_budget_high"))
    if not _is_blank(budget_raw) or lo or hi:
        row = {"label": "Budget"}
        row["value"] = _str(budget_raw) if not _is_blank(budget_raw) else ""
        if lo and hi:
            row["range"] = f"({lo}–{hi})"
        elif lo:
            row["range"] = f"({lo}+)"
        elif hi:
            row["range"] = f"(up to {hi})"
        # If no raw was given, surface the range as the primary value.
        if not row["value"] and row.get("range"):
            row["value"] = row.pop("range").strip("()")
        if row["value"] or row.get("range"):
            sales.append(row)

    likelihood = cf.get("consult_likelihood_percent")
    if not _is_blank(likelihood):
        try:
            sales.append({
                "label": "Likelihood",
                "value": f"{int(round(float(likelihood)))}%",
            })
        except (TypeError, ValueError):
            sales.append({"label": "Likelihood", "value": _str(likelihood)})

    _add(sales, "Close score (1–10)", cf.get("consult_close_score"))
    if not _is_blank(cf.get("consult_waiting_financing")):
        sales.append({
            "label": "Waiting on financing",
            "value": _yn(cf.get("consult_waiting_financing")),
        })
    _add(sales, "Concerns", cf.get("consult_concerns"))
    if cf.get("consult_presentation_shown") is not None:
        sales.append({
            "label": "Presentation shown",
            "value": _yn(cf.get("consult_presentation_shown")),
        })

    has_any = bool(scope or appliances or timeline or sales)
    return {
        "has_any": has_any,
        "scope": scope,
        "appliances": appliances,
        "timeline": timeline,
        "sales": sales,
    }


app.jinja_env.filters["ts"] = _format_timestamp
app.jinja_env.filters["d"] = _format_date


_ADMIN_INDEX_TPL = """<!doctype html>
<html><head><title>Admin · Mock FieldPulse</title>{{ head|safe }}</head>
<body>
  {{ banner|safe }}
  <div class="container">
    <h1>Customers</h1>
    <div class="crumbs">/admin · <a href="/admin/reset">reset data</a></div>
    <table>
      <thead><tr><th>Name</th><th>ID</th><th>Jobs</th></tr></thead>
      <tbody>
      {% for c in customers %}
        <tr>
          <td><a href="/admin/customers/{{ c.id }}">{{ c.display_name }}</a></td>
          <td class="mono">{{ c.id }}</td>
          <td>{{ job_counts[c.id] }}</td>
        </tr>
      {% else %}
        <tr><td colspan="3" class="empty">No customers.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</body></html>
"""

_ADMIN_CUSTOMER_TPL = """<!doctype html>
<html><head><title>{{ customer.display_name }} · Admin</title>{{ head|safe }}</head>
<body>
  {{ banner|safe }}
  <div class="container">
    <div class="crumbs"><a href="/admin">← All customers</a></div>
    <h1>{{ customer.display_name }}</h1>
    <div class="mono">{{ customer.id }}</div>

    <h2>Contact</h2>
    <div class="card">
      <dl>
        <dt>Name</dt><dd>{{ customer.first_name }} {{ customer.last_name }}</dd>
        <dt>Email</dt><dd>{{ customer.email or '—' }}</dd>
        <dt>Phone</dt><dd>{{ customer.phone or '—' }}</dd>
      </dl>
    </div>

    <h2>Address</h2>
    <div class="card">
      {% set a = customer.address or {} %}
      {% if a %}
        <div>{{ a.street }}</div>
        <div>{{ a.city }}, {{ a.state }} {{ a.postal_code }}</div>
      {% else %}
        <div class="empty">No address on file.</div>
      {% endif %}
    </div>

    <h2>Jobs</h2>
    <table>
      <thead><tr><th>Title</th><th>Status</th><th>ID</th></tr></thead>
      <tbody>
      {% for j in jobs %}
        <tr>
          <td><a href="/admin/jobs/{{ j.id }}">{{ j.title }}</a></td>
          <td><span class="pill status-{{ j.status }}">{{ j.status.replace('_', ' ') }}</span></td>
          <td class="mono">{{ j.id }}</td>
        </tr>
      {% else %}
        <tr><td colspan="3" class="empty">No jobs for this customer.</td></tr>
      {% endfor %}
      </tbody>
    </table>

    <h2>Danger zone</h2>
    <div class="card">
      <p>Delete this customer along with all of their jobs, notes, and
         subtasks. This cannot be undone.</p>
      <button id="delete-btn" type="button" class="reset-btn">Delete customer &amp; all data</button>
    </div>
    <script>
      document.getElementById('delete-btn').addEventListener('click', async () => {
        const name = {{ customer.display_name|tojson }};
        if (!confirm('Delete ' + name + ' and ALL their jobs, notes, and subtasks?\\nThis cannot be undone.')) {
          return;
        }
        const btn = document.getElementById('delete-btn');
        btn.disabled = true;
        btn.textContent = 'Deleting…';
        const resp = await fetch('/customers/{{ customer.id }}', {
          method: 'DELETE',
          headers: {'x-api-key': 'admin-ui'},
        });
        if (resp.ok) {
          window.location = '/admin';
        } else {
          btn.disabled = false;
          btn.textContent = 'Delete customer & all data';
          alert('Delete failed (HTTP ' + resp.status + ').');
        }
      });
    </script>
  </div>
</body></html>
"""

_ADMIN_JOB_TPL = """<!doctype html>
<html><head><title>{{ job.title }} · Admin</title>{{ head|safe }}</head>
<body>
  {{ banner|safe }}
  <div class="container">
    <div class="crumbs">
      <a href="/admin">All customers</a> ·
      <a href="/admin/customers/{{ customer.id }}">← {{ customer.display_name }}</a>
    </div>
    <h1>{{ job.title }}</h1>
    <div class="mono">{{ job.id }}</div>

    <h2>Overview</h2>
    <div class="card">
      <dl>
        <dt>Status</dt><dd><span class="pill status-{{ job.status }}">{{ job.status.replace('_', ' ') }}</span></dd>
        <dt>Scheduled start</dt><dd>{{ job.scheduled_start|d }}</dd>
        <dt>Scheduled end</dt><dd>{{ job.scheduled_end|d }}</dd>
        <dt>Contract value</dt>
        <dd>{% if job.contract_value is not none %}${{ '{:,.2f}'.format(job.contract_value) }}{% else %}—{% endif %}</dd>
      </dl>
    </div>

    <h2>Custom fields</h2>
    <table>
      <thead><tr><th>Key</th><th>Value</th></tr></thead>
      <tbody>
      {% for k, v in intake_custom_fields.items() %}
        <tr><td class="mono">{{ k }}</td><td>{{ v }}</td></tr>
      {% else %}
        <tr><td colspan="2" class="empty">No custom fields set.</td></tr>
      {% endfor %}
      </tbody>
    </table>

    {% if consult.has_any %}
    <h2>Consultation</h2>
    <div class="card consult-card">
      {% if consult.scope or consult.appliances %}
        <div class="consult-sub">Scope</div>
        {% if consult.scope %}
        <dl>
        {% for row in consult.scope %}
          <dt>{{ row.label }}</dt>
          <dd>
            {{ row.value }}
            {% if row.details %}<div class="consult-details">{{ row.details }}</div>{% endif %}
          </dd>
        {% endfor %}
        </dl>
        {% endif %}
        {% if consult.appliances %}
          <div class="consult-mini">Appliances</div>
          <dl>
          {% for row in consult.appliances %}
            <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
          {% endfor %}
          </dl>
        {% endif %}
      {% endif %}
      {% if consult.timeline %}
        <div class="consult-sub">Timeline &amp; Readiness</div>
        <dl>
        {% for row in consult.timeline %}
          <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
        {% endfor %}
        </dl>
      {% endif %}
      {% if consult.sales %}
        <div class="consult-sub">Sales</div>
        <dl>
        {% for row in consult.sales %}
          <dt>{{ row.label }}</dt>
          <dd>
            {{ row.value }}{% if row.range %}<span class="consult-range">{{ row.range }}</span>{% endif %}
          </dd>
        {% endfor %}
        </dl>
      {% endif %}
    </div>
    {% endif %}

    <h2>Notes</h2>
    {% for n in notes %}
      {# Parse our own controlled note format into sections (display only). #}
      {% set _t = n.body.split('\n\n--- ORIGINAL TRANSCRIPT ---\n') %}
      {% set _main = _t[0] %}
      {% set _transcript = _t[1] if _t|length > 1 else None %}
      {% set _q = _main.split('\n\nRAW QUOTES:\n') %}
      {% set _pre = _q[0] %}
      {% set _quotes = _q[1] if _q|length > 1 else None %}
      {% set _f = _pre.split('\n\nFLAGS:\n') %}
      {% set _summary = _f[0] %}
      {% set _flags = _f[1] if _f|length > 1 else None %}
      <div class="note">
        <div class="note-head">
          <span class="pill src-{{ n.source }}">{{ n.source.replace('_', ' ') }}</span>
          <span class="note-time">{{ n.created_at|ts }}</span>
        </div>
        <div class="note-summary">{{ _summary }}</div>
        {% if _flags %}
        <div class="flags-box">
          <div class="box-label">Flags</div>
          <ul class="flags-list">
            {% for line in _flags.split('\n') %}
              {% if line.strip() %}<li>{{ line[2:] if line.startswith('- ') else line }}</li>{% endif %}
            {% endfor %}
          </ul>
        </div>
        {% endif %}
        {% if _quotes %}
        <div class="quotes-box">
          <div class="box-label">Raw quotes</div>
          {% for line in _quotes.split('\n') %}
            {% if line.strip() %}<blockquote class="quote">{{ line[2:] if line.startswith('> ') else line }}</blockquote>{% endif %}
          {% endfor %}
        </div>
        {% endif %}
        {% if _transcript %}
        <details class="transcript">
          <summary>Original transcript</summary>
          <div class="transcript-body">{{ _transcript }}</div>
        </details>
        {% endif %}
      </div>
    {% else %}
      <div class="empty">No notes yet.</div>
    {% endfor %}

    <h2>Subtasks</h2>
    <table>
      <thead><tr><th>Title</th><th>Priority</th><th>Due</th><th>Status</th></tr></thead>
      <tbody>
      {% for s in subtasks %}
        <tr>
          <td>{{ s.title }}</td>
          <td>{{ s.priority }}</td>
          <td>{{ s.due_date|d }}</td>
          <td><span class="pill status-{{ s.status }}">{{ s.status.replace('_', ' ') }}</span></td>
        </tr>
      {% else %}
        <tr><td colspan="4" class="empty">No subtasks yet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</body></html>
"""

_ADMIN_RESET_TPL = """<!doctype html>
<html><head><title>Reset · Admin</title>{{ head|safe }}</head>
<body>
  {{ banner|safe }}
  <div class="container">
    <div class="crumbs"><a href="/admin">← Back to admin</a></div>
    <h1>Reset mock data</h1>
    <p>This wipes <span class="mono">mock_data.json</span> and reseeds the
       initial three customers and jobs. Notes and subtasks are cleared.</p>
    <button id="reset-btn" type="button" class="reset-btn">Reset everything</button>
    <script>
      document.getElementById('reset-btn').addEventListener('click', async () => {
        const btn = document.getElementById('reset-btn');
        btn.disabled = true;
        btn.textContent = 'Resetting…';
        await fetch('/_reset', {
          method: 'POST',
          headers: {'x-api-key': 'admin-ui'},
        });
        window.location = '/admin';
      });
    </script>
  </div>
</body></html>
"""


@app.get("/admin")
def admin_index():
    data = load_data()
    job_counts = {c["id"]: 0 for c in data["customers"]}
    for j in data["jobs"]:
        if j["customer_id"] in job_counts:
            job_counts[j["customer_id"]] += 1
    return render_template_string(
        _ADMIN_INDEX_TPL,
        customers=data["customers"],
        job_counts=job_counts,
        head=_ADMIN_HEAD,
        banner=_ADMIN_BANNER,
    )


@app.get("/admin/customers/<customer_id>")
def admin_customer(customer_id):
    data = load_data()
    customer = next((c for c in data["customers"] if c["id"] == customer_id), None)
    if not customer:
        return "Customer not found", 404
    jobs = [j for j in data["jobs"] if j["customer_id"] == customer_id]
    return render_template_string(
        _ADMIN_CUSTOMER_TPL,
        customer=customer,
        jobs=jobs,
        head=_ADMIN_HEAD,
        banner=_ADMIN_BANNER,
    )


@app.get("/admin/jobs/<job_id>")
def admin_job(job_id):
    data = load_data()
    job = next((j for j in data["jobs"] if j["id"] == job_id), None)
    if not job:
        return "Job not found", 404
    customer = next((c for c in data["customers"] if c["id"] == job["customer_id"]), None) or {
        "id": job["customer_id"], "display_name": "(unknown)"
    }
    notes = data.get("notes", {}).get(job_id, [])
    subtasks = data.get("subtasks", {}).get(job_id, [])
    # Split custom_fields for the admin view: consult_* lands in the grouped
    # Consultation section, everything else stays in the flat intake table.
    all_custom = job.get("custom_fields") or {}
    intake_custom_fields = {k: v for k, v in all_custom.items() if not k.startswith("consult_")}
    consult = _consult_groups(all_custom)
    return render_template_string(
        _ADMIN_JOB_TPL,
        job=job,
        customer=customer,
        notes=notes,
        subtasks=subtasks,
        intake_custom_fields=intake_custom_fields,
        consult=consult,
        head=_ADMIN_HEAD,
        banner=_ADMIN_BANNER,
    )


@app.get("/admin/reset")
def admin_reset():
    return render_template_string(
        _ADMIN_RESET_TPL,
        head=_ADMIN_HEAD,
        banner=_ADMIN_BANNER,
    )


if __name__ == "__main__":
    # Load once on startup so the seed file exists
    load_data()
    print("─" * 60)
    print("Mock FieldPulse API running on http://localhost:5000")
    print("API key: dev-test-key-12345 (any non-empty value works)")
    print("Data file: " + DATA_FILE)
    print("─" * 60)
    app.run(port=5000, debug=False)
