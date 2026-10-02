"""Call-count metrics for tool-call validation (#58).

`parse_rate` answers "did the output parse as a tool call?". It cannot answer
"did the model emit the RIGHT NUMBER of calls?", and those come apart badly.

The v5 pilot scored 60/60 parse and 60/60 schema-valid while emitting more
than one call on every single trial — "write 'hello' to /tmp/notes.md"
produced 7 calls: one correct write, then six redundant verification reads.
One good call plus six junk calls scores identically to a clean single call,
so the headline number can improve while behaviour in an agent loop degrades
(wasted tokens, and real side effects from speculative calls).

Kept as its own module rather than inlined in validate_tool_calls.py so it can
be unit-tested without importing that script's argparse/CLI machinery.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


def call_count_metrics(per_trial_calls: list[int]) -> dict[str, Any]:
    """Summarize how many tool calls each trial emitted.

    ``per_trial_calls`` is one integer per trial: the number of tool calls
    parsed from that trial's output.

    Returns the distribution plus the headline figure, ``exactly_one_rate`` —
    the share of trials that emitted exactly one call. That is the number
    ``parse_rate`` cannot see.
    """
    n = len(per_trial_calls)
    if n == 0:
        return {
            "trials": 0,
            "distribution": {},
            "exactly_one": 0,
            "exactly_one_rate": 0.0,
            "mean_calls": 0.0,
            "max_calls": 0,
        }

    dist = Counter(per_trial_calls)
    exactly_one = dist.get(1, 0)
    return {
        "trials": n,
        # str keys so the report round-trips through JSON unchanged
        "distribution": {str(k): v for k, v in sorted(dist.items())},
        "exactly_one": exactly_one,
        "exactly_one_rate": exactly_one / n,
        "mean_calls": sum(per_trial_calls) / n,
        "max_calls": max(per_trial_calls),
    }


def call_count_passed(
    metrics: dict[str, Any], min_exactly_one_rate: float = 0.8
) -> bool:
    """Gate on the call-count metric.

    A metric that cannot fail a run is decoration. Default 0.8 — the observed
    v5 distribution scores 0.0 and fails, while a clean run scores 1.0.
    """
    if not metrics.get("trials"):
        return False
    return metrics.get("exactly_one_rate", 0.0) >= min_exactly_one_rate
