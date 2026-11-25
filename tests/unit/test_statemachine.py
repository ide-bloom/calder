from __future__ import annotations

from calder.infer.statemachine import k_tails, preconditions


def test_ktails_merges_equivalent_states():
    seqs = [["open", "read", "close"], ["open", "read", "read", "close"], ["open", "close"]]
    auto = k_tails(seqs, k=1)
    for s in seqs:
        assert auto.accepts(s)
    assert auto.accepts(["open", "read", "read", "read", "close"])  # generalized loop
    assert not auto.accepts(["read", "open"])
    assert len(auto.states) < sum(len(s) for s in seqs) + 1


def test_ktails_larger_k_is_more_specific():
    seqs = [["a", "b", "c"], ["a", "b", "b", "c"]]
    assert len(k_tails(seqs, k=3).states) >= len(k_tails(seqs, k=1).states)


def test_preconditions_with_disabler():
    seqs = [
        ["idn", "rem", "set", "set", "loc", "rem", "set"],
        ["idn", "poll", "rem", "set", "loc"],
        ["rem", "set", "poll", "set"],
        ["poll", "loc", "rem", "set"],
    ]
    hits = preconditions(seqs, requirements={"rem", "loc", "set"})
    by_target = {(h.message, h.requires): h for h in hits}
    assert ("set", "rem") in by_target
    assert by_target[("set", "rem")].disabled_by == ("loc",)
    assert ("set", "idn") not in by_target  # queries cannot enable anything
    assert ("set", "poll") not in by_target


def test_batched_commands_are_not_preconditions():
    seqs = [["rem", "temp", "rate"], ["rem", "temp", "rate", "temp", "rate"], ["x", "rem", "temp", "rate"]]
    hits = preconditions(seqs, requirements={"rem", "temp", "rate", "x"})
    pairs = {(h.message, h.requires) for h in hits}
    assert ("rate", "rem") in pairs
    assert ("rate", "temp") not in pairs  # always sent immediately before: vendor batching


def test_precondition_needs_support():
    assert preconditions([["a", "b"]], min_support=2) == []
