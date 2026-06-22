"""Standalone test for write_consultation_to_fieldpulse() — exercises both paths.

Test 1: WITH customer_id pointing at a seed customer (Daniel Whitaker / cust_001).
        Should hit match_path="matched_by_id" and PATCH consult_* fields onto
        his existing kitchen job without wiping the intake fields.

Test 2: WITHOUT customer_id, with a brand-new client whose phone/email/name
        don't match any seed customer. Should hit match_path="created_new_no_match"
        and create both a new customer and a new job.

PREREQUISITE: the mock FieldPulse server must be running at http://localhost:5000.
    cd "/Users/nischaldahal/Desktop/my projects/VoiceControlExcel/mockfieldpulse"
    venv/bin/python mock_fieldpulse.py
Leave it running in another terminal, then run this script.
"""

import json

from consultation_writer import WriterError, write_consultation_to_fieldpulse

# ---------------------------------------------------------------------------
# Test 1: kitchen consultation for an existing seed customer.
# ---------------------------------------------------------------------------

WHITAKER_KITCHEN_CONSULTATION = {
    "client": {
        "name": "Daniel Whitaker",
        "phone": "618-555-0143",
        "email": "dan.whitaker@gmail.com",
    },
    "project": {
        "type": "kitchen",
        "vision": (
            "Open up the wall between kitchen and dining, white shaker cabinets, "
            "quartz counters, and an island that seats three."
        ),
    },
    "structural": {
        "answer": "yes",
        "details": "Removing the wall between kitchen and dining (load status to be verified)",
    },
    "plumbing": {"answer": "no", "details": None},
    "electrical": {
        "answer": "yes",
        "details": "Under-cabinet lighting circuit and relocated outlets along new wall line",
    },
    "lighting": {
        "answer": "yes",
        "details": "Recessed lights over the new island; pendants over peninsula",
    },
    "style": "transitional — white shaker, brushed nickel",
    "appliances": {
        "refrigerator": "keep existing",
        "range": "new — gas slide-in",
        "dishwasher": "new",
        "microwave": "drawer microwave under island",
        "hood": "vented range hood",
        "other": None,
    },
    "time_frame": "start in early September, want it usable by Thanksgiving",
    "thinking_duration": "about 6 months",
    "work_done": "none yet",
    "budget": {
        "budget_raw": "high 30s to low 40s",
        "low": 38000,
        "high": 42000,
    },
    "likelihood_percent": 75,
    "close_score": "high",
    "waiting_financing": "no",
    "concerns": (
        "Wall removal timeline — hosting family in early November. "
        "Wants quartz seam location confirmed before signing."
    ),
    "presentation_shown": True,
    "notes_summary": (
        "Daniel and his wife want a full kitchen refresh with a wall removal "
        "between the kitchen and dining room. White shaker cabinets, quartz, "
        "new island with seating, gas range. Budget high 30s to low 40s. "
        "Time pressure around hosting family in early November."
    ),
    "flags": [
        "spouse not on the call — confirm before signing",
        "wall removal needs structural verification — schedule walk-through",
    ],
    "raw_quotes": [
        "We'd want it done before family comes in November",
        "We've been thinking about this for six months",
        "The cabinets are solid, we mostly just hate the laminate counters",
    ],
    "return_appointment": {
        "scheduled": True,
        "date": "2026-06-25",
        "time": "6:30 PM",
    },
}

# ---------------------------------------------------------------------------
# Test 2: brand-new client (no seed match) — kitchen consultation.
# ---------------------------------------------------------------------------

NEW_CLIENT_KITCHEN_CONSULTATION = {
    "client": {
        "name": "Sandra McKee",
        "phone": "618-555-0319",
        "email": "smckee@gmail.com",
    },
    "project": {
        "type": "kitchen",
        "vision": "Galley kitchen opened into the family room, with a peninsula.",
    },
    "structural": {
        "answer": "yes",
        "details": "Remove the half-wall between kitchen and family room",
    },
    "plumbing": {"answer": "yes", "details": "Relocate sink to peninsula"},
    "electrical": {"answer": "yes", "details": "New circuit for induction range"},
    "lighting": {"answer": "yes", "details": "Recessed across both rooms"},
    "style": "modern — flat-panel cabinets, black hardware",
    "appliances": {
        "refrigerator": "new — counter-depth",
        "range": "new — induction",
        "dishwasher": "new",
        "microwave": None,
        "hood": "vented",
        "other": None,
    },
    "time_frame": "late fall",
    "thinking_duration": "about a year",
    "work_done": "none",
    "budget": {
        "budget_raw": "around 55K",
        "low": 50000,
        "high": 60000,
    },
    "likelihood_percent": 60,
    "close_score": "medium",
    "waiting_financing": "yes",
    "concerns": "Waiting on financing decision — should hear back in 2 weeks",
    "presentation_shown": True,
    "notes_summary": (
        "Sandra McKee wants to open her galley kitchen into the family room. "
        "Induction range, modern flat-panel cabinets. Waiting on financing — "
        "expects to know in two weeks."
    ),
    "flags": [
        "financing not yet approved — do not order until confirmed",
        "spouse not on the call",
    ],
    "raw_quotes": [
        "We've been wanting this for a year",
        "It just feels closed off when we have people over",
    ],
    "return_appointment": {
        "scheduled": True,
        "date": "2026-07-08",
        "time": "7:00 PM",
    },
}


def _run(label, extraction, **kwargs):
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    try:
        result = write_consultation_to_fieldpulse(extraction, **kwargs)
    except WriterError as exc:
        print(f"WriterError: {exc}")
        return
    print(json.dumps(result, indent=2))
    if result.get("fieldpulse_url"):
        print(f"\nOpen: {result['fieldpulse_url']}")


def main():
    print("PREREQUISITE: the mock FieldPulse server must be running at localhost:5000.")
    print("Start it with: venv/bin/python mock_fieldpulse.py (in the mockfieldpulse folder)")

    _run(
        "TEST 1 — WITH customer_id (cust_001 = Daniel Whitaker, existing kitchen job)",
        WHITAKER_KITCHEN_CONSULTATION,
        customer_id="cust_001",
    )

    _run(
        "TEST 2 — WITHOUT customer_id (Sandra McKee — brand new, no match expected)",
        NEW_CLIENT_KITCHEN_CONSULTATION,
    )


if __name__ == "__main__":
    main()
