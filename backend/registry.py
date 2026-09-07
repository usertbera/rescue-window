"""
Registry and scenario loader.

Three layers, deliberately kept apart:

  organizations.json      real names, sourced facts, NO capability claims.
                          Never loaded here. Citation material only.

  recipient_profiles.json anonymous IDs, simulated capabilities, tri-state.

  scenario_seeds.json     anonymous IDs, simulated dynamic world state.

The agent sees layers 2 and 3. It never sees a real organisation name
attached to a capability or a behaviour.

Tri-state rule
--------------
A capability may be true, false, or UNKNOWN. UNKNOWN is not a silent true.
`resolve_recipient` returns any unknown fields as `pending_confirmation`, and
`is_committable` refuses to let a plan close while any remain unresolved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from safety_policy import Recipient, TransportOption

UNKNOWN = "unknown"
Tri = Union[bool, str]  # True | False | "unknown"

DATA_DIR = Path(__file__).parent / "data"

CAPABILITY_FIELDS = (
    "licensed_or_registered",
    "transport_facilities",
    "storage_facilities",
    "reheating_facilities",
)


# --------------------------------------------------------------------------

@dataclass
class ResolvedRecipient:
    recipient_id: str
    archetype: str
    lat: float
    lng: float
    area_label: str
    open_time: str
    close_time: str
    accepted_food_types: list[str]
    nominal_capacity_servings: int
    recipient: Recipient
    pending_confirmation: list[str] = field(default_factory=list)

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.pending_confirmation)

    def confirm(self, field_name: str, value: bool) -> None:
        """Record a resolved unknown. In the demo this is the simulated reply."""
        if field_name not in self.pending_confirmation:
            raise ValueError(f"{field_name} is not pending confirmation")
        setattr(self.recipient, field_name, value)
        self.pending_confirmation.remove(field_name)


def _tri(value: Any, field_name: str, pending: list[str]) -> bool:
    """
    Collapse a tri-state to a bool for the safety gate, recording unknowns.

    UNKNOWN collapses to False, so an unresolved capability can never let a
    plan through. It is simultaneously registered as pending, so the agent is
    told the difference between 'this recipient cannot' and 'we do not know
    whether this recipient can'.
    """
    if value is True or value is False:
        return value
    if isinstance(value, str) and value.lower() == UNKNOWN:
        pending.append(field_name)
        return False
    raise ValueError(f"Bad tri-state for {field_name}: {value!r}")


def load_recipients(path: Optional[Path] = None) -> list[ResolvedRecipient]:
    path = path or (DATA_DIR / "recipient_profiles.json")
    raw = json.loads(path.read_text(encoding="utf-8"))

    out: list[ResolvedRecipient] = []
    for p in raw["profiles"]:
        caps = p["capabilities"]
        pending: list[str] = []

        recipient = Recipient(
            recipient_id=p["recipient_id"],
            has_valid_licence_or_registration=_tri(
                caps["licensed_or_registered"], "has_valid_licence_or_registration", pending
            ),
            has_transport_facilities=_tri(
                caps["transport_facilities"], "has_transport_facilities", pending
            ),
            has_storage_facilities=_tri(
                caps["storage_facilities"], "has_storage_facilities", pending
            ),
            has_reheating_facilities=_tri(
                caps["reheating_facilities"], "has_reheating_facilities", pending
            ),
            refrigerator_temp_c=caps.get("refrigerator_temp_c"),
            refrigerator_cleaned_within_7_days=caps.get(
                "refrigerator_cleaned_within_7_days", False
            ),
        )

        loc = p["location"]
        out.append(ResolvedRecipient(
            recipient_id=p["recipient_id"],
            archetype=p["archetype"],
            lat=loc["lat"],
            lng=loc["lng"],
            area_label=loc["area_label"],
            open_time=p["published_hours"]["open"],
            close_time=p["published_hours"]["close"],
            accepted_food_types=p["accepted_food_types"],
            nominal_capacity_servings=p["nominal_capacity_servings"],
            recipient=recipient,
            pending_confirmation=pending,
        ))
    return out


def load_drivers(path: Optional[Path] = None) -> dict[str, tuple[TransportOption, dict]]:
    path = path or (DATA_DIR / "recipient_profiles.json")
    raw = json.loads(path.read_text(encoding="utf-8"))

    out: dict[str, tuple[TransportOption, dict]] = {}
    for d in raw["drivers"]:
        v = d["vehicle"]
        out[d["driver_id"]] = (
            TransportOption(
                vehicle_id=d["driver_id"],
                clean_and_sanitised=v["clean_and_sanitised"],
                can_hold_optimum_temperature=v["can_hold_optimum_temperature"],
                has_insulated_container_or_ice_packs=v["has_insulated_container_or_ice_packs"],
                food_grade_contact_surfaces=v["food_grade_contact_surfaces"],
            ),
            d["base"],
        )
    return out


# --------------------------------------------------------------------------

@dataclass
class Scenario:
    seed_id: str
    label: str
    scenario_start: datetime
    offer_raw: dict
    recipient_state: dict[str, dict]
    driver_state: dict[str, dict]
    capability_replies: dict[str, dict]
    expected_outcome: dict

    def capacity_of(self, recipient_id: str) -> int:
        return self.recipient_state[recipient_id]["capacity_servings"]

    def response_of(self, recipient_id: str) -> tuple[str, Optional[int]]:
        s = self.recipient_state[recipient_id]
        return s["response"], s.get("response_delay_min")

    def driver_available(self, driver_id: str) -> tuple[bool, Optional[int]]:
        s = self.driver_state[driver_id]
        return s["available"], s.get("ready_in_min")


def load_scenario(seed_id: str, path: Optional[Path] = None) -> Scenario:
    path = path or (DATA_DIR / "scenario_seeds.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    s = raw["seeds"][seed_id]

    _assert_anonymous(s, seed_id)

    return Scenario(
        seed_id=seed_id,
        label=s["label"],
        scenario_start=datetime.fromisoformat(s["scenario_start"]),
        offer_raw=s["offer"],
        recipient_state=s["recipient_state"],
        driver_state=s["driver_state"],
        capability_replies=s.get("capability_replies", {}),
        expected_outcome=s["expected_outcome"],
    )


def _assert_anonymous(seed: dict, seed_id: str) -> None:
    """
    Guard rail. Every key in the simulated state must be an anonymous ID.
    Keeps a real organisation name from ever ending up attached to a
    simulated refusal in a public repository.
    """
    for key in seed["recipient_state"]:
        if not key.startswith("recipient_"):
            raise ValueError(
                f"Seed {seed_id}: '{key}' is not an anonymous recipient ID. "
                "Simulated state must never key on a real organisation name."
            )
    for key in seed["driver_state"]:
        if not key.startswith("driver_"):
            raise ValueError(
                f"Seed {seed_id}: '{key}' is not an anonymous driver ID."
            )


def list_seeds(path: Optional[Path] = None) -> list[str]:
    path = path or (DATA_DIR / "scenario_seeds.json")
    return list(json.loads(path.read_text(encoding="utf-8"))["seeds"].keys())


# --------------------------------------------------------------------------

def is_committable(resolved: ResolvedRecipient) -> tuple[bool, list[str]]:
    """
    A plan may not close while a capability is unknown. Returns the reason
    list so the trace can distinguish 'cannot' from 'do not know'.
    """
    if resolved.needs_confirmation:
        return False, [
            f"Capability '{f}' is unknown for {resolved.recipient_id}; confirm before committing."
            for f in resolved.pending_confirmation
        ]
    return True, []


if __name__ == "__main__":
    recipients = load_recipients()
    print(f"Loaded {len(recipients)} recipient profiles\n")
    for r in recipients:
        flag = f"  PENDING: {', '.join(r.pending_confirmation)}" if r.needs_confirmation else ""
        print(f"  {r.recipient_id}  {r.archetype:<32} cap={r.nominal_capacity_servings:>3}{flag}")

    print(f"\nSeeds: {', '.join(list_seeds())}")
    for sid in list_seeds():
        sc = load_scenario(sid)
        print(f"  {sid}: {sc.label} -> {sc.expected_outcome}")
