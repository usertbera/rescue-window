"""
Tool layer.

These are the functions a Strands agent is given. Each one takes scenario
time explicitly -- no tool reads a wall clock. Each returns a structured
result the coordinator can reason over, and each emits an Event so the trace
is a byproduct of the work rather than a separate reporting feature.

Design note: tools report facts and legality. They do not choose. Choosing
happens in coordinator.py, which is the part an LLM can be swapped into.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from safety_policy import (
    Disposition,
    FoodOffer,
    SafetyResult,
    evaluate_offer,
    evaluate_recipient,
    evaluate_timing,
    evaluate_transport,
)
from registry import ResolvedRecipient, is_committable
from simulation import (
    CONFIRM_TIMEOUT_MIN,
    EventType,
    HANDLING_MINUTES,
    Leg,
    OUTREACH_TIMEOUT_MIN,
    Simulator,
    haversine_km,
    travel_minutes,
)


@dataclass
class Candidate:
    recipient_id: str
    distance_km: float
    travel_min: float
    closes_at: str
    nominal_capacity: int
    eligible: bool
    reason: Optional[str] = None
    policy_ref: Optional[str] = None
    pending_confirmation: list[str] = None


# --------------------------------------------------------------------------
# 1. safety_check
# --------------------------------------------------------------------------

def safety_check(sim: Simulator, now: datetime) -> SafetyResult:
    result = evaluate_offer(sim.state.offer, now)
    sim.emit(
        EventType.SAFETY_CHECK,
        result.render().replace("\n", " | "),
        {"eligible": result.eligible, "disposition": result.disposition.value},
        policy_ref="; ".join(result.source_refs) or "FSSAI Surplus Food Regs 2019",
        minutes=1,
    )
    if not result.eligible and result.disposition is Disposition.FOOD_FOR_DISPOSAL:
        sim.emit(
            EventType.DISPOSAL_DIRECTED,
            'Directed to donor: mark container "Food for Disposal".',
            {"disposition": "food_for_disposal"},
            policy_ref="FSSAI Surplus Food Regs 2019, Sch. I, 2(4)",
        )
    return result


# --------------------------------------------------------------------------
# 2. find_recipients -- recomputed from current state every call
# --------------------------------------------------------------------------

def find_recipients(sim: Simulator, now: datetime) -> list[Candidate]:
    s = sim.state
    offer = s.offer
    out: list[Candidate] = []

    for rid in s.available_recipient_ids():
        r: ResolvedRecipient = s.recipients[rid]
        km = haversine_km(
            s.donor_location["lat"], s.donor_location["lng"], r.lat, r.lng
        )
        tmin = travel_minutes(km)

        if offer.food_type.value not in r.accepted_food_types:
            out.append(Candidate(rid, km, tmin, r.close_time, r.nominal_capacity_servings,
                                 False, "Does not accept this food type.", None, []))
            continue

        gate = evaluate_recipient(r.recipient, offer)
        if not gate.eligible:
            # Distinguish 'cannot' from 'do not know'.
            unknown_blocked = [
                f for f in r.pending_confirmation
                if any(f in v.rule or v.rule in ("recipient_licence", "recipient_facilities")
                       for v in gate.violations)
            ]
            if unknown_blocked:
                out.append(Candidate(rid, km, tmin, r.close_time,
                                     r.nominal_capacity_servings, False,
                                     "Blocked pending confirmation.",
                                     "; ".join(gate.source_refs),
                                     list(r.pending_confirmation)))
            else:
                out.append(Candidate(rid, km, tmin, r.close_time,
                                     r.nominal_capacity_servings, False,
                                     gate.violations[0].detail,
                                     "; ".join(gate.source_refs), []))
            continue

        ok, reasons = is_committable(r)
        out.append(Candidate(rid, km, tmin, r.close_time, r.nominal_capacity_servings,
                             ok, None if ok else reasons[0], None,
                             list(r.pending_confirmation)))

    sim.emit(
        EventType.CANDIDATES_GENERATED,
        f"{len(out)} candidates evaluated; {sum(1 for c in out if c.eligible)} viable.",
        {"count": len(out)},
        minutes=1,
    )
    return out


# --------------------------------------------------------------------------
# 3. confirm_capability
# --------------------------------------------------------------------------

def confirm_capability(
    sim: Simulator, recipient_id: str, field_name: str, now: datetime
) -> Optional[bool]:
    """
    Returns True, False, or None (no reply). None is not a failure of the
    agent -- it eliminates the candidate and the loop replans.
    """
    r = sim.state.recipients[recipient_id]
    sim.emit(
        EventType.CONFIRMATION_REQUESTED,
        f"{recipient_id}: asking about '{field_name}'.",
        {"recipient_id": recipient_id, "field": field_name},
        minutes=1,
    )

    reply = sim.truth.capability_reply(recipient_id, field_name)

    if reply is None:
        sim.emit(
            EventType.CONFIRMATION_UNRESOLVED,
            f"{recipient_id}: no reply on '{field_name}' within "
            f"{CONFIRM_TIMEOUT_MIN} min. Eliminated; not an escalation while "
            f"other candidates remain.",
            {"recipient_id": recipient_id, "field": field_name,
             "reason": f"unresolved capability '{field_name}'"},
            minutes=CONFIRM_TIMEOUT_MIN,
        )
        return None

    r.confirm(field_name, reply)
    sim.emit(
        EventType.CONFIRMATION_RESOLVED,
        f"{recipient_id}: '{field_name}' = {reply}.",
        {"recipient_id": recipient_id, "field": field_name, "value": reply},
        minutes=2,
    )
    if reply is False:
        sim.emit(
            EventType.CANDIDATE_ELIMINATED,
            f"{recipient_id}: lacks {field_name}.",
            {"recipient_id": recipient_id, "reason": f"lacks {field_name}"},
            policy_ref="FSSAI Surplus Food Regs 2019, reg. 5(2)",
        )
    return reply


# --------------------------------------------------------------------------
# 4. request_acceptance
# --------------------------------------------------------------------------

def request_acceptance(
    sim: Simulator, recipient_id: str, servings: int, now: datetime
) -> tuple[str, int]:
    sim.emit(
        EventType.OUTREACH_SENT,
        f"{recipient_id}: requesting {servings} servings.",
        {"recipient_id": recipient_id, "servings": servings},
        minutes=1,
    )

    response, delay = sim.truth.response(recipient_id)

    if response == "no_response":
        sim.emit(
            EventType.OUTREACH_TIMEOUT,
            f"{recipient_id}: no reply within {OUTREACH_TIMEOUT_MIN} min.",
            {"recipient_id": recipient_id},
            minutes=OUTREACH_TIMEOUT_MIN,
        )
        return "timeout", 0

    if response == "decline":
        sim.emit(
            EventType.OUTREACH_DECLINED,
            f"{recipient_id}: declined.",
            {"recipient_id": recipient_id},
            minutes=delay or 1,
        )
        return "decline", 0

    capacity = sim.truth.capacity(recipient_id)
    grant = min(capacity, servings)
    sim.emit(
        EventType.OUTREACH_ACCEPTED,
        f"{recipient_id}: accepts {grant} of {servings} "
        f"(capacity {capacity}).",
        {"recipient_id": recipient_id, "capacity": capacity, "granted": grant},
        minutes=delay or 1,
    )
    return "accept", grant


# --------------------------------------------------------------------------
# 5. find_transport
# --------------------------------------------------------------------------

def find_transport(
    sim: Simulator, recipient_id: str, now: datetime
) -> Optional[tuple[str, datetime, datetime]]:
    """Returns (driver_id, pickup_at, delivery_eta) or None."""
    s = sim.state
    r = s.recipients[recipient_id]

    best = None
    for did in s.available_driver_ids():
        vehicle = s.drivers[did]

        gate = evaluate_transport(vehicle, s.offer)
        if not gate.eligible:
            sim.emit(
                EventType.TRANSPORT_REJECTED,
                f"{did}: {gate.violations[0].detail}",
                {"driver_id": did, "permanent": True},
                policy_ref="; ".join(gate.source_refs),
            )
            continue

        available, ready_in = sim.truth.driver(did)
        if not available:
            sim.emit(
                EventType.TRANSPORT_REJECTED,
                f"{did}: unavailable.",
                {"driver_id": did, "permanent": True},
            )
            continue

        base = s.driver_bases[did]
        to_donor = travel_minutes(haversine_km(
            base["lat"], base["lng"],
            s.donor_location["lat"], s.donor_location["lng"]))
        to_recipient = travel_minutes(haversine_km(
            s.donor_location["lat"], s.donor_location["lng"], r.lat, r.lng))

        pickup = now + timedelta(minutes=(ready_in or 0) + to_donor + HANDLING_MINUTES)
        eta = pickup + timedelta(minutes=to_recipient + HANDLING_MINUTES)

        if best is None or eta < best[2]:
            best = (did, pickup, eta)

    if best is None:
        return None

    sim.emit(
        EventType.TRANSPORT_SELECTED,
        f"{best[0]}: pickup {best[1]:%H:%M}, delivery ETA {best[2]:%H:%M}.",
        {"driver_id": best[0]},
        minutes=1,
    )
    return best


# --------------------------------------------------------------------------
# 6. check_timing
# --------------------------------------------------------------------------

def check_timing(sim: Simulator, eta: datetime) -> SafetyResult:
    result = evaluate_timing(sim.state.offer, eta)
    if not result.eligible:
        sim.emit(
            EventType.TIMING_REJECTED,
            result.violations[0].detail,
            {},
            policy_ref="; ".join(result.source_refs),
        )
    return result


# --------------------------------------------------------------------------
# 7. commit_leg
# --------------------------------------------------------------------------

def commit_leg(sim: Simulator, leg: Leg) -> None:
    sim.emit(
        EventType.PLAN_COMMITTED,
        f"{leg.servings} servings -> {leg.recipient_id} via {leg.driver_id}, "
        f"ETA {leg.delivery_eta:%H:%M}.",
        {"leg": leg},
        minutes=1,
    )


# --------------------------------------------------------------------------
# 8. verify_delivery
# --------------------------------------------------------------------------

def verify_delivery(sim: Simulator, leg: Leg) -> None:
    sim.emit(
        EventType.PICKUP_VERIFIED,
        f"{leg.driver_id}: collected {leg.servings} servings.",
        {"driver_id": leg.driver_id},
    )
    sim.clock.advance(
        max(0.0, (leg.delivery_eta - sim.clock.now).total_seconds() / 60.0)
    )
    sim.emit(
        EventType.DELIVERY_VERIFIED,
        f"{leg.recipient_id}: {leg.servings} servings delivered.",
        {"recipient_id": leg.recipient_id, "servings": leg.servings},
    )


# --------------------------------------------------------------------------
# 9. escalate
# --------------------------------------------------------------------------

def escalate(sim: Simulator, reason: str, options: list[str], policy_ref: str = "") -> None:
    s = sim.state
    remaining = (s.offer.consumption_deadline - sim.clock.now).total_seconds() / 60.0
    sim.emit(
        EventType.HUMAN_INTERVENTION,
        f"{reason} | {int(remaining)} min to deadline | "
        f"{s.remaining_servings} servings unplaced | options: " + "; ".join(options),
        {"reason": reason, "options": options,
         "minutes_remaining": int(remaining),
         "unplaced": s.remaining_servings},
        policy_ref=policy_ref,
        decision_point=True,
    )
