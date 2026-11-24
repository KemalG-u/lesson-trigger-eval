#!/usr/bin/env python3
"""Toy candidate: substring match of each lesson's match_phrases against the event input.

Protocol (what any candidate must implement):
  stdin  : one JSON object {"client", "event", "input", "cwd"} (canonical adapter)
  stdout : {"results": [{"id": "L001", "source": "examples/lessons.md:3"}, ...]}
It is deliberately naive; it exists to show the protocol and to make the example runnable.
"""
import json
from pathlib import Path
import sys

LESSONS = Path(__file__).resolve().parent / "lessons.json"


def flatten(value):
    if isinstance(value, dict):
        return " ".join(flatten(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(flatten(v) for v in value)
    return str(value)


def main():
    payload = json.load(sys.stdin)
    text = flatten(payload.get("input", "")).casefold()
    event = payload.get("event")
    lessons = json.loads(LESSONS.read_text(encoding="utf-8"))
    results = []
    for lesson in lessons:
        if event not in lesson["trigger"]["events"]:
            continue
        if any(phrase.casefold() in text for phrase in lesson["match_phrases"]):
            results.append({"id": lesson["id"], "source": lesson["source"]})
    print(json.dumps({"results": results[:3]}))


if __name__ == "__main__":
    main()
