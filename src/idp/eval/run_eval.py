"""Score pipeline output against a gold set.

    idp-eval --gold eval/gold/gold.jsonl --predictions eval/predictions.jsonl \
        --baseline-minutes 12 --review-minutes 4 --spot-minutes 0.5
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from idp.eval.metrics import review_effort_reduction, score


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gold", type=Path, required=True)
    ap.add_argument("--predictions", type=Path, required=True)
    ap.add_argument("--baseline-minutes", type=float)
    ap.add_argument("--review-minutes", type=float)
    ap.add_argument("--spot-minutes", type=float, default=0.0)
    args = ap.parse_args(argv)

    gold = load_jsonl(args.gold)
    preds = {p["document_id"]: p for p in load_jsonl(args.predictions)}
    report = score(gold, preds)
    out = report.to_dict()
    if args.baseline_minutes and args.review_minutes is not None:
        out["review_effort_reduction"] = round(
            review_effort_reduction(report, args.baseline_minutes, args.review_minutes, args.spot_minutes), 4
        )
    print(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    main()
