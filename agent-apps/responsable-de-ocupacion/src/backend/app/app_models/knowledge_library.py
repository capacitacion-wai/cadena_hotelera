"""v7.84.0 — the employee's DOCUMENT KNOWLEDGE LIBRARY (file-based).

Operator-uploaded LIVE documents (catalogue, pricing, policies, requirements,
calendars, FAQs, commercial scripts…) that the employee consults for OFFICIAL
content, WITH source citation. Generic + reusable — NO domain logic. It is
DISTINCT from every other knowledge surface:

  * skills          — reusable METHOD (how it reasons / sells / classifies / decides)
  * memory          — facts the agent LEARNED, per user, from conversations
  * connectors/CRM  — live operational DATA
  * ScopedKnowledge — free-text instructions the operator pushes into the prompt
  * KnowledgeRequest— a single typed business PARAMETER (threshold/target)

Nothing here lives in ``wai-app.json``; documents update with NO redeploy. Content
is stored on the app's own PVC (``/workspace/knowledge/``) and its extracted text
is indexed in SQLite FTS5 (``knowledge_fts.py``) — self-contained, no external
service, no embeddings/vectors (so the UI never shows RAG jargon).

Two tables + an FTS5 index over the chunks:
  * ``KnowledgeDocument`` — one row per uploaded file (metadata + status + governance).
  * ``KnowledgeChunk``    — the extracted text split into searchable chunks.

INVARIANT (skill-owned, restored by ``--fix-invariants``): the library must exist
in EVERY agent-app, so it cannot live in a SCAFFOLD the generator rewrites.
"""
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, Integer, String, Text,
)

from app.app_database import AppBase

# Status of the extract→index pipeline (operator-facing labels are i18n'd).
DOC_STATUSES = ("uploading", "indexing", "ready", "error")
# Same scope axis as ScopedKnowledge, so the operator reasons about both the same
# way. ``all`` (default) = every run; ``autonomous`` = background only;
# ``attending`` = serving a human; ``user`` = one person (``user_email``).
DOC_SCOPES = ("all", "autonomous", "attending", "user")
# ``internal`` = the employee may READ it to reason, but must NEVER quote/forward
# it in an outbound channel message (internal pricing policy, margins…).
# ``shareable`` (default) = official content the employee may cite to people.
DOC_VISIBILITY = ("internal", "shareable")
# Extensions we can extract text from in-app (no OCR → scanned PDFs are rejected).
SUPPORTED_EXTS = ("pdf", "docx", "txt", "md", "markdown", "csv", "xlsx")


class KnowledgeDocument(AppBase):
    """One uploaded knowledge file + its extraction/index status + governance."""
    __tablename__ = "knowledge_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String, nullable=False)            # sanitised name stored on disk
    title = Column(String, nullable=False)               # operator-facing (defaults to the original name)
    mime = Column(String, nullable=True)
    ext = Column(String, nullable=True)                  # pdf|docx|txt|md|csv|xlsx
    size_bytes = Column(Integer, nullable=False, default=0)
    workspace_path = Column(String, nullable=False)      # relative to /workspace (e.g. knowledge/tarifas.pdf)

    status = Column(String, nullable=False, default="uploading", index=True)
    error = Column(Text, nullable=True)                  # human reason when status == 'error'
    enabled = Column(Integer, nullable=False, default=1)  # 0/1 (sqlite-friendly)
    scope = Column(String, nullable=False, default="all", index=True)
    user_email = Column(String, nullable=True, index=True)  # required when scope == 'user'
    visibility = Column(String, nullable=False, default="shareable")  # internal|shareable
    tags = Column(String, nullable=True)                 # comma-separated operator tags; automations'
                                                         # ``requires_knowledge`` matches against these + FTS
    summary = Column(Text, nullable=True)                # short extracted preview (first clean text)
    char_count = Column(Integer, nullable=False, default=0)
    chunk_count = Column(Integer, nullable=False, default=0)
    use_count = Column(Integer, nullable=False, default=0)   # times cited as a source
    last_used_at = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)        # optional staleness hint shown to the operator

    uploaded_by = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class KnowledgeChunk(AppBase):
    """A searchable slice of one document's extracted text. FTS5 indexes ``text``
    (see ``knowledge_fts.py``); the search joins back here → the document for the
    enabled/status/scope/visibility filters + the source citation."""
    __tablename__ = "knowledge_chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(
        Integer, ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    ordinal = Column(Integer, nullable=False, default=0)  # order within the document
    text = Column(Text, nullable=False, default="")

    __table_args__ = (
        Index("ix_knowledge_chunks_doc_ord", "document_id", "ordinal"),
    )
