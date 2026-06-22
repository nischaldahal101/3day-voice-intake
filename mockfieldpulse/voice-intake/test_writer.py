"""Standalone test for write_intake_to_fieldpulse() — run in isolation.

Sends a hardcoded extraction dict (Whitfield-style fake data) to the mock
FieldPulse instance and pretty-prints the returned IDs.

PREREQUISITE: the mock FieldPulse server must be running first:
    cd "/Users/nischaldahal/Desktop/my projects/VoiceControlExcel/mockfieldpulse"
    venv/bin/python mock_fieldpulse.py
Leave it running in another terminal, then run this script.
"""

import json

from fieldpulse_writer import WriterError, write_intake_to_fieldpulse

TEST_EXTRACTION = {
    "match_confidence": 0.88,
    "prospect": {
        "name": "Karen Whitfield",
        "spouse_partner_name": "Tom Whitfield",
        "phone": "555-0142",
        "email": "k.whitfield@outlook.com",
        "street_address": "88 Cedar Hills Road",
        "city_or_part_of_town": "Cedar Hills",
    },
    "home": {
        "year_built": "1996",
        "years_at_residence": "8 years",
        "planning_to_move": False,
        "is_homeowner": True,
    },
    "project": {
        "job_type": "bath",
        "products_already_picked": False,
        "plans_drawn_up": False,
        "time_frame": "before the holidays",
    },
    "lead_source": {
        "channel": "radio",
        "details": "Heard radio ad a couple weeks back",
    },
    "consultation": {
        "scheduled": True,
        "date": "Thursday next week",
        "time": "6:00 PM",
        "both_decision_makers_will_attend": True,
    },
    "notes_for_fieldpulse": (
        "Karen and Tom Whitfield want a full master bath remodel in their 1996 "
        "Cedar Hills home. Hoping to finish before the holidays. Radio lead. "
        "Consultation set for Thursday next week at 6 PM with both attending."
    ),
    "raw_quotes": [
        "Our master bathroom is original to the house and it's falling apart",
        "We want to gut it and redo the whole thing",
        "We're hoping before the holidays",
    ],
    "next_actions": [
        {"title": "Send pre-meeting info email", "priority": "high", "due_date_hint": "today"},
        {"title": "Confirm consultation appointment", "priority": "high", "due_date_hint": "Thursday"},
        {"title": "Prep bath remodel sample folder", "priority": "med", "due_date_hint": "this week"},
    ],
    "flags": ["spouse last name assumed from prospect"],
    "qualification": {
        "full_scope_kitchen_bath": True,
        "refer_elsewhere_reason": None,
    },
}


def main():
    print("NOTE: the mock FieldPulse server must be running at localhost:5000.")
    print("Start it with: venv/bin/python mock_fieldpulse.py (in the mockfieldpulse folder)\n")

    try:
        result = write_intake_to_fieldpulse(TEST_EXTRACTION)
    except WriterError as exc:
        print(f"\nWriterError: {exc}")
        return

    print("\n--- Write result ---")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
