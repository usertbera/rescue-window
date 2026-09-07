"""
Runs one scenario through the live Strands-agent coordinator (Amazon Bedrock).

Requires AWS credentials with Bedrock access and BEDROCK_MODEL_ID set to a
model you have enabled in your account/region. See README: "Live Strands
agent". This is the counterpart to run_demo.py, which runs the deterministic,
reproducible path used for the acceptance checks and the 3-seed metrics.
"""

from __future__ import annotations

import sys

from agent_coordinator import AgentCoordinator
from registry import list_seeds, load_scenario

SEP = "=" * 78


def main() -> int:
    seed_id = sys.argv[1] if len(sys.argv) > 1 else list_seeds()[0]
    scenario = load_scenario(seed_id)

    print(SEP)
    print(f"{seed_id}  --  {scenario.label}  (live Strands agent, Amazon Bedrock)")
    print(SEP)

    coord = AgentCoordinator(scenario)
    result = coord.run()

    print("\nAGENT TRANSCRIPT")
    for line in result.transcript:
        print(line)

    print("\nEVENT LOG")
    for ev in result.events:
        print(ev.render())

    print()
    print(f"  -> {result.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
