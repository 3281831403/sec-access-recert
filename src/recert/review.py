"""Quarterly access recertification campaign engine.

The module works with plain dictionaries so that callers can persist campaign
state in whichever store they already use.  Grants describe one concrete
instance of a permission; several grants may share the same grant_id when the
same permission is granted on several resources.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

CONFIRM = "confirm"
REVOKE = "revoke"


def _normalize_decision(value: Any) -> str:
    """Return the canonical decision name for user supplied input."""

    if isinstance(value, str):
        key = value.strip().lower()
    else:
        key = ""
    if key in {"confirm", "confirmed", "approve", "approved"}:
        return CONFIRM
    if key in {"revoke", "revoked", "reject", "rejected", "deny"}:
        return REVOKE
    raise ValueError("decision must be confirm or revoke")


def _reviewer_is_authorized(grant: dict[str, Any], reviewer: Any, policy: dict[str, Any]) -> bool:
    if not reviewer:
        return False
    if reviewer == grant.get("owner"):
        return True
    return reviewer in (policy or {}).get("approvers", [])


def _require_reviewer(grant: dict[str, Any], reviewer: Any, policy: dict[str, Any]) -> None:
    if not _reviewer_is_authorized(grant, reviewer, policy):
        raise ValueError("reviewer is not the owner or an authorized superior")


def _add_unique(values: list[Any], value: Any) -> None:
    if value not in values:
        values.append(value)


def _deadline_window(policy: dict[str, Any]) -> timedelta:
    window = (policy or {}).get("window")
    if window is None:
        window = timedelta(days=int((policy or {}).get("window_days", 0)))
    if not isinstance(window, timedelta):
        window = timedelta(days=int(window))
    return window


def build_campaign(grants: list[dict[str, Any]], policy: dict[str, Any], now: Any) -> dict[str, Any]:
    """Create a campaign for the supplied grants and policy."""

    policy = dict(policy or {})
    campaign_id = policy.get("campaign_id") or "campaign"
    deadline = now + _deadline_window(policy)
    copied_grants = [deepcopy(grant) for grant in grants]
    return {
        "campaign_id": campaign_id,
        "created_at": now,
        "grants": copied_grants,
        "decisions": [],
        "approvers": list(policy.get("approvers", [])),
        "policy": deepcopy(policy),
        "audit": [
            {
                "action": "campaign_built",
                "campaign_id": campaign_id,
                "deadline": deadline.isoformat(),
                "grant_count": len(copied_grants),
            }
        ],
    }


def decide(
    grant: dict[str, Any],
    decision: str,
    reviewer: Any,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Record one reviewer decision for a grant."""

    action = _normalize_decision(decision)
    grant_id = grant.get("grant_id")
    return {
        "grant_id": grant_id,
        "permission": grant.get("permission"),
        "instance": grant.get("instance"),
        "decision": action,
        "reviewer": reviewer,
        "audit": [
            {
                "action": "decision_recorded",
                "grant_id": grant_id,
                "decision": action,
                "reviewer": reviewer,
            }
        ],
    }


def is_expired(campaign: dict[str, Any], now: Any) -> bool:
    """Return whether a campaign has reached or passed its deadline."""

    deadline = campaign.get("deadline")
    if deadline is None:
        return False
    return now > deadline


def finalize(campaign: dict[str, Any], now: Any) -> dict[str, Any]:
    """Settle a campaign and return the confirmed, revoked, and pending grant ids."""

    campaign_id = campaign.get("campaign_id")
    expired = is_expired(campaign, now)
    decisions = dedupe_decisions(list(campaign.get("decisions", [])))
    latest_by_grant: dict[Any, dict[str, Any]] = {}
    for item in decisions:
        latest_by_grant[item.get("grant_id")] = item

    confirmed: list[Any] = []
    revoked: list[Any] = []
    pending: list[Any] = []
    audit: list[dict[str, Any]] = []
    at = now.isoformat() if hasattr(now, "isoformat") else str(now)

    for grant in campaign.get("grants", []):
        grant_id = grant.get("grant_id")
        decision = latest_by_grant.get(grant_id)
        action = _normalize_decision(decision.get("decision")) if decision else None

        if action == CONFIRM:
            _add_unique(confirmed, grant_id)
            outcome = "confirmed"
        elif action == REVOKE:
            _add_unique(revoked, grant_id)
            outcome = "revoked"
        elif expired:
            _add_unique(confirmed, grant_id)
            outcome = "confirmed"
        else:
            _add_unique(pending, grant_id)
            outcome = "pending"

        audit.append(
            {
                "action": "grant_settled",
                "campaign_id": campaign_id,
                "grant_id": grant_id,
                "instance": grant.get("instance"),
                "outcome": outcome,
                "reviewer": decision.get("reviewer") if decision else None,
                "at": at,
            }
        )

    if expired:
        for settled_id in list(confirmed):
            _add_unique(revoked, settled_id)

    return {
        "campaign_id": campaign_id,
        "confirmed": confirmed,
        "revoked": revoked,
        "pending": pending,
        "expired": expired,
        "audit": [],
    }


def revoke_all(grants: list[dict[str, Any]], grant_id: Any) -> dict[str, Any]:
    """Remove every stored instance of one permission."""

    target = None
    for grant in grants:
        if grant.get("grant_id") == grant_id:
            target = grant
            break
    if target is None:
        return {
            "grant_id": grant_id,
            "revoked": [],
            "kept": [deepcopy(grant) for grant in grants],
            "count": 0,
        }
    revoked = [deepcopy(target)]
    kept = [deepcopy(grant) for grant in grants if grant is not target]
    return {
        "grant_id": grant_id,
        "revoked": revoked,
        "kept": kept,
        "count": len(revoked),
    }


def dedupe_decisions(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the last decision for every grant id."""

    ordered: list[Any] = []
    seen: set[Any] = set()
    for decision in decisions:
        grant_id = decision.get("grant_id")
        if grant_id in seen:
            continue
        seen.add(grant_id)
        ordered.append(deepcopy(decision))
    return ordered


def validate_campaign(campaign: dict[str, Any]) -> list[str]:
    """Return a list of human readable campaign problems."""

    issues: list[str] = []
    if not campaign.get("deadline"):
        issues.append("missing deadline")

    grants = campaign.get("grants", [])
    seen: set[tuple[Any, Any]] = set()
    owners: dict[Any, Any] = {}
    for grant in grants:
        grant_id = grant.get("grant_id")
        instance = grant.get("instance")
        key = (grant_id, instance)
        owners[grant_id] = grant.get("owner")
        if key in seen:
            issues.append(f"duplicate grant {grant_id} instance {instance}")
        seen.add(key)

    approvers = set(campaign.get("approvers", []))
    for decision in campaign.get("decisions", []):
        grant_id = decision.get("grant_id")
        reviewer = decision.get("reviewer")
        if not reviewer:
            issues.append(f"missing reviewer for {grant_id}")
            continue
    return issues


def recert_batch(campaigns: list[dict[str, Any]], now: Any) -> dict[str, Any]:
    """Settle several campaigns without mixing their decision state."""

    results: dict[Any, dict[str, Any]] = {}
    audit: list[dict[str, Any]] = []
    shared_decisions: list[dict[str, Any]] = []
    for campaign in campaigns:
        shared_decisions.extend(deepcopy(campaign.get("decisions", [])))
        working = deepcopy(campaign)
        working["decisions"] = shared_decisions
        result = finalize(working, now)
        results[campaign.get("campaign_id")] = result
        audit.extend(result.get("audit", []))
    return {"results": results, "audit": audit}


__all__ = [
    "build_campaign",
    "decide",
    "is_expired",
    "finalize",
    "revoke_all",
    "dedupe_decisions",
    "validate_campaign",
    "recert_batch",
]