"""Conflict policy: a pure function, no I/O.

Systems are named. One of them (AUTHORITY) is the legal record of consent,
the others are followers. Statuses are canonical: OPTED_IN or WITHDRAWN.

Rules (every action carries its rule id, which goes to the audit trail):
  R2  The authority says withdrawn: followers must be withdrawn too.
  R3  A follower reports a withdrawal that is not older than the authority's
      state: the withdrawal wins and is recorded in the authority. A tie
      resolves to withdrawn.
  R4  The authority holds an opt-in newer than a follower's withdrawal: this
      is a valid re-consent, the follower is set back to opted in.
  R5  A follower is missing a contact that opted in: create it.
  A contact unknown to the authority is never changed, it goes to review.
Only the authority can grant consent, followers can only withdraw it.
"""
from dataclasses import dataclass
from datetime import datetime

AUTHORITY = "consent"
OPTED_IN = "opted_in"
WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class SourceState:
    """One system's view of a contact, in canonical terms."""
    status: str      # OPTED_IN or WITHDRAWN
    at: datetime     # when this status took effect (timezone-aware, UTC)


@dataclass(frozen=True)
class Action:
    target: str      # system name, e.g. "consent" or "email"
    kind: str        # "set_status" or "create"
    status: str      # desired canonical status
    at: datetime     # effective time to record (the original event time)
    source: str      # what caused it, e.g. "sync_from_email"
    rule: str        # rule id for the audit trail


@dataclass(frozen=True)
class Decision:
    actions: tuple[Action, ...] = ()
    review: str | None = None   # reason code when a human must look


def decide(states: dict[str, SourceState | None]) -> Decision:
    authority = states.get(AUTHORITY)
    followers = {name: s for name, s in states.items() if name != AUTHORITY}

    if authority is None:
        if any(s is not None for s in followers.values()):
            return Decision(review="unknown_contact")
        return Decision()

    # 1. Compute the canonical state. A follower's withdrawal wins unless the
    #    authority holds a strictly newer state.
    canonical, origin = authority, AUTHORITY
    for name, state in followers.items():
        if (state is not None and state.status == WITHDRAWN
                and canonical.status == OPTED_IN and state.at >= canonical.at):
            canonical, origin = state, name

    # 2. Derive what each system needs to reach the canonical state.
    source = f"sync_from_{origin}"
    actions: list[Action] = []
    if canonical.status != authority.status:
        actions.append(Action(AUTHORITY, "set_status", canonical.status,
                              canonical.at, source, "R3_withdrawal_from_follower"))
    for name, state in followers.items():
        if state is None:
            if canonical.status == OPTED_IN:
                actions.append(Action(name, "create", OPTED_IN, canonical.at,
                                      source, "R5_create_missing"))
        elif state.status != canonical.status:
            rule = ("R2_propagate_withdrawal" if canonical.status == WITHDRAWN
                    else "R4_reconsent")
            actions.append(Action(name, "set_status", canonical.status,
                                  canonical.at, source, rule))
    return Decision(tuple(actions))
