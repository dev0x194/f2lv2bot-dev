"""Experimentation framework for FileToLinkV2bot (Phase 4).

Provides deterministic, reproducible, privacy-preserving user assignment to
experiments without storing tracking cookies or third-party identifiers.
"""

from dataclasses import dataclass
from typing import Literal
from FileStream.services.analytics import get_user_variant
from FileStream.config import FeatureFlags


@dataclass(frozen=True)
class Experiment:
    id: str
    name: str
    description: str
    hypothesis: str
    variants: tuple[str, ...]
    primary_metric: str
    guardrail_metric: str
    decision_rule: str
    status: Literal["active", "completed", "draft", "paused"]


# Canonical Registry of Active & Planned Product Experiments
EXPERIMENT_REGISTRY: dict[str, Experiment] = {
    "start_cta_v2": Experiment(
        id="start_cta_v2",
        name="Action-Focused Home CTA Layout",
        description="Tests whether presenting quick recent shortcuts on the returning user /start screen improves second-day link creation.",
        hypothesis="Adding a direct shortcut to the user's most recent collection on /start increases 48h repeat file conversions by >= 5%.",
        variants=("control", "variant_action"),
        primary_metric="repeat_conversion_rate",
        guardrail_metric="error_rate",
        decision_rule="Adopt variant_action if repeat conversion rate improves by at least 5% with zero increase in error rate.",
        status="active",
    ),
    "settings_toggle_v1": Experiment(
        id="settings_toggle_v1",
        name="Explicit Toggle Status Indicators in /settings",
        description="Tests whether showing explicit [ON] / [OFF] pill text on inline buttons increases user agency and trust.",
        hypothesis="Explicit toggle badges reduce unintended preference flips and increase user preference confidence.",
        variants=("control", "variant_pills"),
        primary_metric="preference_retention",
        guardrail_metric="notification_opt_out_spike",
        decision_rule="Adopt variant_pills if unintended double-toggles decrease without elevating opt-outs.",
        status="active",
    ),
}


def is_experiment_active(experiment_id: str) -> bool:
    """Check if an experiment is enabled and currently running."""
    if not getattr(FeatureFlags, "EXPERIMENTS", True):
        return False
    exp = EXPERIMENT_REGISTRY.get(experiment_id)
    return bool(exp and exp.status == "active")


def assign_variant(user_id: int | None, experiment_id: str) -> str:
    """Assign a user to an experiment variant deterministically via SHA-256.

    Falls back to 'control' if the experiment is not active, feature flag is off,
    or user_id is missing.
    """
    if not user_id or not is_experiment_active(experiment_id):
        return "control"

    exp = EXPERIMENT_REGISTRY.get(experiment_id)
    if not exp or not exp.variants:
        return "control"

    variant_letter = get_user_variant(user_id, experiment_id)
    # Map 'A' -> variants[0] ('control'), 'B' -> variants[1] (variant)
    if variant_letter == "B" and len(exp.variants) > 1:
        return exp.variants[1]
    return exp.variants[0]


def get_experiment_details(experiment_id: str) -> dict | None:
    """Retrieve full metadata for an experiment."""
    exp = EXPERIMENT_REGISTRY.get(experiment_id)
    if not exp:
        return None
    return {
        "id": exp.id,
        "name": exp.name,
        "description": exp.description,
        "hypothesis": exp.hypothesis,
        "variants": list(exp.variants),
        "primary_metric": exp.primary_metric,
        "guardrail_metric": exp.guardrail_metric,
        "decision_rule": exp.decision_rule,
        "status": exp.status,
    }
