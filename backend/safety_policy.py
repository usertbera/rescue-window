"""
Deterministic safety policy gate.

Grounded in: Food Safety and Standards (Recovery and Distribution of Surplus
Food) Regulations, 2019 (FSSAI), Gazette of India, Extraordinary, Part III
Sec. 4, No. 278, 30 July 2019.

DESIGN RULE
-----------
The LLM agent plans rescues. It does not decide safety. Every rule below is
plain Python with an explicit clause reference. The agent may call
`evaluate_offer` and `evaluate_recipient`, and it may reason about the
results, but it cannot override a Level 0 failure.

SOURCE HONESTY
--------------
Each rule declares its origin:
  REGULATION     -- stated in the 2019 gazette; clause cited
  PROJECT_POLICY -- our own operating choice; the regulation does not set it

Do not blur these. The regulation sets no numeric safety-margin before
expiry (reg. 10(2) leaves that to the Food Authority to specify), so our
buffer is PROJECT_POLICY and is labelled as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------
# Source references
# --------------------------------------------------------------------------

class Origin(str, Enum):
    REGULATION = "REGULATION"
    PROJECT_POLICY = "PROJECT_POLICY"


@dataclass(frozen=True)
class SourceRef:
    origin: Origin
    clause: str
    note: str

    def __str__(self) -> str:
        if self.origin is Origin.REGULATION:
            return f"FSSAI Surplus Food Regs 2019, {self.clause}"
        return f"Project policy ({self.clause})"


R = Origin.REGULATION
P = Origin.PROJECT_POLICY

REF_SURPLUS_DEF = SourceRef(R, "reg. 2(1)(c)", "surplus food = leftover unused portions of safe food not served to customers")
REF_NO_UNSAFE = SourceRef(R, "reg. 4(3)", "FBO shall not distribute unsafe food")
REF_HANDOVER = SourceRef(R, "reg. 4(4)", "hand over in reasonable time before spoilage/expiry")
REF_RECIPIENT_LICENCE = SourceRef(R, "reg. 5(1)", "distribution org regulated under Licensing & Registration Regs 2011; food from sources with valid licence/registration")
REF_RECIPIENT_FACILITIES = SourceRef(R, "reg. 5(2)", "proper facilities for transport, storage and reheating")
REF_NO_POST_EXPIRY = SourceRef(R, "reg. 5(3)", "no distribution after expiry of shelf life")
REF_LABEL_PREPACKED = SourceRef(R, "reg. 6(2)", "original label: item name, manufacturer info, ingredients, expiry")
REF_LABEL_PREPARED = SourceRef(R, "reg. 6(3)", "prepared food label: name, source, prep date, last consumption date, veg/non-veg")
REF_LABEL_UNMASKED = SourceRef(R, "reg. 6(4)", "label information shall not be masked")
REF_SEGREGATION = SourceRef(R, "Sch. I, 1(1)", "safe, segregated into perishable and non-perishable")
REF_PACKING = SourceRef(R, "Sch. I, 1(2)", "packed to avoid contamination in handling and storage")
REF_NO_WASTE = SourceRef(R, "Sch. I, 1(4)", "not kept with waste material or products")
REF_DATE_MARKING = SourceRef(R, "Sch. I, 2(1)", "containers marked with pick-up date and use-by date")
REF_TRANSPORT = SourceRef(R, "Sch. I, 2(2)", "clean sanitised vehicles, optimum temperature, insulated containers/ice packs if necessary")
REF_SERVE_BEFORE_EXPIRY = SourceRef(R, "Sch. I, 2(3)", "distributed or served before expiry or while fit for human consumption")
REF_DISPOSAL = SourceRef(R, "Sch. I, 2(4)", 'unfit food marked "Food for Disposal"')
REF_FRIDGE_TEMP = SourceRef(R, "Sch. I, 2(6)", "refrigerator used for storage maintained below 7 degrees C, cleaned weekly")

REF_MARGIN = SourceRef(P, "handover safety margin", "regulation defers the pre-expiry segregation time to the Food Authority (reg. 10(2)); we set our own")
REF_ETA = SourceRef(P, "delivery ETA feasibility", "derived from reg. 5(3) and Sch. I 2(3); the arithmetic is ours")


# --------------------------------------------------------------------------
# Project policy constants -- OURS, not the regulation's
# --------------------------------------------------------------------------

HANDOVER_MARGIN = timedelta(minutes=30)
"""Delivery must land this far before the consumption deadline. PROJECT_POLICY."""

FRIDGE_MAX_C = 7.0
"""Below 7 C. REGULATION, Sch. I 2(6). Applies to the storage refrigerator."""


# --------------------------------------------------------------------------
# Domain model
# --------------------------------------------------------------------------

class FoodType(str, Enum):
    PREPARED_MEAL = "prepared_meal"
    PRE_PACKED = "pre_packed"


class Disposition(str, Enum):
    ELIGIBLE = "eligible"
    FOOD_FOR_DISPOSAL = "food_for_disposal"   # Sch. I, 2(4)
    NOT_IN_SCOPE = "not_in_scope"             # reg. 2(1)(c)


@dataclass
class PreparedLabel:
    food_name: Optional[str] = None
    source: Optional[str] = None
    preparation_date: Optional[datetime] = None
    last_consumption_date: Optional[datetime] = None
    vegetarian: Optional[bool] = None          # None = not declared
    masked: bool = False

    def missing(self) -> list[str]:
        out = []
        if not self.food_name:
            out.append("food name")
        if not self.source:
            out.append("source of food")
        if self.preparation_date is None:
            out.append("date of preparation")
        if self.last_consumption_date is None:
            out.append("last date of consumption")
        if self.vegetarian is None:
            out.append("vegetarian / non-vegetarian declaration")
        return out


@dataclass
class PrePackedLabel:
    item_name: Optional[str] = None
    manufacturer_info: Optional[str] = None
    ingredients: Optional[list[str]] = None
    expiry_date: Optional[datetime] = None
    masked: bool = False

    def missing(self) -> list[str]:
        out = []
        if not self.item_name:
            out.append("name of item or food")
        if not self.manufacturer_info:
            out.append("manufacturer information")
        if not self.ingredients:
            out.append("list of ingredients")
        if self.expiry_date is None:
            out.append("date of expiry")
        return out


@dataclass
class FoodOffer:
    offer_id: str
    donor_id: str
    donor_has_valid_licence: bool
    food_type: FoodType
    quantity_servings: int
    perishable: bool
    consumption_deadline: datetime          # expiry / last date of consumption
    served_to_customers: bool = False       # reg. 2(1)(c) scope gate
    known_unsafe: bool = False              # reg. 4(3)
    segregated_by_perishability: bool = True
    packed_against_contamination: bool = True
    stored_with_waste: bool = False
    container_date_marked: bool = True      # pick-up date + use-by date
    prepared_label: Optional[PreparedLabel] = None
    prepacked_label: Optional[PrePackedLabel] = None


@dataclass
class Recipient:
    recipient_id: str                       # never a real org name in scenario state
    has_valid_licence_or_registration: bool # reg. 5(1)
    has_transport_facilities: bool          # reg. 5(2)
    has_storage_facilities: bool            # reg. 5(2)
    has_reheating_facilities: bool          # reg. 5(2)
    refrigerator_temp_c: Optional[float] = None   # Sch. I, 2(6)
    refrigerator_cleaned_within_7_days: bool = True


@dataclass
class TransportOption:
    vehicle_id: str
    clean_and_sanitised: bool
    can_hold_optimum_temperature: bool
    has_insulated_container_or_ice_packs: bool
    food_grade_contact_surfaces: bool


@dataclass
class Violation:
    rule: str
    detail: str
    ref: SourceRef

    def render(self) -> str:
        return f"{self.detail}  [{self.ref}]"


@dataclass
class SafetyResult:
    eligible: bool
    disposition: Disposition
    violations: list[Violation] = field(default_factory=list)
    required_conditions: list[str] = field(default_factory=list)

    @property
    def source_refs(self) -> list[str]:
        return sorted({str(v.ref) for v in self.violations})

    def render(self) -> str:
        if self.eligible:
            line = "PASS"
            if self.required_conditions:
                line += " (conditional: " + "; ".join(self.required_conditions) + ")"
            return line
        head = f"FAIL -> {self.disposition.value}"
        return head + "\n" + "\n".join("  - " + v.render() for v in self.violations)


# --------------------------------------------------------------------------
# Level 0: offer admissibility
# --------------------------------------------------------------------------

def evaluate_offer(offer: FoodOffer, now: datetime) -> SafetyResult:
    """Non-negotiable gate on the food itself. The agent cannot override this."""
    v: list[Violation] = []

    # Scope. Not a safety failure -- simply not surplus food under the regs.
    if offer.served_to_customers:
        return SafetyResult(
            eligible=False,
            disposition=Disposition.NOT_IN_SCOPE,
            violations=[Violation(
                "scope",
                "Food was served to customers, so it is not surplus food under the regulations.",
                REF_SURPLUS_DEF,
            )],
        )

    if offer.known_unsafe:
        v.append(Violation("unsafe", "Food is flagged unsafe.", REF_NO_UNSAFE))

    if now >= offer.consumption_deadline:
        v.append(Violation(
            "expired",
            f"Consumption deadline passed at {offer.consumption_deadline:%H:%M}.",
            REF_NO_POST_EXPIRY,
        ))

    if not offer.donor_has_valid_licence:
        v.append(Violation(
            "donor_licence",
            "Donor does not hold a valid licence or registration.",
            REF_RECIPIENT_LICENCE,
        ))

    # Labelling
    if offer.food_type is FoodType.PREPARED_MEAL:
        label = offer.prepared_label
        if label is None:
            v.append(Violation("label", "Prepared food has no donation label.", REF_LABEL_PREPARED))
        else:
            miss = label.missing()
            if miss:
                v.append(Violation(
                    "label",
                    "Prepared food label is missing: " + ", ".join(miss) + ".",
                    REF_LABEL_PREPARED,
                ))
            if label.masked:
                v.append(Violation("label", "Label information is masked.", REF_LABEL_UNMASKED))
    else:
        label = offer.prepacked_label
        if label is None:
            v.append(Violation("label", "Pre-packed food has no original label.", REF_LABEL_PREPACKED))
        else:
            miss = label.missing()
            if miss:
                v.append(Violation(
                    "label",
                    "Original label is missing: " + ", ".join(miss) + ".",
                    REF_LABEL_PREPACKED,
                ))
            if label.masked:
                v.append(Violation("label", "Label information is masked.", REF_LABEL_UNMASKED))

    # Handling
    if not offer.segregated_by_perishability:
        v.append(Violation("handling", "Not segregated into perishable and non-perishable.", REF_SEGREGATION))
    if not offer.packed_against_contamination:
        v.append(Violation("handling", "Not packed to prevent contamination.", REF_PACKING))
    if offer.stored_with_waste:
        v.append(Violation("handling", "Stored together with waste material.", REF_NO_WASTE))

    conditions: list[str] = []
    if not offer.container_date_marked:
        conditions.append("apply pick-up date and use-by date marking to containers before dispatch")

    if v:
        unfit = any(x.rule in {"unsafe", "expired"} for x in v)
        return SafetyResult(
            eligible=False,
            disposition=Disposition.FOOD_FOR_DISPOSAL if unfit else Disposition.NOT_IN_SCOPE,
            violations=v,
        )

    return SafetyResult(True, Disposition.ELIGIBLE, [], conditions)


# --------------------------------------------------------------------------
# Level 0: recipient admissibility
# --------------------------------------------------------------------------

def evaluate_recipient(recipient: Recipient, offer: FoodOffer) -> SafetyResult:
    """Filters candidates before the agent ranks them."""
    v: list[Violation] = []

    if not recipient.has_valid_licence_or_registration:
        v.append(Violation(
            "recipient_licence",
            "Recipient lacks valid licence or registration under the 2011 Licensing Regulations.",
            REF_RECIPIENT_LICENCE,
        ))

    missing = []
    if not recipient.has_transport_facilities:
        missing.append("transport")
    if not recipient.has_storage_facilities:
        missing.append("storage")
    if not recipient.has_reheating_facilities:
        missing.append("reheating")
    if missing:
        v.append(Violation(
            "recipient_facilities",
            "Recipient lacks required facilities: " + ", ".join(missing) + ".",
            REF_RECIPIENT_FACILITIES,
        ))

    # Refrigeration is a property of the recipient's facility, not of the offer.
    if offer.perishable:
        t = recipient.refrigerator_temp_c
        if t is None:
            v.append(Violation(
                "refrigeration",
                "Perishable food, but recipient reports no refrigerated storage.",
                REF_FRIDGE_TEMP,
            ))
        elif t >= FRIDGE_MAX_C:
            v.append(Violation(
                "refrigeration",
                f"Recipient refrigerator at {t:.1f} C; must be below {FRIDGE_MAX_C:.0f} C.",
                REF_FRIDGE_TEMP,
            ))
        if not recipient.refrigerator_cleaned_within_7_days:
            v.append(Violation(
                "refrigeration",
                "Recipient refrigerator not cleaned within the last 7 days.",
                REF_FRIDGE_TEMP,
            ))

    if v:
        return SafetyResult(False, Disposition.NOT_IN_SCOPE, v)
    return SafetyResult(True, Disposition.ELIGIBLE)


# --------------------------------------------------------------------------
# Level 0: transport admissibility
# --------------------------------------------------------------------------

def evaluate_transport(vehicle: TransportOption, offer: FoodOffer) -> SafetyResult:
    v: list[Violation] = []

    if not vehicle.clean_and_sanitised:
        v.append(Violation("transport", "Vehicle is not clean and sanitised.", REF_TRANSPORT))
    if not vehicle.food_grade_contact_surfaces:
        v.append(Violation("transport", "Food-contact surfaces are not food grade.", REF_TRANSPORT))
    if offer.perishable:
        if not vehicle.can_hold_optimum_temperature and not vehicle.has_insulated_container_or_ice_packs:
            v.append(Violation(
                "transport",
                "Perishable food requires temperature control; vehicle has neither active "
                "temperature control nor insulated containers or ice packs.",
                REF_TRANSPORT,
            ))

    if v:
        return SafetyResult(False, Disposition.NOT_IN_SCOPE, v)
    return SafetyResult(True, Disposition.ELIGIBLE)


# --------------------------------------------------------------------------
# Level 0: timing feasibility
# --------------------------------------------------------------------------

def evaluate_timing(offer: FoodOffer, estimated_delivery: datetime) -> SafetyResult:
    """
    The regulation requires distribution before expiry (reg. 5(3), Sch. I 2(3))
    and handover at a reasonable time before spoilage (reg. 4(4)). It does not
    quantify 'reasonable'. HANDOVER_MARGIN is ours.
    """
    latest_ok = offer.consumption_deadline - HANDOVER_MARGIN

    if estimated_delivery >= offer.consumption_deadline:
        return SafetyResult(False, Disposition.NOT_IN_SCOPE, [Violation(
            "timing",
            f"Delivery at {estimated_delivery:%H:%M} is after the consumption "
            f"deadline of {offer.consumption_deadline:%H:%M}.",
            REF_NO_POST_EXPIRY,
        )])

    if estimated_delivery > latest_ok:
        return SafetyResult(False, Disposition.NOT_IN_SCOPE, [Violation(
            "timing",
            f"Delivery at {estimated_delivery:%H:%M} leaves under "
            f"{int(HANDOVER_MARGIN.total_seconds() // 60)} min before the "
            f"{offer.consumption_deadline:%H:%M} deadline.",
            REF_MARGIN,
        )])

    return SafetyResult(True, Disposition.ELIGIBLE)


# --------------------------------------------------------------------------
# Composed gate -- the single entry point the agent is allowed to call
# --------------------------------------------------------------------------

def check_plan(
    offer: FoodOffer,
    recipient: Recipient,
    vehicle: TransportOption,
    estimated_delivery: datetime,
    now: datetime,
) -> SafetyResult:
    """Returns the first failing gate, so the trace shows one clear reason."""
    for result in (
        evaluate_offer(offer, now),
        evaluate_recipient(recipient, offer),
        evaluate_transport(vehicle, offer),
        evaluate_timing(offer, estimated_delivery),
    ):
        if not result.eligible:
            return result
    return SafetyResult(True, Disposition.ELIGIBLE)


# --------------------------------------------------------------------------
# Schedule II record -- the twelve fields the regulation requires
# --------------------------------------------------------------------------

SCHEDULE_II_FIELDS = (
    "donor_org_name_and_address",
    "distribution_org_name_and_address",
    "donation_date",
    "food_product_name",
    "batch_number",
    "date_of_manufacturing",
    "best_before_or_expiry_date",
    "quantity_donated_by_donor",
    "temperature_of_food",
    "quantity_distributed_by_org",
    "area_where_distributed",
    "date_of_distribution",
)
"""FSSAI Surplus Food Regs 2019, Schedule II (see reg. 7). Mirror these in the
event log so a completed rescue emits a compliant record, not a screenshot."""
