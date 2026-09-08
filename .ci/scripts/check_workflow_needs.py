#!/usr/bin/env python3
"""Fail unless each GitHub Actions dependency succeeded or is an allowed skip."""

from __future__ import annotations

import json
import os


def main() -> None:
    needs = json.loads(os.environ["NEEDS_JSON"])
    skippable = set(os.environ.get("SKIPPABLE_JOBS", "").split())
    if not isinstance(needs, dict) or not skippable <= set(needs):
        raise SystemExit("workflow dependency input is malformed")

    failed = []
    for name, value in needs.items():
        if not isinstance(value, dict):
            raise SystemExit("workflow dependency input is malformed")
        result = value.get("result")
        if result == "success" or (result == "skipped" and name in skippable):
            continue
        failed.append(f"{name}={result}")
    if failed:
        raise SystemExit("required workflow dependencies did not succeed: " + ", ".join(failed))


if __name__ == "__main__":
    main()
