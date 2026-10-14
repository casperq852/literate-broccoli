"""Standalone CLI to inspect the financial-model extractor's output for a real file."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from vectorization.financial_model.financial_model_controller import extract_financial_model


def _main() -> None:
    """Parse CLI args, run extraction against the given file, and print a preview."""
    parser = argparse.ArgumentParser(description="Test the financial model extractor")
    parser.add_argument("--file", type=Path, required=True, help="Path to a MODL_*.xlsx file")
    parser.add_argument("--limit", type=int, default=25, help="Max records to print")
    parser.add_argument("--show-paths", action="store_true", help="List unique metric paths")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    records = extract_financial_model(data=args.file.read_bytes(), filename=args.file.name)
    print(f"\n-- {args.file.name}: {len(records)} records --\n")

    if args.show_paths:
        paths = sorted({record["metric"] for record in records})
        preview = 50
        print(f"Unique metric paths ({len(paths)}):\n")
        for path in paths[:preview]:
            print(f"  {path}")
        if len(paths) > preview:
            print(f"  ... and {len(paths) - preview} more")
        return

    seen: set[str] = set()
    for record in records[: args.limit]:
        if record["metric"] in seen:
            continue
        seen.add(record["metric"])
        value = record["content"].split(": ", 1)[-1]
        print(f"  [{record['is_estimate']:8}] {record['period_label']:18} | {record['metric']:55} | {value}")

    if records:
        print(f"\nlatest_reported_date: {records[0]['latest_reported_date']}")
        print("\nSample record:")
        print(json.dumps(records[0], indent=2, default=str))


if __name__ == "__main__":
    _main()
