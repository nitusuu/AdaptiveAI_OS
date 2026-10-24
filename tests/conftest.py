import os
os.environ['ADAPTIVEAI_DATABASE_URL'] = 'sqlite:///./tests/_test.db'
os.environ['ADAPTIVEAI_SECRET_KEY'] = 'test-only-signing-key-not-for-production'

from pathlib import Path
from types import SimpleNamespace
import pickle
import sys
import fitz
import numpy as np
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.db import Base, engine
from app.config import settings


class FakeFlatIndex:
    def __init__(self, d):
        self.d = d
        self.data = np.zeros((0, d), dtype=np.float32)

    def add(self, matrix):
        self.data = np.concatenate((self.data,matrix), axis=0)

    @property
    def ntotal(self):
        return self.data.shape[0]

    def search(self, queries, k):
        scores = queries @ self.data.T
        ranks = np.argsort(-scores, axis=1)[:, :k]
        return np.take_along_axis(scores,ranks,axis=1), ranks


def fake_faiss():
    def normalise(a):
        a /= np.maximum(np.linalg.norm(a,axis=1,keepdims=True),1e-10)
    def write(obj,path):
        with open(path,'wb') as f:pickle.dump(obj,f)
    def read(path):
        with open(path,'rb') as f:return pickle.load(f)
    return SimpleNamespace(normalize_L2=normalise,IndexFlatIP=FakeFlatIndex,write_index=write,read_index=read)


class FakeAI:
    def embed(self, texts, **kwargs):
        result=[]
        for content in texts:
            vec=np.zeros(16,dtype=np.float32)
            for word in content.lower().split():
                vec[sum(word.encode())%16] += 1
            result.append(vec.tolist())
        return result


@pytest.fixture
def stubbed(monkeypatch,tmp_path):
    """The test-only provider/Faiss doubles exercise the actual DB, ingestion, and API code."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings,'adaptiveai_storage_dir',str(tmp_path))
    monkeypatch.setitem(sys.modules,'faiss',fake_faiss())
    from app import documents
    monkeypatch.setattr(documents.ai, 'embed', FakeAI().embed)
    from app import assessment, main
    monkeypatch.setattr(assessment,'validate_evidence_batch',lambda questions,evidence:None)
    seq = {'v':0}
    def interpreter(prompt):
        return {'question_count': 6, 'subject': 'Operating Systems', 'included_topics':['semaphores'], 'excluded_topics': [],
                'type_distribution': {'mcq':6}, 'allowed_difficulties':['medium','hard'], 'difficulty_distribution':{},
                'cognitive_levels':['application'],'requirements':{'source_only':True},'source_names':[],
                'special_instructions':'','explicit_fields':['question_count','type_distribution'], 'assumptions':[]}
    def generation(config, evidence, type_name, count, difficulty=None, existing=None, **kwargs):
        prompts = [
            'How does semaphore waiting protect a critical section?',
            'Why can signaling coordinate competing operating system processes?',
            'What synchronization mechanism controls access to shared memory?',
            'Which concurrency primitive prevents simultaneous entry to protected code?',
            'How can counters represent available operating-system resources?',
            'Why is process synchronization important when programs share mutable state?',
            'Which operation allows a waiting thread to continue after a lock opens?',
            'What purpose does coordination serve when accessing a critical region?',
            'How does resource contention affect concurrent operating-system execution?',
            'Why might bounded semaphores differ from binary semaphores?',
            'What does mutual exclusion guarantee for simultaneous program operations?',
            'Which approach protects shared data from concurrent modification?',
            'How do waiting and signaling coordinate independent processes?',
            'What event lets blocked work resume inside a concurrent program?',
            'Why must resource acquisition and release follow a consistent protocol?',
            'How can synchronization reduce conflicting access to shared resources?',
            'What issue arises when several tasks update one value together?',
            'Which behavior is expected when a thread cannot acquire a permit?',
            'How do permits regulate access among competing execution tasks?',
            'When does an operating-system process become blocked on a wait?',
            'What guarantees that one process enters a protected section at a time?',
            'How does a semaphore manage the availability of shared resources?',
            'What can go wrong when a signal operation is omitted?',
            'Which coordination strategy controls a finite pool of resources?',
        ]
        output=[]
        for _ in range(count):
            seq['v'] += 1
            d = difficulty or 'medium'
            output.append({'question_text':prompts[(seq['v'] - 1) % len(prompts)],
                 'question_type':type_name, 'topic':'semaphores','subtopics':['mutex'], 'difficulty':d,'cognitive_level':'application',
                 'options':[{'id':a,'text':t} for a,t in zip('ABCD',['A synchronization primitive','A CPU instruction','A file extension','A network cable'])] if type_name=='mcq' else [],
                 'correct_answer':'A' if type_name=='mcq' else 'A synchronization primitive',
                 'evaluation_rubric':[{'criterion':'Correct concept', 'marks':2}], 'max_marks':2,
                 'explanation':'The source describes semaphores as synchronization primitives.',
                 'evidence_chunk_ids':[evidence[0]['id']]})
        return output
    monkeypatch.setattr(main,'interpret_prompt',interpreter)
    monkeypatch.setattr(assessment,'question_batch',generation)
    with TestClient(app) as client:
        yield client, seq


def sample_pdf(texts=None):
    doc = fitz.open()
    for s in texts or ['Semaphore synchronization and mutual exclusion are crucial to operating systems.', 'The semaphore wait and signal operations coordinate critical sections.']:
        page=doc.new_page()
        if s: page.insert_text((72,72),s)
    b=doc.tobytes()
    doc.close()
    return b


@pytest.fixture
def registered(stubbed):
    client, seq = stubbed
    registered = client.post('/api/auth/register',json={'email':'student@example.com','password':'password_12345'})
    assert registered.status_code == 200, registered.text
    token = registered.json()['token']
    return client, {'Authorization': 'Bearer '+token}, seq


@pytest.fixture
def ready_doc(registered):
    client, headers, seq = registered
    r=client.post('/api/documents',headers=headers,files={'files':('os.pdf',sample_pdf(),'application/pdf')})
    assert r.status_code==200, r.text
    doc=r.json()[0]
    assert doc['status'] in ('uploading','ready')
    actual=client.get('/api/documents/'+doc['id'],headers=headers).json()
    assert actual['status']=='ready',actual
    return client,headers,actual,seq


@pytest.fixture
def approved_draft(ready_doc):
    client,headers,doc,seq=ready_doc
    made=client.post('/api/drafts',headers=headers,json={'prompt':'Generate exactly 6 questions about semaphores.','document_ids':[doc['id']]})
    assert made.status_code==200,made.text
    did=made.json()['id']
    data=client.post(f'/api/drafts/{did}/interpret',headers=headers)
    assert data.status_code==200,data.text
    approved=client.post(f'/api/drafts/{did}/approve',headers=headers,json={'configuration':data.json()['configuration']})
    assert approved.status_code==200,approved.text
    return client,headers,did,doc,seq


@pytest.fixture
def generated_draft(approved_draft):
    client,headers,did,doc,seq=approved_draft
    generated=client.post(f'/api/drafts/{did}/generate',headers=headers)
    assert generated.status_code==202,generated.text
    status=client.get(f'/api/drafts/{did}',headers=headers).json()
    assert status['state']=='generated',status
    return client,headers,did,doc,seq
