"""Standalone test for the orchestrator — hits the local HTTP server.

Confirms /health is up, then runs the full pipeline via /test-with-sample
and pretty-prints the response.

PREREQUISITES (two servers must be running):
    1. Mock FieldPulse, in the mockfieldpulse folder:
         venv/bin/python mock_fieldpulse.py
    2. The orchestrator, in this (voice-intake) folder:
         venv/bin/python orchestrator.py
"""

import json
import os

import requests

BASE_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:5100")
INTAKE_KEY = os.environ.get("INTAKE_API_KEY", "dev-intake-key")
HEADERS = {"x-intake-key": INTAKE_KEY}


def main():
    # 1. Health check (no auth).
    try:
        health = requests.get(f"{BASE_URL}/health", timeout=5)
    except requests.ConnectionError:
        print(f"Could not connect to the orchestrator at {BASE_URL}.")
        print("Start it first:  venv/bin/python orchestrator.py")
        print("(And make sure mock FieldPulse is running on :5000 too.)")
        return

    print(f"/health -> {health.status_code}: {health.json()}\n")

    # 2. Run the full pipeline against the baked-in sample.
    print("Calling /test-with-sample (this runs Claude + the writer, ~10s)...\n")
    try:
        resp = requests.post(
            f"{BASE_URL}/test-with-sample", headers=HEADERS, timeout=60
        )
    except requests.ConnectionError:
        print(f"Lost connection to the orchestrator at {BASE_URL}. Is it still running?")
        return

    print(f"/test-with-sample -> HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        print("Response was not JSON:")
        print(resp.text)
        return

    print(json.dumps(data, indent=2))

    fp = data.get("fieldpulse") or {}
    url = fp.get("fieldpulse_url")
    if url:
        print(f"\nOpen this in your browser to see the result:\n  {url}")
    else:
        print("\nNo fieldpulse_url in response (the write may have failed — see above).")


if __name__ == "__main__":
    main()
