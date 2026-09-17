"""Tests for :func:`evaluate_settled_verdict` — the Tier-C predicate.

This module is deliberately separate from ``test_pr_gate_ledger.py``: that
file pins the *close-carve-out* predicate (may the loop file this thread
away?), which is a different question with a different failure mode. Here
we pin the board-side "did the human already rule?" predicate that drives
the ``Tier-C 決着 (要確認)`` state on the マージ lane (Bohr msg-825/827).

The blocking constraint every test defends: **do not read prose**. What
the human decided (ship / reject / hold) is unrecoverable from metadata,
and this predicate must never claim otherwise. It only answers the
weaker, structural question "has the adjudication ended?".
"""

from __future__ import annotations

from typing import Any

from magickit.mcp.pr_gate_ledger import (
    SettledVerdict,
    evaluate_settled_verdict,
)

_HUMANS = frozenset({"human"})
_NAYSAYERS = frozenset({"einstein"})


def _thread(status: str = "active", last: str = "msg-9") -> dict[str, Any]:
    return {"status": status, "last_msg_id": last, "thread_id": "T-x"}


def _human_decide(msg_id: str = "msg-10") -> dict[str, Any]:
    return {"msg_id": msg_id, "author": "human", "type": "decide"}


def _naysayer_verdict(msg_id: str = "msg-5", verdict: str = "review") -> dict[str, Any]:
    return {"msg_id": msg_id, "author": "einstein", "type": verdict}


def _naysayer_comment(msg_id: str = "msg-6") -> dict[str, Any]:
    return {"msg_id": msg_id, "author": "einstein", "type": "comment"}


def _agent(msg_id: str = "msg-4", author: str = "Bohr", type_: str = "report") -> dict[str, Any]:
    return {"msg_id": msg_id, "author": author, "type": type_}


def test_closed_thread_is_settled_regardless_of_last_message():
    """Shape 1 (Bohr msg-825): ``status == resolved`` — the human took over."""
    thread = _thread(status="resolved", last="msg-9")
    verdict = evaluate_settled_verdict(
        thread,
        messages=[_naysayer_verdict()],  # last msg is naysayer, doesn't matter
        head_sha="H",
        human_identities=_HUMANS,
        naysayer_identities=_NAYSAYERS,
    )
    assert verdict.settled is True
    assert verdict.settled_msg_id == "msg-9"


def test_superseded_thread_is_settled():
    """Both terminal states count — ``superseded`` is a variant of closed."""
    verdict = evaluate_settled_verdict(
        _thread(status="superseded"),
        messages=[_agent()],
        head_sha="H",
        human_identities=_HUMANS,
        naysayer_identities=_NAYSAYERS,
    )
    assert verdict.settled is True


def test_open_thread_with_human_decide_last_is_settled():
    """Shape 2 (Bohr msg-825): the PR #83 pathway (msg-800 → msg-806).

    Thread is still ``active``, but the human has ruled. No naysayer
    verdict has arrived after; the round has ended.
    """
    verdict = evaluate_settled_verdict(
        _thread(status="active"),
        messages=[
            _naysayer_verdict("msg-3"),
            _human_decide("msg-4"),  # newest
        ],
        head_sha="H",
        human_identities=_HUMANS,
        naysayer_identities=_NAYSAYERS,
    )
    assert verdict.settled is True
    assert verdict.settled_msg_id == "msg-4"


def test_naysayer_verdict_after_human_decide_returns_unsettled():
    """A naysayer speaking after the human wraps means the round reopened."""
    verdict = evaluate_settled_verdict(
        _thread(status="active"),
        messages=[
            _human_decide("msg-4"),
            _naysayer_verdict("msg-5"),  # newest
        ],
        head_sha="H",
        human_identities=_HUMANS,
        naysayer_identities=_NAYSAYERS,
    )
    assert verdict.settled is False


def test_naysayer_comment_after_human_decide_stays_settled():
    """A comment (not a verdict) does not reopen adjudication.

    The predicate distinguishes verdict-carrying msg types (review /
    approve / reject) from incidental ``comment`` naysayer messages;
    only the former mean "the round has re-fired".
    """
    verdict = evaluate_settled_verdict(
        _thread(status="active"),
        messages=[
            _human_decide("msg-4"),
            _naysayer_comment("msg-5"),  # not a verdict
        ],
        head_sha="H",
        human_identities=_HUMANS,
        naysayer_identities=_NAYSAYERS,
    )
    assert verdict.settled is True
    assert verdict.settled_msg_id == "msg-4"


def test_no_human_decide_anywhere_is_unsettled():
    """A thread with only agent / naysayer traffic has not been ruled on."""
    verdict = evaluate_settled_verdict(
        _thread(status="active"),
        messages=[_agent(), _naysayer_verdict()],
        head_sha="H",
        human_identities=_HUMANS,
        naysayer_identities=_NAYSAYERS,
    )
    assert verdict.settled is False


def test_head_sha_missing_returns_unsettled():
    """No head → cannot anchor a settlement; fail closed."""
    verdict = evaluate_settled_verdict(
        _thread(status="resolved"),
        messages=[],
        head_sha=None,
        human_identities=_HUMANS,
        naysayer_identities=_NAYSAYERS,
    )
    assert verdict.settled is False
    assert "head unknown" in verdict.reason


def test_no_thread_returns_unsettled():
    """A ledger we could not read cannot be claimed as settled."""
    verdict = evaluate_settled_verdict(
        None,
        messages=[_human_decide()],
        head_sha="H",
        human_identities=_HUMANS,
        naysayer_identities=_NAYSAYERS,
    )
    assert verdict.settled is False


def test_prose_is_not_read():
    """We never parse a message body — settled says nothing about content.

    A ``decide`` message whose content says "Reject" is *still* settled;
    the board renders 「Tier-C 決着 (要確認)」 and links to chatroom,
    it does NOT claim shipping was authorized (Bohr msg-827).
    """
    msg = _human_decide()
    msg["content"] = "Reject and close. Do not merge."
    verdict = evaluate_settled_verdict(
        _thread(status="active"),
        messages=[msg],
        head_sha="H",
        human_identities=_HUMANS,
        naysayer_identities=_NAYSAYERS,
    )
    # Predicate says "settled" — what the human decided is not this
    # function's concern. Board's UI subtitle prompts the human to
    # verify. See Bohr msg-827 §3 for the design honesty argument.
    assert verdict.settled is True


def test_author_case_is_normalized():
    """Author matching mirrors ``decisions._is_human_decide``: lower-cased."""
    msg = {"msg_id": "m", "author": "HUMAN", "type": "decide"}
    verdict = evaluate_settled_verdict(
        _thread(status="active"),
        messages=[msg],
        head_sha="H",
        human_identities=_HUMANS,
        naysayer_identities=_NAYSAYERS,
    )
    assert verdict.settled is True


def test_empty_messages_on_open_thread_is_unsettled():
    """No evidence to work with; do not invent a settlement."""
    verdict = evaluate_settled_verdict(
        _thread(status="active"),
        messages=[],
        head_sha="H",
        human_identities=_HUMANS,
        naysayer_identities=_NAYSAYERS,
    )
    assert isinstance(verdict, SettledVerdict)
    assert verdict.settled is False
