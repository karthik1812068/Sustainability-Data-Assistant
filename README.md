# Sustainability Data Assistant

KuKi engineering assignment. Upload sustainability documents, process them in the background, extract facts with source locators, answer questions with evidence, and handle Indian financial years (1 April to 31 March).

No LLM key is required. Extraction, Q&A, FY coverage, and the 12 eval cases all run offline.

## Run

Python 3.11+ (tested on 3.12).

Windows:

```powershell
cd d:\kuki-assignment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000

On **Documents**, either:

- **Choose files** — one PDF, or several together
- **Upload all 6 files** — dumps the assignment dataset at once

Each row shows queued / reading / extracting / completed / failed. If one file fails, the others still finish. Duplicate files are skipped (SHA-256). Status is stored in SQLite at `data/kuki.db` and is still there after a restart.

Reproduce the evaluation:

```powershell
python -m app.run_eval
```

This processes the files, runs the same 12 cases in **baseline** then **improved** mode, and writes `evals/baseline.json`, `evals/improved.json`, `evals/comparison.json`.

Optional: set `OPENAI_API_KEY`, `GROQ_API_KEY`, or `GEMINI_API_KEY` in `.env`. The model may only rephrase. Numbers still come from extracted facts.

## Dataset

| File | What it is |
| --- | --- |
| Adani commercial electricity bill.pdf | J Kumar MEPL JV, Mumbai, Apr 2024, 47407 kWh |
| BESCOM Commercial Electricity Bill.pdf | Prestige Estates, Bengaluru, Jun 2024, 63000 kWh |
| water_bill_invoice.pdf | Coca-Cola, Hyderabad, Dec 2025, 2321 m3 |
| form 3 filled.docx.pdf | TMTE Metal Tech, Chennai, hazardous waste |
| quality_policy_nestle.pdf | Nestle quality policy |
| Annual Electricity Consumption.xlsx | Monthly electricity workbook (calendar 2026) |

These files are from different companies and years. Totals are never merged into one company figure.

## How it maps to the brief

**Backend and background processing (30%).** FastAPI accepts one file or a whole batch. Each file becomes its own SQLite job. A worker ingests then extracts, with exponential backoff (3 attempts). A failed PDF does not block the rest of the batch. The same hash is not processed twice. Jobs left in `processing` after a crash are re-queued on startup.

**Extraction and answers, including FY (25%).** PDFs keep page locators. Excel keeps sheet + cell range. Facts are `reported` or `calculated` (for example present reading minus previous reading). Improved Q&A uses those facts, amount roles, organisation boundaries, and Indian FY windows. Baseline is BM25 over raw chunks, kept weaker on purpose.

**Evaluation and improvement (20%).** 12 cases in `evals/cases.json`, numbers checked against the source files. Baseline 5/12. Improved 12/12. Write-up in `EVALUATION.md`.

**Dashboard (5%).** Five tabs: Documents, Extracted facts, Ask (with document checkboxes), Financial year, Evaluation. All numbers come from the API.

**Code and explanation (20%).** One command to run. One command to re-run evals. Layout:

```
app/                         API, worker, ingest, extract, QA, eval
static/                      dashboard
evals/                       cases + saved baseline / improved / comparison
kuki-assignment-dataset/     the 6 source files
data/                        created at runtime (kuki.db + uploads)
```

## Approach

1. Upload + queue — SHA-256, SQLite jobs, asyncio worker, restart recovery.
2. Ingest — PyMuPDF pages, openpyxl sheet/cell ranges.
3. Extract — typed facts with role, locator, reported vs calculated.
4. Ask — baseline = chunk RAG. Improved = facts + roles + FY. Optional LLM rewrite only.
5. Financial year — FY 2024-25 = 1 Apr 2024 to 31 Mar 2025. Jan–Mar 2026 is FY 2025-26; Apr–Dec 2026 is FY 2026-27.

## Evaluation (saved)

| Mode | Passed | Avg score | Pass rate |
| --- | --- | --- | --- |
| Baseline (chunk search) | 5 / 12 | 0.636 | 42% |
| Improved (facts + FY + roles) | 12 / 12 | 1.000 | 100% |

No case got worse. Details in `EVALUATION.md`.

## Limitations

- BESCOM page 2 is a scan. Only page 1 has text.
- Adani PDF text is messy. Main amounts are recovered; some TOD lines can be missed.
- Without an LLM key, policy answers are extractive.
- Bills that cross 31 March / 1 April are flagged, not auto-split.
- Search is BM25, not embeddings. Enough for 6 documents.
