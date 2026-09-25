"""Call-count metric for the tool-call validator (#58).

The v5 pilot scored 60/60 parse and 60/60 schema-valid — and still emitted
MORE THAN ONE tool call on every single trial. "Write the line 'hello' to
/tmp/notes.md" produced 7 calls: one correct write, then six redundant
verification reads.

`parse_rate` cannot see this. A response of 1 correct call + 6 junk calls
scores identically to a clean single call, so the headline number can improve
while real-world behaviour degrades. In an agent loop the junk burns tokens
and can cause real side effects.

These tests pin a metric that CAN see it. They must fail against a validator
that only counts parseability.
"""

import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "call_count_metrics",
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts" / "fine_tune" / "call_count_metrics.py",
)
vtc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vtc)


def _counts(per_trial_calls):
    """Build the metric from a list of per-trial tool-call counts."""
    return vtc.call_count_metrics(per_trial_calls)


class TestDistribution:
    def test_reports_the_full_distribution(self):
        m = _counts([1, 1, 2, 3, 3, 3])
        assert m["distribution"] == {"1": 2, "2": 1, "3": 3}

    def test_counts_trials_emitting_exactly_one_call(self):
        m = _counts([1, 1, 2, 5])
        assert m["exactly_one"] == 2
        assert m["exactly_one_rate"] == 0.5

    def test_mean_calls_per_trial(self):
        assert _counts([1, 1, 2, 4])["mean_calls"] == 2.0

    def test_max_calls(self):
        assert _counts([1, 2, 9])["max_calls"] == 9

    def test_empty_input_does_not_divide_by_zero(self):
        m = _counts([])
        assert m["exactly_one_rate"] == 0.0
        assert m["mean_calls"] == 0.0


class TestGate:
    """The metric must be able to FAIL a run, or it is decoration."""

    def test_clean_single_call_run_passes(self):
        m = _counts([1] * 60)
        assert m["exactly_one_rate"] == 1.0
        assert vtc.call_count_passed(m, min_exactly_one_rate=0.8) is True

    def test_the_observed_v5_distribution_FAILS(self):
        """The real v5 numbers: never exactly one call, on any of 60 trials."""
        observed = (
            [2] * 8 + [3] * 12 + [4] * 15 + [5] * 11 + [6] * 10 + [7] * 2 + [8] + [9]
        )
        assert len(observed) == 60
        m = _counts(observed)
        assert m["exactly_one"] == 0
        assert m["exactly_one_rate"] == 0.0
        assert vtc.call_count_passed(m, min_exactly_one_rate=0.8) is False, (
            "a run where NO trial emits a single clean call must not pass"
        )

    def test_threshold_is_honoured(self):
        m = _counts([1] * 7 + [4] * 3)   # 70% exactly-one
        assert vtc.call_count_passed(m, min_exactly_one_rate=0.6) is True
        assert vtc.call_count_passed(m, min_exactly_one_rate=0.8) is False
