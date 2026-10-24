from __future__ import annotations

import re

from rank_bm25 import BM25Okapi
from sqlalchemy.orm import Session, joinedload

from app.models import Chunk, Document, Fact


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def search_chunks(db: Session, question: str, document_ids: list[str] | None, k: int = 8) -> list[dict]:
    q = db.query(Chunk, Document).join(Document, Chunk.document_id == Document.id)
    if document_ids:
        q = q.filter(Chunk.document_id.in_(document_ids))
    rows = q.all()
    if not rows:
        return []
    corpus = [tokenize(chunk.text) for chunk, _ in rows]
    if not any(corpus):
        return []
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(tokenize(question))
    ranked = sorted(zip(scores, rows), key=lambda x: x[0], reverse=True)
    out = []
    for score, (chunk, doc) in ranked[:k]:
        if score <= 0 and out:
            continue
        out.append(
            {
                "chunk_id": chunk.id,
                "document_id": doc.id,
                "filename": doc.filename,
                "organization": doc.organization,
                "locator": chunk.locator,
                "text": chunk.text[:1200],
                "score": float(score),
            }
        )
    return out


PRIORITY_NAMES = {
    "cross_sheet_conflict",
    "occupier_signature_date",
    "customer_complaint_deadline",
    "workbook_monthly_sum",
    "workbook_internal_check",
    "waste_total",
    "meter_match",
    "electricity_consumption",
    "water_consumption",
    "organization",
}
PRIORITY_ROLES = {
    "inconsistency",
    "reconciliation",
    "calculated_annual_from_months",
    "workbook_annual_total",
    "billed_consumption",
    "policy_gap",
    "calculated_total",
    "current_month_bill",
    "total_payable",
    "amount_after_due_date",
    "date",
}


def relevant_facts(db: Session, question: str, document_ids: list[str] | None, k: int = 80) -> list[Fact]:
    q = db.query(Fact).options(joinedload(Fact.document)).join(Document)
    if document_ids:
        q = q.filter(Fact.document_id.in_(document_ids))
    facts = q.all()
    tokens = set(tokenize(question))
    keywords = {
        "electricity", "kwh", "consumption", "bill", "period", "water", "meter",
        "waste", "sludge", "policy", "complaint", "deadline", "fy", "financial",
        "year", "total", "payable", "amount", "diesel", "organization", "quality",
        "renewable", "grid", "solar", "workbook", "monthly", "quarter", "form",
        "date", "inconsist", "hazardous", "nestle", "adani", "bescom",
    }
    interesting = tokens & keywords or tokens

    def score(fact: Fact) -> float:
        blob = " ".join(
            [
                fact.name,
                fact.role,
                fact.value_text or "",
                fact.unit or "",
                fact.quote or "",
                fact.locator,
                fact.document.filename if fact.document else "",
            ]
        ).lower()
        s = sum(1 for t in interesting if t in blob)
        if fact.provenance == "calculated":
            s += 0.3
        if fact.name in PRIORITY_NAMES or fact.role in PRIORITY_ROLES:
            s += 1.5
        return s

    by_doc: dict[str, list[Fact]] = {}
    for fact in facts:
        by_doc.setdefault(fact.document_id, []).append(fact)

    picked: list[Fact] = []
    per_doc = max(14, k // max(len(by_doc), 1))
    for group in by_doc.values():
        ranked = sorted(group, key=score, reverse=True)
        chosen: list[Fact] = []
        for fact in ranked:
            if fact.name in PRIORITY_NAMES or fact.role in PRIORITY_ROLES:
                chosen.append(fact)
        for fact in ranked:
            if fact not in chosen:
                chosen.append(fact)
            if len(chosen) >= per_doc:
                break
        picked.extend(chosen[:per_doc])
    picked.sort(key=score, reverse=True)
    return picked[:k] or facts[: min(k, len(facts))]
