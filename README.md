# AdaptiveAI

**Prompt-driven question generation from your own PDFs, with source-grounded RAG and adaptive assessments.**

This is a standalone, from-scratch full-stack project. It runs with one Python server and a dependency-free browser frontend. Claude (via the Anthropic API) does prompt interpretation, item writing, source checking, and descriptive grading; PDF search uses a free embedding model that runs locally; production code does not fabricate generated items when an AI service is unavailable.

## Features

- Password-protected accounts, signed seven-day access tokens, and owner-scoped records.
- Drag-and-drop multi-PDF upload; server-side signature, parse, size and page validation; private storage; status tracking and retry.
- PyMuPDF page extraction; page-aware overlapping chunks; local embedding model (fastembed, BAAI/bge-small-en-v1.5); normalized FAISS cosine search with persistent on-disk indexes, DB-backed vector IDs, and source metadata.
- Natural-language question preferences, structured controls, conflict detection, explicit review and approval, immutable original prompt.
- Topic-aware, document-restricted RAG question writing with Pydantic schemas, four-option MCQ checks, rubric mark checks, safe numerical-expression validation, evidence ID verification, and an independent LLM evidence check.
- Draft-only individual and full-set regeneration; new revisions preserve earlier versions.
- Idempotent session creation, one-question-at-a-time testing, deterministic MCQ/numerical grading, LLM rubric-based free-text grading, evaluation retry without losing the answer.
- Explainable, topic-aware difficulty decisions that preserve fixed difficulty/format constraints and cannot modify answered questions.
- Persisted score breakdowns, topic/difficulty analyses, adaptation history, question-level feedback, source-page revision recommendations, and linked new attempts.

## Requirements

- **Python 3.11 or 3.12** recommended (FAISS wheels must support your OS/Python version).
- An internet connection and an Anthropic API key with credit (console.anthropic.com) for Claude calls. The embedding model downloads once (~130 MB) into the storage folder and then runs offline.
- Approximately 1 GB disk headroom for Python libraries and indexes; more for large course materials.

### Windows PowerShell

```powershell
cd path\to\AdaptiveAI
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Open .env and set ANTHROPIC_API_KEY and a random ADAPTIVEAI_SECRET_KEY
python -m uvicorn app.main:app --reload --port 8000
```

### macOS / Linux

```bash
cd AdaptiveAI
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY and ADAPTIVEAI_SECRET_KEY
python -m uvicorn app.main:app --reload --port 8000
```

Open **http://127.0.0.1:8000** and register. To create a strong signing key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**Keep `.env` private.** Never paste your actual API key into the frontend or commit `.env` to Git.

### Docker (optional)

```bash
cp .env.example .env
# Configure the variables in .env
# ADAPTIVEAI_DATABASE_URL=sqlite:///./data/adaptiveai.db
# ADAPTIVEAI_STORAGE_DIR=./data
docker compose up --build
```

Docker is configured as a **single-worker** deployment to match the SQLite/background-task design. For public multi-instance deployments, move to Postgres, a durable task queue, distributed locking, a managed secrets service and an HTTP rate limiter before scaling.

## How to use

1. Create an account, upload one or more **text-based** PDFs and wait for `ready` (scanned/image-only PDFs will clearly fail with an OCR message).
2. Tick the indexed files you want for the assessment. A selected document may be removed from an ungenerated workspace **without deleting it**.
3. Describe the questions, optionally add question-count/format/difficulty preferences, and click **Interpret my prompt**.
4. Inspect the configuration and any conflicts; edit its counts, scope, and difficulty choices, then click **Approve configuration**.
5. Click **Generate questions**. The background job makes and verifies a complete draft; the browser polls its status.
6. Preview the question set **without answers**, optionally regenerate one item or the entire draft, then click **Start Assessment**.
7. Submit one answer at a time. If subjective evaluation fails, the answer is retained as pending and can be retried with identical text.
8. View overall, per-topic, per-difficulty and question-level results with actual adaptation decisions and cited source pages; repeat using your prompt without modifying history.
9. Click **Practice my weak areas** on any result. The app groups the questions from your attempts on the same PDFs into concept areas, scores each area (recent attempts weigh more), and builds a new attempt that puts most questions on your weakest areas: one level easier where you scored low, one level harder where you scored 75%+ last time, plus a few retention checks on strong areas. Earlier questions are never repeated verbatim.
10. When you finish that attempt, its results include **Compared with your previous attempt**: overall and per-area change, what improved, what slipped, what still needs work (with the questions you missed), whether gains came on easier questions, and your score trend across attempts on that material.

### Example assessment instruction

> Generate exactly 20 medium-to-hard questions from the uploaded Operating Systems material. Include 10 MCQs, 5 descriptive questions, and 5 scenario-based questions. Focus on deadlocks, semaphores and synchronization. Prioritize application questions. Do not use anything outside these PDFs.

If sources cannot substantiate the requested questions, generation fails **without publishing a partially verified set**. Adjust the request or upload richer course material.

## Architecture and data lifecycle

```
Browser (HTML/CSS/JS)  --same-origin /api--> FastAPI
                                              |-- Auth (Argon2 + signed tokens)
                                              |-- SQLAlchemy -> SQLite
                                              |-- PDF extractor -> Pages -> Chunks
                                              |-- Local embeddings (fastembed) -> FAISS index files
                                              |-- Topic-aware FAISS retrieval -> LLM question writer
                                              |-- Structural + source-grounding validators
                                              |-- Session grading + bounded adaptation
                                              `-- Analytics from persisted responses
```

Main modules:

| File | Responsibility |
|---|---|
| `app/main.py` | API, access control, lifecycle, background generation, upload and session endpoints |
| `app/models.py`, `app/db.py` | Relational schema, ownership, indexes, transactions |
| `app/documents.py` | PDF validation, extraction, chunking, FAISS indexing/retrieval and retries |
| `app/ai.py` | Real model provider adapter, interpretation, writing, evidence checking, rubric grading |
| `app/assessment.py` | Configuration validation, question checks, generation planning, grading, adaptation, analytics |
| `app/security.py` | Password hashing, bearer tokens, access checks |
| `app/static/` | Responsive interface with no JS build step |
| `tests/` | Deterministic API/service tests (provider and FAISS doubles in tests only) |

**Database tables:** `users`, `documents`, `pages`, `chunks`, `drafts`, `draft_questions`, `assessments`, `session_questions`, `responses`, `adaptations`.

On a **fresh installation**, `Base.metadata.create_all()` initializes these tables automatically at startup. The initial schema is defined in `app/models.py`. This is a **new project, not a migration of an existing database**; use a proper schema migration tool such as Alembic before deploying incompatible schema upgrades to databases containing real student data.

The storage directory (`./data` by default) contains uploaded PDFs and FAISS `.faiss` index files. `chunks.vector_id` maps the FAISS result offset to its stable UUID, page numbers and document ID. Index files are updated with atomic file replacement. Restart-interrupted jobs become retryable failures. The raw source files are not exposed by static routes.

## Configuration

See `.env.example` for all variables. Important settings:

| Variable | What it controls |
|---|---|
| `ANTHROPIC_API_KEY` | Claude requests: interpreting prompts, writing/checking questions, grading free text, grouping weak areas |
| `ADAPTIVEAI_EFFORT` | Claude effort per call (`low` default keeps spend down; `medium`/`high` for more careful items) |
| `ADAPTIVEAI_CHAT_MODEL` | Model for interpretation, creation, verification and grading |
| `ADAPTIVEAI_EMBED_MODEL` | Local embedding model; on change, PDFs are re-indexed automatically at startup (chunk IDs are kept) |
| `ADAPTIVEAI_DATABASE_URL` | SQLite database connection string |
| `ADAPTIVEAI_STORAGE_DIR` | Private documents and FAISS indexes |
| `ADAPTIVEAI_SECRET_KEY` | Server token signing secret; use random 64+ hex digits |
| `ADAPTIVEAI_MAX_UPLOAD_MB`, `ADAPTIVEAI_MAX_PAGES` | PDF safety limits |
| `ADAPTIVEAI_MAX_DOCUMENTS`, `ADAPTIVEAI_MAX_QUESTIONS` | Workspace / assessment limits |
| `ADAPTIVEAI_CHUNK_CHARS`, `ADAPTIVEAI_CHUNK_OVERLAP` | RAG segmentation |
| `ADAPTIVEAI_RETRIEVAL_TOP_K` | FAISS retrieval limit |
| `ADAPTIVEAI_ADAPT_MIN_ANSWERS`, `ADAPTIVEAI_ADAPT_HIGH`, `ADAPTIVEAI_ADAPT_LOW` | Explainable adaptive thresholds |

## Verify the project

```bash
python -m pytest -q
python -m compileall -q app
node --check app/static/app.js  # optional: checks JavaScript syntax if Node is installed
```

Tests use an in-test FAISS/embedding/LLM adapter **only for deterministic test isolation**. The running application never uses that adapter. Actual live-provider + actual-FAISS integration requires dependencies and your own configured credentials.

To test real operations manually, run the server with the key set, upload two PDFs, check the `ready` state and source pages, interpret an instruction, approve it, generate, regenerate one question, start a session, answer everything, refresh and inspect analytics. Expect paid provider calls; generated items undergo a second LLM verification pass.

## API overview

- `POST /api/auth/register`, `/api/auth/login`; `GET /api/auth/me`
- `GET/POST /api/documents`; `GET/DELETE /api/documents/{id}`; `GET /api/documents/{id}/file` (authorized inline PDF); `POST /api/documents/{id}/retry`
- `POST /api/drafts`; `GET /api/drafts/{id}`; `DELETE /api/drafts/{id}/documents/{docId}`
- `POST /api/drafts/{id}/interpret`, `/approve`, `/generate`, `/start`
- `POST /api/drafts/{id}/questions/{questionId}/regenerate`
- `GET /api/assessments`, `GET /api/assessments/{id}`, `POST /api/assessments/{id}/answer`
- `GET /api/assessments/{id}/analytics`, `POST /api/assessments/{id}/repeat`
- `POST /api/assessments/{id}/focus` (optional `{"question_count": n}`) — builds and generates a weak-area attempt; analytics then include `comparison` and `focus_plan`
- Built-in interactive API docs: **http://127.0.0.1:8000/docs**

All user-data endpoints require an `Authorization: Bearer <token>` header. Initial preview and assessment payloads intentionally omit reference answers and rubrics. Only authorized post-submission feedback and analytics include those fields.

## Known limitations and deployment cautions

1. **Claude spend.** Every Claude call logs its tokens and an estimated cost (`Claude claude-opus-5-5: ... = ~$0.0123`) in the server output. Defaults keep spend low: `ADAPTIVEAI_EFFORT=low`, schema-constrained JSON (no paid retries for malformed output), only the relevant settings sent to the writer, and cached source passages. For the lowest cost set `ADAPTIVEAI_CHAT_MODEL=claude-haiku-5-5` (about 1/40 of Opus), at some cost to question quality and fact-checking.
2. Image-only PDFs require an external OCR step before upload; OCR is not silently faked.
3. BackgroundTasks and in-process locks are suitable for a **single-server project**, not a durable distributed job system. Jobs interrupted by restart are marked failed for retry. Long-running per-answer LLM adaptation is synchronous by design.
4. JSON-mode LLM output and independent LLM evidence checks can reduce errors but **cannot mathematically guarantee factual correctness**; subjective grading should be reviewed when consequential.
5. The default SQLite database and simple bearer-token authentication are appropriate for development and a modest project. Before public production, implement rate limits, monitoring, durable job processing, operational backups, migration management, secret rotation and a formal security review.
6. Question/topic diversity uses topic rotation and duplicate-text checks plus instruction-based model verification. Semantic near-duplicates may still require manual preview/regeneration.

See `TEST_RESULTS.md` for actual checks performed during creation.
