"""Smoke-test the /process-consultation endpoint of the orchestrator.

Hits /test-consultation-sample which runs the baked-in kitchen transcript
through extract_consultation + write_consultation_to_fieldpulse — no audio
file, no customer_id, so it exercises the create-new-customer path.

Also prints the curl commands you can copy/paste to:
  1. trigger the same smoke test from the shell
  2. POST a real audio file with a customer_id

PREREQUISITES (two servers must be running):
    1. Mock FieldPulse, in mockfieldpulse/:
         venv/bin/python mock_fieldpulse.py
    2. Orchestrator, in voice-intake/:
         venv/bin/python orchestrator.py
"""

import json
import os

import requests

BASE_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:5100")
INTAKE_KEY = os.environ.get("INTAKE_API_KEY", "dev-intake-key")
HEADERS = {"x-intake-key": INTAKE_KEY}


def main():
    print(f"Hitting {BASE_URL}/test-consultation-sample ...\n")
    try:
        resp = requests.post(
            f"{BASE_URL}/test-consultation-sample",
            headers=HEADERS,
            timeout=120,
        )
    except requests.ConnectionError:
        print(f"Could not connect to {BASE_URL}.")
        print("Start the orchestrator:  venv/bin/python orchestrator.py")
        print("(And make sure mock FieldPulse is running on :5000 too.)")
        return

    print(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        print(resp.text)
        return

    # Trim transcript in output so it's readable.
    if isinstance(data.get("transcript"), str) and len(data["transcript"]) > 400:
        data["transcript"] = data["transcript"][:400] + " ... [trimmed]"
    print(json.dumps(data, indent=2))

    fp = data.get("fieldpulse") or {}
    if fp.get("fieldpulse_url"):
        print(f"\nOpen in browser:\n  {fp['fieldpulse_url']}")

    # --- Copy/paste curl commands ---------------------------------------
    print("\n" + "=" * 70)
    print("CURL COMMANDS")
    print("=" * 70)
    print(f"""
# 1. Smoke-test with the baked-in sample (no body needed):
curl -X POST {BASE_URL}/test-consultation-sample \\
  -H "x-intake-key: {INTAKE_KEY}"

# 2. Process a transcript directly (no audio file) — handy for iterating:
curl -X POST {BASE_URL}/process-consultation \\
  -H "x-intake-key: {INTAKE_KEY}" \\
  -H "Content-Type: application/json" \\
  -d '{{"transcript": "REP: ... HOMEOWNER: ...", "customer_id": "cust_001"}}'

# 3. Process a REAL audio file (replace path + customer_id):
curl -X POST {BASE_URL}/process-consultation \\
  -H "x-intake-key: {INTAKE_KEY}" \\
  -F "audio=@/path/to/consultation.m4a" \\
  -F "customer_id=cust_001" \\
  -F "source=field-rep-iphone"
""")


if __name__ == "__main__":
    main()
