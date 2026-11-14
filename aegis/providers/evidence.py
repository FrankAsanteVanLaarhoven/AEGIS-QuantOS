"""Bitemporal SourceDocument contract, entity resolution, and hostile-document sanitization."""
from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

INJECTION_PATTERNS = (
    re.compile(r'<\s*script[^>]*>.*?<\s*/\s*script\s*>', re.IGNORECASE | re.DOTALL),
    re.compile(r'ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions', re.IGNORECASE),
    re.compile(r'(?:system|assistant|developer)\s*:\s*', re.IGNORECASE),
    re.compile(r'\b(?:broker_submit|change_risk_policy|read_secret|arbitrary_shell)\b', re.IGNORECASE),
)


def sanitize_untrusted_text(raw: str) -> tuple[str, list[str]]:
    """Strip HTML scripts and neutralize prompt-injection directives in external documents."""
    flags: list[str] = []
    cleaned = raw
    for pat in INJECTION_PATTERNS:
        if pat.search(cleaned):
            flags.append(pat.pattern[:48])
            cleaned = pat.sub(' [SANITIZED_DIRECTIVE] ', cleaned)
    cleaned = re.sub(r'<[^>]+>', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned, flags


class SourceDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')

    doc_id: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    source_kind: Literal[
        'sec_filing',
        'sec_company_facts',
        'licensed_news',
        'earnings',
        'congressional_disclosure',
        'prediction_market',
    ]
    content_hash: str = Field(min_length=16)
    first_observed_at: datetime
    published_at: datetime
    accepted_at: datetime
    available_at: datetime
    version: int = Field(default=1, ge=1)
    supersedes_doc_id: str | None = None
    extracted_text: str
    sanitized_text: str
    injection_flags: list[str] = Field(default_factory=list)
    licensing_tag: str = Field(min_length=1)
    entity_symbol: str | None = None
    entity_cik: str | None = None
    entity_certainty: float = Field(default=1.0, ge=0.0, le=1.0)
    point_in_time_eligible: bool = True

    @classmethod
    def from_raw(
        cls,
        *,
        doc_id: str,
        uri: str,
        provider: str,
        source_kind: Literal[
            'sec_filing',
            'sec_company_facts',
            'licensed_news',
            'earnings',
            'congressional_disclosure',
            'prediction_market',
        ],
        raw_text: str,
        published_at: datetime,
        accepted_at: datetime,
        available_at: datetime | None = None,
        first_observed_at: datetime | None = None,
        licensing_tag: str,
        entity_symbol: str | None = None,
        entity_cik: str | None = None,
        entity_certainty: float = 1.0,
        version: int = 1,
        supersedes_doc_id: str | None = None,
    ) -> 'SourceDocument':
        now = datetime.now(timezone.utc)
        avail = available_at or accepted_at
        if avail < accepted_at:
            avail = accepted_at
        sanitized, flags = sanitize_untrusted_text(raw_text)
        return cls(
            doc_id=doc_id,
            uri=uri,
            provider=provider,
            source_kind=source_kind,
            content_hash=hashlib.sha256(raw_text.encode('utf-8')).hexdigest(),
            first_observed_at=first_observed_at or now,
            published_at=published_at,
            accepted_at=accepted_at,
            available_at=avail,
            version=version,
            supersedes_doc_id=supersedes_doc_id,
            extracted_text=raw_text,
            sanitized_text=sanitized,
            injection_flags=flags,
            licensing_tag=licensing_tag,
            entity_symbol=entity_symbol,
            entity_cik=entity_cik,
            entity_certainty=entity_certainty,
        )


class EvidenceStore:
    """Bitemporal document store (in-memory + optional SQLite Store) preserving amendments and point-in-time cutoffs."""

    def __init__(self, store: Any = None) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._docs: dict[str, SourceDocument] = {}
        self._ensure_schema()
        self._load_all()

    def _ensure_schema(self) -> None:
        if self._store is None:
            return
        with self._store.lock:
            self._store.db.executescript(
                'CREATE TABLE IF NOT EXISTS evidence_documents (doc_id TEXT PRIMARY KEY, doc_json TEXT NOT NULL);'
            )

    def _load_all(self) -> None:
        if self._store is None:
            return
        with self._store.lock:
            rows = self._store.db.execute('SELECT doc_id, doc_json FROM evidence_documents').fetchall()
            self._docs = {r['doc_id']: SourceDocument.model_validate_json(r['doc_json']) for r in rows}

    def _persist(self, doc: SourceDocument) -> None:
        self._docs[doc.doc_id] = doc
        if self._store is None:
            return
        with self._store.tx():
            self._store.db.execute(
                'INSERT INTO evidence_documents(doc_id, doc_json) VALUES (?, ?) '
                'ON CONFLICT(doc_id) DO UPDATE SET doc_json = excluded.doc_json',
                (doc.doc_id, doc.model_dump_json()),
            )

    def add(self, doc: SourceDocument) -> SourceDocument:
        with self._lock:
            self._load_all()
            if doc.doc_id in self._docs:
                existing = self._docs[doc.doc_id]
                if existing.content_hash == doc.content_hash:
                    return existing
                # Create a new versioned record rather than overwriting
                new_ver = existing.version + 1
                amended = doc.model_copy(
                    update={
                        'doc_id': f'{doc.doc_id}:v{new_ver}',
                        'version': new_ver,
                        'supersedes_doc_id': existing.doc_id,
                    }
                )
                self._persist(amended)
                return amended
            self._persist(doc)
            return doc

    def as_of(
        self,
        cutoff: datetime,
        *,
        symbol: str | None = None,
        source_kind: str | None = None,
    ) -> list[SourceDocument]:
        cut_utc = cutoff.astimezone(timezone.utc) if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
        with self._lock:
            self._load_all()
            out = []
            for doc in self._docs.values():
                if not doc.point_in_time_eligible:
                    continue
                if doc.available_at.astimezone(timezone.utc) > cut_utc:
                    continue
                if symbol and doc.entity_symbol != symbol.upper():
                    continue
                if source_kind and doc.source_kind != source_kind:
                    continue
                out.append(doc)
            return sorted(out, key=lambda d: (d.available_at, d.version))

    def all_documents(self) -> list[SourceDocument]:
        with self._lock:
            self._load_all()
            return list(self._docs.values())

