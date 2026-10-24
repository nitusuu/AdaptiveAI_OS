from sqlalchemy import select
from app.db import SessionLocal
from app.models import Chunk, Page
from app.documents import split_pages, index_path
from conftest import sample_pdf


def test_chunk_boundaries_preserve_page_refs():
    pages=[(1,'A'*700),(2,'B'*700)]
    chunks=split_pages(pages,chunk_chars=850,overlap=100)
    assert chunks
    assert any(refs==[1,2] for _,refs in chunks)
    assert chunks[0][1]==[1]
    assert 2 in chunks[-1][1]


def test_pdf_upload_multi_document_and_persistent_index(registered):
    c,h,_=registered
    original_pdf=sample_pdf()
    files=[('files',('a.pdf',original_pdf,'application/pdf')),('files',('b.pdf',sample_pdf(['Routing is a fundamental network layer task.']),'application/pdf'))]
    uploaded=c.post('/api/documents',headers=h,files=files)
    assert uploaded.status_code==200,uploaded.text
    docs=uploaded.json()
    assert len(docs)==2
    for d in docs:
        item=c.get('/api/documents/'+d['id'],headers=h).json()
        assert item['status']=='ready'
        assert index_path(d['id']).exists()
    with SessionLocal() as db:
        assert len(db.scalars(select(Page)).all())==3
        assert len(db.scalars(select(Chunk)).all())>=2
        for chunk in db.scalars(select(Chunk)).all():
            assert chunk.pages and all(p in (1,2) for p in chunk.pages)
    repeated=c.post('/api/documents',headers=h,files={'files':('copy.pdf',original_pdf,'application/pdf')})
    assert repeated.json()[0]['id']==docs[0]['id']


def test_invalid_scanned_and_oversized_files(registered,monkeypatch):
    c,h,_=registered
    assert c.post('/api/documents',headers=h,files={'files':('evil.pdf',b'bad','application/pdf')}).status_code==400
    scan=c.post('/api/documents',headers=h,files={'files':('scan.pdf',sample_pdf(['']),'application/pdf')})
    assert scan.status_code==200
    status=c.get('/api/documents/'+scan.json()[0]['id'],headers=h).json()
    assert status['status']=='failed' and 'OCR' in status['error']
    from app.config import settings
    monkeypatch.setattr(settings,'adaptiveai_max_upload_mb',0)
    assert c.post('/api/documents',headers=h,files={'files':('too_big.pdf',sample_pdf(),'application/pdf')}).status_code==413


def test_invalid_embedding_output_marks_document_failed(registered, monkeypatch):
    from app import documents

    c, headers, _ = registered
    monkeypatch.setattr(documents.ai, 'embed', lambda texts: [[0.0] * 16 for _ in texts])

    upload = c.post(
        '/api/documents',
        headers=headers,
        files={'files': ('zero-vectors.pdf', sample_pdf(), 'application/pdf')},
    )

    assert upload.status_code == 200, upload.text
    document = c.get(
        '/api/documents/' + upload.json()[0]['id'],
        headers=headers,
    ).json()
    assert document['status'] == 'failed'
    assert 'Embedding shape or values invalid' in document['error']
