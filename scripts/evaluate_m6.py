#!/usr/bin/env python3
"""Execute one explicitly selected frozen M6 split; no live key or HTTP."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluation_cases import evaluate_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("development", "holdout"), required=True)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    cases = args.cases or root / "evals" / "m6" / args.split
    output = args.output or root / "data" / "live" / "m6" / f"{args.split}-result.json"
    result = evaluate_directory(cases, split=args.split, output=output, artifacts=args.artifacts)
    print(f"{args.split}: {result['passed_case_count']}/{result['case_count']} hard-rule cases passed; "
          "live calls=0; semantic support requires review")
    print(output)
    return 0 if result["hard_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
