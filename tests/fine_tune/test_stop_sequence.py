"""The generation request must terminate after one tool call (#58).

Every one of the 5000 v5 training rows contains EXACTLY ONE tool call, so the
model was never taught multi-call behaviour. It over-emitted anyway — measured
7.33 calls per trial, max 15 — because the request carried no stop sequence,
so generation ran on past `</tool_call>` and the model kept inventing
follow-ups (and eventually repeating itself verbatim).

Measured on the loaded v5 model, same weights, one parameter changed:

    without stop:  1/18 trials emitted exactly one call (6%),  mean 7.33
    with stop:    18/18 trials emitted exactly one call (100%), mean 1.00

This is a GENERATION defect, not a data defect — a retrain on scrubbed data
would have reproduced it exactly. These tests pin the request shape so the
stop sequence cannot be dropped again.
"""

import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "vtc",
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts" / "fine_tune" / "validate_tool_calls.py",
)


def _source() -> str:
    return (
        pathlib.Path(__file__).resolve().parents[2]
        / "scripts" / "fine_tune" / "validate_tool_calls.py"
    ).read_text()


class TestOpenAIPayload:
    def test_stop_sequence_is_sent(self):
        """gen_openai must include a stop sequence in its payload."""
        src = _source()
        start = src.index("def gen_openai(")
        body = src[start:src.index("\ndef ", start + 1)]
        assert '"stop"' in body, (
            "gen_openai sends no stop sequence. Generation then runs past "
            "</tool_call> and the model emits 7+ calls for a one-call task."
        )

    def test_stop_sequence_closes_a_tool_call(self):
        """The stop token must be the tool-call terminator.

        Asserted against the module-level constant rather than gen_openai's
        body: the constant is defined above the function (so it is documented
        and reusable), and an earlier version of this test sliced only the
        body and failed on correct code.
        """
        src = _source()
        assert 'TOOL_CALL_STOP = ["</tool_call>"]' in src, (
            "the stop sequence must terminate on </tool_call> — that is the "
            "token the model runs past when over-emitting"
        )
        start = src.index("def gen_openai(")
        body = src[start:src.index("\ndef ", start + 1)]
        assert "TOOL_CALL_STOP" in body, "gen_openai must actually use it"

    def test_stop_is_overridable(self):
        """An operator must be able to disable it to REPRODUCE the bug."""
        src = _source()
        assert "--no-stop" in src or "no_stop" in src, (
            "there must be a way to turn the stop sequence off, or the "
            "over-emission bug becomes unreproducible"
        )
