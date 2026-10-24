# Evaluation

Cases live in `evals/cases.json`. Expected numbers were read from the source files, not from model output. Saved runs: `evals/baseline.json`, `evals/improved.json`, `evals/comparison.json`.

Each case has a question, expected numbers/phrases, and source references.

## Source references (checked on the files)

| Case | Expected | Source |
| --- | --- | --- |
| bills_consumption_period | 47407 kWh Apr 2024; 63000 kWh Jun 2024 | Adani bill page 1; BESCOM bill page 1 |
| water_meter_match | 14821 − 12500 = 2321 m3 | water_bill_invoice.pdf page 1 |
| waste_total | 1.0 + 0.5 + 0.5 + 0.4 = 2.4 MT | form 3 filled.docx.pdf |
| workbook_monthly_vs_annual | 2425123 vs 2427123 kWh | Annual Electricity Consumption.xlsx monthly vs quarterly annual cells |
| policy_complaint_deadline | no complaint SLA | quality_policy_nestle.pdf |
| fy_2024_25_electricity | Adani + BESCOM only, different orgs, incomplete year | bills in FY 2024-25; workbook is calendar 2026 |
| fy_2025_26_coverage | water Dec 2025 + workbook Jan–Mar 2026 | water bill; Excel months |
| adani_amount_roles | 632023.17 current month vs 631515.48 payable | Adani bill amount block |
| missing_diesel | no diesel in pack | all six files |
| org_mismatch | cannot add into one company total | different bill-to names |
| form3_date_inconsistency | rows 10.03.2023 vs signature 08.03.2022 | Form 3 |
| policy_food_safety | food safety non-negotiable; Executive Board, June 2026 | Nestle policy |

## 1. First working version (baseline)

BM25 over page/sheet chunks, then a stitched answer. No amount roles, no reported vs calculated, no FY engine.

**5 / 12 passed, average 0.636.**

It could echo policy wording. It failed when structure mattered: two electricity bills, water meter math, several rupee figures on Adani, monthly vs quarterly Excel totals, FY coverage.

## 2. Weakness

The damaging failure is **undifferentiated numbers**. Adani lists current-month charges, net payable, and amount after due date. The Excel lists three different annual totals (2,425,123 / 2,426,123 / 2,427,123 kWh). Chunk search treats them as interchangeable.

A second failure: the workbook’s many month-rows crowd out Form 3 dates and sheet conflicts.

## 3. Targeted improvement

Improved mode keeps the same ingest, then:

- stores typed facts with a role, locator, and reported vs calculated
- recalculates meter consumption and the sum of monthly rows
- flags Excel sheet conflicts and the Form 3 signature/row date clash
- applies Indian FY (1 April–31 March), totals per organisation
- always injects conflict / gap facts so month-rows cannot bury them

If an LLM key is set, it may only rephrase. It cannot invent figures.

## 4. Same cases, rerun

**12 / 12 passed, average 1.000.**

| Case | Baseline | Improved | Delta |
| --- | --- | --- | --- |
| bills_consumption_period | 0.33 fail | 1.00 pass | +0.67 |
| water_meter_match | 0.67 fail | 1.00 pass | +0.33 |
| waste_total | 0.50 fail | 1.00 pass | +0.50 |
| workbook_monthly_vs_annual | 0.20 fail | 1.00 pass | +0.80 |
| policy_complaint_deadline | 1.00 pass | 1.00 pass | 0 |
| fy_2024_25_electricity | 0.60 fail | 1.00 pass | +0.40 |
| fy_2025_26_coverage | 0.75 pass | 1.00 pass | +0.25 |
| adani_amount_roles | 0.75 pass | 1.00 pass | +0.25 |
| missing_diesel | 0.50 fail | 1.00 pass | +0.50 |
| org_mismatch | 1.00 pass | 1.00 pass | 0 |
| form3_date_inconsistency | 0.33 fail | 1.00 pass | +0.67 |
| policy_food_safety | 1.00 pass | 1.00 pass | 0 |

## 5. What improved, what regressed, what remains

**Improved.** Bill periods and kWh by organisation; water meter arithmetic; waste 2.4 MT as calculated; workbook internal conflict; Adani amount roles; FY 2024-25 / 2025-26 coverage; Form 3 dates; explicit diesel and complaint-SLA gaps.

**Regressed.** None of the 12 cases lost score.

**Still unresolved.**

- BESCOM page 2 is an image
- Adani TOD lines are noisy in the PDF text
- Form 3 category labels are a bit fragmented
- Scoring checks numbers/phrases and evidence presence, not that every citation is the best page
