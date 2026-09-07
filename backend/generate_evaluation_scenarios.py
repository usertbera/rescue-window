"""
Generates a 30-scenario evaluation set for the deterministic coordinator.

The three hand-authored seeds (scenario_seeds.json) are a narrative demo:
each one is written to exercise a specific failure mode and is checked by
eye. This generator produces a much larger, varied set so the human-
intervention metric stops being "2 of 3" (not a rate at n=3) and becomes a
number with actual statistical weight -- see Metrics.render()'s own warning
about reporting percentages at small sample sizes.

What varies, per the project's own scope note (README, "Human-intervention
metric"): quantity, deadline pressure, perishability, recipient response
patterns, and driver availability. What does NOT vary: the offer always
clears the safety gate (valid donor licence, complete labels, not served to
customers, not known-unsafe). That gate is a separate, already-covered
concern (safety_policy.py); this generator is exercising the coordination
loop -- replanning, capability confirmation, and escalation -- not
re-testing regulation compliance.

Determinism: every scenario is built from Random(BASE_SEED + i), so the same
30 scenarios come out on any machine, any time this is run. That is what
lets run_evaluation.py's numbers be cited without an asterisk.

Generated scenarios are kept in a separate file from the hand-authored demo
seeds (evaluation_seeds.json, not scenario_seeds.json) -- the demo seeds stay
exactly as authored and reviewed; this file can be regenerated at will.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from registry import DATA_DIR

BASE_SEED = 20260914  # date-shaped, arbitrary, fixed -- never change this value
N_SCENARIOS = 30

RECIPIENT_IDS = [f"recipient_{i:03d}" for i in range(1, 8)]
DRIVER_IDS = [f"driver_{i:03d}" for i in range(1, 5)]

# Fields on these two recipients are "unknown" in recipient_profiles.json,
# so they are the only ones a generated scenario can resolve via
# capability_replies. Keep in sync with backend/data/recipient_profiles.json.
UNKNOWN_CAPABILITIES = {
    "recipient_002": "has_valid_licence_or_registration",
    "recipient_005": "has_reheating_facilities",
}

DONOR_TYPES = [
    ("donor_wedding_hall", "Wedding Hall", "Mixed vegetable biryani and raita"),
    ("donor_corporate_canteen", "Corporate Canteen", "Dal, rice, and mixed sabzi"),
    ("donor_grocery_store", "Grocery Store", "Packaged bread and dairy overstock"),
    ("donor_event_caterer", "Event Catering Co.", "Paneer curry and chapati"),
    ("donor_restaurant", "Restaurant", "Vegetable fried rice and manchurian"),
    ("donor_hostel_mess", "Hostel Mess", "Khichdi and mixed vegetable curry"),
    ("donor_bakery", "Bakery", "Packaged bakery items nearing best-before"),
    ("donor_conference_center", "Conference Center", "Boxed vegetarian lunch sets"),
]

# Matches the actual footprint of recipient_profiles.json's recipients and
# driver bases (roughly lat 22.50-22.62, lng 88.33-88.40) -- not the whole
# city. A donor placed outside where the recipients/drivers actually are
# would make every leg's travel time an artifact of geography rather than
# the thing this generator is meant to vary (deadline pressure, responses,
# availability).
LAT_RANGE = (22.52, 22.60)
LNG_RANGE = (88.34, 88.39)


def _build_offer(rng: random.Random, idx: int, scenario_start: datetime) -> dict:
    donor_key, donor_label, food_name = rng.choice(DONOR_TYPES)
    food_type = rng.choice(["prepared_meal", "pre_packed"])
    perishable = rng.random() < 0.7
    deadline_window_min = rng.randint(120, 300)
    deadline = scenario_start + timedelta(minutes=deadline_window_min)

    offer = {
        "offer_id": f"gen_offer_{idx:03d}",
        "donor_id": f"{donor_key}_{idx:03d}",
        "donor_has_valid_licence": True,
        "food_type": food_type,
        "quantity_servings": rng.randint(15, 90),
        "perishable": perishable,
        "served_to_customers": False,
        "consumption_deadline": deadline.isoformat(),
        "segregated_by_perishability": True,
        "packed_against_contamination": True,
        "stored_with_waste": False,
        "container_date_marked": rng.random() < 0.85,
        "donor_location": {
            "lat": round(rng.uniform(*LAT_RANGE), 4),
            "lng": round(rng.uniform(*LNG_RANGE), 4),
        },
    }

    prep_date = scenario_start - timedelta(minutes=rng.randint(30, 150))
    if food_type == "prepared_meal":
        offer["label"] = {
            "food_name": food_name,
            "source": donor_label,
            "preparation_date": prep_date.isoformat(),
            "last_consumption_date": deadline.isoformat(),
            "vegetarian": True,
            "masked": False,
        }
    else:
        offer["label"] = {
            "item_name": food_name,
            "manufacturer_info": donor_label,
            "ingredients": ["mixed ingredients — see original packaging"],
            "expiry_date": deadline.isoformat(),
            "masked": False,
        }
    return offer


def _build_recipient_state(rng: random.Random) -> dict:
    out = {}
    for rid in RECIPIENT_IDS:
        response = rng.choices(
            ["accept", "decline", "no_response"], weights=[0.65, 0.2, 0.15]
        )[0]
        out[rid] = {
            "capacity_servings": rng.randint(15, 90),
            "response": response,
            "response_delay_min": rng.randint(1, 14) if response != "no_response" else None,
        }
    return out


def _build_driver_state(rng: random.Random) -> dict:
    out = {}
    for did in DRIVER_IDS:
        available = rng.random() < 0.7
        out[did] = {
            "available": available,
            "ready_in_min": rng.randint(0, 15) if available else None,
        }
    return out


def _build_capability_replies(rng: random.Random) -> dict:
    """For each recipient with an unknown capability: resolve true (30%),
    resolve false (30%), or leave unresolved -- no entry at all (40%),
    meaning confirm_capability returns None (no reply)."""
    out: dict = {}
    for rid, field_name in UNKNOWN_CAPABILITIES.items():
        roll = rng.random()
        if roll < 0.3:
            out[rid] = {field_name: True}
        elif roll < 0.6:
            out[rid] = {field_name: False}
        # else: leave unresolved
    return out


def build_scenario(idx: int) -> dict:
    rng = random.Random(BASE_SEED + idx)
    scenario_start = datetime.fromisoformat("2026-09-14T12:00:00+05:30") + timedelta(
        minutes=rng.randint(0, 6 * 60)
    )
    return {
        "label": f"Generated evaluation scenario {idx:03d}",
        "scenario_start": scenario_start.isoformat(),
        "offer": _build_offer(rng, idx, scenario_start),
        "recipient_state": _build_recipient_state(rng),
        "driver_state": _build_driver_state(rng),
        "capability_replies": _build_capability_replies(rng),
        "expected_outcome": {
            "note": "generated scenario; no hand-verified expectation, "
            "evaluated in aggregate only (see run_evaluation.py)",
        },
    }


def generate() -> dict:
    seeds = {
        f"generated_{i:03d}": build_scenario(i) for i in range(1, N_SCENARIOS + 1)
    }
    return {
        "_README": {
            "purpose": (
                f"{N_SCENARIOS} scripted scenarios for the human-intervention "
                "evaluation, distinct from the 3 hand-authored narrative seeds "
                "in scenario_seeds.json."
            ),
            "hard_rule": (
                "Anonymous recipient_/driver_ IDs only, same as scenario_seeds.json."
            ),
            "generation": (
                f"Produced by generate_evaluation_scenarios.py from a fixed base "
                f"seed ({BASE_SEED}). Deterministic: re-running the generator "
                "reproduces this file byte-for-byte. Varies quantity, deadline "
                "pressure, perishability, recipient response patterns, driver "
                "availability, and unresolved-capability outcomes. Every offer "
                "clears the safety gate by construction -- this set evaluates "
                "the coordination loop, not the safety module."
            ),
            "usage": "python run_evaluation.py",
        },
        "seeds": seeds,
    }


if __name__ == "__main__":
    data = generate()
    out_path = DATA_DIR / "evaluation_seeds.json"
    out_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path} ({len(data['seeds'])} scenarios)")
