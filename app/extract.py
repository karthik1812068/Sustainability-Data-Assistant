"""Extract typed facts from document text."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from dateutil import parser as dateparser

from app.fy import month_bounds, parse_iso_date, parse_month_label
from app.llm import complete_json, llm_available
from app.prompts import CLASSIFY_SYSTEM, EXTRACT_SYSTEM


DATE_RE = re.compile(
    r"\b(\d{1,2}(?:st|nd|rd|th)?[./\-]\d{1,2}[./\-]\d{2,4}|\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+20\d{2}|20\d{2}-\d{2}-\d{2})\b",
    re.I,
)
ISO_TRY = [
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d.%m.%Y",
    "%d-%m-%y",
    "%Y-%m-%d",
]


ALLOWED_TYPES = {
    "electricity_bill",
    "water_bill",
    "waste_record",
    "policy",
    "workbook",
    "other",
}


def classify_document(filename: str, text: str, kind: str) -> str:
    name = filename.lower()
    blob = f"{name}\n{text[:4000]}".lower()
    if kind == "xlsx":
        rule = "workbook"
    elif "form" in blob and ("hazardous" in blob or "waste" in blob or "etp sludge" in blob):
        rule = "waste_record"
    elif "quality policy" in blob or "food safety" in blob and "policy" in blob:
        rule = "policy"
    elif "water bill" in blob or "water supply" in blob or "cubic meter" in blob or "cu.m" in blob:
        rule = "water_bill"
    elif "electricity" in blob or "kwh" in blob or "tariff" in blob or "bescom" in blob or "adani" in blob:
        rule = "electricity_bill"
    else:
        rule = "other"
    if rule == "other" and llm_available() and kind != "xlsx":
        out = complete_json(
            CLASSIFY_SYSTEM,
            f"Filename: {filename}\nText:\n{text[:4000]}\nReturn JSON only.",
        )
        guessed = (out or {}).get("doc_type")
        if guessed in ALLOWED_TYPES:
            return guessed
    return rule


def extract_facts(path: str | Path, filename: str, ingested: dict) -> dict:
    text = ingested.get("preview") or "\n".join(c["text"] for c in ingested.get("chunks", [])[:40])
    kind = ingested.get("kind", "pdf")
    doc_type = classify_document(filename, text, kind)
    facts: list[dict] = []
    organization = _guess_organization(text, filename)

    if kind == "xlsx":
        facts.extend(extract_workbook_facts(path, filename))
        doc_type = "workbook"
        organization = organization or "Unnamed facility (electricity workbook)"
    elif doc_type == "electricity_bill":
        facts.extend(extract_electricity_facts(ingested, filename))
    elif doc_type == "water_bill":
        facts.extend(extract_water_facts(ingested, filename))
    elif doc_type == "waste_record":
        facts.extend(extract_waste_facts(ingested, filename))
    elif doc_type == "policy":
        facts.extend(extract_policy_facts(ingested, filename))
    else:
        facts.extend(extract_generic_facts(ingested, filename))

    facts.append(
        _fact(
            "document_type",
            value_text=doc_type,
            role="metadata",
            locator="document",
            quote=filename,
            provenance="calculated",
        )
    )
    if organization:
        facts.append(
            _fact(
                "organization",
                value_text=organization,
                role="organization",
                locator="document",
                quote=organization,
            )
        )
    if llm_available() and doc_type == "other":
        extra = complete_json(
            EXTRACT_SYSTEM,
            f"Filename: {filename}\nDocument type: {doc_type}\nText:\n{text[:8000]}\nReturn JSON only.",
        )
        seen = {f["name"] for f in facts}
        for raw in (extra or {}).get("facts") or []:
            if not isinstance(raw, dict) or not raw.get("locator") or not raw.get("name"):
                continue
            name = str(raw.get("name"))
            if name in seen:
                continue
            seen.add(name)
            facts.append(
                _fact(
                    name,
                    value_num=raw.get("value_num") if isinstance(raw.get("value_num"), (int, float)) else None,
                    value_text=str(raw.get("value_text") or "") or None,
                    unit=raw.get("unit"),
                    role=str(raw.get("role") or "other"),
                    period_start=raw.get("period_start") or None,
                    period_end=raw.get("period_end") or None,
                    locator=str(raw.get("locator")),
                    quote=(str(raw.get("quote"))[:400] if raw.get("quote") else None),
                    provenance="reported" if raw.get("provenance") != "calculated" else "calculated",
                )
            )
    return {"doc_type": doc_type, "organization": organization, "facts": facts}


def extract_workbook_facts(path: str | Path, filename: str) -> list[dict]:
    facts: list[dict] = []
    xls = pd.ExcelFile(path)
    year_hint = _year_from_sheet_names(xls.sheet_names) or _year_from_text(filename)

    for sheet in xls.sheet_names:
        df = pd.read_excel(path, sheet_name=sheet, header=None)
        facts.extend(_workbook_sheet_facts(df, sheet, year_hint))
    return facts


def _workbook_sheet_facts(df: pd.DataFrame, sheet: str, year_hint: int | None) -> list[dict]:
    facts: list[dict] = []
    header_idx = _find_header_row(df)
    if header_idx is None:
        return facts
    header = [_norm_header(v) for v in df.iloc[header_idx].tolist()]
    colmap = {name: i for i, name in enumerate(header) if name}

    month_rows = []
    for r in range(header_idx + 1, len(df)):
        row = df.iloc[r].tolist()
        label = str(row[0]).strip() if row and pd.notna(row[0]) else ""
        if not label or label.lower() in {"nan"}:
            continue
        locator = f"{sheet}!A{r + 1}:{_col_letter(len(row))}{r + 1}"
        quote = " | ".join(str(x) for x in row if pd.notna(x))
        period = None
        if re.fullmatch(
            r"(january|february|march|april|may|june|july|august|september|october|november|december)(\s+20\d{2})?",
            label,
            re.I,
        ):
            period = parse_month_label(label, year_hint)
        is_annual = "annual" in label.lower() or label.lower() in {"total"}
        is_quarter = bool(re.match(r"^q[1-4]\b", label, re.I))
        if is_quarter and year_hint and not period:
            qm = re.search(r"q([1-4])", label, re.I)
            if qm:
                qn = int(qm.group(1))
                start_m = {1: 1, 2: 4, 3: 7, 4: 10}[qn]
                start_d, _ = month_bounds(year_hint, start_m)
                _, end_d = month_bounds(year_hint, start_m + 2)
                period = (start_d, end_d)

        def num(col_aliases: list[str]) -> float | None:
            for alias in col_aliases:
                idx = colmap.get(alias)
                if idx is None or idx >= len(row) or pd.isna(row[idx]):
                    continue
                return _to_float(row[idx])
            return None

        grid = num(["grid_supply_kwh", "grid_kwh", "grid"])
        solar = num(["solar_power_kwh", "solar_kwh", "solar"])
        wind = num(["wind_power_kwh", "wind_kwh", "wind"])
        renewable = num(["total_renewable_kwh", "renewable_kwh"])
        total = num(["total_consumption_kwh", "total_kwh", "total"])
        cost = num(["grid_cost_inr", "cost_inr", "net_cost_inr"])
        savings = num(["renewable_savings_inr", "savings_inr"])
        start = period[0].isoformat() if period else None
        end = period[1].isoformat() if period else None
        if is_annual and year_hint:
            start, end = f"{year_hint}-01-01", f"{year_hint}-12-31"
        provenance = "reported"

        payload = {
            "grid": grid,
            "solar": solar,
            "wind": wind,
            "renewable": renewable,
            "total": total,
            "cost": cost,
            "savings": savings,
        }
        if total is None and any(v is not None for v in (grid, solar, wind)):
            total = sum(v or 0 for v in (grid, solar, wind))
            payload["total"] = total

        if any(v is not None for v in payload.values()) and (period or is_annual or is_quarter):
            grain = "annual" if is_annual else "quarter" if is_quarter else "month"
            if grain == "month" and period:
                month_rows.append(payload | {"start": start, "end": end, "label": label})
            facts.append(
                _fact(
                    f"{grain}_row",
                    value_num=total,
                    value_text=label,
                    unit="kWh",
                    role=f"workbook_{grain}",
                    period_start=start,
                    period_end=end,
                    locator=locator,
                    quote=quote,
                    extra={"sheet": sheet, **{k: v for k, v in payload.items() if v is not None}},
                )
            )
            if total is not None:
                facts.append(
                    _fact(
                        "electricity_consumption",
                        value_num=total,
                        unit="kWh",
                        role=f"workbook_{grain}_total",
                        period_start=start,
                        period_end=end,
                        locator=locator,
                        quote=quote,
                        extra={"sheet": sheet, "label": label, "source": "total_consumption"},
                    )
                )
            if grid is not None:
                facts.append(
                    _fact(
                        "grid_consumption",
                        value_num=grid,
                        unit="kWh",
                        role=f"workbook_{grain}_grid",
                        period_start=start,
                        period_end=end,
                        locator=locator,
                        quote=quote,
                        extra={"sheet": sheet, "label": label},
                    )
                )
            if renewable is not None:
                facts.append(
                    _fact(
                        "renewable_generation",
                        value_num=renewable,
                        unit="kWh",
                        role=f"workbook_{grain}_renewable",
                        period_start=start,
                        period_end=end,
                        locator=locator,
                        quote=quote,
                        extra={"sheet": sheet, "label": label},
                    )
                )

        # named KPI rows such as Highest Total Consumption
        if "highest" in label.lower() or "lowest" in label.lower() or "capacity" in label.lower():
            number = next(( _to_float(x) for x in row[1:] if _to_float(x) is not None), None)
            facts.append(
                _fact(
                    "workbook_kpi",
                    value_num=number,
                    value_text=quote,
                    role="kpi",
                    locator=locator,
                    quote=quote,
                    extra={"sheet": sheet},
                )
            )

    if month_rows and sheet.lower().startswith("monthly"):
        calc_total = sum(r.get("total") or 0 for r in month_rows)
        calc_grid = sum(r.get("grid") or 0 for r in month_rows)
        calc_solar = sum(r.get("solar") or 0 for r in month_rows)
        calc_wind = sum(r.get("wind") or 0 for r in month_rows)
        facts.append(
            _fact(
                "workbook_monthly_sum",
                value_num=calc_total,
                unit="kWh",
                role="calculated_annual_from_months",
                period_start=month_rows[0]["start"],
                period_end=month_rows[-1]["end"],
                locator=sheet,
                quote=f"Sum of {len(month_rows)} monthly totals",
                provenance="calculated",
                extra={"grid": calc_grid, "solar": calc_solar, "wind": calc_wind, "months": len(month_rows), "sheet": sheet},
            )
        )
        reported = next((f for f in facts if f["name"] == "electricity_consumption" and f["role"] == "workbook_annual_total" and f.get("extra", {}).get("sheet") == sheet), None)
        if reported and reported.get("value_num") is not None:
            delta = calc_total - reported["value_num"]
            facts.append(
                _fact(
                    "workbook_internal_check",
                    value_num=delta,
                    unit="kWh",
                    role="reconciliation",
                    locator=sheet,
                    quote=f"Monthly sum {calc_total} vs reported annual {reported['value_num']}",
                    provenance="calculated",
                    extra={"matches": abs(delta) < 0.5, "sheet": sheet},
                )
            )
    return facts


def extract_electricity_facts(ingested: dict, filename: str) -> list[dict]:
    facts: list[dict] = []
    pages = ingested.get("pages") or [{"page": 1, "text": ingested.get("preview", "")}]
    full = "\n".join(p["text"] for p in pages)
    period = _find_billing_period(full)
    start, end = (period or (None, None))

    consumption = _first_number(
        full,
        [
            r"units\s*consumed\s*[:\s]*([0-9,]+)",
            r"net\s*consumption\s*[:\s]*([0-9,]+)",
            r"energy\s*charges:\s*([0-9,]+(?:\.[0-9]+)?)\s*kwh",
            r"consumption\s*\(kwh\)\s*([0-9,]+)",
        ],
    )
    if consumption is not None:
        facts.append(
            _fact(
                "electricity_consumption",
                value_num=consumption,
                unit="kWh",
                role="billed_consumption",
                period_start=start,
                period_end=end,
                locator=_locator_for(pages, "consum"),
                quote=_quote_around(full, str(int(consumption)) if consumption == int(consumption) else str(consumption)),
            )
        )

    for name, patterns, role, unit in [
        ("current_month_bill", [r"current\s*month(?:'s)?\s*bill(?:\s*amount)?\s*[:\s]*r?\s*([0-9,]+(?:\.[0-9]+)?)"], "current_month_bill", "INR"),
        ("current_bill_amount", [r"current\s*bill\s*amount\s*[:\s]*([0-9,]+(?:\.[0-9]+)?)", r"\(a\)\s*current month's bill amount[^\n]*\n?.*?([0-9,]+(?:\.[0-9]+)?)"], "current_month_bill", "INR"),
        ("net_payable", [r"net\s*payable\s*amount\s*[:\s]*([0-9,]+(?:\.[0-9]+)?)", r"total\s*bill\s*amount\s*\(a\+b\+c\)\s*([0-9,]+(?:\.[0-9]+)?)"], "total_payable", "INR"),
        ("previous_outstanding", [r"previous\s*outstanding\s*[:\s]*r?\s*([0-9,]+(?:\.[0-9]+)?)", r"net\s*previous\s*balance\s*([0-9,]+(?:\.[0-9]+)?)"], "previous_outstanding", "INR"),
        ("amount_after_due_date", [r"round sum payable after due date[\s\S]{0,220}?amt\s*[:\s]*r?\s*([0-9,]+(?:\.[0-9]+)?)", r"total bill amount with dpc\s*([0-9,]+(?:\.[0-9]+)?)"], "amount_after_due_date", "INR"),
        ("amount_by_discount_date", [r"round sum payable by discount date[\s\S]{0,220}?amt\s*[:\s]*r?\s*([0-9,]+(?:\.[0-9]+)?)"], "amount_by_discount_date", "INR"),
        ("energy_charges", [r"energy\s*charges:\s*[0-9,.]+\s*kwh at rs [0-9.]+ per kwh\s*([0-9,]+(?:\.[0-9]+)?)", r"energy\s*charge\s*([0-9,]+(?:\.[0-9]+)?)"], "energy_charge", "INR"),
        ("demand_charges", [r"demand charges:\s*[0-9,.]+\s*kva[^\n]*\n?([0-9,]+(?:\.[0-9]+)?)", r"demand/fixed\s*charge\s*([0-9,]+(?:\.[0-9]+)?)"], "demand_charge", "INR"),
        ("contract_demand", [r"contract demand\s*\(?kva\)?\s*[:\s]*([0-9,]+(?:\.[0-9]+)?)", r"contract demand\s*([0-9,]+(?:\.[0-9]+)?)\s*kva"], "contract_demand", "kVA"),
        ("recorded_demand", [r"recorded demand\s*\(?kva\)?\s*[:\s]*([0-9,]+(?:\.[0-9]+)?)"], "recorded_demand", "kVA"),
        ("billing_demand", [r"billing demand\s*\(?kva\)?\s*[:\s]*([0-9,]+(?:\.[0-9]+)?)"], "billing_demand", "kVA"),
        ("due_date", [r"due date\s*[:\s]*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{2,4})"], "due_date", None),
    ]:
        if name in {"due_date"}:
            m = re.search(patterns[0], full, re.I)
            if m:
                facts.append(_fact(name, value_text=_norm_date(m.group(1)), role=role, locator=_locator_for(pages, "due date"), quote=m.group(0)[:180]))
            continue
        value = _first_number(full, patterns)
        if value is not None:
            facts.append(
                _fact(
                    name,
                    value_num=value,
                    unit=unit,
                    role=role,
                    period_start=start,
                    period_end=end,
                    locator=_locator_for(pages, name.split("_")[0]),
                    quote=_quote_around(full, _fmt_num(value)),
                )
            )

    readings = _meter_readings(full)
    if readings:
        prev = next((f["value_num"] for f in readings if f["name"] == "previous_reading"), None)
        curr = next((f["value_num"] for f in readings if f["name"] == "present_reading"), None)
        mf = next((f["value_num"] for f in readings if f["name"] == "meter_constant"), 1) or 1
        if prev is not None and curr is not None:
            calc = (curr - prev) * mf
            plausible = consumption is None or (calc > 0 and (consumption == 0 or abs(calc - consumption) / max(consumption, 1) < 0.05))
            if plausible:
                facts.extend(readings)
                facts.append(
                    _fact(
                        "consumption_from_readings",
                        value_num=round(calc, 3),
                        unit="kWh",
                        role="calculated_from_meter",
                        period_start=start,
                        period_end=end,
                        locator=_locator_for(pages, "present reading"),
                        quote=f"({curr} - {prev}) x MF {mf}",
                        provenance="calculated",
                    )
                )

    if start:
        facts.append(_fact("billing_period_start", value_text=start, role="period", period_start=start, period_end=end, locator=_locator_for(pages, "bill"), quote=start))
        facts.append(_fact("billing_period_end", value_text=end, role="period", period_start=start, period_end=end, locator=_locator_for(pages, "bill"), quote=end))

    utility = _utility_name(full, filename)
    if utility:
        facts.append(_fact("utility", value_text=utility, role="organization", locator="page 1", quote=utility))
    return facts


def extract_water_facts(ingested: dict, filename: str) -> list[dict]:
    facts: list[dict] = []
    pages = ingested.get("pages") or [{"page": 1, "text": ingested.get("preview", "")}]
    full = "\n".join(p["text"] for p in pages)
    period = _find_billing_period(full) or _period_from_month_year(full)
    start, end = period or (None, None)

    prev = _first_number(full, [r"previous reading[\s\S]{0,160}?([0-9,]{3,9}(?:\.[0-9]+)?)\s*(?:cubic meters|cu\.?m)"])
    curr = _first_number(full, [r"current reading[\s\S]{0,160}?([0-9,]{3,9}(?:\.[0-9]+)?)\s*(?:cubic meters|cu\.?m)"])
    reported = _first_number(full, [r"units consumed[\s\S]{0,160}?([0-9,]{2,9}(?:\.[0-9]+)?)\s*(?:cubic meters|cu\.?m)"])
    if prev is not None:
        facts.append(_fact("previous_reading", value_num=prev, unit="m3", role="meter_previous", period_start=start, period_end=end, locator=_locator_for(pages, "previous reading"), quote=_quote_around(full, _fmt_num(prev))))
    if curr is not None:
        facts.append(_fact("present_reading", value_num=curr, unit="m3", role="meter_present", period_start=start, period_end=end, locator=_locator_for(pages, "current reading"), quote=_quote_around(full, _fmt_num(curr))))
    if reported is not None:
        facts.append(_fact("water_consumption", value_num=reported, unit="m3", role="billed_consumption", period_start=start, period_end=end, locator=_locator_for(pages, "units consumed"), quote=_quote_around(full, _fmt_num(reported))))
    if prev is not None and curr is not None:
        calc = curr - prev
        facts.append(_fact("consumption_from_readings", value_num=calc, unit="m3", role="calculated_from_meter", period_start=start, period_end=end, locator=_locator_for(pages, "meter"), quote=f"{curr} - {prev} = {calc}", provenance="calculated"))
        if reported is not None:
            facts.append(
                _fact(
                    "meter_match",
                    value_text="match" if abs(calc - reported) < 0.05 else "mismatch",
                    value_num=abs(calc - reported),
                    unit="m3",
                    role="reconciliation",
                    locator=_locator_for(pages, "units consumed"),
                    quote=f"Reported {reported} vs calculated {calc}",
                    provenance="calculated",
                    extra={"matches": abs(calc - reported) < 0.05},
                )
            )
    total = _first_number(full, [r"total amount due\s*[:\s]*₹?\s*([0-9,]+(?:\.[0-9]+)?)", r"total amount due\s*₹?\s*([0-9,]+(?:\.[0-9]+)?)"])
    if total is not None:
        facts.append(_fact("total_payable", value_num=total, unit="INR", role="total_payable", period_start=start, period_end=end, locator=_locator_for(pages, "total amount"), quote=_quote_around(full, _fmt_num(total))))
    invoice = re.search(r"invoice number\s*[:\s]*([A-Z0-9\-]+)", full, re.I)
    if invoice:
        facts.append(_fact("invoice_number", value_text=invoice.group(1), role="identifier", locator=_locator_for(pages, "invoice number"), quote=invoice.group(0)))
    if start:
        facts.append(_fact("billing_period_start", value_text=start, role="period", period_start=start, period_end=end, locator="page 1", quote=start))
        facts.append(_fact("billing_period_end", value_text=end, role="period", period_start=start, period_end=end, locator="page 1", quote=end))
    return facts


def extract_waste_facts(ingested: dict, filename: str) -> list[dict]:
    facts: list[dict] = []
    pages = ingested.get("pages") or [{"page": 1, "text": ingested.get("preview", "")}]
    full = "\n".join(p["text"] for p in pages)
    # quantities like 1.0 T / 0.5 T near a waste category
    items = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*T\b", full):
        qty = float(m.group(1))
        window = full[max(0, m.start() - 180) : m.end() + 80]
        cat = _waste_category(window)
        items.append((qty, cat, window, m.group(0)))
    # de-duplicate identical consecutive values with same category
    seen = []
    for qty, cat, window, token in items:
        key = (qty, cat)
        if key in seen:
            continue
        seen.append(key)
        date_m = re.search(r"(\d{1,2}\s*\.\s*\d{2}\s*\.\s*20\d{2}|\d{1,2}/\d{1,2}/20\d{2})", window)
        wdate = _norm_date(re.sub(r"\s+", "", date_m.group(1))) if date_m else None
        facts.append(
            _fact(
                "waste_quantity",
                value_num=qty,
                unit="MT",
                value_text=cat,
                role="waste_item",
                period_start=wdate,
                period_end=wdate,
                locator=_locator_for(pages, cat.split("–")[-1].strip()[:12] if cat else "T"),
                quote=re.sub(r"\s+", " ", window)[:220],
                extra={"category": cat},
            )
        )
    if facts:
        total = sum(f["value_num"] or 0 for f in facts if f["name"] == "waste_quantity")
        facts.append(
            _fact(
                "waste_total",
                value_num=round(total, 3),
                unit="MT",
                role="calculated_total",
                locator="page 1",
                quote=" + ".join(f"{f['value_num']} MT {f.get('value_text') or ''}".strip() for f in facts if f["name"] == "waste_quantity"),
                provenance="calculated",
            )
        )
    signed = re.search(r"date\s*[:\s]*([0-9]{1,2}\.[0-9]{1,2}\.20[0-9]{2})", full, re.I)
    if signed:
        facts.append(_fact("occupier_signature_date", value_text=_norm_date(signed.group(1)), role="date", locator=_locator_for(pages, "Signature"), quote=signed.group(0)))
    auth = re.search(r"(20[A-Z0-9]{8,})\s*dated\s*([0-9/.\-]+)", full, re.I)
    if auth:
        facts.append(_fact("authorization_ref", value_text=f"{auth.group(1)} dated {auth.group(2)}", role="identifier", locator="page 1", quote=auth.group(0)))
    return facts


def extract_policy_facts(ingested: dict, filename: str) -> list[dict]:
    facts: list[dict] = []
    pages = ingested.get("pages") or [{"page": 1, "text": ingested.get("preview", "")}]
    full = "\n".join(p["text"] for p in pages)
    title = "Food Safety and Quality Policy" if "food safety" in full.lower() else "Quality Policy"
    facts.append(_fact("policy_title", value_text=title, role="policy", locator=_locator_for(pages, "Food Safety"), quote=title))
    approved = re.search(r"date of approval\s*([A-Za-z]+ \d{4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})", full, re.I)
    if approved:
        facts.append(_fact("approval_date", value_text=approved.group(1).strip(), role="date", locator=_locator_for(pages, "Date of approval"), quote=approved.group(0)))
        # June 2026 -> period
        month_period = parse_month_label(approved.group(1).strip())
        if month_period:
            facts[-1]["period_start"] = month_period[0].isoformat()
            facts[-1]["period_end"] = month_period[1].isoformat()
    approver = re.search(r"approver\s*([^\n]+)", full, re.I)
    if approver:
        facts.append(_fact("approver", value_text=approver.group(1).strip(), role="policy", locator=_locator_for(pages, "Approver"), quote=approver.group(0)))
    ceo = re.search(r"([A-Z][a-z]+ [A-Z][a-z]+)\s*Chief Executive Officer", full)
    if ceo:
        facts.append(_fact("signatory", value_text=ceo.group(1), role="policy", locator=_locator_for(pages, "Chief Executive"), quote=ceo.group(0)))

    clauses = [
        ("food_safety_non_negotiable", r"food safety and compliance are\s*non-negotiable"),
        ("quality_end_to_end", r"quality is end-to-end"),
        ("everyone_responsibility", r"food safety and quality are everyone.?s\s*responsibility"),
        ("never_compromise", r"we never compromise on food safety"),
    ]
    for name, pat in clauses:
        m = re.search(pat, full, re.I)
        if m:
            facts.append(_fact(name, value_text=m.group(0), role="policy_clause", locator=_locator_for(pages, m.group(0)[:18]), quote=_sentence_around(full, m.start())))

    complaint = re.search(r"complaint[s]?[^\n.]{0,80}(day|hours|deadline|within)", full, re.I)
    facts.append(
        _fact(
            "customer_complaint_deadline",
            value_text=complaint.group(0) if complaint else "not specified",
            role="policy_gap" if not complaint else "policy_clause",
            locator="document",
            quote=complaint.group(0) if complaint else "No customer-complaint resolution deadline found in the policy text.",
            provenance="calculated" if not complaint else "reported",
            extra={"present": bool(complaint)},
        )
    )
    return facts


def extract_generic_facts(ingested: dict, filename: str) -> list[dict]:
    pages = ingested.get("pages") or [{"page": 1, "text": ingested.get("preview", "")}]
    full = "\n".join(p.get("text", "") for p in pages)
    facts = []
    for m in re.finditer(r"\b([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+\.[0-9]+)\s*(kWh|kVA|MT|INR|Rs\.?|m3|Cu\.M)?", full):
        facts.append(
            _fact(
                "numeric_mention",
                value_num=_to_float(m.group(1)),
                unit=m.group(2),
                role="unclassified_amount",
                locator=_locator_for(pages, m.group(1)),
                quote=_quote_around(full, m.group(0)),
                confidence=0.4,
            )
        )
        if len(facts) > 25:
            break
    return facts


def _workbook_reconcile_across_sheets(facts: list[dict]) -> list[dict]:
    annuals = [f for f in facts if f["name"] == "electricity_consumption" and "annual" in f.get("role", "")]
    if len(annuals) >= 2:
        vals = {round(f["value_num"], 1): f for f in annuals if f.get("value_num") is not None}
        if len(vals) > 1:
            numbers = list(vals)
            facts.append(
                _fact(
                    "cross_sheet_conflict",
                    value_num=abs(numbers[0] - numbers[1]),
                    unit="kWh",
                    role="inconsistency",
                    locator="workbook",
                    quote=f"Annual totals differ across sheets: {numbers}",
                    provenance="calculated",
                    extra={"values": numbers},
                )
            )
    return facts


def postprocess_facts(facts: list[dict]) -> list[dict]:
    return _workbook_reconcile_across_sheets(facts)


def _fact(
    name: str,
    value_num: float | None = None,
    value_text: str | None = None,
    unit: str | None = None,
    role: str = "other",
    period_start: str | None = None,
    period_end: str | None = None,
    locator: str = "",
    quote: str | None = None,
    provenance: str = "reported",
    confidence: float = 0.85,
    extra: dict | None = None,
) -> dict:
    return {
        "name": name,
        "value_num": value_num,
        "value_text": value_text,
        "unit": unit,
        "role": role,
        "period_start": period_start,
        "period_end": period_end,
        "locator": locator,
        "quote": (quote or "")[:400],
        "provenance": provenance,
        "confidence": confidence,
        "extra_json": json.dumps(extra) if extra else None,
    }


def _guess_organization(text: str, filename: str) -> str | None:
    patterns = [
        r"BILL TO:\s*Name:\s*([^\n]+)",
        r"Name & Address:\s*([A-Za-z0-9 .,&\-]+(?:Pvt(?:\.|)\s*Ltd)?)",
        r"Name\s+and\s+Address of\s+the\s+Facility\s*:?\s*([A-Z0-9 .,&\-]+(?:PVT LTD|Ltd)?)",
        r"(TMTE METAL TECH PVT LTD)",
        r"(J KUMAR MEPL JV)",
        r"(Prestige Estates Projects Pvt\s*Ltd)",
        r"(Coca-Cola Beverages India Pvt\.?\s*Ltd\.?)",
        r"(Nestl[eé] S\.A\.)",
        r"(Adani Electricity Mumbai Limited)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip(" :")
    return None


def _utility_name(text: str, filename: str) -> str | None:
    if re.search(r"adani electricity", text, re.I) or "adani" in filename.lower():
        return "Adani Electricity Mumbai Limited"
    if re.search(r"\bbescom\b", text, re.I) or "bescom" in filename.lower():
        return "BESCOM"
    return None


def _find_header_row(df: pd.DataFrame) -> int | None:
    for i in range(min(6, len(df))):
        values = [str(v).lower() for v in df.iloc[i].tolist() if pd.notna(v)]
        joined = " ".join(values)
        if "kwh" in joined or "month" in joined or "quarter" in joined or "energy source" in joined:
            return i
    return 0 if len(df) else None


def _norm_header(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9%]+", "_", text).strip("_")
    aliases = {
        "grid_supply_kwh": "grid_supply_kwh",
        "grid_kwh": "grid_kwh",
        "solar_power_kwh": "solar_power_kwh",
        "wind_power_kwh": "wind_power_kwh",
        "total_renewable_kwh": "total_renewable_kwh",
        "total_consumption_kwh": "total_consumption_kwh",
        "total_kwh": "total_kwh",
        "grid_cost_inr": "grid_cost_inr",
        "renewable_savings_inr": "renewable_savings_inr",
        "net_cost_inr": "net_cost_inr",
        "savings_inr": "savings_inr",
        "cost_inr": "cost_inr",
        "annual_generation_kwh": "annual_generation_kwh",
    }
    return aliases.get(text, text)


def _col_letter(n: int) -> str:
    n = max(1, n)
    s = ""
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def _year_from_sheet_names(names: list[str]) -> int | None:
    for name in names:
        m = re.search(r"(20\d{2})", name)
        if m:
            return int(m.group(1))
    return None


def _year_from_text(text: str) -> int | None:
    m = re.search(r"(20\d{2})", text)
    return int(m.group(1)) if m else None


def _to_float(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    text = text.replace("₹", "").replace(",", "").replace("%", "")
    text = re.sub(r"[^\d.\-]", "", text)
    if text in {"", ".", "-", "-."}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _first_number(text: str, patterns: list[str]) -> float | None:
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return _to_float(m.group(1))
    return None


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return str(value)


def _norm_date(text: str) -> str | None:
    raw = re.sub(r"(st|nd|rd|th)", "", text, flags=re.I)
    raw = re.sub(r"\s+", " ", raw.strip())
    compact = raw.replace(" ", "")
    parsed = parse_iso_date(compact.replace(".", "-"))
    if parsed:
        return parsed.isoformat()
    from datetime import datetime

    for candidate in (raw, compact, compact.replace(".", "-")):
        for fmt in ISO_TRY + ["%d.%m.%Y", "%d %B %Y", "%d %b %Y"]:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
    try:
        return dateparser.parse(raw, dayfirst=True).date().isoformat()
    except (ValueError, OverflowError, TypeError):
        return None


def _find_billing_period(text: str) -> tuple[str, str] | None:
    m = re.search(
        r"(?:billing period|bill period|period)\s*[:\-]?\s*"
        r"(\d{1,2}(?:st|nd|rd|th)?[./\- ](?:\d{1,2}|[A-Za-z]+)[./\- ]?\d{2,4}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
        r"\s*(?:to|–|-)\s*"
        r"(\d{1,2}(?:st|nd|rd|th)?[./\- ](?:\d{1,2}|[A-Za-z]+)[./\- ]?\d{2,4}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        text,
        re.I,
    )
    if m:
        a, b = _norm_date(m.group(1)), _norm_date(m.group(2))
        if a and b:
            return a, b
    m = re.search(r"(\d{2}-\d{2}-\d{4})\s*-\s*(\d{2}[.-]\d{2}[.-]\d{4})", text)
    if m:
        a, b = _norm_date(m.group(1)), _norm_date(m.group(2))
        if a and b:
            return a, b
    return None


def _period_from_month_year(text: str) -> tuple[str, str] | None:
    m = re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(20\d{2})", text, re.I)
    if not m:
        return None
    bounds = parse_month_label(m.group(0))
    if not bounds:
        return None
    return bounds[0].isoformat(), bounds[1].isoformat()


def _meter_readings(text: str) -> list[dict]:
    facts = []
    present = _first_number(
        text,
        [
            r"present reading\s+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\s+([0-9]+(?:\.[0-9]+)?)",
            r"present\s+([0-9]{3,}(?:\.[0-9]+)?)",
            r"present reading\s+([0-9]+\.[0-9]+)",
        ],
    )
    previous = _first_number(
        text,
        [
            r"previous reading\s+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\s+([0-9]+(?:\.[0-9]+)?)",
            r"previous\s+([0-9]{3,}(?:\.[0-9]+)?)",
            r"previous reading\s+([0-9]+\.[0-9]+)",
        ],
    )
    mf = _first_number(text, [r"meter\s*constant\s*([0-9,]+)", r"multiplying\s*factor\s*([0-9,]+)"])
    if present is not None:
        facts.append(_fact("present_reading", value_num=present, role="meter_present", locator="meter table", quote=str(present)))
    if previous is not None:
        facts.append(_fact("previous_reading", value_num=previous, role="meter_previous", locator="meter table", quote=str(previous)))
    if mf is not None:
        facts.append(_fact("meter_constant", value_num=mf, role="meter_factor", locator="meter table", quote=str(mf)))
    return facts


def _locator_for(pages: list[dict], needle: str) -> str:
    needle = needle.lower()
    for page in pages:
        if needle in (page.get("text") or "").lower():
            return f"page {page.get('page', 1)}"
    return f"page {pages[0].get('page', 1) if pages else 1}"


def _quote_around(text: str, token: str, radius: int = 90) -> str:
    idx = text.lower().find(str(token).lower())
    if idx < 0:
        compact = str(token).replace(",", "")
        idx = text.replace(",", "").lower().find(compact.lower())
        if idx < 0:
            return str(token)
    start = max(0, idx - radius)
    end = min(len(text), idx + len(str(token)) + radius)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _sentence_around(text: str, idx: int) -> str:
    start = max(0, text.rfind(".", 0, idx) + 1)
    end = text.find(".", idx)
    if end < 0:
        end = min(len(text), idx + 220)
    return re.sub(r"\s+", " ", text[start:end + 1]).strip()


def _waste_category(window: str) -> str:
    m = re.search(r"(\d+\.\d+\s*[–-]\s*[A-Za-z][A-Za-z /]*)", window)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"(ETP sludge|Waste Powder|Phosphate\s*Sludge|residues containing\s*oil)", window, re.I)
    if m:
        return m.group(1)
    return "hazardous waste"
