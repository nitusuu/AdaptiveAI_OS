from sqlalchemy import select
from app.db import SessionLocal
from app.models import DraftQuestion, Draft, SessionQuestion, Response, Document
import pytest
from app.assessment import build_configuration,validate_configuration
from conftest import sample_pdf


def test_detach_document_does_not_delete_private_source(ready_doc):
    c,h,d,_=ready_doc
    did=c.post('/api/drafts',headers=h,json={'prompt':'Make a short assessment of semaphores.','document_ids':[d['id']]}).json()['id']
    assert c.delete('/api/documents/'+d['id'],headers=h).status_code==409
    detached=c.delete(f'/api/drafts/{did}/documents/{d["id"]}',headers=h)
    assert detached.status_code==200,detached.text
    assert detached.json()['document_ids']==[]
    assert c.get('/api/documents/'+d['id'],headers=h).status_code==200
    assert c.delete('/api/documents/'+d['id'],headers=h).status_code==200
    assert c.get('/api/documents/'+d['id'],headers=h).status_code==404


def test_invalid_mcq_option_does_not_record_answer(generated_draft):
    c,h,did,d,_=generated_draft
    sid=c.post('/api/drafts/'+did+'/start',headers=h,json={'idempotency_key':'invalid-selection-test'}).json()['session_id']
    q=c.get('/api/assessments/'+sid,headers=h).json()['question']
    rejected=c.post('/api/assessments/'+sid+'/answer',headers=h,json={'question_revision_id':q['id'],'answer':'Z'})
    assert rejected.status_code==422
    with SessionLocal() as db:
        assert not db.scalars(select(Response).where(Response.session_id==sid)).all()


def test_old_question_preserved_after_regeneration_failure(generated_draft,monkeypatch):
    from app import assessment
    c,h,did,d,_=generated_draft
    old=c.get('/api/drafts/'+did,headers=h).json()['questions']
    def fail(*args,**kwargs):
        raise ValueError('Insufficient evidence in PDFs')
    monkeypatch.setattr(assessment,'question_batch',fail)
    request=c.post(f'/api/drafts/{did}/generate',headers=h)
    assert request.status_code==202
    current=c.get('/api/drafts/'+did,headers=h).json()
    assert current['state']=='generated'
    assert current['error'] and 'preserved' in current['error']
    assert [q['id'] for q in current['questions']]==[q['id'] for q in old]


def test_individual_regeneration_failure_preserves_active_question(generated_draft, monkeypatch):
    from app import assessment

    c,h,did,_,_=generated_draft
    before=c.get('/api/drafts/'+did,headers=h).json()['questions']
    calls={'count':0}
    def fail(*args,**kwargs):
        calls['count']+=1
        raise ValueError('No supported replacement')
    monkeypatch.setattr(assessment,'question_batch',fail)

    failed=c.post(
        f'/api/drafts/{did}/questions/{before[0]["id"]}/regenerate',
        headers=h,
    )
    after=c.get('/api/drafts/'+did,headers=h).json()['questions']

    assert failed.status_code==503
    assert 'original was preserved' in failed.json()['detail']
    assert calls['count']==assessment.GENERATION_ROUNDS
    assert [q['id'] for q in after]==[q['id'] for q in before]


def test_bad_evidence_prevents_publishing(approved_draft,monkeypatch):
    from app import assessment
    c,h,did,d,_=approved_draft
    monkeypatch.setattr(assessment,'retrieve',lambda *args,**kwargs:[])
    response=c.post(f'/api/drafts/{did}/generate',headers=h)
    assert response.status_code==202
    draft=c.get(f'/api/drafts/{did}',headers=h).json()
    assert draft['state']=='failed'
    assert not draft['questions']
    assert 'Insufficient' in draft['error']


def test_rubric_evaluation_failure_persists_answer_and_recovers(approved_draft,monkeypatch):
    from app import assessment
    c,h,did,d,_=approved_draft
    info=c.get('/api/drafts/'+did,headers=h).json()
    config=info['configuration']
    config['question_count']=1
    config['type_distribution']={'short_answer':1}
    config['allowed_difficulties']=['medium']
    accepted=c.post('/api/drafts/'+did+'/approve',headers=h,json={'configuration':config})
    assert accepted.status_code==200,accepted.text
    generated=c.post('/api/drafts/'+did+'/generate',headers=h)
    assert generated.status_code==202,generated.text
    assert c.get('/api/drafts/'+did,headers=h).json()['state']=='generated'
    sid=c.post('/api/drafts/'+did+'/start',headers=h,json={'idempotency_key':'llm-retry-test'}).json()['session_id']
    q=c.get('/api/assessments/'+sid,headers=h).json()['question']
    calls={'n':0}
    def intermittent(*a,**kw):
        calls['n']+=1
        if calls['n']==1:raise RuntimeError('Provider temporarily unavailable')
        return {'marks_awarded':1.5,'max_marks':2,'feedback':'Partially correct','explanation':'Concept mentioned, example missing','incorrect_claims':[],'confidence':0.8,'criteria_satisfied':['synchronization'],'missing_elements':['example']}
    monkeypatch.setattr(assessment,'grade_text',intermittent)
    body={'question_revision_id':q['id'],'answer':'Semaphore synchronization protects critical sections.'}
    failed=c.post('/api/assessments/'+sid+'/answer',headers=h,json=body)
    assert failed.status_code==200 and failed.json()['state']=='pending',failed.text
    assert c.get('/api/assessments/'+sid,headers=h).json()['progress']['pending']==1
    with SessionLocal() as db:
        responses=db.scalars(select(Response).where(Response.session_id==sid)).all()
        assert len(responses)==1 and responses[0].answer==body['answer']
    recovered=c.post('/api/assessments/'+sid+'/answer',headers=h,json=body)
    assert recovered.status_code==200 and recovered.json()['marks']==1.5,recovered.text
    report=c.get('/api/assessments/'+sid+'/analytics',headers=h).json()
    assert report['overall']['descriptive_marks']==1.5
    assert report['status']=='completed'
    assert calls['n']==2


@pytest.mark.parametrize('bad_marks',[float('nan'),float('inf')])
def test_non_finite_free_text_marks_remain_pending(approved_draft,monkeypatch,bad_marks):
    from app import assessment

    c,h,did,_,_=approved_draft
    config=c.get('/api/drafts/'+did,headers=h).json()['configuration']
    config['question_count']=1
    config['type_distribution']={'short_answer':1}
    config['allowed_difficulties']=['medium']
    approved=c.post(f'/api/drafts/{did}/approve',headers=h,json={'configuration':config})
    assert approved.status_code==200,approved.text
    generated=c.post(f'/api/drafts/{did}/generate',headers=h)
    assert generated.status_code==202,generated.text
    assert c.get(f'/api/drafts/{did}',headers=h).json()['state']=='generated'
    sid=c.post(
        f'/api/drafts/{did}/start',
        headers=h,
        json={'idempotency_key':f'nonfinite-score-{str(bad_marks)}'},
    ).json()['session_id']
    question=c.get(f'/api/assessments/{sid}',headers=h).json()['question']
    monkeypatch.setattr(assessment,'grade_text',lambda *args,**kwargs:{
        'marks_awarded':bad_marks,
        'max_marks':2,
        'criteria_satisfied':[],
        'missing_elements':['evidence'],
        'incorrect_claims':[],
        'feedback':'The response needs evidence.',
        'explanation':'The answer lacks a cited detail.',
        'confidence':0.5,
    })

    response=c.post(
        f'/api/assessments/{sid}/answer',
        headers=h,
        json={
            'question_revision_id':question['id'],
            'answer':'Semaphore operations coordinate access.',
        },
    )

    assert response.status_code==200
    assert response.json()['state']=='pending'
    assert c.get(f'/api/assessments/{sid}',headers=h).json()['progress']['pending']==1


def test_named_pdf_limits_source_scope():
    docs=[Document(id='one',owner_id='u',filename='os.pdf',storage_key='p',sha256='a',size_bytes=1,status='ready'),
          Document(id='two',owner_id='u',filename='networks.pdf',storage_key='p',sha256='b',size_bytes=1,status='ready')]
    raw={'question_count':2,'type_distribution':{'mcq':2},'allowed_difficulties':['medium'],'source_names':['os.pdf']}
    config,err=build_configuration(raw,{},docs)
    assert not err
    assert config['document_ids']==['one']
    validate_configuration(config)


def test_bad_llm_interpretation_is_rejected(ready_doc,monkeypatch):
    from app import main
    c,h,d,_=ready_doc
    draft=c.post('/api/drafts',headers=h,json={'prompt':'Generate six questions on the uploaded notes.','document_ids':[d['id']]}).json()['id']
    monkeypatch.setattr(main,'interpret_prompt',lambda prompt:{'included_topics': 'NOT_A_LIST'})
    result=c.post(f'/api/drafts/{draft}/interpret',headers=h)
    assert result.status_code==503
    assert c.get(f'/api/drafts/{draft}',headers=h).json()['state']=='failed'


def test_missing_document_index_blocks_retrieval(ready_doc):
    from app.documents import index_path,retrieve
    c,h,d,_=ready_doc
    index_path(d['id']).unlink()
    with SessionLocal() as db:
        item=db.get(Document,d['id'])
        import pytest
        with pytest.raises(ValueError,match='Index missing'):
            retrieve(db,[item],['semaphores'])
