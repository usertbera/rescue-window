"""
Simulated world.

The coordinator never mutates reality. Tools emit Events, the Simulator
applies them, and the next reasoning cycle observes the new WorldState. This
is what makes replanning genuine: the coordinator recomputes from current
state rather than advancing through a scripted branch.

Two separations matter here.

GROUND TRUTH vs AGENT KNOWLEDGE
    The seed knows a recipient will decline. The coordinator does not, until
    it asks. `WorldState` exposes only what has been learned. Ground truth
    lives in `GroundTruth` and is reachable only through tool calls.

WALL CLOCK vs SCENARIO CLOCK
    Nothing calls datetime.now(). Every time-sensitive operation receives
    scenario time explicitly. Playback speed is a display concern only and
    never affects the decisions taken.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

from safety_policy import (
    FoodOffer,
    FoodType,
    PreparedLabel,
    PrePackedLabel,
)
from registry import ResolvedRecipient, Scenario, load_drivers, load_recipients

# PROJECT_POLICY: average effective vehicle speed for travel-time arithmetic.
AVG_SPEED_KMH = 18.0
# PROJECT_POLICY: how long a handover takes at each end.
HANDLING_MINUTES = 10
# PROJECT_POLICY: how long to wait for a recipient reply before giving up.
OUTREACH_TIMEOUT_MIN = 15
# PROJECT_POLICY: how long to wait for a capability confirmation.
CONFIRM_TIMEOUT_MIN = 10


# --------------------------------------------------------------------------
# Clock
# --------------------------------------------------------------------------

class SimulatedClock:
    """Scenario time. Advances only when a tool says work took time."""

    def __init__(self, start: datetime, playback_multiplier: float = 60.0):
        self._now = start
        self.start = start
        self.playback_multiplier = playback_multiplier

    @property
    def now(self) -> datetime:
        return self._now

    def advance(self, minutes: float) -> None:
        if minutes < 0:
            raise ValueError("time does not run backwards")
        self._now += timedelta(minutes=minutes)

    def elapsed_min(self) -> float:
        return (self._now - self.start).total_seconds() / 60.0

    def real_seconds_for(self, minutes: float) -> float:
        """How long this interval should take on screen."""
        return minutes * 60.0 / self.playback_multiplier

    def stamp(self) -> str:
        return self._now.strftime("%H:%M")


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

class EventType(str, Enum):
    OFFER_RECEIVED = "OFFER_RECEIVED"
    SAFETY_CHECK = "SAFETY_CHECK"
    CANDIDATES_GENERATED = "CANDIDATES_GENERATED"
    CANDIDATE_ELIMINATED = "CANDIDATE_ELIMINATED"
    CONFIRMATION_REQUESTED = "CONFIRMATION_REQUESTED"
    CONFIRMATION_RESOLVED = "CONFIRMATION_RESOLVED"
    CONFIRMATION_UNRESOLVED = "CONFIRMATION_UNRESOLVED"
    OUTREACH_SENT = "OUTREACH_SENT"
    OUTREACH_ACCEPTED = "OUTREACH_ACCEPTED"
    OUTREACH_DECLINED = "OUTREACH_DECLINED"
    OUTREACH_TIMEOUT = "OUTREACH_TIMEOUT"
    TRANSPORT_REJECTED = "TRANSPORT_REJECTED"
    TRANSPORT_SELECTED = "TRANSPORT_SELECTED"
    TIMING_REJECTED = "TIMING_REJECTED"
    PLAN_COMMITTED = "PLAN_COMMITTED"
    REPLAN = "REPLAN"
    PICKUP_VERIFIED = "PICKUP_VERIFIED"
    DELIVERY_VERIFIED = "DELIVERY_VERIFIED"
    HUMAN_INTERVENTION = "HUMAN_INTERVENTION"
    DISPOSAL_DIRECTED = "DISPOSAL_DIRECTED"
    RESCUE_CLOSED = "RESCUE_CLOSED"


@dataclass
class Event:
    at: datetime
    type: EventType
    detail: str
    data: dict[str, Any] = field(default_factory=dict)
    policy_ref: Optional[str] = None
    is_decision_point: bool = False

    def render(self) -> str:
        head = f"{self.at:%H:%M}  {self.type.value:<24} {self.detail}"
        if self.policy_ref:
            head += f"\n                                 policy: {self.policy_ref}"
        return head


# --------------------------------------------------------------------------
# Allocation and plan
# --------------------------------------------------------------------------

@dataclass
class Leg:
    recipient_id: str
    driver_id: str
    servings: int
    pickup_at: datetime
    delivery_eta: datetime


@dataclass
class Plan:
    legs: list[Leg] = field(default_factory=list)

    @property
    def allocated(self) -> int:
        return sum(l.servings for l in self.legs)


# --------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------

@dataclass
class GroundTruth:
    """The seed's hidden answers. Reachable only via tool calls."""
    recipient_state: dict[str, dict]
    driver_state: dict[str, dict]
    capability_replies: dict[str, dict[str, Optional[bool]]] = field(default_factory=dict)

    def capacity(self, rid: str) -> int:
        return self.recipient_state[rid]["capacity_servings"]

    def response(self, rid: str) -> tuple[str, Optional[int]]:
        s = self.recipient_state[rid]
        return s["response"], s.get("response_delay_min")

    def driver(self, did: str) -> tuple[bool, Optional[int]]:
        s = self.driver_state[did]
        return s["available"], s.get("ready_in_min")

    def capability_reply(self, rid: str, field_name: str) -> Optional[bool]:
        """None means no reply arrived. Default: unknowns stay unknown."""
        return self.capability_replies.get(rid, {}).get(field_name, None)


# --------------------------------------------------------------------------
# World state -- what the coordinator can see
# --------------------------------------------------------------------------

@dataclass
class WorldState:
    offer: FoodOffer
    recipients: dict[str, ResolvedRecipient]
    drivers: dict[str, Any]
    driver_bases: dict[str, dict]
    donor_location: dict[str, float]

    remaining_servings: int = 0
    delivered_servings: int = 0

    # learned facts
    known_capacity: dict[str, int] = field(default_factory=dict)
    declined: set[str] = field(default_factory=set)
    timed_out: set[str] = field(default_factory=set)
    eliminated: dict[str, str] = field(default_factory=dict)
    unavailable_drivers: set[str] = field(default_factory=set)
    committed_driver_ids: set[str] = field(default_factory=set)

    plan: Plan = field(default_factory=Plan)
    rescue_closed: bool = False
    escalated: bool = False
    disposition: Optional[str] = None

    def available_recipient_ids(self) -> list[str]:
        """Recomputed every cycle. This is why replanning is real."""
        return [
            rid for rid in self.recipients
            if rid not in self.eliminated
            and rid not in self.declined
            and rid not in self.timed_out
            and rid not in {l.recipient_id for l in self.plan.legs}
        ]

    def available_driver_ids(self) -> list[str]:
        return [
            did for did in self.drivers
            if did not in self.unavailable_drivers
            and did not in self.committed_driver_ids
        ]


# --------------------------------------------------------------------------
# Simulator
# --------------------------------------------------------------------------

class Simulator:
    """Applies events to the world. The coordinator cannot bypass it."""

    def __init__(self, scenario: Scenario, clock: SimulatedClock):
        self.scenario = scenario
        self.clock = clock
        self.log: list[Event] = []

        recipients = {r.recipient_id: r for r in load_recipients()}
        drivers_raw = load_drivers()

        self.truth = GroundTruth(
            recipient_state=scenario.recipient_state,
            driver_state=scenario.driver_state,
            capability_replies=scenario.capability_replies,
        )

        offer = build_offer(scenario.offer_raw)
        self.state = WorldState(
            offer=offer,
            recipients=recipients,
            drivers={k: v[0] for k, v in drivers_raw.items()},
            driver_bases={k: v[1] for k, v in drivers_raw.items()},
            donor_location=scenario.offer_raw.get(
                "donor_location", {"lat": 22.5726, "lng": 88.3639}
            ),
            remaining_servings=offer.quantity_servings,
        )

    # -- event emission ----------------------------------------------------

    def emit(
        self,
        etype: EventType,
        detail: str,
        data: Optional[dict] = None,
        policy_ref: Optional[str] = None,
        decision_point: bool = False,
        minutes: float = 0.0,
    ) -> Event:
        if minutes:
            self.clock.advance(minutes)
        ev = Event(
            at=self.clock.now,
            type=etype,
            detail=detail,
            data=data or {},
            policy_ref=policy_ref,
            is_decision_point=decision_point,
        )
        self.log.append(ev)
        self.apply(ev)
        return ev

    def apply(self, ev: Event) -> None:
        """The only place world state changes."""
        s = self.state
        d = ev.data

        if ev.type is EventType.CANDIDATE_ELIMINATED:
            s.eliminated[d["recipient_id"]] = d["reason"]
        elif ev.type is EventType.OUTREACH_DECLINED:
            s.declined.add(d["recipient_id"])
        elif ev.type is EventType.OUTREACH_TIMEOUT:
            s.timed_out.add(d["recipient_id"])
        elif ev.type is EventType.OUTREACH_ACCEPTED:
            s.known_capacity[d["recipient_id"]] = d["capacity"]
        elif ev.type is EventType.CONFIRMATION_UNRESOLVED:
            s.eliminated[d["recipient_id"]] = d["reason"]
        elif ev.type is EventType.TRANSPORT_REJECTED:
            if d.get("permanent"):
                s.unavailable_drivers.add(d["driver_id"])
        elif ev.type is EventType.PLAN_COMMITTED:
            leg: Leg = d["leg"]
            s.plan.legs.append(leg)
            s.committed_driver_ids.add(leg.driver_id)
            s.remaining_servings -= leg.servings
        elif ev.type is EventType.DELIVERY_VERIFIED:
            s.delivered_servings += d["servings"]
        elif ev.type is EventType.HUMAN_INTERVENTION:
            s.escalated = True
        elif ev.type is EventType.DISPOSAL_DIRECTED:
            s.disposition = d.get("disposition")
        elif ev.type is EventType.RESCUE_CLOSED:
            s.rescue_closed = True

    def snapshot(self) -> WorldState:
        return self.state


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def build_offer(raw: dict) -> FoodOffer:
    ftype = FoodType(raw["food_type"])
    prepared = prepacked = None

    if ftype is FoodType.PREPARED_MEAL and raw.get("label"):
        l = raw["label"]
        prepared = PreparedLabel(
            food_name=l.get("food_name"),
            source=l.get("source"),
            preparation_date=_dt(l.get("preparation_date")),
            last_consumption_date=_dt(l.get("last_consumption_date")),
            vegetarian=l.get("vegetarian"),
            masked=l.get("masked", False),
        )
    elif raw.get("label"):
        l = raw["label"]
        prepacked = PrePackedLabel(
            item_name=l.get("item_name"),
            manufacturer_info=l.get("manufacturer_info"),
            ingredients=l.get("ingredients"),
            expiry_date=_dt(l.get("expiry_date")),
            masked=l.get("masked", False),
        )

    return FoodOffer(
        offer_id=raw["offer_id"],
        donor_id=raw["donor_id"],
        donor_has_valid_licence=raw["donor_has_valid_licence"],
        food_type=ftype,
        quantity_servings=raw["quantity_servings"],
        perishable=raw["perishable"],
        consumption_deadline=_dt(raw["consumption_deadline"]),
        served_to_customers=raw.get("served_to_customers", False),
        known_unsafe=raw.get("known_unsafe", False),
        segregated_by_perishability=raw.get("segregated_by_perishability", True),
        packed_against_contamination=raw.get("packed_against_contamination", True),
        stored_with_waste=raw.get("stored_with_waste", False),
        container_date_marked=raw.get("container_date_marked", True),
        prepared_label=prepared,
        prepacked_label=prepacked,
    )


def _dt(v: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(v) if v else None


def haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def travel_minutes(km: float) -> float:
    return (km / AVG_SPEED_KMH) * 60.0
