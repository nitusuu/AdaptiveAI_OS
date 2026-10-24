"""AdaptiveAI REST API and static single-page application."""
import logging
import os
import re
import threading
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, update, func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from argon2.exceptions import VerifyMismatchError, VerificationError
from app.config import settings
from app.db import Base, engine, get_db
from app.models import User, Document, Draft, DraftQuestion, Assessment, SessionQuestion, Response, Adaptation, Chunk, uid, now
from app.security import ph, token_for, current_user, owned
from app.documents import validate_pdf, process_document, index_path
from app.ai import ProviderUnavailable, interpret_prompt
from app.assessment import (build_configuration, validate_configuration, get_documents,
    generate_set, generate_group, draft_preview, safe_question, grade, session_lock,
    next_question, progress, adapt, assessment_report, focus_plan_matches, sync_focus_plan)
from app.remediation import attempt_items, build_focus_plan, compare_with_previous, focus_prompt
from app.documents import retrieve

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    if os.getenv('ADAPTIVEAI_ENV', 'development') == 'production' and (len(settings.adaptiveai_secret_key) < 32 or 'REPLACE' in settings.adaptiveai_secret_key.upper() or settings.adaptiveai_secret_key.startswith('development-only')):
        raise RuntimeError('Set a strong ADAPTIVEAI_SECRET_KEY for production')
    settings.storage_path.mkdir(parents=True, exist_ok=True)
    if settings.adaptiveai_database_url.startswith('sqlite:///'):
        Path(settings.adaptiveai_database_url.replace('sqlite:///', '', 1)).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    from app.db import SessionLocal
    with SessionLocal() as db:
        for doc in db.scalars(select(Document).where(Document.status.in_(['extracting','chunking','embedding','indexing','uploading']))).all():
            doc.status = 'failed'
            doc.error = 'Indexing interrupted by application restart. Retry processing.'
        for draft in db.scalars(select(Draft).where(Draft.state == 'generating')).all():
            draft.state = 'failed'
            draft.error = 'Generation interrupted. Retry generation.'
        stale = db.scalars(select(Document).where(Document.status == 'ready', Document.embedding_model != settings.adaptiveai_embed_model)).all()
        for doc in stale:
            doc.status, doc.error = 'uploading', None
        stale_ids = [doc.id for doc in stale]
        db.commit()
    if stale_ids:
        logger.info('Re-indexing %d PDF(s) for embedding model %s', len(stale_ids), settings.adaptiveai_embed_model)
        threading.Thread(target=lambda: [process_document(doc_id) for doc_id in stale_ids], daemon=True).start()
    yield


app = FastAPI(title='AdaptiveAI', version='1.0.0', lifespan=lifespan)
@app.middleware('http')
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if request.url.path not in ('/docs', '/redoc', '/openapi.json'):
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'"
    return response


app.mount('/assets', StaticFiles(directory=str(Path(__file__).parent/'static')), name='assets')


@app.get('/')
def home():
    return FileResponse(Path(__file__).parent/'static'/'index.html')


@app.get('/health')
def health():
    return {'status': 'ok'}


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=256)


@app.post('/api/auth/register')
def register(payload: Credentials, db: Session = Depends(get_db)):
    email = payload.email.lower().strip()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise HTTPException(422, 'A valid email address is required')
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, 'Email is already registered')
    user = User(email=email, password_hash=ph.hash(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Email is already registered') from None
    return {'token': token_for(user), 'email': user.email}


@app.post('/api/auth/login')
def login(payload: Credentials, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.lower().strip()))
    try:
        verified = bool(user and ph.verify(user.password_hash, payload.password))
    except (VerifyMismatchError, VerificationError):
        verified = False
    if not verified:
        raise HTTPException(401, 'Invalid login credentials')
    return {'token': token_for(user), 'email': user.email}


@app.get('/api/auth/me')
def me(user: User = Depends(current_user)):
    return {'id': user.id, 'email': user.email}


def doc_out(d: Document):
    return {'id': d.id, 'filename': d.filename, 'size_bytes': d.size_bytes, 'page_count': d.page_count, 'status': d.status, 'error': d.error, 'created_at': d.created_at.isoformat()}


@app.get('/api/documents')
def documents(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [doc_out(d) for d in db.scalars(select(Document).where(Document.owner_id == user.id).order_by(Document.created_at.desc())).all()]


@app.post('/api/documents')
async def upload(files: list[UploadFile] = File(...), background: BackgroundTasks = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not files or len(files) > settings.adaptiveai_max_documents:
        raise HTTPException(400, f'Choose between 1 and {settings.adaptiveai_max_documents} PDFs per upload')
    staged = []
    for file in files:
        name = Path((file.filename or 'document.pdf').replace('\\','/')).name[:200]
        if not name.lower().endswith('.pdf'):
            raise HTTPException(400, f'{name}: only PDFs are supported')
        blob = await file.read(settings.adaptiveai_max_upload_mb * 1024 * 1024 + 1)
        if len(blob) > settings.adaptiveai_max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f'{name}: file exceeds configured upload limit')
        if not blob:
            raise HTTPException(400, f'{name}: empty file')
        try:
            pages = validate_pdf(blob)
        except ValueError as exc:
            raise HTTPException(400, f'{name}: {exc}') from exc
        staged.append((name, blob, pages, sha256(blob).hexdigest()))
    result = []
    for name, blob, pages, digest in staged:
        previous = db.scalar(select(Document).where(Document.owner_id == user.id, Document.sha256 == digest, Document.status == 'ready'))
        if previous:
            result.append(doc_out(previous))
            continue
        doc_id = uid()
        base = settings.storage_path / 'uploads' / user.id
        base.mkdir(parents=True, exist_ok=True)
        path = base / f'{doc_id}.pdf'
        path.write_bytes(blob)
        doc = Document(id=doc_id, owner_id=user.id, filename=name, storage_key=str(path), sha256=digest, size_bytes=len(blob), page_count=pages, status='uploading')
        db.add(doc)
        db.commit()
        result.append(doc_out(doc))
        background.add_task(process_document, doc.id)
    return result


@app.get('/api/documents/{doc_id}')
def document_status(doc_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return doc_out(owned(db, Document, doc_id, user.id))


@app.get('/api/documents/{doc_id}/file')
def read_private_document(doc_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    doc = owned(db, Document, doc_id, user.id)
    path = Path(doc.storage_key)
    if not path.is_file():
        raise HTTPException(404, 'Stored PDF file is unavailable')
    return FileResponse(path, media_type='application/pdf', filename=doc.filename,
                        content_disposition_type='inline')


@app.post('/api/documents/{doc_id}/retry')
def retry_document(doc_id: str, background: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(current_user)):
    doc = owned(db, Document, doc_id, user.id)
    if doc.status != 'failed':
        raise HTTPException(409, 'Only failed indexing jobs can be retried')
    doc.status, doc.error = 'uploading', None
    db.commit()
    background.add_task(process_document, doc.id)
    return doc_out(doc)


@app.delete('/api/documents/{doc_id}')
def delete_document(doc_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    doc = owned(db, Document, doc_id, user.id)
    for draft in db.scalars(select(Draft).where(Draft.owner_id == user.id)).all():
        if doc.id in draft.document_ids:
            raise HTTPException(409, 'Document is referenced by a draft or assessment. Remove it from the draft workspace first; historical sources are retained.')
    if doc.status not in ('ready', 'failed'):
        raise HTTPException(409, 'Cannot delete a document while it is processing')
    path = Path(doc.storage_key)
    db.delete(doc)
    db.commit()
    path.unlink(missing_ok=True)
    index_path(doc.id).unlink(missing_ok=True)
    return {'deleted': True}


class DraftInput(BaseModel):
    prompt: str = Field(min_length=10, max_length=12000)
    document_ids: list[str] = Field(min_length=1)
    controls: dict = Field(default_factory=dict)
    parent_session_id: str | None = None


@app.post('/api/drafts')
def create_draft(payload: DraftInput, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if len(set(payload.document_ids)) != len(payload.document_ids) or len(payload.document_ids) > settings.adaptiveai_max_documents:
        raise HTTPException(400, 'Duplicate or too many selected PDFs')
    docs = db.scalars(select(Document).where(Document.id.in_(payload.document_ids), Document.owner_id == user.id)).all()
    if len(docs) != len(payload.document_ids):
        raise HTTPException(404, 'One or more documents unavailable')
    if any(d.status != 'ready' for d in docs):
        raise HTTPException(409, 'Wait for all selected documents to finish indexing')
    if payload.parent_session_id:
        owned(db, Assessment, payload.parent_session_id, user.id)
    draft = Draft(owner_id=user.id, prompt=payload.prompt, document_ids=payload.document_ids, controls=payload.controls, parent_session_id=payload.parent_session_id)
    db.add(draft)
    db.commit()
    return {'id': draft.id, 'state': draft.state}


def draft_info(db, draft):
    return {'id': draft.id, 'prompt': draft.prompt, 'document_ids': draft.document_ids, 'controls': draft.controls, 'configuration': draft.configuration, 'conflicts': draft.conflicts, 'state': draft.state, 'error': draft.error, 'questions': draft_preview(db, draft.id)}


@app.get('/api/drafts/{draft_id}')
def get_draft(draft_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return draft_info(db, owned(db, Draft, draft_id, user.id))


@app.delete('/api/drafts/{draft_id}/documents/{doc_id}')
def detach_document(draft_id: str, doc_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Remove a document from a workspace without deleting the uploaded private file."""
    draft = owned(db, Draft, draft_id, user.id)
    if doc_id not in draft.document_ids:
        raise HTTPException(404, 'Document not selected in this workspace')
    if draft.state in ('generated', 'generating') or db.scalar(select(Assessment.id).where(Assessment.draft_id == draft.id)):
        raise HTTPException(409, 'Cannot change sources once questions are generated or an assessment is started')
    draft.document_ids = [x for x in draft.document_ids if x != doc_id]
    draft.configuration, draft.conflicts, draft.state = None, [], 'created'
    db.commit()
    return draft_info(db, draft)


@app.post('/api/drafts/{draft_id}/interpret')
def interpret(draft_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    draft = owned(db, Draft, draft_id, user.id)
    if draft.state in ('generating', 'generated'):
        raise HTTPException(409, 'This draft already has generated questions; create a new draft to change the request')
    try:
        docs = get_documents(db, draft)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        raw = interpret_prompt(draft.prompt)
        config, conflicts = build_configuration(raw, draft.controls, docs, draft.prompt)
        draft.configuration, draft.conflicts, draft.state, draft.error = config, conflicts, 'review', None
        db.commit()
    except Exception as exc:
        db.rollback()
        draft = db.get(Draft, draft.id)
        draft.state, draft.error = 'failed', str(exc)[:1000]
        db.commit()
        raise HTTPException(503, str(exc)) from exc
    return draft_info(db, draft)


class Approval(BaseModel):
    configuration: dict


@app.post('/api/drafts/{draft_id}/approve')
def approve(draft_id: str, payload: Approval, db: Session = Depends(get_db), user: User = Depends(current_user)):
    draft = owned(db, Draft, draft_id, user.id)
    # A failed generation keeps its configuration so the student can adjust it (e.g. fewer questions).
    if draft.state not in ('review', 'approved', 'generated') and not (draft.state == 'failed' and draft.configuration):
        raise HTTPException(409, 'Interpret the prompt before approval')
    config = payload.configuration
    if config.get('focus_plan') and not focus_plan_matches(config):
        # The student edited counts/topics of a weak-area plan; generate from the edited values instead.
        config = {k: v for k, v in config.items() if k != 'focus_plan'}
    try:
        validate_configuration(config)
        get_documents(db, draft, config['document_ids'])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # User has explicitly reviewed and resolved conflicts or changed the proposed values.
    if draft.state == 'generated' and config != draft.configuration:
        raise HTTPException(409, 'Create a new draft to change an already generated configuration')
    draft.configuration, draft.conflicts, draft.state, draft.error = config, [], 'approved' if draft.state != 'generated' else 'generated', None
    db.commit()
    return draft_info(db, draft)


def run_generation(draft_id: str):
    """Background worker owns its DB session; all new questions committed atomically."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        draft = db.get(Draft, draft_id)
        if not draft or draft.state != 'generating':
            return
        try:
            existing = db.scalars(select(DraftQuestion).where(DraftQuestion.draft_id == draft.id)).all()
            questions = generate_set(
                db,
                draft,
                draft.configuration,
                existing=[row.payload['question_text'] for row in existing],
            )
            version_by_slot = {}
            for old in existing:
                version_by_slot[old.slot] = max(old.revision, version_by_slot.get(old.slot, 0))
                old.active = False
            for index, question in enumerate(questions, 1):
                db.add(DraftQuestion(draft_id=draft.id, slot=index, revision=version_by_slot.get(index, 0)+1, payload=question))
            draft.configuration = sync_focus_plan(draft.configuration, questions)
            draft.state, draft.error = 'generated', None
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.exception('Question generation failed for draft %s', draft_id)
            draft = db.get(Draft, draft_id)
            if draft:
                has_old_set = bool(db.scalar(select(DraftQuestion.id).where(DraftQuestion.draft_id == draft_id, DraftQuestion.active == True)))
                draft.state = 'generated' if has_old_set else 'failed'
                draft.error = f'Generation failed; {"previous questions preserved" if has_old_set else "no questions were published"}: {str(exc)[:700]}'
                db.commit()


@app.post('/api/drafts/{draft_id}/generate', status_code=202)
def generate(draft_id: str, background: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(current_user)):
    draft = owned(db, Draft, draft_id, user.id)
    if draft.state not in ('approved', 'generated', 'failed') or not draft.configuration:
        raise HTTPException(409, 'Approve the interpreted configuration before generating')
    if draft.conflicts:
        raise HTTPException(409, 'Resolve configuration conflicts first')
    try:
        validate_configuration(draft.configuration)
        get_documents(db, draft, draft.configuration['document_ids'])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    draft.state, draft.error = 'generating', None
    db.commit()
    background.add_task(run_generation, draft.id)
    return {'id': draft.id, 'state': draft.state, 'status_url': f'/api/drafts/{draft.id}'}


@app.post('/api/drafts/{draft_id}/questions/{question_id}/regenerate')
def regenerate_question(draft_id: str, question_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    draft = owned(db, Draft, draft_id, user.id)
    q = db.get(DraftQuestion, question_id)
    if draft.state != 'generated' or not q or q.draft_id != draft.id or not q.active:
        raise HTTPException(404, 'Active draft question not found')
    try:
        docs = get_documents(db, draft, draft.configuration['document_ids'])
        current = db.scalars(select(DraftQuestion).where(DraftQuestion.draft_id == draft.id, DraftQuestion.active == True)).all()
        evidence = retrieve(db, docs, [q.payload['topic'], q.payload['question_text']])
        known = [x.payload['question_text'] for x in current]
        validated = generate_group(
            draft.configuration,
            evidence,
            q.payload['question_type'],
            q.payload['difficulty'],
            1,
            existing=known,
            focus=q.payload['topic'],
        )[0]
        new = DraftQuestion(draft_id=draft.id, slot=q.slot, revision=q.revision+1, payload=validated, replaced_from=q.id)
        db.add(new)
        db.flush()
        q.active = False
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(503, f'Regeneration failed; original was preserved: {exc}') from exc
    return draft_info(db, draft)


class StartInput(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=100)


@app.post('/api/drafts/{draft_id}/start')
def start(draft_id: str, payload: StartInput, db: Session = Depends(get_db), user: User = Depends(current_user)):
    existing = db.scalar(select(Assessment).where(Assessment.owner_id == user.id, Assessment.start_key == payload.idempotency_key))
    if existing:
        if existing.draft_id != draft_id:
            raise HTTPException(409, 'Idempotency key already used for another draft')
        return {'session_id': existing.id}
    draft = owned(db, Draft, draft_id, user.id)
    if draft.state != 'generated' or draft.conflicts:
        raise HTTPException(409, 'Generate a valid approved draft first')
    questions = db.scalars(select(DraftQuestion).where(DraftQuestion.draft_id == draft_id, DraftQuestion.active == True).order_by(DraftQuestion.slot)).all()
    config = draft.configuration
    validate_configuration(config)
    from collections import Counter
    if len(questions) != config['question_count'] or Counter(q.payload['question_type'] for q in questions) != Counter(config['type_distribution']):
        raise HTTPException(409, 'The generated question set violates approved counts')
    fixed = config.get('difficulty_distribution') or {}
    if fixed and Counter(q.payload['difficulty'] for q in questions) != Counter({k:v for k,v in fixed.items() if v}):
        raise HTTPException(409, 'The generated difficulty distribution is invalid')
    assessment = Assessment(owner_id=user.id, draft_id=draft.id, start_key=payload.idempotency_key, prompt=draft.prompt, configuration=config, initial_snapshot=[q.payload for q in questions])
    try:
        db.add(assessment)
        db.flush()
        for question in questions:
            db.add(SessionQuestion(session_id=assessment.id, slot=question.slot, payload=question.payload, revision=1))
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Assessment).where(Assessment.owner_id == user.id, Assessment.start_key == payload.idempotency_key))
        if not existing: raise
        assessment = existing
    return {'session_id': assessment.id}


@app.get('/api/assessments')
def my_assessments(db: Session = Depends(get_db), user: User = Depends(current_user)):
    out = []
    for s in db.scalars(select(Assessment).where(Assessment.owner_id == user.id).order_by(Assessment.created_at.desc())).all():
        items = attempt_items(db, s)
        maximum = sum(i['max_marks'] for i in items)
        out.append({'id': s.id, 'status': s.status, 'created_at': s.created_at.isoformat(), 'total': s.configuration['question_count'],
                    'subject': s.configuration.get('subject'), 'focused': bool(s.configuration.get('focus_plan')),
                    'percent': round(100 * sum(i['marks'] for i in items) / maximum, 1) if maximum else None})
    return out


@app.get('/api/assessments/{session_id}')
def assessment_state(session_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    session = owned(db, Assessment, session_id, user.id)
    q = next_question(db, session.id)
    pending = db.scalar(select(Response).where(Response.session_id == session.id, Response.state == 'pending').order_by(Response.slot))
    return {'id': session.id, 'progress': progress(db, session), 'question': {'id': q.id, 'slot': q.slot, 'revision': q.revision, **safe_question(q.payload)} if q else None,
            'pending': {'slot': pending.slot, 'question_revision_id': pending.question_revision_id, 'answer': pending.answer} if pending else None}


class AnswerInput(BaseModel):
    question_revision_id: str
    answer: str = Field(min_length=1, max_length=15000)


@app.post('/api/assessments/{session_id}/answer')
def answer(session_id: str, payload: AnswerInput, db: Session = Depends(get_db), user: User = Depends(current_user)):
    session = owned(db, Assessment, session_id, user.id)
    with session_lock(session.id):
        question = db.get(SessionQuestion, payload.question_revision_id)
        if not question or question.session_id != session.id:
            raise HTTPException(404, 'Question not found in this assessment')
        if question.payload['question_type'] == 'mcq' and payload.answer not in {option['id'] for option in question.payload['options']}:
            raise HTTPException(422, 'Select one of the provided MCQ options')
        existing = db.scalar(select(Response).where(Response.session_id == session.id, Response.slot == question.slot))
        if existing:
            if existing.answer != payload.answer or existing.question_revision_id != question.id:
                raise HTTPException(409, 'This question already has a different submitted answer')
            response = existing
        else:
            if session.status != 'active' or not question.active:
                raise HTTPException(409, 'Assessment or question is no longer active')
            current = next_question(db, session.id)
            if not current or current.id != question.id:
                raise HTTPException(409, 'Submit the current question before proceeding')
            response = Response(session_id=session.id, question_revision_id=question.id, slot=question.slot, answer=payload.answer, max_marks=question.payload['max_marks'], state='pending')
            db.add(response)
            try:
                db.commit()  # Persists answer before external LLM evaluation.
            except IntegrityError:
                db.rollback()
                existing = db.scalar(select(Response).where(Response.session_id == session.id, Response.slot == question.slot))
                if not existing or existing.answer != payload.answer or existing.question_revision_id != question.id:
                    raise HTTPException(409, 'Another answer was recorded') from None
                response = existing
        if response.state == 'graded':
            return {'state': 'graded', 'slot': response.slot, 'marks': response.marks, 'max_marks': response.max_marks, 'feedback': response.feedback, 'evaluation': response.evaluation, 'correct_answer': question.payload['correct_answer'], 'explanation': question.payload['explanation']}
        try:
            outcome = grade(db, question.payload, response.answer)
            response.marks = float(outcome['marks_awarded'])
            response.method = outcome['method']
            response.feedback = outcome['feedback']
            response.evaluation = outcome
            response.state = 'graded'
            db.commit()
            # Adaptation happens after committed grading; failure cannot erase the answer.
            try:
                adapt(db, session, response)
            except Exception as exc:
                db.rollback()
                logger.exception(
                    'Adaptive replacement failed for assessment %s; graded answer remains saved',
                    session.id,
                )
            scored = db.scalar(select(func.count()).select_from(Response).where(Response.session_id == session.id, Response.state == 'graded'))
            if scored >= session.configuration['question_count']:
                session.status, session.completed_at = 'completed', now()
                db.commit()
            return {'state': 'graded', 'slot': response.slot, 'marks': response.marks, 'max_marks': response.max_marks, 'feedback': response.feedback, 'evaluation': response.evaluation, 'correct_answer': question.payload['correct_answer'], 'explanation': question.payload['explanation']}
        except Exception as exc:
            db.rollback()
            logger.exception(
                'Answer evaluation failed for assessment %s, question slot %s',
                session.id,
                question.slot,
            )
            return {'state': 'pending', 'slot': response.slot, 'error': f'Answer saved; evaluation pending. Retry with the same answer. {str(exc)[:200]}'}


@app.get('/api/assessments/{session_id}/analytics')
def analytics(session_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    session = owned(db, Assessment, session_id, user.id)
    report = assessment_report(db, session)
    report['comparison'] = compare_with_previous(db, session)
    plan = session.configuration.get('focus_plan')
    report['focus_plan'] = [
        {k: area[k] for k in ('name', 'mastery', 'questions', 'difficulty', 'status', 'reason')}
        for area in plan['areas']
    ] if plan else None
    return report


class FocusInput(BaseModel):
    question_count: int | None = Field(default=None, ge=1, le=100)


@app.post('/api/assessments/{session_id}/focus', status_code=202)
def focus_on_weak_areas(session_id: str, background: BackgroundTasks, payload: FocusInput | None = None,
                        db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Build a new attempt that concentrates on the weakest concept areas across this student's
    attempts on the same material, then generate it in the background."""
    session = owned(db, Assessment, session_id, user.id)
    count = payload.question_count if payload else None
    if count is not None and count > settings.adaptiveai_max_questions:
        raise HTTPException(422, f'Choose at most {settings.adaptiveai_max_questions} questions')
    try:
        config = build_focus_plan(db, session, count)
        draft = Draft(owner_id=user.id, prompt=focus_prompt(config, session), document_ids=config['document_ids'],
                      controls={}, parent_session_id=session.id, configuration=config, conflicts=[], state='approved')
        get_documents(db, draft, config['document_ids'])
        validate_configuration(config)
    except ProviderUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    draft.state = 'generating'
    db.add(draft)
    db.commit()
    background.add_task(run_generation, draft.id)
    return {'draft_id': draft.id, 'state': draft.state}


@app.post('/api/assessments/{session_id}/repeat')
def repeat(session_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    session = owned(db, Assessment, session_id, user.id)
    draft = Draft(owner_id=user.id, prompt=session.prompt, document_ids=session.configuration['document_ids'], controls={}, parent_session_id=session.id)
    db.add(draft)
    db.commit()
    return {'draft_id': draft.id}
