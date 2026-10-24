import pytest
from app.assessment import build_configuration
from app.models import Document


def test_explicit_prompt_distribution_cannot_be_silently_changed():
    doc=Document(id='doc',owner_id='u',filename='os.pdf',storage_key='/not/public',sha256='sha',size_bytes=10,status='ready')
    raw={'question_count':20,'type_distribution':{'mcq':10,'descriptive':10},'allowed_difficulties':['medium']}
    prompt='Generate exactly 20 medium-to-hard questions. Include 10 MCQs, 5 descriptive questions, and 5 scenario-based questions.'
    config,conflicts=build_configuration(raw,{},[doc],prompt)
    assert config['question_count']==20
    assert any('Explicit format counts' in c for c in conflicts)


def test_auth_is_required_for_private_pdf_file(ready_doc):
    c,h,doc,_=ready_doc
    private=f'/api/documents/{doc["id"]}/file'
    assert c.get(private).status_code==401
    received=c.get(private,headers=h)
    assert received.status_code==200
    assert received.headers['content-type'].startswith('application/pdf')
    assert received.content.startswith(b'%PDF-')
    other=c.post('/api/auth/register',json={'email':'second@university.edu','password':'other_password_123'})
    headers={'Authorization':'Bearer '+other.json()['token']}
    assert c.get(private,headers=headers).status_code==404


def test_session_state_and_analytics_are_owner_scoped(generated_draft):
    c,h,did,doc,_=generated_draft
    started=c.post(f'/api/drafts/{did}/start',headers=h,json={'idempotency_key':'session-scope-111'}).json()['session_id']
    token=c.post('/api/auth/register',json={'email':'intruder@university.edu','password':'other_password_123'}).json()['token']
    h2={'Authorization':'Bearer '+token}
    for endpoint in (f'/api/assessments/{started}',f'/api/assessments/{started}/analytics'):
        assert c.get(endpoint,headers=h2).status_code==404


def test_real_faiss_roundtrip_when_available(tmp_path):
    faiss=pytest.importorskip('faiss',reason='faiss-cpu was unavailable in the authoring container')
    import numpy as np
    points=np.asarray([[1.,0.,0.],[0.,1.,0.]],dtype=np.float32)
    index=faiss.IndexFlatIP(3)
    index.add(points)
    path=str(tmp_path/'probe.faiss')
    faiss.write_index(index,path)
    restored=faiss.read_index(path)
    values, ids=restored.search(points[:1],1)
    assert ids[0,0]==0 and values[0,0]==pytest.approx(1.)
