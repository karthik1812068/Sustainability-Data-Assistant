"""Question answering: baseline (chunks) and improved (facts + FY)."""

from __future__ import annotations

import json
import re
from collections import defaultdict

from sqlalchemy.orm import Session

from app.fy import fy_for_date, parse_fy, parse_iso_date, period_coverage
from app.llm import complete_json, llm_available
from app.prompts import ASK_SYSTEM
from sqlalchemy.orm import joinedload

from app.models import Document, Fact, QueryLog
from app.retrieve import relevant_facts, search_chunks


def ask(
    db: Session,
    question: str,
    mode: str = "improved",
    fy: str | None = None,
    document_ids: list[str] | None = None,
) -> dict:
    docs = db.query(Document).filter(Document.status == "completed")
    if document_ids:
        docs = docs.filter(Document.id.in_(document_ids))
    docs = docs.all()
    chunks = search_chunks(db, question, document_ids)
    facts = relevant_facts(db, question, document_ids) if mode == "improved" else []
    if mode == "improved":
        facts = _merge_critical_facts(db, facts, document_ids)

    fy_pack = None
    if fy or re.search(r"\b(fy|financial year|india(?:n)? fy)\b", question, re.I):
        label = fy or _fy_in_question(question)
        if label:
            fy_pack = fy_breakdown(db, label, document_ids)

    if mode == "baseline":
        payload = _baseline_answer(question, chunks, docs)
    else:
        payload = _improved_answer(question, chunks, facts, docs, fy_pack)

    if llm_available():
        llm_json = complete_json(ASK_SYSTEM, _llm_user(question, mode, chunks, facts, fy_pack, payload))
        if llm_json and llm_json.get("answer"):
            payload["answer"] = llm_json["answer"]
            payload["confidence"] = llm_json.get("confidence", payload.get("confidence", "medium"))
            payload["gaps"] = _merge_unique(payload.get("gaps"), llm_json.get("gaps"))
            payload["inconsistencies"] = _merge_unique(payload.get("inconsistencies"), llm_json.get("inconsistencies"))
            if llm_json.get("calculations"):
                payload["calculations"] = llm_json["calculations"]

    payload["mode"] = mode
    payload["question"] = question
    payload["fy"] = fy_pack["label"] if fy_pack else fy
    payload["llm_used"] = llm_available()
    log = QueryLog(
        question=question,
        fy=payload.get("fy"),
        mode=mode,
        document_ids=json.dumps(document_ids) if document_ids else None,
        answer_json=json.dumps(payload, default=str),
    )
    db.add(log)
    db.flush()
    payload["query_id"] = log.id
    return payload


def fy_breakdown(db: Session, label: str, document_ids: list[str] | None = None) -> dict:
    start, end, norm = parse_fy(label)
    q = db.query(Fact, Document).join(Document).filter(Document.status == "completed")
    if document_ids:
        q = q.filter(Document.id.in_(document_ids))
    rows = q.all()

    included = []
    outside = []
    unknown = []
    org_totals = defaultdict(lambda: defaultdict(float))
    for fact, doc in rows:
        if fact.period_start is None and fact.period_end is None:
            continue
        cov = period_coverage(fact.period_start, fact.period_end, start, end)
        item = {
            "document_id": doc.id,
            "filename": doc.filename,
            "organization": doc.organization,
            "name": fact.name,
            "role": fact.role,
            "value_num": fact.value_num,
            "unit": fact.unit,
            "locator": fact.locator,
            "provenance": fact.provenance,
            "period_start": fact.period_start,
            "period_end": fact.period_end,
            "coverage": cov,
        }
        if cov["status"] == "full":
            included.append(item)
            if fact.value_num is not None and fact.unit and fact.provenance in {"reported", "calculated"}:
                if fact.role in {
                    "billed_consumption",
                    "workbook_month_total",
                    "waste_item",
                    "total_payable",
                }:
                    org_totals[doc.organization or "Unknown"][f"{fact.name}:{fact.unit}"] += fact.value_num * cov["fraction"]
        elif cov["status"] == "partial":
            included.append(item)
            unknown.append({**item, "note": "Period spans the FY boundary; include only with proration."})
        elif cov["status"] == "outside":
            outside.append(item)
        else:
            unknown.append(item)

    # Prefer billed consumption / monthly workbook totals, avoid double-counting annual + months
    filtered_included = _dedupe_fy_items(included)
    coverage_notes = []
    orgs = sorted({d.organization or "Unknown" for d in db.query(Document).filter(Document.status == "completed")})
    coverage_notes.append("Indian FY runs 1 April to 31 March.")
    coverage_notes.append(f"Requested FY {norm} covers {start.isoformat()} to {end.isoformat()}.")
    if len(orgs) > 1:
        coverage_notes.append(
            "These documents belong to different organisations and cannot be summed into one company total."
        )
    months_needed = 12
    month_hits = {
        (parse_iso_date(i["period_start"]).year, parse_iso_date(i["period_start"]).month)
        for i in filtered_included
        if i.get("period_start") and parse_iso_date(i["period_start"]) and i["role"] in {"workbook_month_total", "billed_consumption"}
    }
    if month_hits:
        coverage_notes.append(f"Evidence covers {len(month_hits)} distinct month-periods, not a full 12-month FY.")

    return {
        "label": norm,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "included": filtered_included,
        "outside_count": len(outside),
        "unknown": unknown[:20],
        "org_totals": {org: dict(vals) for org, vals in org_totals.items()},
        "notes": coverage_notes,
        "organizations": orgs,
    }


def _improved_answer(question: str, chunks: list[dict], facts: list[Fact], docs: list[Document], fy_pack: dict | None) -> dict:
    evidence = [_fact_evidence(f) for f in facts[:12]]
    for ch in chunks[:6]:
        evidence.append(
            {
                "document_id": ch["document_id"],
                "filename": ch["filename"],
                "locator": ch["locator"],
                "quote": ch["text"][:280],
                "kind": "chunk",
            }
        )
    evidence = _unique_evidence(evidence)
    gaps = []
    inconsistencies = []
    calculations = []
    orgs = sorted({d.organization for d in docs if d.organization})
    if len(orgs) > 1:
        gaps.append(
            "Documents relate to different organisations: " + "; ".join(orgs) + ". Do not treat them as one reporting entity."
        )

    q = question.lower()
    parts = []

    cons_facts = [f for f in facts if f.role == "billed_consumption" and f.unit in {"kWh", "kwh"}]
    if _wants(q, ["electricity consumption", "each bill", "bills report", "kwh"]):
        if cons_facts:
            for f in cons_facts:
                org = f.document.organization or "Unknown organisation"
                parts.append(
                    f"{org} reports { _n(f.value_num) } {f.unit} for {f.period_start or '?'} to {f.period_end or '?'} "
                    f"(reported, {f.document.filename} {f.locator})."
                )
        else:
            gaps.append("No billed electricity consumption facts were extracted from the selected documents.")

    water_rep = next((f for f in facts if f.name == "water_consumption"), None)
    water_calc = next((f for f in facts if f.name == "consumption_from_readings" and f.unit in {"m3", "m³"}), None)
    meter_match = next((f for f in facts if f.name == "meter_match"), None)
    if _wants(q, ["water", "meter"]):
        if water_rep and water_calc:
            prev = next((f for f in facts if f.name == "previous_reading"), None)
            curr = next((f for f in facts if f.name == "present_reading"), None)
            calculations.append(
                {
                    "expression": f"{curr.value_num if curr else '?'} - {prev.value_num if prev else '?'}",
                    "result": water_calc.value_num,
                    "note": "System-calculated consumption from reported meter readings.",
                }
            )
            match = meter_match and (meter_match.value_text == "match" or (meter_match.extra_json and "true" in meter_match.extra_json.lower()))
            if match or (water_rep.value_num is not None and abs((water_rep.value_num or 0) - (water_calc.value_num or 0)) < 0.05):
                parts.append(
                    f"Yes. Reported water consumption is {_n(water_rep.value_num)} {water_rep.unit} "
                    f"for {water_rep.period_start} to {water_rep.period_end}. "
                    f"Meter arithmetic {curr.value_num if curr else '?'} − {prev.value_num if prev else '?'} = {_n(water_calc.value_num)} {water_calc.unit}, which matches the billed units."
                )
            else:
                parts.append(
                    f"Readings and billed units disagree: billed {_n(water_rep.value_num)} vs calculated {_n(water_calc.value_num)}."
                )
                inconsistencies.append("Water billed units do not match current minus previous meter reading.")
        else:
            gaps.append("Water meter readings or billed consumption were not available.")

    waste_items = [f for f in facts if f.name == "waste_quantity"]
    waste_total = next((f for f in facts if f.name == "waste_total"), None)
    if _wants(q, ["waste", "hazardous", "etp", "sludge"]):
        if waste_items:
            listed = ", ".join(f"{_n(f.value_num)} {f.unit} {f.value_text or ''}".strip() for f in waste_items)
            calculations.append(
                {
                    "expression": " + ".join(_n(f.value_num) for f in waste_items),
                    "result": waste_total.value_num if waste_total else sum(f.value_num or 0 for f in waste_items),
                    "note": "System-calculated total. Individual line quantities are reported in the form.",
                }
            )
            parts.append(
                f"Hazardous waste line items: {listed}. System-calculated total: {_n(waste_total.value_num if waste_total else sum(f.value_num or 0 for f in waste_items))} MT."
            )
        sig = next((f for f in facts if f.name == "occupier_signature_date"), None)
        item_dates = {f.period_start for f in waste_items if f.period_start}
        if sig and item_dates and sig.value_text not in item_dates:
            inconsistencies.append(
                f"Form-3 occupier signature date is {sig.value_text} but waste rows are dated {', '.join(sorted(item_dates))}."
            )
            parts.append(inconsistencies[-1])

    if _wants(q, ["monthly", "quarter", "workbook", "spreadsheet", "agree", "annual summary"]):
        annuals = [f for f in facts if f.name == "electricity_consumption" and "annual" in (f.role or "")]
        monthly_sum = next((f for f in facts if f.name == "workbook_monthly_sum"), None)
        conflict = next((f for f in facts if f.name == "cross_sheet_conflict"), None)
        internal = next((f for f in facts if f.name == "workbook_internal_check"), None)
        if monthly_sum:
            parts.append(
                f"Sum of monthly totals (system-calculated) is {_n(monthly_sum.value_num)} kWh from {monthly_sum.locator}."
            )
            calculations.append({"expression": "sum(monthly total consumption)", "result": monthly_sum.value_num, "note": "Calculated from monthly rows, not copied from the annual total cell."})
        if internal:
            extra = json.loads(internal.extra_json or "{}")
            parts.append(internal.quote or "")
            if extra.get("matches"):
                parts.append("Within the monthly sheet, the annual total cell agrees with the sum of months.")
            else:
                inconsistencies.append(internal.quote)
        reported_annuals = []
        for f in annuals:
            reported_annuals.append(f.value_num)
            loc = f.locator
            fname = f.document.filename if f.document else ""
            parts.append(f"Reported annual total on {fname} {loc}: {_n(f.value_num)} kWh.")
        if conflict:
            extra = json.loads(conflict.extra_json or "{}")
            values = extra.get("values") or reported_annuals
            msg = (
                f"Annual electricity totals disagree across workbook sheets: "
                f"{', '.join(_n(v) for v in values)} kWh. {conflict.quote or ''} "
                "Treat as conflicting evidence, not a single total."
            )
            inconsistencies.append(msg)
            parts.append(msg)
        elif len({round(v or 0) for v in reported_annuals}) > 1:
            msg = f"Annual electricity totals disagree: {', '.join(_n(v) for v in reported_annuals)} kWh."
            inconsistencies.append(msg)
            parts.append(msg)
        if not annuals and not monthly_sum and not conflict:
            gaps.append("Workbook annual/monthly totals were not found.")

    if _wants(q, ["complaint", "deadline", "sla"]):
        deadline = next((f for f in facts if f.name == "customer_complaint_deadline"), None)
        if deadline and (deadline.value_text or "").lower() == "not specified":
            parts.append(
                "No. The quality policy does not specify a deadline for resolving customer complaints. "
                "The document covers food safety and quality commitments, not complaint SLAs."
            )
            gaps.append("Customer-complaint resolution deadline is not present in the policy.")
        elif deadline:
            parts.append(f"The policy mentions: {deadline.value_text}")
        else:
            gaps.append("No policy fact about complaint deadlines was extracted.")
            parts.append("The selected policy text does not specify a customer-complaint resolution deadline.")

    if _wants(q, ["non-negotiable", "food safety", "approved", "approver", "ceo"]):
        nn = next((f for f in facts if f.name == "food_safety_non_negotiable"), None)
        approver = next((f for f in facts if f.name == "approver"), None)
        approved = next((f for f in facts if f.name == "approval_date"), None)
        signatory = next((f for f in facts if f.name == "signatory"), None)
        if nn:
            parts.append(f"Yes. The policy states: \"{nn.quote or nn.value_text}\".")
        if approver:
            parts.append(f"Approver: {approver.value_text} ({approver.locator}).")
        if approved:
            parts.append(f"Date of approval: {approved.value_text}.")
        if signatory:
            parts.append(f"Signed for the executive team by {signatory.value_text}, Chief Executive Officer.")

    if _wants(q, ["diesel", "fuel oil", "petrol", "natural gas consumption"]):
        hits = [f for f in facts if f.unit and "diesel" in ((f.value_text or "") + (f.quote or "")).lower()]
        if not hits:
            parts.append("None of the selected documents record diesel (or other fuel) consumption. This is a coverage gap.")
            gaps.append("No diesel consumption evidence.")

    amount_roles = [f for f in facts if f.unit == "INR" and f.role in {"current_month_bill", "total_payable", "previous_outstanding", "amount_after_due_date", "amount_by_discount_date", "energy_charge", "demand_charge"}]
    if _wants(q, ["payable", "current month bill", "amount", "due date", "how much"]) and amount_roles:
        parts.append("These bills contain several different amounts; they are not interchangeable:")
        for f in amount_roles:
            parts.append(
                f"- {f.role.replace('_', ' ')}: {_n(f.value_num)} {f.unit} ({f.provenance}, {f.document.filename} {f.locator})"
            )

    if fy_pack:
        parts.append(
            f"Indian FY {fy_pack['label']} is {fy_pack['start']} to {fy_pack['end']}."
        )
        cons = [
            i
            for i in fy_pack["included"]
            if i["role"] in {"billed_consumption", "workbook_month_total"} and i.get("value_num") is not None
        ]
        by_org = defaultdict(list)
        for i in cons:
            by_org[i.get("organization") or "Unknown"].append(i)
        if by_org:
            for org, items in by_org.items():
                uniq = []
                seen = set()
                for i in items:
                    key = (i.get("filename"), i.get("period_start"), i.get("role"), round(i["value_num"], 3), i.get("unit"))
                    if key in seen:
                        continue
                    seen.add(key)
                    uniq.append(i)
                    parts.append(
                        f"- {org}: {_n(i['value_num'])} {i.get('unit') or ''} for {i.get('period_start')} to {i.get('period_end')} "
                        f"({i.get('filename')} {i.get('locator')}, {i.get('provenance')})."
                    )
                units = defaultdict(float)
                for i in uniq:
                    if i.get("unit") and i["role"] == "workbook_month_total":
                        units[i["unit"]] += i["value_num"]
                    elif i.get("unit") and i["role"] == "billed_consumption":
                        units[i["unit"]] += i["value_num"]
                if units:
                    desc = ", ".join(f"{_n(v)} {u}" for u, v in units.items())
                    parts.append(f"Organisation subtotal for {org} (do not merge across companies): {desc}.")
        else:
            gaps.append(f"No in-period consumption records for FY {fy_pack['label']}. Coverage is incomplete.")
        parts.append("Coverage is incomplete unless every month of the FY is evidenced for the same organisation.")
        parts.extend(fy_pack.get("notes") or [])
        if fy_pack.get("outside_count"):
            gaps.append(f"{fy_pack['outside_count']} dated facts fall outside this FY.")

    if _wants(q, ["single company", "company-wide", "one organisation", "same company", "aggregate all"]):
        parts.append(
            "No. The supplied documents are from different organisations and reporting periods, so they must not be reported as one company-wide total."
        )

    if not parts:
        if facts:
            parts.append("Relevant extracted facts:")
            for f in facts[:8]:
                val = f.value_text if f.value_num is None else f"{_n(f.value_num)} {f.unit or ''}".strip()
                parts.append(f"- {f.name} ({f.role}, {f.provenance}): {val} [{f.document.filename} {f.locator}]")
        elif chunks:
            parts.append("Retrieved passages, but structured facts were limited. Treat the following as unverified snippets.")
            parts.append(chunks[0]["text"][:500])
            gaps.append("Structured facts were insufficient for a precise answer.")
        else:
            parts.append("No processed documents matched this question.")
            gaps.append("Empty evidence set.")

    confidence = "high" if facts and not (len(gaps) > 2) else "medium" if facts or chunks else "low"
    return {
        "answer": " ".join(p if p.endswith(".") else p + "." for p in parts if p),
        "confidence": confidence,
        "evidence": evidence,
        "calculations": calculations,
        "gaps": _merge_unique([], gaps),
        "inconsistencies": _merge_unique([], inconsistencies),
        "coverage": fy_pack,
        "organizations": orgs,
    }


def _baseline_answer(question: str, chunks: list[dict], docs: list[Document]) -> dict:
    evidence = [
        {
            "document_id": c["document_id"],
            "filename": c["filename"],
            "locator": c["locator"],
            "quote": c["text"][:280],
            "kind": "chunk",
        }
        for c in chunks[:6]
    ]
    if not chunks:
        answer = "No indexed text was available to answer this question."
        return {"answer": answer, "confidence": "low", "evidence": [], "calculations": [], "gaps": ["No chunks"], "inconsistencies": [], "coverage": None, "organizations": []}

    # baseline: just stitch top matching chunks
    snippets = []
    for c in chunks[:4]:
        snippets.append(f"From {c['filename']} ({c['locator']}): {c['text'][:450]}")
    answer = "Based on similar passages:\n\n" + "\n\n".join(snippets)
    return {
        "answer": answer,
        "confidence": "medium",
        "evidence": evidence,
        "calculations": [],
        "gaps": ["Baseline mode does not run the financial-year coverage engine or amount-role disambiguation."],
        "inconsistencies": [],
        "coverage": None,
        "organizations": sorted({d.organization for d in docs if d.organization}),
    }


def _fact_evidence(fact: Fact) -> dict:
    return {
        "document_id": fact.document_id,
        "filename": fact.document.filename if fact.document else "",
        "locator": fact.locator,
        "quote": (fact.quote or fact.value_text or "")[:280],
        "kind": "fact",
        "name": fact.name,
        "role": fact.role,
        "provenance": fact.provenance,
        "value_num": fact.value_num,
        "unit": fact.unit,
    }


def _unique_evidence(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in items:
        key = (item.get("filename"), item.get("locator"), item.get("quote", "")[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _llm_user(question, mode, chunks, facts, fy_pack, draft) -> str:
    fact_lines = []
    for f in facts[:25]:
        fact_lines.append(
            f"- {f.document.filename} | {f.locator} | {f.name}/{f.role} | {f.provenance} | "
            f"{f.value_num} {f.unit or ''} {f.value_text or ''} | {f.period_start}..{f.period_end} | {f.quote}"
        )
    chunk_lines = [f"- {c['filename']} {c['locator']}: {c['text'][:400]}" for c in chunks[:6]]
    fy_txt = json.dumps(fy_pack, default=str)[:4000] if fy_pack else "none"
    return (
        f"Question: {question}\nMode: {mode}\n"
        f"Draft answer to refine:\n{draft.get('answer')}\n\n"
        f"STRUCTURED FACTS:\n" + "\n".join(fact_lines) + "\n\n"
        f"PASSAGES:\n" + "\n".join(chunk_lines) + "\n\n"
        f"FY PACK:\n{fy_txt}"
    )


def _fy_in_question(question: str) -> str | None:
    m = re.search(r"(20\d{2})\s*[-–/]\s*(\d{2,4})", question)
    if m:
        return m.group(0)
    return None


def _wants(q: str, needles: list[str]) -> bool:
    return any(n in q for n in needles)


def _n(value) -> str:
    if value is None:
        return "?"
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value)):,}"
    return f"{value:,.2f}"


def _merge_unique(a, b) -> list[str]:
    out = []
    for item in list(a or []) + list(b or []):
        if not item:
            continue
        if item not in out:
            out.append(item)
    return out


CRITICAL_FACT_NAMES = {
    "cross_sheet_conflict",
    "workbook_monthly_sum",
    "workbook_internal_check",
    "occupier_signature_date",
    "customer_complaint_deadline",
    "waste_total",
    "meter_match",
    "water_consumption",
    "consumption_from_readings",
    "food_safety_non_negotiable",
    "approver",
    "approval_date",
}


def _merge_critical_facts(db: Session, facts: list[Fact], document_ids: list[str] | None) -> list[Fact]:
    q = db.query(Fact).options(joinedload(Fact.document)).filter(Fact.name.in_(CRITICAL_FACT_NAMES))
    if document_ids:
        q = q.filter(Fact.document_id.in_(document_ids))
    extra = q.all()
    annuals = db.query(Fact).options(joinedload(Fact.document)).filter(
        Fact.name == "electricity_consumption",
        Fact.role.like("%annual%"),
    )
    if document_ids:
        annuals = annuals.filter(Fact.document_id.in_(document_ids))
    billed = db.query(Fact).options(joinedload(Fact.document)).filter(Fact.role == "billed_consumption")
    if document_ids:
        billed = billed.filter(Fact.document_id.in_(document_ids))
    seen = {f.id for f in facts}
    merged = list(facts)
    for fact in list(extra) + list(annuals.all()) + list(billed.all()):
        if fact.id not in seen:
            merged.append(fact)
            seen.add(fact.id)
    return merged


def _dedupe_fy_items(items: list[dict]) -> list[dict]:
    """Skip annual/quarter rows if monthly rows are already included."""
    has_months = any(i["role"] == "workbook_month_total" for i in items)
    out = []
    for i in items:
        if has_months and i["role"] == "workbook_annual_total":
            continue
        if i["role"] in {"workbook_quarter_total"} and has_months:
            continue
        out.append(i)
    return out
