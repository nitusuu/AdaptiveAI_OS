import json
from types import SimpleNamespace

import pytest

from app import ai, assessment


def question(evidence, question_text):
    return {
        'question_text': question_text,
        'question_type': 'mcq',
        'topic': 'semaphores',
        'subtopics': ['critical sections'],
        'difficulty': 'medium',
        'cognitive_level': 'application',
        'options': [
            {'id': option_id, 'text': text}
            for option_id, text in zip(
                'ABCD',
                [
                    'A synchronization operation',
                    'A processor instruction',
                    'A file extension',
                    'A network protocol',
                ],
            )
        ],
        'correct_answer': 'A',
        'evaluation_rubric': [{'criterion': 'Correct concept', 'marks': 2}],
        'max_marks': 2,
        'explanation': 'The selected operation coordinates semaphore access.',
        'evidence_chunk_ids': [evidence[0]['id']],
    }


def test_question_normalization_detects_exact_and_near_duplicates():
    accepted = 'Which semaphore operation coordinates access to critical sections?'

    assert assessment.duplicate_question(
        ' WHICH semaphore operation coordinates access to critical sections! ',
        [accepted],
    ) == 'Duplicate question'
    assert assessment.duplicate_question(
        'Which semaphore operation coordinates access to the critical sections?',
        [accepted],
    ) == 'Near-duplicate question'
    assert assessment.duplicate_question(
        'How does routing choose a path between networks?',
        [accepted],
    ) is None


def test_question_batch_sends_every_previous_question(monkeypatch):
    seen = []
    monkeypatch.setattr(ai.ai, 'json', lambda system, user, **kwargs: seen.append(''.join(b['text'] for b in user)) or {'questions': []})
    previous = [f'Previously accepted question {index}' for index in range(100)]
    evidence = [{'id': 'chunk-1', 'filename': 'notes.pdf', 'pages': [1], 'text': 'Source text.'}]

    ai.question_batch({}, evidence, 'mcq', 1, existing=previous)

    assert json.dumps(previous, ensure_ascii=False) in seen[0]


def test_verifier_receives_only_each_questions_cited_evidence(monkeypatch):
    captured = []

    def verify(system, user, **kwargs):
        captured.append(json.loads(user))
        return {'valid': [True], 'reasons': ['Supported by the citation.']}

    monkeypatch.setattr(ai.ai, 'json', verify)
    evidence = [
        {'id': 'cited', 'document_id': 'doc-a', 'filename': 'a.pdf', 'pages': [2], 'text': 'Cited passage.'},
        {'id': 'uncited', 'document_id': 'doc-b', 'filename': 'b.pdf', 'pages': [9], 'text': 'Unrelated passage.'},
    ]
    candidate = question(evidence, 'Which semaphore operation coordinates access?')
    candidate['evidence_chunk_ids'] = ['cited']

    ai.validate_evidence_batch([candidate], evidence)

    cited_evidence = captured[0]['questions'][0]['cited_evidence']
    assert list(cited_evidence) == ['cited']
    assert 'uncited' not in json.dumps(captured[0])


EVIDENCE = [{
    'id': 'chunk-1',
    'document_id': 'doc-1',
    'filename': 'os.pdf',
    'pages': [1],
    'text': 'Semaphore operations coordinate access to critical sections.',
}]


def two_mcq_config(draft):
    config = dict(draft.configuration)
    config['question_count'] = 2
    config['type_distribution'] = {'mcq': 2}
    return config


def test_generation_replaces_duplicate_and_unverified_candidates(approved_draft, monkeypatch):
    _, _, draft_id, _, _ = approved_draft
    from app.db import SessionLocal
    from app.models import Draft

    monkeypatch.setattr(assessment, 'retrieve', lambda *args, **kwargs: EVIDENCE)
    first = question(EVIDENCE, 'Which semaphore operation coordinates access to critical sections?')
    near_duplicate = question(EVIDENCE, 'Which semaphore operation coordinates access to the critical sections?')
    unsupported = question(EVIDENCE, 'How does a semaphore operation coordinate critical section access?')
    replacement = question(EVIDENCE, 'What happens to a process that waits on a zero-valued semaphore?')
    rounds = [[first, near_duplicate, unsupported], [replacement]]
    calls = []

    def generate(config, passages, question_type, count, difficulty=None, existing=None, **kwargs):
        calls.append({'count': count, 'existing': list(existing or []), **kwargs})
        return rounds[len(calls) - 1]

    verifier_calls = []

    def verify(items, passages):
        texts = [item['question_text'] for item in items]
        verifier_calls.append(texts)
        if unsupported['question_text'] in texts:
            raise ai.EvidenceVerificationError([(texts.index(unsupported['question_text']) + 1, 'Unsupported answer.')])

    monkeypatch.setattr(assessment, 'question_batch', generate)
    monkeypatch.setattr(assessment, 'validate_evidence_batch', verify)
    with SessionLocal() as db:
        draft = db.get(Draft, draft_id)
        result = assessment.generate_set(db, draft, two_mcq_config(draft))

    assert [item['question_text'] for item in result] == [first['question_text'], replacement['question_text']]
    assert calls[0]['count'] == 3  # over-generates so one bad item does not cost a whole round
    assert calls[1]['count'] == 2
    # Accepted and rejected texts are all off-limits for the refill round.
    for item in (first, near_duplicate, unsupported):
        assert item['question_text'] in calls[1]['existing']
    assert near_duplicate['question_text'] not in verifier_calls[0]


def test_generation_passes_focus_topic_and_canonicalizes_label(approved_draft, monkeypatch):
    _, _, draft_id, _, _ = approved_draft
    from app.db import SessionLocal
    from app.models import Draft

    monkeypatch.setattr(assessment, 'retrieve', lambda *args, **kwargs: EVIDENCE)
    focuses = []

    def generate(config, passages, question_type, count, difficulty=None, existing=None, **kwargs):
        focuses.append(kwargs.get('focus'))
        items = [question(EVIDENCE, 'Which semaphore operation coordinates access to critical sections?'),
                 question(EVIDENCE, 'What happens to a process that waits on a zero-valued semaphore?')]
        items[1]['topic'] = 'Semaphore wait queues'
        return items

    monkeypatch.setattr(assessment, 'question_batch', generate)
    with SessionLocal() as db:
        draft = db.get(Draft, draft_id)
        result = assessment.generate_set(db, draft, two_mcq_config(draft))

    assert focuses == ['semaphores']
    assert [item['topic'] for item in result] == ['semaphores', 'semaphores']
    assert 'Semaphore wait queues' in result[1]['subtopics']


def test_uncovered_topic_fails_fast_with_clear_message(approved_draft, monkeypatch):
    _, _, draft_id, _, _ = approved_draft
    from app.db import SessionLocal
    from app.models import Draft

    monkeypatch.setattr(assessment, 'retrieve', lambda *args, **kwargs: EVIDENCE)
    calls = []

    def generate(*args, **kwargs):
        calls.append(1)
        raise ai.InsufficientEvidenceError('The passages cover ALOHA, not TCP congestion control.')

    monkeypatch.setattr(assessment, 'question_batch', generate)
    with SessionLocal() as db:
        draft = db.get(Draft, draft_id)
        with pytest.raises(ai.InsufficientEvidenceError, match='do not cover "semaphores".*ALOHA'):
            assessment.generate_set(db, draft, two_mcq_config(draft))
    assert len(calls) == 1


def test_repeated_duplicates_report_shortfall_and_reason(approved_draft, monkeypatch):
    _, _, draft_id, _, _ = approved_draft
    from app.db import SessionLocal
    from app.models import Draft

    monkeypatch.setattr(assessment, 'retrieve', lambda *args, **kwargs: EVIDENCE)
    same = question(EVIDENCE, 'Which semaphore operation coordinates access to critical sections?')
    monkeypatch.setattr(assessment, 'question_batch', lambda *args, **kwargs: [dict(same), dict(same)])
    with SessionLocal() as db:
        draft = db.get(Draft, draft_id)
        with pytest.raises(ValueError, match=r'Only 1 of 2 .*Duplicate question'):
            assessment.generate_set(db, draft, two_mcq_config(draft))


def test_question_validation_normalizes_common_llm_output_shapes():
    config = {'allowed_difficulties': ['medium'], 'cognitive_levels': ['understanding'],
              'excluded_topics': ['ip'], 'included_topics': []}
    numeric = {
        'question_text': 'What is the usable channel rate of a 56 kbps pure ALOHA channel?',
        'question_type': 'Numerical', 'topic': 'ALOHA', 'subtopics': None, 'difficulty': 'Hard',
        'cognitive_level': 'application', 'options': None, 'correct_answer': 10.304,
        'calculation': '0.184 * 56', 'tolerance': None, 'units': 'kbps',
        'evaluation_rubric': [{'criterion': 'Uses 18.4% throughput', 'marks': 1},
                              {'criterion': 'Correct product', 'marks': 1}],
        'max_marks': 1, 'explanation': 'Pure ALOHA peaks at 18.4% throughput (multiplied by 56).',
        'evidence_chunk_ids': ['[CHUNK chunk-1]', 'invented-id'],
    }
    clean = assessment.validate_question(numeric, config, EVIDENCE, 'numerical')
    assert clean['correct_answer'] == '10.304'
    assert clean['difficulty'] == 'medium' and clean['cognitive_level'] == 'understanding'
    assert clean['max_marks'] == 2
    assert clean['evidence_chunk_ids'] == ['chunk-1']
    # The excluded topic 'ip' must only match whole words, not 'multiplied' or 'ALOHA ... ip...'.

    mcq = question(EVIDENCE, 'Which semaphore operation coordinates access to critical sections?')
    mcq['correct_answer'] = 'A synchronization operation'
    assert assessment.validate_question(mcq, config, EVIDENCE, 'mcq')['correct_answer'] == 'A'


def fake_claude(monkeypatch, reply=None, error=None):
    """Stand-in Anthropic client recording each request; replies with one text block."""
    calls = []

    def create(**params):
        calls.append(params)
        if error:
            raise error
        return SimpleNamespace(
            content=[SimpleNamespace(type='text', text=reply)], stop_reason='end_turn', model=params['model'],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )

    messages = SimpleNamespace(create=create)
    monkeypatch.setattr(ai, 'client', lambda: SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)))
    return calls


def test_claude_calls_use_schema_low_effort_and_cached_passages(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'adaptiveai_chat_model', 'claude-opus-5-5')
    monkeypatch.setattr(settings, 'adaptiveai_effort', 'low')
    calls = fake_claude(monkeypatch, reply='{"questions": [], "insufficient_evidence": null}')
    evidence = [{'id': 'chunk-1', 'filename': 'notes.pdf', 'pages': [1], 'text': 'Source text.'}]

    assert ai.question_batch({'subject': 'OS', 'focus_plan': {'big': 'x'}}, evidence, 'mcq', 2) == []
    params = calls[0]
    assert params['output_config']['effort'] == 'low'
    schema = params['output_config']['format']['schema']
    question = schema['properties']['questions']['items']
    assert question['properties']['evidence_chunk_ids']['items']['enum'] == ['chunk-1']  # citations must be real chunks
    assert question['properties']['question_type']['enum'] == ['mcq']
    passages, instruction = params['messages'][0]['content']
    assert passages['cache_control'] == {'type': 'ephemeral'} and 'Source text.' in passages['text']
    assert 'focus_plan' not in instruction['text']  # bookkeeping is not sent to the model
    assert params['fallbacks'] == 'default' and 'temperature' not in params


def test_interpretation_lists_become_maps(monkeypatch):
    reply = json.dumps({
        'question_count': 6, 'subject': 'Networks', 'included_topics': [], 'excluded_topics': [],
        'type_distribution': [{'type': 'mcq', 'count': 4}, {'type': 'numerical', 'count': 2}],
        'requested_types': [], 'allowed_difficulties': ['medium'], 'difficulty_distribution': [],
        'cognitive_levels': [], 'requirements': {k: False for k in ('exam_oriented', 'code_based', 'numerical', 'scenario_based', 'conceptual', 'application', 'analytical', 'source_only')},
        'source_names': [], 'special_instructions': '', 'explicit_fields': [], 'assumptions': [],
    })
    fake_claude(monkeypatch, reply=reply)
    data = ai.interpret_prompt('Generate 4 MCQs and 2 numerical problems.')
    assert data['type_distribution'] == {'mcq': 4, 'numerical': 2}
    assert data['difficulty_distribution'] == {}


def test_local_embedding_adapter_rejects_mismatched_output(monkeypatch):
    monkeypatch.setattr(ai, 'embedder', lambda: SimpleNamespace(embed=lambda texts: iter([[1.0] * 8])))
    with pytest.raises(ai.ProviderError, match='count did not match'):
        ai.ai.embed(['first', 'second'])


def test_exhausted_credits_give_an_actionable_message(monkeypatch):
    import anthropic
    import httpx2
    response = httpx2.Response(400, request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
    error = anthropic.BadRequestError('Your credit balance is too low to access the Anthropic API.', response=response, body=None)
    fake_claude(monkeypatch, error=error)
    with pytest.raises(ai.ProviderUnavailable, match='no credits left'):
        ai.ai.json('system', 'user')
