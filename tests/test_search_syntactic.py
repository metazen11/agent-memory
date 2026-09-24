"""Tests for syntactic (literal/exact) search.

Semantic search answers "what was this about". It cannot answer "where does
this exact string appear" — for `old_string matches` it returned 0/3 rows
containing the phrase while literal mode returned 3/3. Symbols, file:line
refs, config keys and error strings need the syntactic path.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "foo()",            # parentheses
    "a & b",            # tsquery AND operator
    "file.py:42",       # colon — tsquery weight-label syntax
    "a | b",            # tsquery OR operator
    "!negated",         # tsquery NOT operator
    "unbalanced(",      # malformed
])
async def test_punctuation_queries_do_not_500(client, query):
    """User punctuation must never reach tsquery as syntax.

    Regression: the FTS path used to_tsquery(), which parses raw tsquery
    SYNTAX. Any query containing ( ) & | : ! raised a syntax error that
    surfaced as a 500 — so searching for a function name crashed the API.
    websearch_to_tsquery() parses human input and never throws.
    """
    resp = await client.post("/api/observations/search", json={
        "query": query, "mode": "fts", "limit": 2,
    })
    assert resp.status_code == 200, f"{query!r} returned {resp.status_code}"


@pytest.mark.asyncio
async def test_literal_mode_returns_only_exact_substring_matches(client):
    """Every literal-mode hit must actually contain the query string."""
    needle = "CLAUDE_PLUGIN_ROOT"
    resp = await client.post("/api/observations/search", json={
        "query": needle, "mode": "literal", "limit": 5,
    })
    assert resp.status_code == 200
    observations = resp.json()["observations"]
    if not observations:
        pytest.skip("no observations containing the probe string")
    for obs in observations:
        blob = " ".join(str(obs.get(f) or "") for f in
                        ("title", "subtitle", "narrative"))
        blob += " " + " ".join(str(x) for x in (obs.get("facts") or []))
        assert needle.lower() in blob.lower(), (
            f"literal hit {obs['id']} does not contain {needle!r}"
        )


@pytest.mark.asyncio
async def test_literal_mode_matches_multiword_phrase_intact(client):
    """A multi-word phrase must match as a phrase, not as OR-ed terms.

    The pre-existing keyword pass splits on whitespace and ORs the terms,
    so a row sharing one common word could outrank an exact phrase match.
    """
    phrase = "old_string matches"
    resp = await client.post("/api/observations/search", json={
        "query": phrase, "mode": "literal", "limit": 3,
    })
    assert resp.status_code == 200
    observations = resp.json()["observations"]
    if not observations:
        pytest.skip("no observations containing the probe phrase")
    import json as _json
    for obs in observations:
        assert phrase.lower() in _json.dumps(obs).lower()


@pytest.mark.asyncio
async def test_literal_mode_respects_type_filter(client):
    """Filters still apply in literal mode."""
    resp = await client.post("/api/observations/search", json={
        "query": "the", "mode": "literal", "type": ["gotcha"], "limit": 5,
    })
    assert resp.status_code == 200
    for obs in resp.json()["observations"]:
        assert obs["type"] == "gotcha"


@pytest.mark.asyncio
async def test_empty_literal_query_is_handled(client):
    resp = await client.post("/api/observations/search", json={
        "query": "   ", "mode": "literal", "limit": 3,
    })
    assert resp.status_code == 200
