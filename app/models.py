from datetime import datetime
from pydantic import BaseModel, Field


# ── Observation types ─────────────────────────────────

OBSERVATION_TYPES = (
    "decision", "bugfix", "feature", "refactor",
    "discovery", "change", "pattern", "gotcha",
)

CONCEPT_TAGS = (
    "how-it-works", "why-it-exists", "what-changed",
    "problem-solution", "gotcha", "pattern", "trade-off",
)


# ── Queue ingest (from hooks) ────────────────────────

class QueueItem(BaseModel):
    """Payload from post-tool-use hook."""
    session_id: str
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_response_preview: str | None = None
    cwd: str | None = None
    last_user_message: str | None = None


# ── Observation schemas ───────────────────────────────

class ObservationCreate(BaseModel):
    """Direct observation creation (bypasses queue)."""
    session_id: str
    project: str
    title: str
    subtitle: str | None = None
    type: str = "discovery"
    narrative: str | None = None
    facts: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    tool_name: str | None = None
    prompt_number: int | None = None


class ObservationOut(BaseModel):
    """Observation returned from API."""
    id: int
    session_id: int
    project_id: int
    project_name: str | None = None
    title: str
    subtitle: str | None = None
    type: str
    narrative: str | None = None
    facts: list = Field(default_factory=list)
    concepts: list = Field(default_factory=list)
    files_read: list = Field(default_factory=list)
    files_modified: list = Field(default_factory=list)
    tool_name: str | None = None
    prompt_number: int | None = None
    has_embedding: bool = False
    created_at: datetime
    score: float | None = None


# ── Search ────────────────────────────────────────────

class SearchRequest(BaseModel):
    """Hybrid search request."""
    query: str
    project: str | None = None
    cross_project: bool = False
    type: list[str] | None = None
    limit: int = 10
    mode: str = "hybrid"  # "vector" | "fts" | "hybrid"


class SearchResult(BaseModel):
    """Search result with relevance score."""
    observations: list[ObservationOut]
    query: str
    mode: str
    total: int


# ── Session schemas ───────────────────────────────────

# ── Lesson schemas ───────────────────────────────────

LESSON_SEVERITIES = ("critical", "warning", "info")


class LessonCreate(BaseModel):
    """Create a new lesson."""
    title: str
    rule: str
    severity: str = "warning"
    project: str | None = None  # None = global
    trigger_tool: str | None = None
    trigger_pattern: str | None = None
    source_observation_id: int | None = None


class LessonUpdate(BaseModel):
    """Update an existing lesson."""
    title: str | None = None
    rule: str | None = None
    severity: str | None = None
    trigger_tool: str | None = None
    trigger_pattern: str | None = None
    active: bool | None = None


class LessonOut(BaseModel):
    """Lesson returned from API."""
    id: int
    project_id: int | None = None
    project_name: str | None = None
    title: str
    rule: str
    severity: str
    trigger_tool: str | None = None
    trigger_pattern: str | None = None
    source_observation_id: int | None = None
    trigger_count: int = 0
    last_triggered_at: datetime | None = None
    active: bool = True
    created_at: datetime


class LessonMatch(BaseModel):
    """Lesson match result for PreToolUse hook."""
    id: int
    title: str
    rule: str
    severity: str
    project_name: str | None = None
    trigger_count: int = 0


# ── Session schemas ───────────────────────────────────

class SessionCreate(BaseModel):
    """Start a new session."""
    session_id: str
    project: str
    project_path: str | None = None
    agent_type: str = "claude-code"


class SessionUpdate(BaseModel):
    """End/update a session."""
    status: str | None = None
    summary: str | None = None


class SessionOut(BaseModel):
    """Session returned from API."""
    id: int
    session_id: str
    project_id: int
    project_name: str | None = None
    agent_type: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    summary: str | None = None
    prompt_count: int
