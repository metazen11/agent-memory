from pathlib import Path

from app.observation_identity import observation_dedupe_key
from app.project import project_path_filter_strict
from scripts import diagnose_connections
from scripts.compact_memory_index import archive_and_compact, compact_markdown


def test_strict_project_filter_never_matches_child_from_parent():
    clause, next_index = project_path_filter_strict(3)
    assert clause == "(p.full_path = $3 OR starts_with($4, p.full_path || '/'))"
    assert next_index == 5


def test_compaction_is_bounded_and_keeps_archive_friendly_headings():
    source = "# Root\n\n" + ("detail " * 1000) + "\n\n## Child\n\nmore detail"
    compacted = compact_markdown(source, 300)
    assert len(compacted) <= 300
    assert compacted.startswith("# Root\n")
    assert "detail detail" in compacted


def test_compaction_utility_is_present_and_apply_is_explicit():
    script = Path(__file__).parents[1] / "scripts" / "compact_memory_index.py"
    text = script.read_text()
    assert "archive_and_compact" in text
    assert "--apply" in text


def test_repeated_compaction_keeps_distinct_archives(tmp_path):
    source = tmp_path / "MEMORY.md"
    original = "# Root\n\nfull detail\n"
    source.write_text(original)
    first = archive_and_compact(source, 256)
    source.write_text(original)
    second = archive_and_compact(source, 256)
    assert first != second
    assert first.read_text() == original
    assert second.read_text() == original


def test_compaction_honors_cap_for_oversized_heading():
    assert len(compact_markdown("# " + ("x" * 500), 256).encode()) <= 256


def test_archive_preserves_restrictive_permissions(tmp_path):
    source = tmp_path / "MEMORY.md"
    source.write_text("# Root\n")
    source.chmod(0o600)
    archive = archive_and_compact(source, 256)
    assert archive.stat().st_mode & 0o777 == 0o600


def test_http_probe_handles_json_arrays_without_hiding_result(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"[]"

    monkeypatch.setattr(diagnose_connections.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    assert diagnose_connections.http_probe("http://127.0.0.1:1", 0.1)["http_status"] == 200


def test_direct_and_queue_identity_ignores_provenance_text():
    args = (1, "title", None, "discovery", "narrative", ["f"], [], [], [])
    assert observation_dedupe_key(*args, "direct") == observation_dedupe_key(*args, "queue provider=local")
