"""Run baseline and improved evals."""

from __future__ import annotations

import json
import time

from app.db import Base, ENGINE, session_scope
from app.evals import compare_runs, run_eval
from app.models import Document
from app.worker import process_one, recover_stale_jobs, seed_dataset


def wait_until_idle(timeout: int = 120) -> None:
    start = time.time()
    while time.time() - start < timeout:
        with session_scope() as db:
            pending = db.query(Document).filter(Document.status.in_(["queued", "processing"])).count()
            if pending == 0:
                return
            process_one(db)
        time.sleep(0.05)
    raise TimeoutError("Documents still processing")


def main() -> None:
    Base.metadata.create_all(bind=ENGINE)
    with session_scope() as db:
        recover_stale_jobs(db)
        seed_dataset(db)
    wait_until_idle()
    with session_scope() as db:
        failed = db.query(Document).filter(Document.status == "failed").all()
        for doc in failed:
            print("FAILED", doc.filename, doc.error)
        completed = db.query(Document).filter(Document.status == "completed").count()
        print(f"Completed documents: {completed}")
        baseline = run_eval(db, "baseline", "baseline-v1")
        improved = run_eval(db, "improved", "improved-v1")
    comparison = compare_runs(baseline, improved)
    from app.config import ROOT

    path = ROOT / "evals" / "comparison.json"
    path.write_text(json.dumps(comparison, indent=2, default=str), encoding="utf-8")
    print(json.dumps(comparison["baseline"], indent=2))
    print(json.dumps(comparison["improved"], indent=2))
    print("Wrote", path)


if __name__ == "__main__":
    main()
