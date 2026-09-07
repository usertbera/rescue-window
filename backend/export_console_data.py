"""
Exports the three demo runs to JSON for the operations console.

The console renders directly from these event objects. There is no
UI-specific representation of the agent's reasoning: the timestamps, policy
references and decision-point flags shown on screen are the same fields the
coordinator emitted while working.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator import Coordinator, Metrics
from registry import list_seeds, load_scenario

# Category drives the visual encoding in the console.
CATEGORY = {
    "OFFER_RECEIVED": "fact",
    "OUTREACH_ACCEPTED": "fact",
    "OUTREACH_DECLINED": "fact",
    "OUTREACH_TIMEOUT": "fact",
    "CONFIRMATION_RESOLVED": "fact",
    "CONFIRMATION_UNRESOLVED": "fact",
    "PICKUP_VERIFIED": "fact",
    "DELIVERY_VERIFIED": "fact",

    "CANDIDATES_GENERATED": "agent",
    "CONFIRMATION_REQUESTED": "agent",
    "OUTREACH_SENT": "agent",
    "TRANSPORT_SELECTED": "agent",
    "PLAN_COMMITTED": "agent",

    "SAFETY_CHECK": "policy",
    "CANDIDATE_ELIMINATED": "policy",
    "TRANSPORT_REJECTED": "policy",
    "TIMING_REJECTED": "policy",
    "DISPOSAL_DIRECTED": "policy",

    "REPLAN": "replan",
    "HUMAN_INTERVENTION": "human",
    "RESCUE_CLOSED": "close",
}

LABEL = {
    "fact": "Fact",
    "agent": "Agent action",
    "policy": "Policy gate",
    "replan": "Replan",
    "human": "Human decision",
    "close": "Closed",
}


def export() -> dict:
    runs = []
    results = []

    for sid in list_seeds():
        scenario = load_scenario(sid)
        coord = Coordinator(scenario)
        result = coord.run()
        results.append(result)

        offer = coord.sim.state.offer
        events = []
        for i, e in enumerate(result.events):
            cat = CATEGORY.get(e.type.value, "agent")
            events.append({
                "i": i,
                "at": e.at.strftime("%H:%M"),
                "iso": e.at.isoformat(),
                "type": e.type.value,
                "category": cat,
                "category_label": LABEL[cat],
                "detail": e.detail,
                "policy_ref": e.policy_ref,
                "decision_point": e.is_decision_point,
                "recipient_id": e.data.get("recipient_id"),
                "driver_id": e.data.get("driver_id"),
                "options": e.data.get("options"),
                "minutes_remaining": e.data.get("minutes_remaining"),
                "unplaced": e.data.get("unplaced"),
            })

        runs.append({
            "seed_id": sid,
            "label": scenario.label,
            "demonstrates": scenario.__dict__.get("demonstrates", ""),
            "offer": {
                "servings": offer.quantity_servings,
                "food_type": offer.food_type.value,
                "vegetarian": bool(
                    offer.prepared_label and offer.prepared_label.vegetarian),
                "food_name": (offer.prepared_label.food_name
                              if offer.prepared_label else offer.offer_id),
                "start": scenario.scenario_start.strftime("%H:%M"),
                "deadline": offer.consumption_deadline.strftime("%H:%M"),
                "window_min": int(
                    (offer.consumption_deadline
                     - scenario.scenario_start).total_seconds() / 60),
            },
            "outcome": {
                "delivered": result.servings_delivered,
                "offered": result.servings_offered,
                "interventions": result.human_interventions,
                "decision_points": result.decision_points,
                "replans": result.replans,
                "confirmations": result.confirmations_attempted,
                "unresolved": result.confirmations_unresolved,
                "closed_reason": result.closed_reason,
            },
            "events": events,
        })

    m = Metrics(results)
    return {
        "runs": runs,
        "metrics": {
            "scenarios": m.scenarios,
            "completed_without_human": m.completed_without_human,
            "interventions": m.interventions,
            "decision_points": m.decision_points,
            "replans": sum(r.replans for r in results),
            "servings_delivered": m.servings_delivered,
            "servings_offered": m.servings_offered,
        },
    }


if __name__ == "__main__":
    data = export()
    out = Path(__file__).parent / "console_data.json"
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} "
          f"({sum(len(r['events']) for r in data['runs'])} events, "
          f"{len(data['runs'])} runs)")
