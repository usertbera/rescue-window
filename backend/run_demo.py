"""
Runs the three demo scenarios through the identical coordinator.

Nothing in coordinator.py, tools.py or safety_policy.py reads the seed name.
That is the acceptance test: same code, three different worlds, three
different plans.
"""

from __future__ import annotations

import sys

from coordinator import Coordinator, DeterministicPolicy, Metrics
from registry import list_seeds, load_scenario

SEP = "=" * 78


def run_seed(seed_id: str, verbose: bool = True):
    scenario = load_scenario(seed_id)
    coord = Coordinator(scenario, policy=DeterministicPolicy())
    result = coord.run()

    if verbose:
        print(SEP)
        print(f"{seed_id}  --  {scenario.label}")
        print(SEP)
        for ev in result.events:
            print(ev.render())
        print()
        print(f"  -> {result.summary()}")
        print()

    return result


def main() -> int:
    seeds = list_seeds()
    results = [run_seed(s) for s in seeds]

    print(SEP)
    print(Metrics(results).render())
    print(SEP)

    print("\nACCEPTANCE CHECKS")
    checks = [
        ("recipient rejection handled",
         any(e.type.value == "OUTREACH_DECLINED" for r in results for e in r.events)),
        ("unresolved capability handled",
         any(e.type.value in ("CONFIRMATION_RESOLVED", "CONFIRMATION_UNRESOLVED")
             for r in results for e in r.events)),
        ("transport failure handled",
         any(e.type.value == "TRANSPORT_REJECTED" for r in results for e in r.events)),
        ("replanning occurred",
         any(r.replans > 0 for r in results)),
        ("at least one correct escalation",
         any(r.human_interventions > 0 for r in results)),
        ("at least one fully autonomous completion",
         any(r.human_interventions == 0 and r.servings_delivered == r.servings_offered
             for r in results)),
    ]
    failed = 0
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        failed += 0 if ok else 1

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
