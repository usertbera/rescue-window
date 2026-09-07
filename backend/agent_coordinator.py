"""
Strands-agent-driven coordinator.

This is the live, LLM-orchestrated counterpart to coordinator.Coordinator.
Both share the exact same safety gate (safety_policy.py) and the exact same
low-level tool functions (tools.py) -- nothing about eligibility, transport
legality, or timing feasibility is ever decided by the model. What changes is
who decides *which candidate to try next, when to ask a clarifying question,
and when to give up and hand off to a human*: here, a Strands Agent backed by
Amazon Bedrock makes those calls, through tool calls Strands itself drives.

TOOL DESIGN RULE, load-bearing: every tool exposed to the agent is safe by
construction. `attempt_placement` is the only tool that can commit servings,
and it always runs request_acceptance -> find_transport -> check_timing ->
commit_leg in that fixed order internally, so there is no sequence of tool
calls the agent can make that skips a safety check. The agent chooses *which*
recipient to attempt and *when* to ask, escalate, or stop -- never *whether*
a regulation is satisfied. See safety_policy.py's own module docstring for
the same rule stated from the gate's side.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from strands import Agent, tool
from strands.models import BedrockModel

import tools
from registry import Scenario
from simulation import EventType, Leg, SimulatedClock, Simulator

DEFAULT_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "")
"""No hardcoded default: Bedrock model access is enabled per-account,
per-region, and changes over time. Set BEDROCK_MODEL_ID to a model you have
enabled access to (AWS Console -> Bedrock -> Model access). See README."""

SYSTEM_PROMPT = """You are the RescueWindow coordinator. Surplus food has arrived and must \
reach a lawful recipient before its consumption deadline.

You do not decide safety. Every tool you call enforces the Food Safety and Standards \
(Recovery and Distribution of Surplus Food) Regulations, 2019 (India) in deterministic code. \
If a tool reports a recipient or a vehicle as ineligible, that is final -- do not argue with \
it, do not retry the same recipient the same way, move on to the next candidate.

Your job, each cycle:
1. Call get_situation to see remaining servings, minutes to deadline, and candidate recipients.
2. If a candidate has a field listed in pending_confirmation, call confirm_capability on it \
before considering it further -- you cannot place food with a recipient whose capability is \
unresolved.
3. Pick the single best eligible candidate (prefer one whose capacity covers the remaining \
servings; break ties by shortest travel time) and call attempt_placement on it.
4. attempt_placement can fail for lawful reasons (declined, timed out, no lawful transport, \
timing infeasible). That is normal -- call get_situation again and try the next best \
candidate. A failed attempt is a replan, not an error, and not something to explain at length.
5. Only call escalate when get_situation shows zero remaining viable candidates and servings \
are still unplaced. Give a clear, specific reason and 2-3 concrete options a human could \
authorise.
6. Stop as soon as remaining_servings reaches 0, or immediately after you call escalate.

Be terse. State each decision in one short sentence before the tool call that acts on it. Do \
not repeat information the tool result already gave you."""


@dataclass
class AgentRunResult:
    seed_id: str
    servings_offered: int
    servings_delivered: int
    human_interventions: int
    transcript: list[str] = field(default_factory=list)
    events: list = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.seed_id}: {self.servings_delivered}/{self.servings_offered} "
            f"delivered | interventions {self.human_interventions}"
        )


class AgentCoordinator:
    """Runs one scenario with a Strands Agent driving the tool calls."""

    def __init__(
        self,
        scenario: Scenario,
        model_id: str = DEFAULT_MODEL_ID,
        playback_multiplier: float = 60.0,
        **model_kwargs,
    ):
        if not model_id:
            raise ValueError(
                "No Bedrock model id given. Set BEDROCK_MODEL_ID to a model you have "
                "enabled access to, or pass model_id= explicitly. See README: "
                "'Live Strands agent'."
            )

        self.scenario = scenario
        self.clock = SimulatedClock(scenario.scenario_start, playback_multiplier)
        self.sim = Simulator(scenario, self.clock)
        self._transcript: list[str] = []

        sim = self.sim
        clock = self.clock

        # -- tools exposed to the agent, closed over this run's simulator ---
        # Each one is safe by construction: see the module docstring.

        @tool
        def get_situation() -> dict:
            """Return remaining servings, minutes to deadline, and every
            currently available candidate recipient with distance, capacity,
            close time, eligibility, and any pending capability
            confirmations. Call this first, and again after every
            attempt_placement or confirm_capability call -- state changes
            each time."""
            state = sim.state
            candidates = tools.find_recipients(sim, clock.now)
            minutes = int(
                (state.offer.consumption_deadline - clock.now).total_seconds() / 60
            )
            return {
                "remaining_servings": state.remaining_servings,
                "minutes_to_deadline": minutes,
                "candidates": [
                    {
                        "recipient_id": c.recipient_id,
                        "distance_km": round(c.distance_km, 1),
                        "travel_min": round(c.travel_min, 1),
                        "closes_at": c.closes_at,
                        "nominal_capacity": c.nominal_capacity,
                        "eligible_now": c.eligible,
                        "reason_if_ineligible": c.reason,
                        "pending_confirmation": c.pending_confirmation or [],
                    }
                    for c in candidates
                ],
            }

        @tool
        def confirm_capability(recipient_id: str, field_name: str) -> dict:
            """Ask a recipient to confirm one unresolved capability field
            (e.g. 'has_reheating_facilities', from that candidate's
            pending_confirmation list). Returns resolved: true, false, or
            null. Null means no reply arrived in time -- the candidate is
            eliminated for this cycle. That is not an escalation on its own;
            check get_situation for other viable candidates first."""
            reply = tools.confirm_capability(sim, recipient_id, field_name, clock.now)
            return {"recipient_id": recipient_id, "field": field_name, "resolved": reply}

        @tool
        def attempt_placement(recipient_id: str) -> dict:
            """Try to place the remaining servings with one recipient:
            request acceptance, then (only if accepted) find lawful
            transport and check delivery timing, then commit and verify
            delivery. Returns what happened at whichever step it stopped,
            with the regulation clause if a legal check failed. This is the
            only tool that can place food, and it always runs the legal
            checks in order -- there is no way to skip them."""
            state = sim.state
            status, granted = tools.request_acceptance(
                sim, recipient_id, state.remaining_servings, clock.now
            )
            if status != "accept" or granted <= 0:
                return {"recipient_id": recipient_id, "outcome": status, "servings_placed": 0}

            transport = tools.find_transport(sim, recipient_id, clock.now)
            if transport is None:
                return {
                    "recipient_id": recipient_id,
                    "outcome": "no_lawful_transport",
                    "servings_placed": 0,
                }

            driver_id, pickup, eta = transport
            timing = tools.check_timing(sim, eta)
            if not timing.eligible:
                return {
                    "recipient_id": recipient_id,
                    "outcome": "timing_infeasible",
                    "servings_placed": 0,
                    "reason": timing.violations[0].detail if timing.violations else None,
                }

            leg = Leg(recipient_id, driver_id, granted, pickup, eta)
            tools.commit_leg(sim, leg)
            tools.verify_delivery(sim, leg)
            return {
                "recipient_id": recipient_id,
                "outcome": "delivered",
                "servings_placed": granted,
                "driver_id": driver_id,
            }

        @tool
        def escalate(reason: str, options: list[str]) -> dict:
            """Hand off to a human. Only call this when get_situation shows
            no remaining viable candidates and servings are still unplaced.
            `options` should be 2-3 concrete, authorisable actions."""
            tools.escalate(sim, reason, options)
            return {"escalated": True, "reason": reason}

        model = BedrockModel(model_id=model_id, temperature=0.2, **model_kwargs)
        self.agent = Agent(
            model=model,
            system_prompt=SYSTEM_PROMPT,
            tools=[get_situation, confirm_capability, attempt_placement, escalate],
        )

    # -- driving the agent ---------------------------------------------------

    def run(self, max_turns: int = 20) -> AgentRunResult:
        state = self.sim.state
        self.sim.emit(
            EventType.OFFER_RECEIVED,
            f"{state.offer.quantity_servings} servings, {state.offer.food_type.value}, "
            f"deadline {state.offer.consumption_deadline:%H:%M}.",
            {"offer_id": state.offer.offer_id},
        )

        gate = tools.safety_check(self.sim, self.clock.now)
        if gate.eligible:
            prompt = (
                f"New offer: {state.offer.quantity_servings} servings of "
                f"{state.offer.food_type.value}, consumption deadline "
                f"{state.offer.consumption_deadline:%H:%M}. Coordinate its rescue."
            )
            result = self.agent(prompt, limits={"turns": max_turns})
            self._transcript.append(_extract_text(result.message))

        self.sim.emit(
            EventType.RESCUE_CLOSED,
            f"{self.sim.state.delivered_servings}/{state.offer.quantity_servings} "
            f"servings delivered.",
            {},
        )
        return self._close()

    def _close(self) -> AgentRunResult:
        state = self.sim.state
        return AgentRunResult(
            seed_id=self.scenario.seed_id,
            servings_offered=state.offer.quantity_servings,
            servings_delivered=state.delivered_servings,
            human_interventions=sum(
                1 for e in self.sim.log if e.type is EventType.HUMAN_INTERVENTION
            ),
            transcript=self._transcript,
            events=self.sim.log,
        )


def _extract_text(message) -> str:
    """Strands messages are {'role': ..., 'content': [ContentBlock, ...]}."""
    if isinstance(message, dict):
        blocks = message.get("content", [])
        return "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and "text" in b)
    return str(message)
