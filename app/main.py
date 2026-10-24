from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload

from app.config import ROOT, settings
from app.db import Base, ENGINE, get_db, session_scope
from app.evals import compare_runs, load_cases, run_eval
from app.llm import llm_available
from app.models import Chunk, Document, EvalRun, Fact
from app.qa import ask, fy_breakdown
from app.worker import (
    STAGE_LABELS,
    STAGE_PROGRESS,
    accept_upload,
    process_one,
    recover_stale_jobs,
    retry_document,
    seed_dataset_results,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("kuki")
STATIC = ROOT / "static"
STOP = asyncio.Event()


async def worker_loop():
    await asyncio.sleep(0.4)
    while not STOP.is_set():
        try:
            with session_scope() as db:
                did = process_one(db)
        except Exception:
            log.exception("worker tick failed")
            did = False
        try:
            await asyncio.wait_for(STOP.wait(), timeout=0.4 if did else 1.0)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=ENGINE)
    with session_scope() as db:
        n = recover_stale_jobs(db)
        if n:
            log.info("Re-queued %s stale jobs after restart", n)
    STOP.clear()
    task = asyncio.create_task(worker_loop())
    yield
    STOP.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


app = FastAPI(title="Sustainability Data Assistant", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "llm": llm_available(),
        "db": str(settings.data_dir / "kuki.db"),
        "max_attempts": settings.max_attempts,
    }


@app.get("/api/documents")
def list_documents(db: Session = Depends(get_db)):
    docs = db.query(Document).order_by(Document.created_at.desc()).all()
    return [_doc_out(d, db) for d in docs]


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    return _doc_out(doc, db, detail=True)


@app.get("/api/documents/{doc_id}/facts")
def document_facts(doc_id: str, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    facts = db.query(Fact).filter(Fact.document_id == doc_id).all()
    return [_fact_out(f) for f in facts]


@app.get("/api/documents/{doc_id}/chunks")
def document_chunks(doc_id: str, db: Session = Depends(get_db)):
    chunks = db.query(Chunk).filter(Chunk.document_id == doc_id).order_by(Chunk.ordinal).all()
    return [{"id": c.id, "locator": c.locator, "loc_type": c.loc_type, "text": c.text[:2000]} for c in chunks]


@app.post("/api/documents/upload")
async def upload(files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    if not files:
        raise HTTPException(400, "Choose at least one file")
    seen: set[str] = set()
    items = []
    for file in files:
        name = file.filename or "upload.bin"
        try:
            data = await file.read()
            result = accept_upload(db, name, file.content_type or "application/octet-stream", data, seen)
            db.commit()
            items.append(_batch_out(result, db))
        except Exception as exc:
            db.rollback()
            log.exception("Upload failed for %s", name)
            items.append({"filename": name, "outcome": "rejected", "error": str(exc)[:500], "document": None})
    return _batch_response(items)


@app.post("/api/documents/{doc_id}/retry")
def retry(doc_id: str, db: Session = Depends(get_db)):
    doc = retry_document(db, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    return _doc_out(doc, db)


@app.post("/api/seed")
def seed(db: Session = Depends(get_db)):
    items = seed_dataset_results(db)
    payload = _batch_response([_batch_out(item, db) for item in items])
    payload["seeded"] = payload["queued"] + payload["requeued"] + payload["already_queued"]
    return payload


@app.post("/api/ask")
def ask_endpoint(
    payload: dict,
    db: Session = Depends(get_db),
):
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    mode = payload.get("mode") or "improved"
    if mode not in {"baseline", "improved"}:
        raise HTTPException(400, "mode must be baseline or improved")
    return ask(db, question, mode=mode, fy=payload.get("fy"), document_ids=payload.get("document_ids"))


@app.get("/api/fy/{label}")
def fy_view(label: str, db: Session = Depends(get_db)):
    try:
        return fy_breakdown(db, label)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/eval/cases")
def eval_cases():
    return load_cases()


@app.post("/api/eval/run")
def eval_run(payload: dict | None = None, db: Session = Depends(get_db)):
    payload = payload or {}
    mode = payload.get("mode") or "improved"
    if mode not in {"baseline", "improved"}:
        raise HTTPException(400, "mode must be baseline or improved")
    pending = db.query(Document).filter(Document.status.in_(["queued", "processing"])).count()
    if pending:
        raise HTTPException(409, f"{pending} documents are still processing")
    if db.query(Document).filter(Document.status == "completed").count() == 0:
        raise HTTPException(409, "No completed documents. Seed or upload first.")
    return run_eval(db, mode=mode, label=payload.get("label") or mode)


@app.get("/api/eval/results")
def eval_results(db: Session = Depends(get_db)):
    runs = (
        db.query(EvalRun)
        .options(joinedload(EvalRun.results))
        .order_by(EvalRun.created_at.desc())
        .limit(20)
        .all()
    )
    items = []
    for run in runs:
        results = []
        for row in run.results or []:
            try:
                payload = json.loads(row.payload_json) if row.payload_json else {}
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload["score"] = row.score
            payload["passed"] = bool(row.passed)
            payload["case_id"] = payload.get("case_id") or row.case_id
            results.append(payload)
        try:
            summary = json.loads(run.summary_json) if run.summary_json else {}
        except json.JSONDecodeError:
            summary = {}
        items.append(
            {
                "id": run.id,
                "label": run.label,
                "mode": run.mode,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "summary": summary,
                "results": results,
            }
        )
    baseline = next((i for i in items if i["mode"] == "baseline"), None)
    improved = next((i for i in items if i["mode"] == "improved"), None)
    comparison = compare_runs(baseline or {}, improved or {}) if baseline and improved else None
    if not comparison:
        saved = ROOT / "evals" / "comparison.json"
        if saved.exists():
            try:
                comparison = json.loads(saved.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                comparison = None
    return {"runs": items, "comparison": comparison}


def _batch_out(result: dict, db: Session) -> dict:
    doc = result.get("document")
    return {
        "filename": result.get("filename"),
        "outcome": result.get("outcome"),
        "error": result.get("error"),
        "already_processed": result.get("outcome") == "duplicate",
        "document": _doc_out(doc, db) if doc else None,
    }


def _batch_response(items: list[dict]) -> dict:
    def count(key: str) -> int:
        return sum(1 for i in items if i.get("outcome") == key)

    return {
        "queued": count("queued") + count("requeued"),
        "requeued": count("requeued"),
        "duplicates": count("duplicate") + count("already_queued"),
        "already_queued": count("already_queued"),
        "rejected": count("rejected"),
        "documents": [i["document"] for i in items if i.get("document")],
        "items": items,
    }


def _doc_out(doc: Document, db: Session, detail: bool = False) -> dict:
    n_facts = db.query(Fact).filter(Fact.document_id == doc.id).count()
    n_chunks = db.query(Chunk).filter(Chunk.document_id == doc.id).count()
    stage = doc.stage or doc.status or "queued"
    data = {
        "id": doc.id,
        "filename": doc.filename,
        "status": doc.status,
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, stage.replace("_", " ")),
        "progress": STAGE_PROGRESS.get(stage, 0),
        "attempts": doc.attempts,
        "max_attempts": settings.max_attempts,
        "error": doc.error,
        "doc_type": doc.doc_type,
        "organization": doc.organization,
        "size_bytes": doc.size_bytes,
        "sha256": doc.sha256,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        "completed_at": doc.completed_at.isoformat() if doc.completed_at else None,
        "n_facts": n_facts,
        "n_chunks": n_chunks,
    }
    if detail:
        data["facts"] = [_fact_out(f) for f in db.query(Fact).filter(Fact.document_id == doc.id).all()]
    return data


def _fact_out(f: Fact) -> dict:
    extra = None
    if f.extra_json:
        try:
            extra = json.loads(f.extra_json)
        except json.JSONDecodeError:
            extra = f.extra_json
    return {
        "id": f.id,
        "name": f.name,
        "role": f.role,
        "value_num": f.value_num,
        "value_text": f.value_text,
        "unit": f.unit,
        "period_start": f.period_start,
        "period_end": f.period_end,
        "locator": f.locator,
        "quote": f.quote,
        "provenance": f.provenance,
        "confidence": f.confidence,
        "extra": extra,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=False)
