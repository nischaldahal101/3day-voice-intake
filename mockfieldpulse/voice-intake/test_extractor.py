"""Standalone test for extract_intake() — run in isolation before wiring up.

Sends a hardcoded fake intake transcript through the extractor and
pretty-prints the resulting dict.
"""

import json

from extractor import ExtractionError, extract_intake

TEST_TRANSCRIPT = """\
REP: Hi, this is Dave with 3 Day Kitchen and Bath, returning your call. Did I catch you at an okay time?
PROSPECT: Yeah, this is fine. I'm Karen Whitfield.
REP: Great to meet you, Karen. So what kind of project are you thinking about?
PROSPECT: Our master bathroom is original to the house and it's falling apart. We want to gut it and redo the whole thing.
REP: A full bath remodel, got it. What part of town are you over in?
PROSPECT: We're out in Cedar Hills, off the old mill road.
REP: And do you have a time frame in mind for getting started?
PROSPECT: Honestly, as soon as we can. We're hoping before the holidays.
REP: Perfect. And how did you hear about us?
PROSPECT: I heard your ad on the radio a couple weeks back.
REP: Love it. Can I get an email to send over our pre-meeting info?
PROSPECT: Sure, it's k.whitfield at outlook dot com.
"""


def main():
    try:
        result = extract_intake(TEST_TRANSCRIPT)
    except ExtractionError as exc:
        print(f"\nExtractionError: {exc}")
        if exc.raw_response is not None:
            print("\n--- Raw model response ---")
            print(exc.raw_response)
        return

    print("\n--- Extracted intake ---")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
