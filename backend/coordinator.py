"""
Coordinator.

The loop returns to candidate generation on every replan. It does not walk a
prepared candidate list. That is the difference between a stateful
coordinator and an if/else in disguise, and it is the property the three
acceptance scenarios are designed to test.

Ranking is behind `RankingPolicy` so a Strands agent can be dropped in
without touching the loop or the gates. The deterministic policy is the
default because the 30-scenario evaluation needs reproducibility; the LLM
policy is what the demo runs.

Intervention accounting, per the locked definition:

    AUTONOMOUS         gather, evaluate, replan, execute, verify
    INTERVENTION       a decision boundary is reached
                       AND evidence does not determine a safe action
                       AND no viable autonomous plan remains

An unresolved unknown eliminates the candidate and triggers a replan. It
becomes an intervention only when that replan finds nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional, Protocol

import tools
from registry import Scenario
from simulation import EventType, Leg, SimulatedClock, Simulator, WorldState

MAX_CYCLES = 40  # guard against a pathological replan loop


# --------------------------------------------------------------------------
# Ranking policy -- the swappable seam
# --------------------------------------------------------------------------

class RankingPolicy(Protocol):
    def rank(self, candidates: list[tools.Candidate], state: WorldState) -> list[tools.Candidate]:
        ...


class DeterministicPolicy:
    """
    Capacity first, then proximity. Deliberately simple: the interesting
    behaviour lives in the gates and the replan loop, not in the ranking.
    """

    def rank(self, candidates, state):
        viable = [c for c in candidates if c.eligible]
        need = state.remaining_servings
        return sorted(
            viable,
            key=lambda c: (
                0 if c.nominal_capacity >= need else 1,
                -c.nominal_capacity if c.nominal_capacity < need else 0,
                c.travel_min,
            ),
        )


class StrandsPolicy:
    """
    Ranking-only seam for dropping an LLM into this same deterministic loop.

    Contract: receives the candidate list and current state, returns the same
    candidates reordered. It may not add candidates, may not mark an
    ineligible candidate eligible, and may not see ground truth. Anything it
    returns still passes through the deterministic gates before commitment.

    This class stays a documented no-op fallback on purpose. The actual
    hackathon-required Strands integration -- a real Agent driving the whole
    tool-calling loop over Amazon Bedrock, not just reordering a list -- lives
    in agent_coordinator.AgentCoordinator. That is the "thorough and skillful
    use of Strands Agents" deliverable; run it with run_agent_demo.py. This
    class remains as the lighter-weight seam for a future ranking-only model.
    """

    def __init__(self, agent=None):
        self.agent = agent
        self._fallback = DeterministicPolicy()

    def rank(self, candidates, state):
        if self.agent is None:
            return self._fallback.rank(candidates, state)
        # ordered_ids = self.agent(build_prompt(candidates, state))
        # return [c for cid in ordered_ids for c in candidates
        #         if c.recipient_id == cid and c.eligible]
        return self._fallback.rank(candidates, state)


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------

@dataclass
class RescueResult:
    seed_id: str
    servings_offered: int
    servings_delivered: int
    human_interventions: int
    decision_points: int
    replans: int
    confirmations_attempted: int
    confirmations_unresolved: int
    closed_reason: str
    events: list = field(default_factory=list)

    @property
    def completed_without_human_decision(self) -> bool:
        return self.human_interventions == 0

    def summary(self) -> str:
        return (
            f"{self.seed_id}: {self.servings_delivered}/{self.servings_offered} "
            f"delivered | interventions {self.human_interventions} | "
            f"decision points {self.decision_points} | replans {self.replans} | "
            f"{self.closed_reason}"
        )


# --------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------

class Coordinator:

    def __init__(self, scenario: Scenario, policy: Optional[RankingPolicy] = None,
                 playback_multiplier: float = 60.0):
        self.scenario = scenario
        self.clock = SimulatedClock(scenario.scenario_start, playback_multiplier)
        self.sim = Simulator(scenario, self.clock)
        self.policy = policy or DeterministicPolicy()

        self.decision_points = 0
        self.replans = 0
        self.confirmations_attempted = 0
        self._confirmations_asked: set[tuple[str, str]] = set()
        self.confirmations_unresolved = 0

    # -- main loop ---------------------------------------------------------

    def run(self) -> RescueResult:
        sim, state = self.sim, self.sim.state

        sim.emit(
            EventType.OFFER_RECEIVED,
            f"{state.offer.quantity_servings} servings, "
            f"{state.offer.food_type.value}, deadline "
            f"{state.offer.consumption_deadline:%H:%M}.",
            {"offer_id": state.offer.offer_id},
        )

        gate = tools.safety_check(sim, self.clock.now)
        if not gate.eligible:
            return self._close("offer failed the safety gate")

        for condition in gate.required_conditions:
            sim.emit(EventType.SAFETY_CHECK, f"Condition applied: {condition}.",
                     policy_ref="FSSAI Surplus Food Regs 2019, Sch. I, 2(1)")

        cycles = 0
        while state.remaining_servings > 0 and not state.rescue_closed:
            cycles += 1
            if cycles > MAX_CYCLES:
                return self._close("cycle limit reached")

            if self.clock.now >= state.offer.consumption_deadline:
                tools.escalate(
                    sim, "Consumption deadline reached with servings unplaced.",
                    ["Direct remaining quantity to Sch. I 2(4) disposal handling"],
                    "FSSAI Surplus Food Regs 2019, reg. 5(3)")
                self.decision_points += 1
                return self._close("deadline reached")

            placed = self._attempt_one_leg()

            if not placed:
                # Replan means regenerating candidates from current state.
                # Only when regeneration yields nothing viable do we escalate.
                if self._any_viable_remaining():
                    self.replans += 1
                    sim.emit(
                        EventType.REPLAN,
                        f"No plan from this cycle; regenerating candidates. "
                        f"{state.remaining_servings} servings unplaced.",
                        {"cycle": cycles},
                    )
                    continue

                reason, options, ref = self._escalation_reason()
                tools.escalate(sim, reason, options, ref)
                self.decision_points += 1
                return self._close("no viable autonomous plan")

        return self._close("all servings placed")

    # -- one placement attempt --------------------------------------------

    def _attempt_one_leg(self) -> bool:
        sim, state = self.sim, self.sim.state

        candidates = tools.find_recipients(sim, self.clock.now)

        # Step 1. Resolve one unknown, if any is outstanding. Gathering is not
        # an intervention. Returning False here sends the loop back to
        # candidate generation, which is the point.
        for c in candidates:
            if c.pending_confirmation and not c.eligible:
                fname = c.pending_confirmation[0]
                # Defensive: the three confirmation outcomes already clear
                # the field or eliminate the candidate, so a repeat should
                # be unreachable. Guarded explicitly so the generated
                # evaluation set cannot inflate the denominator.
                if (c.recipient_id, fname) in self._confirmations_asked:
                    sim.state.eliminated[c.recipient_id] = (
                        f"capability '{fname}' already queried")
                    return False
                self._confirmations_asked.add((c.recipient_id, fname))
                self.confirmations_attempted += 1
                self.decision_points += 1
                reply = tools.confirm_capability(
                    sim, c.recipient_id, fname, self.clock.now)
                if reply is None:
                    self.confirmations_unresolved += 1
                    sim.state.eliminated[c.recipient_id] = (
                        f"unresolved capability '{fname}'")
                return False  # regenerate candidates with the new knowledge

        # Step 2. Attempt the single best candidate only. Any failure returns
        # to the caller, which regenerates from updated state rather than
        # walking down a cached list.
        ranked = self.policy.rank(candidates, state)
        if not ranked:
            return False

        c = ranked[0]
        self.decision_points += 1

        status, granted = tools.request_acceptance(
            sim, c.recipient_id, state.remaining_servings, self.clock.now)
        if status != "accept" or granted <= 0:
            return False

        transport = tools.find_transport(sim, c.recipient_id, self.clock.now)
        if transport is None:
            sim.emit(
                EventType.CANDIDATE_ELIMINATED,
                f"{c.recipient_id}: no lawful transport available.",
                {"recipient_id": c.recipient_id,
                 "reason": "no lawful transport"},
                policy_ref="FSSAI Surplus Food Regs 2019, Sch. I, 2(2)")
            return False

        driver_id, pickup, eta = transport
        timing = tools.check_timing(sim, eta)
        if not timing.eligible:
            sim.emit(
                EventType.CANDIDATE_ELIMINATED,
                f"{c.recipient_id}: cannot be reached in time via {driver_id}.",
                {"recipient_id": c.recipient_id, "reason": "timing"},
                policy_ref="; ".join(timing.source_refs))
            return False

        leg = Leg(c.recipient_id, driver_id, granted, pickup, eta)
        tools.commit_leg(sim, leg)
        tools.verify_delivery(sim, leg)
        return True

    # -- helpers -----------------------------------------------------------

    def _any_viable_remaining(self) -> bool:
        """Recompute, do not consult a cached list."""
        return any(
            c.eligible or c.pending_confirmation
            for c in tools.find_recipients(self.sim, self.clock.now)
        )

    def _escalation_reason(self) -> tuple[str, list[str], str]:
        state = self.sim.state
        minutes = int(
            (state.offer.consumption_deadline - self.clock.now).total_seconds() / 60)

        transport_blocked = any(
            e.type is EventType.TRANSPORT_REJECTED for e in self.sim.log)
        all_declined = bool(state.declined) and not state.available_recipient_ids()

        if transport_blocked and not state.available_driver_ids():
            return (
                "No available vehicle satisfies transport conditions for "
                "perishable food before the consumption deadline.",
                ["Source an insulated container to pair with a sanitised vehicle",
                 "Authorise a nearer recipient reachable by an available vehicle",
                 "Stand down and direct the donor to Sch. I 2(4) disposal handling"],
                "FSSAI Surplus Food Regs 2019, Sch. I, 2(2)")

        if all_declined:
            return (
                f"All eligible recipients declined or did not respond; "
                f"{minutes} min remain.",
                ["Extend outreach beyond the registry",
                 "Stand down and direct the donor to Sch. I 2(4) disposal handling"],
                "FSSAI Surplus Food Regs 2019, reg. 5(3)")

        return (
            f"No lawful plan places the remaining {state.remaining_servings} "
            f"servings before the deadline.",
            ["Authorise a split across additional recipients",
             "Stand down and direct the donor to Sch. I 2(4) disposal handling"],
            "FSSAI Surplus Food Regs 2019, reg. 5(3)")

    def _close(self, reason: str) -> RescueResult:
        state = self.sim.state
        self.sim.emit(
            EventType.RESCUE_CLOSED,
            f"{state.delivered_servings}/{state.offer.quantity_servings} "
            f"servings delivered. {reason}.",
            {"reason": reason})
        return RescueResult(
            seed_id=self.scenario.seed_id,
            servings_offered=state.offer.quantity_servings,
            servings_delivered=state.delivered_servings,
            human_interventions=sum(
                1 for e in self.sim.log if e.type is EventType.HUMAN_INTERVENTION),
            decision_points=self.decision_points,
            replans=self.replans,
            confirmations_attempted=self.confirmations_attempted,
            confirmations_unresolved=self.confirmations_unresolved,
            closed_reason=reason,
            events=self.sim.log,
        )


# --------------------------------------------------------------------------
# Metrics across scenarios
# --------------------------------------------------------------------------

@dataclass
class Metrics:
    results: list[RescueResult]

    @property
    def scenarios(self) -> int:
        return len(self.results)

    @property
    def completed_without_human(self) -> int:
        return sum(1 for r in self.results if r.completed_without_human_decision)

    @property
    def interventions(self) -> int:
        return sum(r.human_interventions for r in self.results)

    @property
    def decision_points(self) -> int:
        return sum(r.decision_points for r in self.results)

    @property
    def servings_offered(self) -> int:
        return sum(r.servings_offered for r in self.results)

    @property
    def servings_delivered(self) -> int:
        return sum(r.servings_delivered for r in self.results)

    def render(self) -> str:
        dp = self.decision_points or 1
        lines = [
            "METRICS",
            f"  Scenarios completed without a human decision: "
            f"{self.completed_without_human} of {self.scenarios}",
            f"  Human interventions per decision point: "
            f"{self.interventions} of {dp} ({100 * self.interventions / dp:.1f}%)",
            f"  Servings delivered: {self.servings_delivered} of {self.servings_offered}",
            f"  Replans: {sum(r.replans for r in self.results)}",
            f"  Confirmations attempted: "
            f"{sum(r.confirmations_attempted for r in self.results)} "
            f"(unresolved: {sum(r.confirmations_unresolved for r in self.results)})",
        ]
        if self.scenarios < 10:
            lines.append(
                "  NOTE: report raw fractions at this sample size. Percentages "
                "over a handful of hand-authored scenarios are not a rate.")
        return "\n".join(lines)
