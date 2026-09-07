"""
Runs the 30 generated scenarios through the identical deterministic
coordinator used by run_demo.py, and reports the aggregate metrics.

This is the evidence half of the pair described in the README: the 3
hand-authored seeds are the narrative demo ("I can see it replan"), this is
the evidence that it is not one lucky scripted scenario. Run
generate_evaluation_scenarios.py first if backend/data/evaluation_seeds.json
does not exist yet or you want to regenerate it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from coordinator import Coordinator, DeterministicPolicy, Metrics
from registry import DATA_DIR, list_seeds, load_scenario

SEEDS_PATH = DATA_DIR / "evaluation_seeds.json"


def main() -> int:
    if not SEEDS_PATH.exists():
        print(f"{SEEDS_PATH} not found. Run generate_evaluation_scenarios.py first.")
        return 1

    seed_ids = list_seeds(path=SEEDS_PATH)
    results = []
    for sid in seed_ids:
        scenario = load_scenario(sid, path=SEEDS_PATH)
        coord = Coordinator(scenario, policy=DeterministicPolicy())
        results.append(coord.run())

    print(f"Ran {len(results)} generated scenarios (evaluation_seeds.json)\n")
    for r in results:
        print(f"  {r.summary()}")

    print()
    print(Metrics(results).render())

    completed = sum(1 for r in results if r.completed_without_human_decision)
    print(f"\n{completed}/{len(results)} scenarios completed without a human decision.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
