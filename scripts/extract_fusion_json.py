#!/usr/bin/env python3
"""Extract the first JSON object from fenced or noisy model output."""

import argparse
import json
import sys
from pathlib import Path


def candidates(text: str):
    # Prefer fenced JSON, then scan balanced braces while respecting strings.
    marker = "```json"
    start = text.find(marker)
    if start >= 0:
        end = text.find("```", start + len(marker))
        if end >= 0:
            yield text[start + len(marker):end].strip()

    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for end in range(start, len(text)):
            ch = text[end]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:end + 1]
                    break


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()

    text = args.input.read_text(encoding="utf-8")
    for candidate in candidates(text):
        try:
            packet = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(packet, dict):
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
            print(f"EXTRACTED {args.output}")
            return 0

    print("NO_JSON_OBJECT_FOUND", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
