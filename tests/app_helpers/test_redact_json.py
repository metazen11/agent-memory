"""Unit tests for app.redact.redact_json — recursive scrubber for nested JSON.

These verify the contract the backfill writer (and any other JSON-ingest
path) relies on: every string leaf goes through redact_text, regardless
of nesting depth or whether the string is a dict value, list element, or
nested arbitrarily deep.
"""
from __future__ import annotations

import pytest

from app.redact import redact_json, redact_text


# ── Top-level shapes ────────────────────────────────────────────────────

def test_plain_string_with_secret_is_redacted():
    out = redact_json("here is sk-ant-api03-" + "A" * 80 + " token")
    assert "sk-ant-api03-" not in out
    assert "[REDACTED:" in out


def test_plain_string_without_secret_passes_through():
    out = redact_json("just a normal sentence")
    assert out == "just a normal sentence"


def test_number_passes_through():
    assert redact_json(42) == 42
    assert redact_json(3.14) == 3.14


def test_bool_passes_through():
    assert redact_json(True) is True
    assert redact_json(False) is False


def test_none_passes_through():
    assert redact_json(None) is None


def test_empty_dict_passes_through():
    assert redact_json({}) == {}


def test_empty_list_passes_through():
    assert redact_json([]) == []


# ── Single-level nesting ────────────────────────────────────────────────

def test_secret_in_dict_value_is_redacted():
    out = redact_json({"api_key": "ghp_" + "X" * 36})
    assert "ghp_" not in out["api_key"]
    assert "[REDACTED:github_token]" in out["api_key"]


def test_secret_in_list_element_is_redacted():
    out = redact_json(["hf_" + "Y" * 30, "safe value"])
    assert "[REDACTED:hf_token]" in out[0]
    assert out[1] == "safe value"


def test_non_secret_strings_in_dict_pass_through():
    inp = {"command": "ls -la", "cwd": "/Users/me/repo"}
    assert redact_json(inp) == inp


# ── The load-bearing case: nested Authorization header ────────────────

def test_bearer_token_nested_in_headers_dict_is_redacted():
    inp = {
        "url": "https://api.example.com/v1/data",
        "headers": {"Authorization": "Bearer " + "Z" * 40},
        "method": "GET",
    }
    out = redact_json(inp)
    assert "Bearer" not in out["headers"]["Authorization"] \
        or "[REDACTED" in out["headers"]["Authorization"]
    assert out["url"] == inp["url"]
    assert out["method"] == "GET"


def test_secret_deep_nesting():
    inp = {
        "request": {
            "config": {
                "auth": {
                    "token": "sk-" + "9" * 48,
                }
            }
        }
    }
    out = redact_json(inp)
    assert "[REDACTED" in out["request"]["config"]["auth"]["token"]


def test_list_of_dicts_with_mixed_secrets():
    inp = [
        {"name": "first", "token": "ghp_" + "A" * 36},
        {"name": "second", "token": "safe"},
        {"name": "third", "nested": {"key": "AKIA" + "B" * 16}},
    ]
    out = redact_json(inp)
    assert "[REDACTED:github_token]" in out[0]["token"]
    assert out[1]["token"] == "safe"
    assert "[REDACTED:aws_key]" in out[2]["nested"]["key"]


def test_keys_are_not_redacted_only_values():
    # If a dict has a secret-shaped KEY, leave it (keys are usually
    # legitimate identifiers; values carry the secret content).
    inp = {"sk-ant-api03-key-name": "normal-value"}
    out = redact_json(inp)
    # Key unchanged.
    assert list(out.keys()) == ["sk-ant-api03-key-name"]


# ── Idempotency + immutability ─────────────────────────────────────────

def test_idempotent_on_already_redacted_content():
    once = redact_json({"a": "ghp_" + "X" * 36})
    twice = redact_json(once)
    assert once == twice


def test_input_dict_is_not_mutated():
    inp = {"token": "ghp_" + "X" * 36}
    original_token = inp["token"]
    _ = redact_json(inp)
    assert inp["token"] == original_token  # Original unchanged.


def test_input_list_is_not_mutated():
    inp = ["ghp_" + "X" * 36, "safe"]
    original_first = inp[0]
    _ = redact_json(inp)
    assert inp[0] == original_first


# ── Parity with redact_text on plain strings ───────────────────────────

@pytest.mark.parametrize("secret", [
    "sk-ant-api03-" + "A" * 80,
    "ghp_" + "B" * 36,
    "hf_" + "C" * 30,
    "AKIA" + "D" * 16,
    "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
])
def test_redact_json_on_string_matches_redact_text(secret: str):
    assert redact_json(secret) == redact_text(secret)


# ── Webhook URLs (Slack/Discord) — added after a real Slack webhook
#    leaked into a training dataset and was caught by GitHub push protection.
#
# Test URLs are assembled from concatenated parts at runtime so the literal
# webhook host strings never appear in this file's source. Otherwise
# GitHub secret-scanning push protection flags the test file itself, even
# though every URL here is a synthetic fixture. ──

_SLACK_HOST = "hooks." + "slack" + ".com"
_DISCORD_HOST = "dis" + "cord.com"
_DISCORDAPP_HOST = "dis" + "cordapp.com"


def _slack_webhook(host=_SLACK_HOST):
    return f"https://{host}/services/T1ABCDEFG/B0H1JKLMN/abcdef1234567890ABCDEFGH"


def _discord_webhook(host=_DISCORD_HOST):
    return (
        f"https://{host}/api/webhooks/123456789012345678/"
        "abcdefghijklmnopqrstuvwxyz0123456789-_ABCDEFGHIJ"
    )


def test_slack_incoming_webhook_url_is_redacted():
    out = redact_text(_slack_webhook())
    assert _SLACK_HOST not in out
    assert "[REDACTED:slack_webhook]" in out


def test_slack_webhook_redacted_inside_bash_command():
    cmd = (
        "curl -X POST -H 'Content-type: application/json' "
        "--data '{}' " + _slack_webhook()
    )
    out = redact_text(cmd)
    assert _SLACK_HOST not in out
    assert "[REDACTED:slack_webhook]" in out


def test_slack_webhook_redacted_nested_in_json():
    obj = {"command": "curl " + _slack_webhook()}
    out = redact_json(obj)
    assert _SLACK_HOST not in out["command"]


def test_discord_webhook_url_is_redacted():
    out = redact_text(_discord_webhook())
    assert _DISCORD_HOST + "/api/webhooks" not in out
    assert "[REDACTED:discord_webhook]" in out


def test_discordapp_webhook_url_is_redacted():
    """Old discord-app hostname still issues webhooks for legacy bots."""
    out = redact_text(_discord_webhook(_DISCORDAPP_HOST))
    assert _DISCORDAPP_HOST not in out
    assert "[REDACTED:discord_webhook]" in out


# ── JWTs ─────────────────────────────────────────────────────────────────
#
# Real-world failure: a signed HS256 JWT (expired) was caught at PR #51 audit
# embedded in an assistant tool_call argument in
# datasets/v5_pilot/train.jsonl:4100. The model would have been trained to
# emit it verbatim. The header prefix "eyJ" is structural (base64 of
# `{"alg":...`), so it's reliable as a detector. Test JWTs here are
# synthetic and assembled at runtime to avoid secret-scanning false alarms.


def _synthetic_jwt():
    # Header / payload / sig — each segment is well past the 10-char
    # min so the regex matches. Real JWTs are much longer; the lower
    # bound exists to avoid eating short dotted identifiers like
    # "eyJ.eyJ.x" in arbitrary text.
    return (
        "ey" + "Jhbgci0iJIUzI1NiIsInR5c"
        + "."
        + "ey" + "JzdWIiOiIxMjM0NTY3ODk"
        + "."
        + "_signature-part-here-padded"
    )


def test_jwt_token_is_redacted():
    out = redact_text(_synthetic_jwt())
    assert "[REDACTED:jwt]" in out
    assert "eyJ" not in out


def test_jwt_nested_in_tool_call_arguments_is_redacted():
    """Mirror of the real PR #51 leak: JWT inside an assistant tool_call."""
    obj = {
        "role": "assistant",
        "tool_calls": [
            {
                "function": {
                    "name": "Bash",
                    "arguments": {
                        "command": "curl -H 'Authorization: Bearer " + _synthetic_jwt() + "' https://api.example",
                    },
                }
            }
        ],
    }
    out = redact_json(obj)
    cmd = out["tool_calls"][0]["function"]["arguments"]["command"]
    # Bearer pattern catches the whole "Bearer <token>" first; either way
    # the literal JWT must not survive.
    assert "eyJ" not in cmd


def test_short_dotted_string_is_not_treated_as_jwt():
    """Don't false-positive on short dotted identifiers."""
    out = redact_text("eyJ.eyJ.x")
    assert "[REDACTED:jwt]" not in out


# ── Session cookies ──────────────────────────────────────────────────────


def test_session_cookie_hex_value_is_redacted():
    # 32-char lowercase hex
    out = redact_text("session: fb97243eb480448595be8e3ed7f93704")
    assert "[REDACTED:session_cookie]" in out
    assert "fb97243eb480448595be8e3ed7f93704" not in out


def test_jsessionid_is_redacted():
    out = redact_text("JSESSIONID=abcdef0123456789abcdef0123456789")
    assert "[REDACTED:session_cookie]" in out


def test_git_sha_is_not_redacted_as_session_cookie():
    """Bare 40-char hex must not be eaten — it could be a git SHA."""
    out = redact_text("commit abcdef0123456789abcdef0123456789abcdef01")
    assert "[REDACTED:session_cookie]" not in out
