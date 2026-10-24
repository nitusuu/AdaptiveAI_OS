from collections import Counter

from app import remediation


def split_by_parity(items, max_areas=8):
    """Test double for the concept grouper: even items vs odd items."""
    return [{'name': 'Semaphore Basics', 'items': [i['index'] for i in items if i['index'] % 2 == 0]},
            {'name': 'Wait Queues', 'items': [i['index'] for i in items if i['index'] % 2 == 1]}]


def take_attempt(client, headers, draft_id, key, answer_for_slot):
    session_id = client.post(f'/api/drafts/{draft_id}/start', headers=headers, json={'idempotency_key': key}).json()['session_id']
    while True:
        state = client.get(f'/api/assessments/{session_id}', headers=headers).json()
        question = state['question']
        if not question:
            return session_id
        answer = answer_for_slot(question['slot'])
        result = client.post(f'/api/assessments/{session_id}/answer', headers=headers,
                             json={'question_revision_id': question['id'], 'answer': answer})
        assert result.status_code == 200 and result.json()['state'] == 'graded', result.text


def test_weak_area_attempt_targets_weak_concepts_and_reports_progress(generated_draft, monkeypatch):
    client, headers, draft_id, _, _ = generated_draft
    monkeypatch.setattr(remediation, 'cluster_concepts', split_by_parity)
    # Odd slots (items 0, 2, 4) right, even slots (items 1, 3, 5) wrong -> "Wait Queues" is the weak area.
    first = take_attempt(client, headers, draft_id, 'first-attempt-key', lambda slot: 'A' if slot % 2 else 'B')

    started = client.post(f'/api/assessments/{first}/focus', headers=headers, json={})
    assert started.status_code == 202, started.text
    focus_draft = client.get(f'/api/drafts/{started.json()["draft_id"]}', headers=headers).json()
    assert focus_draft['state'] == 'generated', focus_draft['error']
    plan = {a['name']: a for a in focus_draft['configuration']['focus_plan']['areas']}
    assert plan['Wait Queues']['mastery'] == 0 and plan['Semaphore Basics']['mastery'] == 1
    assert plan['Wait Queues']['status'] == 'weak' and plan['Semaphore Basics']['status'] == 'retention'
    assert Counter(q['topic'] for q in focus_draft['questions']) == {'Wait Queues': 5, 'Semaphore Basics': 1}
    assert plan['Wait Queues']['weak_points'], 'missed questions should steer the new questions'
    previous_texts = {q['question_text'] for q in client.get(f'/api/assessments/{first}/analytics', headers=headers).json()['results']
                      for q in [{'question_text': q['question']}]}
    assert not previous_texts & {q['question_text'] for q in focus_draft['questions']}

    second = take_attempt(client, headers, focus_draft['id'], 'focused-attempt-key', lambda slot: 'A')
    report = client.get(f'/api/assessments/{second}/analytics', headers=headers).json()
    comparison = report['comparison']
    assert comparison['previous_session_id'] == first
    assert comparison['overall']['before']['percent'] == 50 and comparison['overall']['now']['percent'] == 100
    assert 'Wait Queues' in comparison['improved']
    assert not comparison['needs_work']
    assert [t['current'] for t in comparison['trend']] == [False, True]
    assert any('Wait Queues' in line for line in comparison['summary'])
    assert report['focus_plan'] and {a['name'] for a in report['focus_plan']} == {'Wait Queues', 'Semaphore Basics'}

    history = client.get('/api/assessments', headers=headers).json()
    assert [h['focused'] for h in history] == [True, False]
    assert history[0]['percent'] == 100


def test_plan_falls_back_to_topic_labels_when_grouping_fails(generated_draft, monkeypatch):
    client, headers, draft_id, _, _ = generated_draft

    def unavailable(*args, **kwargs):
        raise RuntimeError('provider down')

    monkeypatch.setattr(remediation, 'cluster_concepts', unavailable)
    first = take_attempt(client, headers, draft_id, 'fallback-attempt-key', lambda slot: 'B')
    started = client.post(f'/api/assessments/{first}/focus', headers=headers, json={'question_count': 3})
    assert started.status_code == 202, started.text
    draft = client.get(f'/api/drafts/{started.json()["draft_id"]}', headers=headers).json()
    assert draft['configuration']['focus_plan']['grouping'] == 'topic_labels'
    assert draft['configuration']['question_count'] == 3 and len(draft['questions']) == 3


def test_editing_a_plan_drops_it_instead_of_generating_inconsistently(generated_draft, monkeypatch):
    client, headers, draft_id, _, _ = generated_draft
    monkeypatch.setattr(remediation, 'cluster_concepts', split_by_parity)
    first = take_attempt(client, headers, draft_id, 'edit-plan-attempt-key', lambda slot: 'B')
    focus_id = client.post(f'/api/assessments/{first}/focus', headers=headers, json={}).json()['draft_id']
    # Simulate a failed generation that the student then edits and re-approves.
    from app.db import SessionLocal
    from app.models import Draft
    with SessionLocal() as db:
        db.get(Draft, focus_id).state = 'failed'
        db.commit()
    config = client.get(f'/api/drafts/{focus_id}', headers=headers).json()['configuration']
    config['question_count'] = 2
    config['type_distribution'] = {'mcq': 2}
    approved = client.post(f'/api/drafts/{focus_id}/approve', headers=headers, json={'configuration': config})
    assert approved.status_code == 200, approved.text
    assert 'focus_plan' not in approved.json()['configuration']


def test_topic_keys_merge_inconsistent_labels():
    assert remediation.topic_key('PTBR_function') == remediation.topic_key('PTBR Functionality') == 'ptbr'
    assert remediation.topic_key('internal_fragmentation') == remediation.topic_key('Internal Fragmentation')


def test_thin_weak_area_hands_its_questions_to_the_next_weakest(generated_draft, monkeypatch):
    from app import assessment
    client, headers, draft_id, _, _ = generated_draft
    monkeypatch.setattr(remediation, 'cluster_concepts', split_by_parity)
    first = take_attempt(client, headers, draft_id, 'thin-area-attempt-key', lambda slot: 'A' if slot % 2 else 'B')
    real_group = assessment.generate_group

    def thin_wait_queues(config, evidence, qtype, level, number, **kwargs):
        if kwargs.get('focus') == 'Wait Queues':
            # The source only supports 2 distinct questions on this concept.
            kwargs['allow_partial'] = True
            return real_group(config, evidence, qtype, level, min(number, 2), **kwargs)
        return real_group(config, evidence, qtype, level, number, **kwargs)

    monkeypatch.setattr(assessment, 'generate_group', thin_wait_queues)
    draft_id = client.post(f'/api/assessments/{first}/focus', headers=headers, json={}).json()['draft_id']
    draft = client.get(f'/api/drafts/{draft_id}', headers=headers).json()
    assert draft['state'] == 'generated', draft['error']
    assert Counter(q['topic'] for q in draft['questions']) == {'Wait Queues': 2, 'Semaphore Basics': 4}
    plan = {a['name']: a for a in draft['configuration']['focus_plan']['areas']}
    assert plan['Wait Queues']['questions'] == 2 and 'Planned 5' in plan['Wait Queues']['reason']
    assert plan['Semaphore Basics']['questions'] == 4
    assert draft['configuration']['included_topics'] == ['Wait Queues', 'Semaphore Basics']
    assert client.post(f'/api/drafts/{draft_id}/start', headers=headers, json={'idempotency_key': 'thin-area-start'}).status_code == 200
