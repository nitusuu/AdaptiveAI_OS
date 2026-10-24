from app.db import SessionLocal
from app.models import SessionQuestion,Response,Adaptation
from sqlalchemy import select


def test_no_secret_answer_in_initial_payload(generated_draft):
    c,h,did,doc,_=generated_draft
    p=c.get('/api/drafts/'+did,headers=h).json()
    assert len(p['questions'])==6
    assert all('correct_answer' not in q and 'evaluation_rubric' not in q for q in p['questions'])
    key='test-idempotent-draft-1'
    start=c.post('/api/drafts/'+did+'/start',headers=h,json={'idempotency_key':key})
    assert start.status_code==200,start.text
    second=c.post('/api/drafts/'+did+'/start',headers=h,json={'idempotency_key':key})
    assert second.json()==start.json()
    active=c.get('/api/assessments/'+start.json()['session_id'],headers=h)
    assert 'correct_answer' not in active.json()['question']
    assert 'evaluation_rubric' not in active.json()['question']


def test_regeneration_preserves_all_others(generated_draft):
    c,h,did,doc,_=generated_draft
    original=c.get('/api/drafts/'+did,headers=h).json()['questions']
    swapped=c.post(f'/api/drafts/{did}/questions/{original[0]["id"]}/regenerate',headers=h)
    assert swapped.status_code==200,swapped.text
    changed=swapped.json()['questions']
    assert changed[0]['id']!=original[0]['id']
    assert [q['id'] for q in changed[1:]]==[q['id'] for q in original[1:]]


def test_submission_adaptation_and_history(generated_draft):
    c,h,did,doc,_=generated_draft
    sid=c.post('/api/drafts/'+did+'/start',headers=h,json={'idempotency_key':'fresh-session-1'}).json()['session_id']
    visited=[]
    for i in range(6):
        current=c.get('/api/assessments/'+sid,headers=h).json()
        assert current['question'],current
        item=current['question']
        visited.append(item)
        submitted=c.post('/api/assessments/'+sid+'/answer',headers=h,json={'question_revision_id':item['id'],'answer':'A'})
        assert submitted.status_code==200,submitted.text
        assert submitted.json()['state']=='graded',submitted.json()
        if i==0:
            retry=c.post('/api/assessments/'+sid+'/answer',headers=h,json={'question_revision_id':item['id'],'answer':'A'})
            assert retry.status_code==200 and retry.json()['marks']==2
            assert c.post('/api/assessments/'+sid+'/answer',headers=h,json={'question_revision_id':item['id'],'answer':'B'}).status_code==409
    end=c.get('/api/assessments/'+sid,headers=h).json()
    assert end['progress']['status']=='completed'
    report=c.get('/api/assessments/'+sid+'/analytics',headers=h).json()
    assert report['overall']['marks']==12
    assert report['overall']['percent']==100
    assert report['overall']['objective_accuracy']==100
    assert report['adaptations']
    assert any(x['decision']=='replaced' for x in report['adaptations'])
    assert any(q['revision']==2 for q in visited)
    assert len(report['results'])==6
    with SessionLocal() as db:
        responses=db.scalars(select(Response).where(Response.session_id==sid)).all()
        assert len(responses)==6 and len(set(x.slot for x in responses))==6
        assert len(db.scalars(select(SessionQuestion).where(SessionQuestion.session_id==sid,SessionQuestion.active==True)).all())==6


def test_hard_only_prevents_difficulty_lowering(generated_draft):
    c,h,did,doc,_=generated_draft
    config=c.get('/api/drafts/'+did,headers=h).json()['configuration']
    # Change constraints by making a new draft, not overwriting the generated one.
    new=c.post('/api/drafts',headers=h,json={'prompt':'Generate six hard-only questions on semaphores.', 'document_ids':[doc['id']]}).json()['id']
    interpreted=c.post(f'/api/drafts/{new}/interpret',headers=h).json()['configuration']
    interpreted['allowed_difficulties']=['hard']
    interpreted['difficulty_distribution']={'hard':6}
    appr=c.post(f'/api/drafts/{new}/approve',headers=h,json={'configuration':interpreted})
    assert appr.status_code==200,appr.text
    gen=c.post(f'/api/drafts/{new}/generate',headers=h)
    assert gen.status_code==202,gen.text
    assert c.get(f'/api/drafts/{new}',headers=h).json()['state']=='generated'
    sid=c.post(f'/api/drafts/{new}/start',headers=h,json={'idempotency_key':'hard-only-session-1'}).json()['session_id']
    for i in range(3):
        question=c.get('/api/assessments/'+sid,headers=h).json()['question']
        assert question['difficulty']=='hard'
        result=c.post(f'/api/assessments/{sid}/answer',headers=h,json={'question_revision_id':question['id'],'answer':'B'})
        assert result.status_code==200,result.text
    remaining=c.get('/api/assessments/'+sid,headers=h).json()['question']
    assert remaining['difficulty']=='hard'
    history=c.get('/api/assessments/'+sid+'/analytics',headers=h).json()['adaptations']
    assert all(h['decision']!='replaced' for h in history)


def test_cross_user_forbidden(generated_draft):
    c,h,did,doc,_=generated_draft
    second=c.post('/api/auth/register',json={'email':'other@example.com','password':'another_secret_pw'}).json()
    h2={'Authorization':'Bearer '+second['token']}
    assert c.get('/api/drafts/'+did,headers=h2).status_code==404
    assert c.get('/api/documents/'+doc['id'],headers=h2).status_code==404
    assert c.post('/api/drafts/'+did+'/start',headers=h2,json={'idempotency_key':'unauthorized-1'}).status_code==404
