"""Background job worker."""

from __future__ import annotations

import hashlib
import logging
import traceback
from datetime import timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.db import session_scope
from app.extract import extract_facts, postprocess_facts
from app.ingest import ingest_file
from app.models import Chunk, Document, Fact, Job, utcnow

log = logging.getLogger("kuki.worker")

ALLOWED_SUFFIXES = {".pdf", ".xlsx", ".xls"}
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
STAGE_PROGRESS = {
    "queued": 8,
    "claimed": 20,
    "ingesting": 50,
    "extracting": 80,
    "retry_wait": 18,
    "completed": 100,
    "failed": 100,
}
STAGE_LABELS = {
    "queued": "Waiting in queue",
    "claimed": "Starting",
    "ingesting": "Reading file",
    "extracting": "Extracting facts",
    "retry_wait": "Retrying after a temporary error",
    "completed": "Completed",
    "failed": "Failed",
}


def accept_upload(
    db: Session,
    filename: str,
    content_type: str,
    data: bytes,
    seen_hashes: set[str] | None = None,
) -> dict:
    """Queue one file. A bad file must not block the rest of a batch."""
    name = filename or "upload.bin"
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        return _batch_item(name, "rejected", error="Only PDF and Excel files are accepted")
    if not data:
        return _batch_item(name, "rejected", error="Empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        return _batch_item(name, "rejected", error="File is larger than 40 MB")

    digest = hashlib.sha256(data).hexdigest()
    if seen_hashes is not None:
        if digest in seen_hashes:
            return _batch_item(name, "duplicate", error="Same file already included in this batch", sha256=digest)
        seen_hashes.add(digest)

    existing = db.query(Document).filter(Document.sha256 == digest).first()
    if existing and existing.status == "completed":
        return _batch_item(name, "duplicate", document=existing, sha256=digest)
    if existing and existing.status in {"queued", "processing"}:
        return _batch_item(name, "already_queued", document=existing, sha256=digest)

    was_failed = bool(existing and existing.status == "failed")
    doc = enqueue_upload(db, name, content_type, data)
    return _batch_item(name, "requeued" if was_failed else "queued", document=doc, sha256=digest)


def enqueue_upload(db: Session, filename: str, content_type: str, data: bytes, force: bool = False) -> Document:
    digest = hashlib.sha256(data).hexdigest()
    existing = db.query(Document).filter(Document.sha256 == digest).first()
    if existing and not force:
        if existing.status in {"failed"}:
            _queue_job(db, existing, reset=True)
            existing.status = "queued"
            existing.stage = "queued"
            existing.error = None
            existing.updated_at = utcnow()
        return existing

    stored = settings.data_dir / "uploads" / f"{digest[:16]}_{_safe_name(filename)}"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(data)
    doc = Document(
        filename=filename,
        stored_path=str(stored),
        content_type=content_type or "application/octet-stream",
        sha256=digest,
        size_bytes=len(data),
        status="queued",
        stage="queued",
    )
    db.add(doc)
    db.flush()
    _queue_job(db, doc)
    return doc


def seed_dataset(db: Session) -> list[Document]:
    return [item["document"] for item in seed_dataset_results(db) if item.get("document")]


def seed_dataset_results(db: Session) -> list[dict]:
    folder = settings.dataset_dir
    if not folder.exists():
        return []
    seen: set[str] = set()
    items = []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        try:
            items.append(accept_upload(db, path.name, _guess_mime(path), path.read_bytes(), seen))
            db.commit()
        except Exception as exc:
            db.rollback()
            log.exception("Failed to queue %s", path.name)
            items.append(_batch_item(path.name, "rejected", error=str(exc)[:500]))
    return items


def _batch_item(
    filename: str,
    outcome: str,
    document: Document | None = None,
    error: str | None = None,
    sha256: str | None = None,
) -> dict:
    return {
        "filename": filename,
        "outcome": outcome,
        "error": error,
        "sha256": sha256 or (document.sha256 if document else None),
        "document": document,
    }


def recover_stale_jobs(db: Session) -> int:
    cutoff = utcnow() - timedelta(seconds=settings.stale_processing_seconds)
    n = 0
    for job in db.query(Job).filter(Job.status.in_(["processing", "running"])).all():
        locked = job.locked_at.replace(tzinfo=timezone.utc) if job.locked_at and job.locked_at.tzinfo is None else job.locked_at
        if locked is None or locked < cutoff:
            job.status = "queued"
            job.locked_at = None
            job.run_after = utcnow()
            doc = db.get(Document, job.document_id)
            if doc and doc.status == "processing":
                doc.status = "queued"
                doc.stage = "queued"
            n += 1
    return n


def process_one(db: Session) -> bool:
    now = utcnow()
    job = (
        db.query(Job)
        .filter(Job.status == "queued", Job.run_after <= now)
        .order_by(Job.run_after.asc())
        .first()
    )
    if not job:
        return False
    job.status = "processing"
    job.locked_at = now
    job.attempts += 1
    doc = db.get(Document, job.document_id)
    if not doc:
        job.status = "failed"
        job.last_error = "Document missing"
        return True
    doc.status = "processing"
    doc.stage = "claimed"
    doc.attempts = job.attempts
    doc.updated_at = now
    db.commit()
    try:
        _process_document(db, doc)
        job.status = "completed"
        job.locked_at = None
        job.last_error = None
        db.commit()
    except Exception as exc:
        db.rollback()
        # re-fetch after rollback
        job = db.get(Job, job.id)
        doc = db.get(Document, job.document_id)
        err = f"{exc}\n{traceback.format_exc()}"
        log.exception("Processing failed for %s", doc.filename if doc else job.document_id)
        if job.attempts >= settings.max_attempts:
            job.status = "failed"
            if doc:
                doc.status = "failed"
                doc.error = str(exc)[:2000]
                doc.stage = "failed"
        else:
            job.status = "queued"
            job.run_after = utcnow() + timedelta(seconds=min(60, 2 ** job.attempts))
            if doc:
                doc.status = "queued"
                doc.error = str(exc)[:2000]
                doc.stage = "retry_wait"
        job.last_error = err[:4000]
        job.locked_at = None
        if doc:
            doc.updated_at = utcnow()
        db.commit()
    return True


def retry_document(db: Session, document_id: str) -> Document | None:
    doc = db.get(Document, document_id)
    if not doc:
        return None
    _queue_job(db, doc, reset=True)
    doc.status = "queued"
    doc.stage = "queued"
    doc.error = None
    doc.updated_at = utcnow()
    return doc


def _process_document(db: Session, doc: Document) -> None:
    path = Path(doc.stored_path)
    if not path.exists():
        raise FileNotFoundError(doc.stored_path)

    doc.stage = "ingesting"
    doc.updated_at = utcnow()
    db.commit()
    ingested = ingest_file(path, doc.filename)

    db.query(Chunk).filter(Chunk.document_id == doc.id).delete()
    for i, chunk in enumerate(ingested.get("chunks") or []):
        db.add(
            Chunk(
                document_id=doc.id,
                loc_type=chunk.get("loc_type") or "page",
                locator=chunk.get("locator") or f"part {i}",
                text=chunk.get("text") or "",
                ordinal=chunk.get("ordinal", i),
            )
        )
    db.flush()

    doc.stage = "extracting"
    doc.updated_at = utcnow()
    db.commit()
    extracted = extract_facts(path, doc.filename, ingested)
    facts = postprocess_facts(extracted.get("facts") or [])
    db.query(Fact).filter(Fact.document_id == doc.id).delete()
    for fact in facts:
        db.add(
            Fact(
                document_id=doc.id,
                name=fact["name"],
                role=fact.get("role") or "other",
                value_num=fact.get("value_num"),
                value_text=fact.get("value_text"),
                unit=fact.get("unit"),
                period_start=fact.get("period_start"),
                period_end=fact.get("period_end"),
                locator=fact.get("locator") or "",
                quote=fact.get("quote"),
                provenance=fact.get("provenance") or "reported",
                confidence=fact.get("confidence") or 0.7,
                extra_json=fact.get("extra_json"),
            )
        )
    doc.doc_type = extracted.get("doc_type")
    doc.organization = extracted.get("organization")
    doc.status = "completed"
    doc.stage = "completed"
    doc.error = None
    doc.completed_at = utcnow()
    doc.updated_at = utcnow()
    db.flush()


def _queue_job(db: Session, doc: Document, reset: bool = False) -> Job:
    open_job = (
        db.query(Job)
        .filter(Job.document_id == doc.id, Job.status.in_(["queued", "processing"]))
        .first()
    )
    if open_job and not reset:
        return open_job
    if open_job and reset:
        open_job.status = "queued"
        open_job.attempts = 0
        open_job.last_error = None
        open_job.run_after = utcnow()
        open_job.locked_at = None
        return open_job
    job = Job(document_id=doc.id, kind="process", status="queued", run_after=utcnow())
    db.add(job)
    db.flush()
    return job


def _safe_name(name: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in name)
    return keep[:120] or "upload.bin"


def _guess_mime(path: Path) -> str:
    return {
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
    }.get(path.suffix.lower(), "application/octet-stream")
