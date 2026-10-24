import os
import re
import logging
from hashlib import sha256
from pathlib import Path
from threading import Lock
import fitz
import numpy as np
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from app.config import settings
from app.db import SessionLocal
from app.models import Document, Page, Chunk
from app.ai import ai

_index_locks = {}
_guard = Lock()
logger = logging.getLogger(__name__)


def lock_for(document_id):
    with _guard:
        return _index_locks.setdefault(document_id, Lock())


def faiss_module():
    try:
        import faiss
        return faiss
    except ImportError as exc:
        raise RuntimeError('FAISS is required. Install dependencies with pip install -r requirements.txt') from exc


def validate_pdf(blob: bytes) -> int:
    if not blob.startswith(b'%PDF-'):
        raise ValueError('Invalid PDF signature')
    try:
        with fitz.open(stream=blob, filetype='pdf') as pdf:
            if pdf.needs_pass:
                raise ValueError('Password-protected PDFs are not supported')
            n = len(pdf)
            if n == 0:
                raise ValueError('PDF has no pages')
            if n > settings.adaptiveai_max_pages:
                raise ValueError(f'PDF exceeds the {settings.adaptiveai_max_pages}-page limit')
            return n
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f'PDF cannot be parsed: {exc}') from exc


def split_pages(pages: list[tuple[int, str]], chunk_chars: int | None = None, overlap: int | None = None) -> list[tuple[str, list[int]]]:
    """Chunk across page boundaries while retaining all source page IDs."""
    size = chunk_chars or settings.adaptiveai_chunk_chars
    ov = settings.adaptiveai_chunk_overlap if overlap is None else overlap
    if not 0 <= ov < size or size < 100:
        raise ValueError('Invalid chunk size or overlap')
    sections = []
    all_text = ''
    for page, body in pages:
        cleaned = re.sub(r'[ \t]+', ' ', body).strip()
        if not cleaned:
            continue
        start = len(all_text)
        all_text += cleaned + '\n\n'
        sections.append((start, len(all_text), page))
    result = []
    pos = 0
    while pos < len(all_text):
        tentative = min(len(all_text), pos + size)
        end = tentative
        if tentative < len(all_text):
            candidates = [all_text.rfind(sep, pos + size // 2, tentative) for sep in ('\n\n', '. ', '; ', '\n', ' ')]
            k = max(candidates)
            if k > pos + size // 2:
                end = k + 1
        segment = all_text[pos:end].strip()
        if segment:
            refs = sorted({p for start, stop, p in sections if start < end and stop > pos})
            result.append((segment, refs))
        if end >= len(all_text):
            break
        pos = max(pos + 1, end - ov)
    return result


def index_path(doc_id: str) -> Path:
    return settings.storage_path / 'indexes' / f'{doc_id}.faiss'


def process_document(doc_id: str):
    """Idempotent per-document worker; all stages observable in DB; failure keeps retry possible."""
    with lock_for(doc_id):
        with SessionLocal() as db:
            doc = db.get(Document, doc_id)
            if not doc or doc.status == 'ready':
                return
            try:
                doc.status, doc.error = 'extracting', None
                db.commit()
                with fitz.open(doc.storage_key) as pdf:
                    page_data = [(i + 1, pdf[i].get_text('text')) for i in range(len(pdf))]
                if not any(text.strip() for _, text in page_data):
                    raise ValueError('No extractable text. This appears to be a scanned PDF; run OCR before uploading.')
                db.execute(delete(Page).where(Page.document_id == doc.id))
                for page, body in page_data:
                    db.add(Page(document_id=doc.id, number=page, text=body))
                doc.page_count = len(page_data)
                doc.status = 'chunking'
                db.commit()
                rows = split_pages(page_data)
                if not rows:
                    raise ValueError('No usable text chunks found')
                if len(rows) > 3000:
                    raise ValueError('Document exceeds 3000 chunks')
                # Re-indexing (e.g. after an embedding-model change) keeps existing chunk rows when the
                # text is unchanged: saved questions cite chunk IDs, and grading reads passages by ID.
                existing = db.scalars(select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.position)).all()
                reuse = [(c.text, list(c.pages)) for c in existing] == [(text, list(refs)) for text, refs in rows]
                if not reuse:
                    db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
                doc.status = 'embedding'
                db.commit()
                vectors = []
                for start in range(0, len(rows), 32):
                    vectors.extend(ai.embed([text for text, _ in rows[start:start+32]]))
                matrix = np.asarray(vectors, dtype=np.float32)
                norms = np.linalg.norm(matrix, axis=1) if matrix.ndim == 2 else np.array([])
                if (
                    matrix.ndim != 2
                    or matrix.shape[0] != len(rows)
                    or matrix.shape[1] < 8
                    or not np.isfinite(matrix).all()
                    or not np.isfinite(norms).all()
                    or np.any(norms == 0)
                ):
                    raise ValueError('Embedding shape or values invalid')
                faiss = faiss_module()
                faiss.normalize_L2(matrix)
                index = faiss.IndexFlatIP(matrix.shape[1])
                index.add(matrix)
                doc.status = 'indexing'
                db.commit()
                out = index_path(doc_id)
                out.parent.mkdir(parents=True, exist_ok=True)
                temp = out.with_name(out.name + '.tmp')
                faiss.write_index(index, str(temp))
                os.replace(temp, out)
                if reuse:
                    for i, chunk in enumerate(existing):
                        chunk.vector_id = i
                else:
                    for i, (content, page_refs) in enumerate(rows):
                        db.add(Chunk(document_id=doc_id, position=i, pages=page_refs, text=content, vector_id=i))
                doc.embedding_model = settings.adaptiveai_embed_model
                doc.dimension = int(matrix.shape[1])
                doc.status, doc.error = 'ready', None
                db.commit()
            except Exception as exc:
                db.rollback()
                doc = db.get(Document, doc_id)
                if doc:
                    doc.status, doc.error = 'failed', str(exc)[:1000]
                    db.commit()
                logger.exception('Document indexing failed for document %s', doc_id)


def retrieve(db: Session, docs: list[Document], queries: list[str], top_k: int | None = None) -> list[dict]:
    faiss = faiss_module()
    k = settings.adaptiveai_retrieval_top_k if top_k is None else top_k
    if k < 1:
        raise ValueError('Retrieval top_k must be positive')
    queries = [x for x in queries if x and x.strip()]
    if not queries:
        raise ValueError('No topic queries to retrieve')
    query_vecs = np.asarray(ai.embed(queries, query=True), dtype=np.float32)
    query_norms = np.linalg.norm(query_vecs, axis=1) if query_vecs.ndim == 2 else np.array([])
    if (
        query_vecs.ndim != 2
        or query_vecs.shape[0] != len(queries)
        or query_vecs.shape[1] < 8
        or not np.isfinite(query_vecs).all()
        or not np.isfinite(query_norms).all()
        or np.any(query_norms == 0)
    ):
        raise ValueError('Query embedding shape or values are invalid; check the active embedding model')
    faiss.normalize_L2(query_vecs)
    scored = {}
    for doc in docs:
        if doc.status != 'ready' or doc.embedding_model != settings.adaptiveai_embed_model:
            raise ValueError(f'{doc.filename} is still being re-indexed for the current embedding model; try again in a moment')
        path = index_path(doc.id)
        if not path.exists():
            raise ValueError(f'Index missing for {doc.filename}; reprocess required')
        index = faiss.read_index(str(path))
        if index.d != query_vecs.shape[1]:
            raise ValueError('Embedding dimension mismatch. Reindex the document.')
        ids = {row.vector_id: row for row in db.scalars(select(Chunk).where(Chunk.document_id == doc.id)).all()}
        if index.ntotal < 1 or index.ntotal != len(ids) or set(ids) != set(range(index.ntotal)):
            raise ValueError(f'Index/chunk mapping is inconsistent for {doc.filename}; reprocess required')
        dists, found = index.search(query_vecs, min(k, index.ntotal))
        for ds, ix in zip(dists, found):
            for score, idx in zip(ds, ix):
                if int(idx) not in ids:
                    continue
                row = ids[int(idx)]
                key = row.id
                if float(score) > scored.get(key, (-99, None))[0]:
                    scored[key] = (float(score), {'id': row.id, 'document_id': doc.id, 'filename': doc.filename, 'pages': row.pages, 'text': row.text})
    return [v[1] for v in sorted(scored.values(), key=lambda x: -x[0])[:max(k, len(queries)*k)]]
