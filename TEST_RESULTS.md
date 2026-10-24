# Verification record

## Automated checks

Executed in the workspace `.venv` (Python 3.14.3):

```text
.\.venv\Scripts\python.exe -m pytest -q
34 passed in 6.24s

.\.venv\Scripts\python.exe -m compileall -q app tests
node --check .\app\static\app.js
```

Python compilation and JavaScript syntax validation both exited successfully. The test suite covers PDF upload/index lifecycle, source mapping, authorization, configuration validation, duplicate and near-duplicate generation, citation-scoped evidence checks and replacement, regeneration preservation, numerical grading, non-finite grading rejection, adaptive replacement constraints, analytics, and pending-answer recovery. Tests use deterministic provider and FAISS doubles.

## Integration and environment limits

- A local FAISS 1.15.1 smoke check created an in-memory index, added vectors, and retrieved the expected nearest vector.
- The OpenAI client object constructed successfully, but no API request was made; paid provider behavior and the full live workflow remain **not tested**.
- The installed `openai` version is 3.29.0, outside the declared `openai>=1.50,<3` requirement. `pip check` also reports `httptools 0.9.0`, `PyYAML 6.0.3`, `SQLAlchemy 2.1.4`, and `websockets 17.2` as unsupported on this platform. Passing mocked tests do not establish that this environment is suitable for live provider use.
- The README recommends Python 3.11 or 3.12. The active workspace venv is Python 3.14.3; use a clean supported environment with dependencies installed from `requirements.txt` before performing a paid end-to-end check.
- No `.env` credentials, uploaded documents, indexes, or persistent user database were changed as part of this audit.

## Question-generation fix (2026-10-12)

Executed in `.venv-clean` (Python 3.11.9): `python -m pytest -q` → 38 passed.

Live OpenAI runs (`gpt-4o-mini`, `text-embedding-3-small`) against a copy of `data/` using `Module 3.6.pdf` (3 pages on ALOHA and MAC addressing):

- Original request (15 MCQs on TCP congestion control, routing, subnetting, network-layer protocols): now fails in ~7 s with "The selected PDFs do not cover "TCP congestion control"…" instead of the previous "Duplicate question".
- 6 questions on ALOHA + MAC addresses (3 MCQ, 2 numerical, 1 short answer): 2/2 runs succeeded (~40 s).
- 10 questions on the same topics: 2/3 runs succeeded; the failing run reported "Only 2 of 3 … mcq questions on "ALOHA"", which is a genuine source-size limit for a 3-page sheet.

## Weak-area practice and attempt comparison (2026-10-12)

`python -m pytest -q` → 44 passed (`tests/test_remediation.py` covers plan allocation, label fallback, thin-area hand-off, plan edits and the comparison report).

Live OpenAI checks on a copy of `data/`: weak-area plans built from both real attempts (concept grouping ~5 s). The paging plan generated 10 targeted questions in 56 s. The networking run was cut short when the OpenAI account ran out of credits (429 `insufficient_quota`). The full UI flow (plan → attempt → comparison → second-round plan) was then walked through on a separate local server using the data copy with stand-in question writing.

## Switch to Claude + local embeddings (2026-10-12)

`python -m pytest -q` → 44 passed (Claude request shape: schema, `effort: low`, cached passages, refusal fallbacks; interpretation list→map conversion; credit-exhausted message; local embedding validation).

Local embeddings (fastembed `BAAI/bge-small-en-v1.5`, 384 dims) re-indexed a copy of all three PDFs in 22 s including the one-time model download; chunk IDs were preserved, and spot-check searches returned the expected pages (MAC addressing p.3, ALOHA collision p.1-2, TLB p.29-32, internal fragmentation p.49-50). No Claude request has been made yet: no Anthropic API key was configured.
