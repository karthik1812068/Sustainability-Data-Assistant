from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import ROOT
from app.models import EvalResult, EvalRun, utcnow
from app.qa import ask

CASES_PATH = ROOT / "evals" / "cases.json"


def load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def run_eval(db: Session, mode: str, label: str | None = None) -> dict:
    cases = load_cases()
    results = []
    for case in cases:
        answer = ask(db, case["question"], mode=mode, fy=case.get("fy"))
        scored = score_case(case, answer)
        scored["question"] = case["question"]
        scored["answer"] = answer.get("answer")
        scored["evidence"] = answer.get("evidence")
        scored["calculations"] = answer.get("calculations")
        scored["gaps"] = answer.get("gaps")
        scored["inconsistencies"] = answer.get("inconsistencies")
        scored["confidence"] = answer.get("confidence")
        results.append(scored)

    passed = sum(1 for r in results if r["passed"])
    avg = sum(r["score"] for r in results) / len(results) if results else 0
    summary = {
        "label": label or mode,
        "mode": mode,
        "n_cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "average_score": round(avg, 4),
        "pass_rate": round(passed / len(results), 4) if results else 0,
    }
    run = EvalRun(label=summary["label"], mode=mode, summary_json=json.dumps(summary))
    db.add(run)
    db.flush()
    for row in results:
        db.add(
            EvalResult(
                run_id=run.id,
                case_id=row["case_id"],
                score=row["score"],
                passed=1 if row["passed"] else 0,
                payload_json=json.dumps(row, default=str),
            )
        )
    out_dir = ROOT / "evals"
    out_dir.mkdir(exist_ok=True)
    payload = {"run_id": run.id, "summary": summary, "results": results}
    (out_dir / f"{mode}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def compare_runs(baseline: dict, improved: dict) -> dict:
    b = {r["case_id"]: r for r in baseline.get("results", [])}
    i = {r["case_id"]: r for r in improved.get("results", [])}
    rows = []
    for case_id in sorted(set(b) | set(i)):
        br, ir = b.get(case_id), i.get(case_id)
        delta = (ir["score"] if ir else 0) - (br["score"] if br else 0)
        rows.append(
            {
                "case_id": case_id,
                "baseline_score": br["score"] if br else None,
                "improved_score": ir["score"] if ir else None,
                "delta": round(delta, 4),
                "baseline_passed": bool(br and br["passed"]),
                "improved_passed": bool(ir and ir["passed"]),
                "question": (ir or br or {}).get("question"),
            }
        )
    return {
        "baseline": baseline.get("summary"),
        "improved": improved.get("summary"),
        "cases": rows,
        "improved_cases": [r["case_id"] for r in rows if r["delta"] > 0.05],
        "regressed_cases": [r["case_id"] for r in rows if r["delta"] < -0.05],
        "unchanged": [r["case_id"] for r in rows if abs(r["delta"]) <= 0.05],
    }


def score_case(case: dict, answer: dict) -> dict:
    text = " ".join(
        [
            answer.get("answer") or "",
            json.dumps(answer.get("calculations") or []),
            json.dumps(answer.get("gaps") or []),
            json.dumps(answer.get("inconsistencies") or []),
            json.dumps(answer.get("coverage") or {}),
        ]
    ).lower()
    checks = []

    def add(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    for number in case.get("expect_numbers") or []:
        add(f"has_number_{number}", _has_number(text, number), f"looked for {number}")

    any_phrases = case.get("expect_any_phrases") or []
    if any_phrases:
        add("any_phrase", any(_norm(p) in _norm(text) for p in any_phrases), ",".join(any_phrases))

    for phrase in case.get("expect_all_phrases") or []:
        add(f"phrase_{phrase}", _norm(phrase) in _norm(text), phrase)

    period_hints = case.get("expect_period_hints") or []
    if period_hints:
        add("period_hints", all(h.lower() in text for h in period_hints), ",".join(period_hints))

    for phrase in case.get("forbid_phrases") or []:
        add(f"forbid_{phrase}", _norm(phrase) not in _norm(text), phrase)

    if case.get("require_evidence"):
        add("has_evidence", bool(answer.get("evidence")), f"{len(answer.get('evidence') or [])} items")

    if case.get("require_calculation"):
        add("has_calculation", bool(answer.get("calculations")), "")

    if case.get("require_gap"):
        gap_text = " ".join(answer.get("gaps") or []).lower() + " " + (answer.get("answer") or "").lower()
        add("has_gap", bool(answer.get("gaps")) or "gap" in gap_text or "not specify" in gap_text, "")

    if case.get("require_inconsistency"):
        blob = " ".join(answer.get("inconsistencies") or []).lower() + " " + (answer.get("answer") or "").lower()
        add(
            "has_inconsistency",
            bool(answer.get("inconsistencies")) or any(w in blob for w in ["inconsist", "disagree", "differ", "conflict"]),
            "",
        )

    if case.get("require_gap_or_org_warning"):
        blob = " ".join(answer.get("gaps") or []).lower() + " " + (answer.get("answer") or "").lower()
        add(
            "org_or_coverage",
            any(w in blob for w in ["organisation", "organization", "incomplete", "coverage", "cannot", "must not", "different"]),
            "",
        )

    n = len(checks) or 1
    score = sum(1 for c in checks if c["passed"]) / n
    return {
        "case_id": case["id"],
        "score": round(score, 4),
        "passed": score >= 0.75,
        "checks": checks,
        "notes": case.get("notes"),
    }


def _has_number(text: str, number: float) -> bool:
    compact = text.replace(" ", "").replace(",", "")
    if abs(number - round(number)) < 1e-6:
        n = int(round(number))
        variants = {str(n), f"{n}.0", f"{n}.00"}
        return any(v in compact or v in text for v in variants)
    variants = {f"{number}", f"{number:.1f}", f"{number:.2f}", str(int(number)) if number >= 100 else f"{number}"}
    return any(v in compact or v in text for v in variants)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
