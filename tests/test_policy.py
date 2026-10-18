from datetime import datetime, timezone

from glue.policy import OPTED_IN, WITHDRAWN, SourceState, decide


def t(minute: int) -> datetime:
    return datetime(2026, 10, 18, 10, minute, tzinfo=timezone.utc)


def S(status: str, minute: int) -> SourceState:
    return SourceState(status, t(minute))


def apply(states, decision):
    """Pretend every action succeeded, to check that decisions converge."""
    new = dict(states)
    for a in decision.actions:
        new[a.target] = SourceState(a.status, a.at)
    return new


def test_in_sync_does_nothing():
    d = decide({"consent": S(OPTED_IN, 5), "email": S(OPTED_IN, 5)})
    assert d.actions == () and d.review is None


def test_authority_withdrawal_propagates_to_email():
    d = decide({"consent": S(WITHDRAWN, 5), "email": S(OPTED_IN, 3)})
    (a,) = d.actions
    assert (a.target, a.kind, a.status) == ("email", "set_status", WITHDRAWN)
    assert a.rule == "R2_propagate_withdrawal" and a.source == "sync_from_consent"
    assert a.at == t(5)


def test_email_unsubscribe_is_recorded_with_original_timestamp():
    d = decide({"consent": S(OPTED_IN, 5), "email": S(WITHDRAWN, 8)})
    (a,) = d.actions
    assert (a.target, a.status) == ("consent", WITHDRAWN)
    assert a.at == t(8)                      # the customer's click time, not "now"
    assert a.rule == "R3_withdrawal_from_follower" and a.source == "sync_from_email"


def test_newer_consent_resubscribes_email():
    d = decide({"consent": S(OPTED_IN, 9), "email": S(WITHDRAWN, 5)})
    (a,) = d.actions
    assert (a.target, a.status, a.rule) == ("email", OPTED_IN, "R4_reconsent")


def test_tie_resolves_to_withdrawn():
    d = decide({"consent": S(OPTED_IN, 5), "email": S(WITHDRAWN, 5)})
    (a,) = d.actions
    assert (a.target, a.status) == ("consent", WITHDRAWN)


def test_missing_email_contact_is_created_when_opted_in():
    d = decide({"consent": S(OPTED_IN, 5), "email": None})
    (a,) = d.actions
    assert (a.target, a.kind, a.rule) == ("email", "create", "R5_create_missing")


def test_missing_email_contact_is_not_created_when_withdrawn():
    d = decide({"consent": S(WITHDRAWN, 5), "email": None})
    assert d.actions == () and d.review is None


def test_contact_unknown_to_consent_platform_goes_to_review():
    d = decide({"consent": None, "email": S(OPTED_IN, 5)})
    assert d.actions == () and d.review == "unknown_contact"


def test_both_withdrawn_does_nothing():
    d = decide({"consent": S(WITHDRAWN, 5), "email": S(WITHDRAWN, 6)})
    assert d.actions == ()


def test_decisions_converge_in_one_pass():
    """After the actions are applied, a second pass must find nothing to do.
    This is also why our own writes coming back as changes cause no echo loop."""
    scenarios = [
        {"consent": S(OPTED_IN, 5), "email": S(WITHDRAWN, 8)},
        {"consent": S(WITHDRAWN, 5), "email": S(OPTED_IN, 3)},
        {"consent": S(OPTED_IN, 9), "email": S(WITHDRAWN, 5)},
        {"consent": S(OPTED_IN, 5), "email": S(WITHDRAWN, 5)},
        {"consent": S(OPTED_IN, 5), "email": None},
        {"consent": None, "email": S(OPTED_IN, 5)},
    ]
    for states in scenarios:
        assert decide(apply(states, decide(states))).actions == (), states


def test_third_system_needs_no_policy_change():
    states = {"consent": S(OPTED_IN, 5), "email": S(OPTED_IN, 5), "crm": S(WITHDRAWN, 8)}
    d = decide(states)
    assert [a.target for a in d.actions] == ["consent", "email"]
    assert all(a.status == WITHDRAWN and a.at == t(8) for a in d.actions)
