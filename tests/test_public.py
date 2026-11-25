from datetime import datetime, timedelta

import pytest

from recert.review import build_campaign, decide, finalize, recert_batch, revoke_all

NOW = datetime(2026, 11, 19, 9, 0)
POLICY = {
    "campaign_id": "2026-Q4",
    "window": timedelta(days=14),
    "approvers": ["alice", "carol"],
}


def make_grant(instance="prod", grant_id="g-1", owner="alice"):
    return {
        "grant_id": grant_id,
        "permission": "payments:read",
        "principal": "bob",
        "instance": instance,
        "owner": owner,
    }


def make_campaign(campaign_id, deadline, grants, decisions=None):
    return {
        "campaign_id": campaign_id,
        "deadline": deadline,
        "grants": grants,
        "decisions": list(decisions or []),
        "approvers": ["alice", "carol"],
        "audit": [],
    }


def test_campaign_is_built_with_a_deadline():
    campaign = build_campaign([make_grant()], POLICY, NOW)

    assert campaign["campaign_id"] == "2026-Q4"
    assert campaign.get("deadline") == NOW + timedelta(days=14)
    assert campaign["decisions"] == []
    assert len(campaign["grants"]) == 1


def test_owner_can_confirm_but_an_outsider_cannot():
    grant = make_grant()

    decision = decide(grant, "confirm", "alice", POLICY)

    assert decision["grant_id"] == "g-1"
    assert decision["decision"] == "confirm"
    assert decision["reviewer"] == "alice"
    with pytest.raises(ValueError):
        decide(grant, "confirm", "mallory", POLICY)


def test_revoke_removes_every_instance_of_the_permission():
    grants = [
        make_grant(instance="prod"),
        make_grant(instance="dev"),
        make_grant(instance="prod", grant_id="g-2"),
    ]

    result = revoke_all(grants, "g-1")

    assert result["count"] == 2
    assert {grant["instance"] for grant in result["revoked"]} == {"prod", "dev"}
    assert [grant["grant_id"] for grant in result["kept"]] == ["g-2"]


def test_expired_unconfirmed_permission_is_revoked_by_default():
    campaign = make_campaign("expired", NOW - timedelta(hours=1), [make_grant()])

    result = finalize(campaign, NOW)

    assert result["revoked"] == ["g-1"]
    assert result["confirmed"] == []
    assert result["pending"] == []


def test_two_campaigns_are_processed_independently():
    confirmed = make_campaign(
        "campaign-a",
        NOW - timedelta(hours=1),
        [make_grant()],
        [{"grant_id": "g-1", "decision": "confirm", "reviewer": "alice"}],
    )
    untouched = make_campaign("campaign-b", NOW + timedelta(days=1), [make_grant()])

    batch = recert_batch([confirmed, untouched], NOW)

    assert batch["results"]["campaign-a"]["confirmed"] == ["g-1"]
    assert batch["results"]["campaign-b"]["confirmed"] == []
    assert batch["results"]["campaign-b"]["pending"] == ["g-1"]