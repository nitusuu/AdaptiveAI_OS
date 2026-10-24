"""Configuration, grounded generation, grading, adaptation, and analytics services."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from math import isclose
import math
import ast
import json
import re
import threading
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from app.ai import COGNITIVE, LEVELS, TYPES, EvidenceVerificationError, InsufficientEvidenceError, ai, interpret_prompt, question_batch, validate_evidence_batch, grade_text
from app.config import settings
from app.documents import retrieve
from app.models import Document, Chunk, Draft, DraftQuestion, Assessment, SessionQuestion, Response, Adaptation, uid, now
class InterpreterOutput(BaseModel):
    """Validate the LLM response before treating it as assessment instructions."""
    model_config = ConfigDict(extra='ignore')
    question_count: int | None = Field(default=None, ge=1, le=1000)
    subject: str = Field(default='', max_length=200)
    included_topics: list[str] = Field(default_factory=list)
    excluded_topics: list[str] = Field(default_factory=list)
    type_distribution: dict[str, int] = Field(default_factory=dict)
    requested_types: list[str] = Field(default_factory=list)
    allowed_difficulties: list[str] = Field(default_factory=list)
    difficulty_distribution: dict[str, int] = Field(default_factory=dict)
    cognitive_levels: list[str] = Field(default_factory=list)
    requirements: dict[str, bool] = Field(default_factory=dict)
    source_names: list[str] = Field(default_factory=list)
    special_instructions: str = Field(default='', max_length=6000)
    explicit_fields: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
class FreeTextEvaluation(BaseModel):
    marks_awarded: float = Field(ge=0, le=100)
    max_marks: float = Field(gt=0, le=100)
    criteria_satisfied: list[str]
    missing_elements: list[str]
    incorrect_claims: list[str]
    feedback: str = Field(min_length=3)
    explanation: str = Field(min_length=3)
    confidence: float = Field(ge=0, le=1)
class RubricItem(BaseModel):
    criterion: str = Field(min_length=3)
    marks: float = Field(gt=0, le=100)
class Option(BaseModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
class Question(BaseModel):
    model_config = ConfigDict(extra='ignore')
    question_text: str = Field(min_length=12)
    question_type: str
    topic: str = Field(min_length=2)
    subtopics: list[str] = Field(default_factory=list)
    difficulty: str
    cognitive_level: str
    options: list[Option] = Field(default_factory=list)
    correct_answer: str = Field(min_length=1)
    evaluation_rubric: list[RubricItem] = Field(min_length=1)
    max_marks: float = Field(gt=0, le=100)
    explanation: str = Field(min_length=10)
    evidence_chunk_ids: list[str] = Field(min_length=1)
    calculation: str | None = None
    units: str | None = None
    tolerance: float = Field(default=0.0001, ge=0)
    @model_validator(mode='before')
    @classmethod
    def coerce_llm_shapes(cls, data):
        """LLMs often return numbers where strings are expected (and vice versa); normalize before validating."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for key in ('correct_answer', 'calculation'):
            if isinstance(data.get(key), (int, float)) and not isinstance(data.get(key), bool):
                data[key] = repr(data[key]) if isinstance(data[key], float) else str(data[key])
        if data.get('tolerance') is None:
            data.pop('tolerance', None)
        for key in ('question_type', 'difficulty', 'cognitive_level'):
            if isinstance(data.get(key), str):
                data[key] = data[key].strip().casefold().replace('-', '_').replace(' ', '_')
        data['question_type'] = {'multiple_choice': 'mcq', 'scenario_based': 'scenario', 'short': 'short_answer'}.get(data.get('question_type'), data.get('question_type'))
        options = data.get('options')
        if isinstance(options, list) and options and all(isinstance(o, str) for o in options):
            data['options'] = [{'id': 'ABCD'[i] if i < 4 else str(i + 1), 'text': o} for i, o in enumerate(options)]
        elif isinstance(options, list):
            data['options'] = [{**o, 'id': str(o['id']).strip()} if isinstance(o, dict) and o.get('id') is not None else o for o in options]
        if isinstance(data.get('evidence_chunk_ids'), str):
            data['evidence_chunk_ids'] = [data['evidence_chunk_ids']]
        if isinstance(data.get('evidence_chunk_ids'), list):
            data['evidence_chunk_ids'] = [str(x).strip() for x in data['evidence_chunk_ids']]
        if isinstance(data.get('subtopics'), str):
            data['subtopics'] = [data['subtopics']]
        if data.get('subtopics') is None:
            data['subtopics'] = []
        if data.get('options') is None:
            data['options'] = []
        return data
    @field_validator('question_type')
    @classmethod
    def valid_type(cls, value):
        if value not in TYPES:
            raise ValueError('Unsupported question type')
        return value
    @field_validator('difficulty')
    @classmethod
    def valid_difficulty(cls, value):
        if value not in LEVELS:
            raise ValueError('Unsupported difficulty')
        return value
    @field_validator('cognitive_level')
    @classmethod
    def valid_cognitive(cls, value):
        if value not in COGNITIVE:
            raise ValueError('Unsupported cognitive level')
        return value
CALC_LIMIT = 1e18
CALC_FUNCTIONS = {
    'exp': lambda x: math.exp(x) if abs(x) <= 40 else _calc_error('Exponent too large'),
    'log': lambda x: math.log(x) if x > 0 else _calc_error('Logarithm of a non-positive number'),
    'ln': lambda x: math.log(x) if x > 0 else _calc_error('Logarithm of a non-positive number'),
    'log2': lambda x: math.log2(x) if x > 0 else _calc_error('Logarithm of a non-positive number'),
    'log10': lambda x: math.log10(x) if x > 0 else _calc_error('Logarithm of a non-positive number'),
    'sqrt': lambda x: math.sqrt(x) if x >= 0 else _calc_error('Square root of a negative number'),
}
CALC_CONSTANTS = {'e': math.e, 'pi': math.pi}
def _calc_error(message: str):
    raise ValueError(message)
def safe_calculation(expression: str) -> float:
    """Evaluate literal arithmetic plus a fixed set of math functions; no variables, attributes on
    arbitrary objects, or other code."""
    def name_of(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'math':
            return node.attr
        return None
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return float(node.value)
        if name_of(node) in CALC_CONSTANTS and not isinstance(node, ast.Call):
            return CALC_CONSTANTS[name_of(node)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return (-1 if isinstance(node.op, ast.USub) else 1) * walk(node.operand)
        if isinstance(node, ast.Call) and name_of(node.func) in CALC_FUNCTIONS and len(node.args) == 1 and not node.keywords:
            return CALC_FUNCTIONS[name_of(node.func)](walk(node.args[0]))
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.BitXor)):
            x, y = walk(node.left), walk(node.right)
            if abs(x) > CALC_LIMIT or abs(y) > CALC_LIMIT:
                raise ValueError('Expression too large')
            if isinstance(node.op, ast.Add): return x+y
            if isinstance(node.op, ast.Sub): return x-y
            if isinstance(node.op, ast.Mult): return x*y
            if isinstance(node.op, ast.Div):
                if y == 0:
                    raise ValueError('Division by zero in calculation')
                return x/y
            # Writers often use ^ for exponentiation in arithmetic answers.
            if abs(y) > 64 or (abs(x) > 1 and y * math.log10(abs(x)) > 18):
                raise ValueError('Expression too large')
            if x == 0 and y < 0:
                raise ValueError('Division by zero in calculation')
            result = x ** y
            if isinstance(result, complex):
                raise ValueError('Calculation has no real result')
            return result
        raise ValueError('Unsupported calculation expression')
    if len(expression) > 300:
        raise ValueError('Calculation too long')
    try:
        result = walk(ast.parse(expression.replace('×', '*').replace('−', '-'), mode='eval'))
    except (SyntaxError, OverflowError) as exc:
        raise ValueError('Invalid or out-of-range calculation expression') from exc
    if not -CALC_LIMIT < result < CALC_LIMIT:
        raise ValueError('Calculation out of range')
    return result
def build_configuration(raw: dict, controls: dict, selected_docs: list[Document], original_prompt: str = '') -> tuple[dict, list[str]]:
    raw = InterpreterOutput.model_validate(raw).model_dump()
    explicit = set(raw.get('explicit_fields') or [])
    conflicts = []
    count = raw.get('question_count')
    ctrl_count = controls.get('question_count')
    if count is None:
        inferred_total = sum((raw.get('type_distribution') or {}).values()) if raw.get('type_distribution') else None
        count = ctrl_count or inferred_total or 20
    elif ctrl_count is not None and int(ctrl_count) != int(count):
        conflicts.append(f'Prompt specifies {count} questions; selector specifies {ctrl_count}.')
    if not isinstance(count, int) or not 1 <= count <= settings.adaptiveai_max_questions:
        conflicts.append(f'Question count must be 1–{settings.adaptiveai_max_questions}.')
    count = max(1, min(int(count) if isinstance(count, int) else 20, settings.adaptiveai_max_questions))
    types = dict(raw.get('type_distribution') or {})
    if types and (any(k not in TYPES or not isinstance(v, int) or v < 0 for k, v in types.items()) or sum(types.values()) != count):
        conflicts.append('Requested question-type distribution must sum exactly to the question count.')
    selected_type = controls.get('question_type')
    if selected_type and selected_type != 'mixed':
        if types and (types.get(selected_type, 0) != count or len([v for v in types.values() if v > 0]) > 1):
            conflicts.append(f'Question-type selector {selected_type} conflicts with the prompt distribution.')
        if not types:
            types = {selected_type: count}
    if not types:
        requested_types = [t for t in raw.get('requested_types') or [] if t in TYPES]
        requested_types = list(dict.fromkeys(requested_types)) or ['mcq','short_answer','descriptive']
        types = {t:count//len(requested_types) + (1 if i<count%len(requested_types) else 0) for i,t in enumerate(requested_types)}
    types = {k: v for k, v in types.items() if v > 0}
    if sum(types.values()) != count:
        conflicts.append('Format counts are inconsistent.')
    difficulties = raw.get('allowed_difficulties') or []
    if not isinstance(difficulties, list) or any(x not in LEVELS for x in difficulties):
        conflicts.append('Unsupported difficulty preference.')
        difficulties = ['medium']
    selection = controls.get('difficulty')
    if selection and selection != 'any':
        if difficulties and selection not in difficulties:
            conflicts.append(f'Difficulty selector {selection} conflicts with the prompt restriction.')
        if not difficulties:
            difficulties = [selection]
    if not difficulties:
        difficulties = ['medium']
    difficulties = [x for x in LEVELS if x in difficulties]
    difficulty_distribution = dict(raw.get('difficulty_distribution') or {})
    if difficulty_distribution and (any(k not in LEVELS or not isinstance(v, int) or v < 0 for k, v in difficulty_distribution.items()) or sum(difficulty_distribution.values()) != count or any(k not in difficulties for k, v in difficulty_distribution.items() if v)):
        conflicts.append('Difficulty distribution must sum to count and fit the allowed difficulty levels.')
    topics = list(raw.get('included_topics') or [])
    ctrl_topics = controls.get('topics') or []
    if ctrl_topics and topics and not set(t.casefold() for t in ctrl_topics).issuperset(t.casefold() for t in topics):
        conflicts.append('Topic selections conflict with topics expressly required by the prompt.')
    if not topics:
        topics = ctrl_topics
    excluded = list(raw.get('excluded_topics') or [])
    if any(t.casefold() in {x.casefold() for x in excluded} for t in topics):
        conflicts.append('A topic is both included and excluded.')
    names = raw.get('source_names') or []
    if names and any(not any(n.casefold() in d.filename.casefold() for d in selected_docs) for n in names):
        conflicts.append('The prompt requires a PDF that is not included in the workspace.')
    source_docs = [d for d in selected_docs if any(n.casefold() in d.filename.casefold() for n in names)] if names else selected_docs
    if not source_docs:
        source_docs = selected_docs
    if original_prompt:
        # Cross-check obvious exact counts instead of solely trusting the LLM to preserve them.
        stated_total = re.search(r'\b(?:generate|create|write|make|prepare)\s+(?:exactly\s+)?(\d+)\s+(?:[a-z-]+\s+){0,5}questions\b', original_prompt, re.I)
        if stated_total and int(stated_total.group(1)) != count:
            conflicts.append(f'Prompt explicitly requests {stated_total.group(1)} questions; interpretation has {count}.')
        aliases = {
            'mcq':'mcq','multiple-choice question':'mcq', 'multiple choice question':'mcq',
            'short-answer question':'short_answer','short answer question':'short_answer',
            'descriptive question':'descriptive','numerical problem':'numerical',
            'scenario-based question':'scenario','scenario based question':'scenario'
        }
        found = Counter()
        for m in re.finditer(r'\b(\d+)\s+(mcqs?|multiple[- ]choice questions?|short[- ]answer questions?|descriptive questions?|numerical problems?|scenario[- ]based questions?)\b', original_prompt, re.I):
            name = m.group(2).casefold().rstrip('s')
            kind = aliases.get(name)
            if kind:
                found[kind] += int(m.group(1))
        if found and (any(types.get(k,0)!=v for k,v in found.items()) or sum(found.values()) > count):
            conflicts.append(f'Explicit format counts in the prompt {dict(found)} do not match the interpreted distribution {types}.')
    cognitive = raw.get('cognitive_levels') or []
    if any(x not in COGNITIVE for x in cognitive):
        conflicts.append('Unsupported cognitive level.')
    config = {
        'question_count': count, 'subject': str(raw.get('subject') or 'Uploaded course materials')[:200],
        'included_topics': topics, 'excluded_topics': excluded,
        'type_distribution': types, 'requested_types': raw.get('requested_types') or [], 'allowed_difficulties': difficulties,
        'difficulty_distribution': difficulty_distribution,
        'cognitive_levels': cognitive or list(COGNITIVE), 'requirements': raw.get('requirements') or {},
        'special_instructions': str(raw.get('special_instructions') or ''),
        'document_ids': [d.id for d in source_docs], 'source_only': True,
        'assumptions': raw.get('assumptions') or [], 'explicit_fields': sorted(explicit),
    }
    return config, conflicts
def validate_configuration(config: dict) -> None:
    n = config.get('question_count')
    if type(n) is not int or not 1 <= n <= settings.adaptiveai_max_questions:
        raise ValueError('Invalid question count')
    dist = config.get('type_distribution') or {}
    if not dist or set(dist) - set(TYPES) or any(type(v) is not int or v < 0 for v in dist.values()) or sum(dist.values()) != n:
        raise ValueError('Question-type counts do not match the question count')
    levels = config.get('allowed_difficulties') or []
    if not levels or set(levels) - set(LEVELS):
        raise ValueError('Invalid allowed difficulty list')
    d_dist = config.get('difficulty_distribution') or {}
    if d_dist and (set(d_dist) - set(levels) or any(type(v) is not int or v < 0 for v in d_dist.values()) or sum(d_dist.values()) != n):
        raise ValueError('Invalid fixed difficulty distribution')
    if set(config.get('cognitive_levels') or []) - set(COGNITIVE):
        raise ValueError('Invalid cognitive levels')
    ids = config.get('document_ids')
    if not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
        raise ValueError('Choose valid source documents')
    if not isinstance(config.get('included_topics', []), list) or not isinstance(config.get('excluded_topics', []), list):
        raise ValueError('Invalid topic lists')
    if set(config.get('included_topics', [])) & set(config.get('excluded_topics', [])):
        raise ValueError('A topic cannot be both included and excluded')
    if config.get('focus_plan') and not focus_plan_matches(config):
        raise ValueError('The focus plan no longer matches the question counts or topics')
def focus_plan_matches(config: dict) -> bool:
    """A weak-area plan is only usable while the configuration's counts and topics still agree with it."""
    areas = [a for a in (config.get('focus_plan') or {}).get('areas', []) if a.get('questions')]
    types = Counter()
    for area in areas:
        if area.get('difficulty') not in LEVELS or sum((area.get('types') or {}).values()) != area['questions']:
            return False
        types.update(area['types'])
    return (bool(areas)
            and sum(a['questions'] for a in areas) == config.get('question_count')
            and +types == +Counter(config.get('type_distribution') or {})
            and [a['name'] for a in areas] == list(config.get('included_topics') or []))
def get_documents(db: Session, draft: Draft, selected_ids: list[str] | None = None) -> list[Document]:
    ids = draft.document_ids if selected_ids is None else selected_ids
    if not ids or not set(ids).issubset(set(draft.document_ids)):
        raise ValueError('A restricted source document is outside the draft workspace')
    docs = db.scalars(select(Document).where(Document.id.in_(ids), Document.owner_id == draft.owner_id)).all()
    if len(docs) != len(set(ids)):
        raise ValueError('A selected document is unavailable')
    if any(d.status != 'ready' for d in docs):
        raise ValueError('All selected PDFs must finish indexing')
    return docs
def clean_text(text: str) -> str:
    # Underscores split words too, so "internal_fragmentation" matches "Internal Fragmentation".
    return ' '.join(re.findall(r'[^\W_]+', text.casefold()))


def duplicate_question(candidate: str, existing: list[str]) -> str | None:
    normalized = clean_text(candidate)
    candidate_tokens = set(normalized.split())
    for previous in existing:
        prior = clean_text(previous)
        if normalized == prior:
            return 'Duplicate question'
        prior_tokens = set(prior.split())
        if len(candidate_tokens) < 5 or len(prior_tokens) < 5:
            continue
        shared = len(candidate_tokens & prior_tokens)
        overlap = shared / len(candidate_tokens | prior_tokens)
        # Also catch one question restating another with a few extra words ("What is the maximum
        # number of stations..." vs "How many stations..." over the same long scenario).
        contained = shared / min(len(candidate_tokens), len(prior_tokens)) if min(len(candidate_tokens), len(prior_tokens)) >= 8 else 0
        if overlap >= .86 or contained >= .9 or SequenceMatcher(None, normalized, prior, autojunk=False).ratio() >= .90:
            return 'Near-duplicate question'
    return None


def validate_question_topic(question: dict, config: dict) -> None:
    if config.get('included_topics') and not any(
        clean_text(topic) in clean_text(question['topic'])
        or clean_text(question['topic']) in clean_text(topic)
        for topic in config['included_topics']
    ):
        raise ValueError('Generated question topic is outside the approved topic list')


def _contains_phrase(text: str, phrase: str) -> bool:
    phrase = clean_text(phrase)
    return bool(phrase) and f' {phrase} ' in f' {clean_text(text)} '


def validate_question(raw: dict, config: dict, evidence: list[dict], expected_type: str, difficulty: str | None = None, existing: list[str] | None = None, focus: str | None = None) -> dict:
    q = Question.model_validate(raw)
    if q.question_type != expected_type:
        raise ValueError('Incorrect generated question type')
    # Difficulty and cognitive level are the writer's self-reported labels for an item it was
    # explicitly asked to write at an approved level, so align the label instead of discarding it.
    allowed_levels = config['allowed_difficulties']
    if difficulty:
        q.difficulty = difficulty
    elif q.difficulty not in allowed_levels:
        q.difficulty = min(allowed_levels, key=lambda level: abs(LEVELS.index(level) - LEVELS.index(q.difficulty)))
    allowed_cognitive = config.get('cognitive_levels') or COGNITIVE
    if q.cognitive_level not in allowed_cognitive:
        q.cognitive_level = min(allowed_cognitive, key=lambda level: abs(COGNITIVE.index(level) - COGNITIVE.index(q.cognitive_level)))
    if focus:
        # The writer was asked for this approved topic; keep the label canonical for analytics and adaptation.
        if clean_text(q.topic) != clean_text(focus):
            q.subtopics = [q.topic, *[x for x in q.subtopics if clean_text(x) != clean_text(q.topic)]]
        q.topic = focus
    scope_text = ' '.join([q.topic, q.question_text, q.explanation, *q.subtopics])
    if any(_contains_phrase(scope_text, t) for t in config.get('excluded_topics') or []):
        raise ValueError('Question covers an excluded topic')
    allowed_ids = {row['id'] for row in evidence}
    def resolve(cid: str) -> str:
        # Writers sometimes echo the passage header ("[CHUNK <id>]") instead of the bare ID.
        return cid if cid in allowed_ids else re.sub(r'^\[?\s*chunk\s+', '', cid, flags=re.I).rstrip(' ]')
    cited = [cid for cid in dict.fromkeys(map(resolve, q.evidence_chunk_ids)) if cid in allowed_ids]
    if not cited:
        raise ValueError('Fabricated or missing evidence chunk reference')
    q.evidence_chunk_ids = cited
    rubric_total = round(sum(x.marks for x in q.evaluation_rubric), 4)
    if not isclose(rubric_total, q.max_marks, abs_tol=.01):
        if rubric_total > 100:
            raise ValueError('Rubric marks do not add up')
        q.max_marks = rubric_total
    if expected_type == 'mcq':
        if len(q.options) != 4 or len({x.id for x in q.options}) != 4 or len({clean_text(x.text) for x in q.options}) != 4:
            raise ValueError('MCQ must have four unique options')
        if q.correct_answer not in {x.id for x in q.options}:
            answer = q.correct_answer.strip()
            by_id = {x.id.casefold(): x.id for x in q.options}
            by_text = {clean_text(x.text): x.id for x in q.options}
            match = by_id.get(answer.strip('.) ').casefold()) or by_text.get(clean_text(answer))
            if not match:
                raise ValueError('MCQ has no unique correct option')
            q.correct_answer = match
    elif q.options:
        raise ValueError('Only MCQs may have options')
    if expected_type == 'numerical':
        if not q.calculation:
            # A value read directly from the source (e.g. "18.4%" throughput) has no arithmetic;
            # it is still checked against its citation by the evidence verifier.
            q.calculation = q.correct_answer.replace(',', '').strip()
        q.correct_answer = q.correct_answer.replace(',', '').strip()
        try:
            expected = float(q.correct_answer)
        except ValueError as exc:
            raise ValueError('Numerical answer must be numeric') from exc
        # A stated answer that is the calculation rounded to its own precision (0.88 for 0.8775...)
        # is consistent; widen the grading tolerance to match so equally rounded answers score.
        decimals = len(q.correct_answer.split('.', 1)[1]) if '.' in q.correct_answer and 'e' not in q.correct_answer.casefold() else 0
        rounding = 0.5 * 10 ** -decimals * 1.0001 if '.' in q.correct_answer or abs(expected) >= 1 else 0
        if not isclose(safe_calculation(q.calculation), expected, abs_tol=max(q.tolerance, rounding, 0.000001), rel_tol=1e-7):
            raise ValueError('Numerical answer inconsistent with calculation')
        q.tolerance = max(q.tolerance, rounding)
    duplicate = duplicate_question(q.question_text, existing or [])
    if duplicate:
        raise ValueError(duplicate)
    lookup = {e['id']: e for e in evidence}
    clean = q.model_dump()
    clean['source_references'] = [
        {'document_id': lookup[cid]['document_id'], 'filename': lookup[cid]['filename'],
         'pages': lookup[cid]['pages'], 'chunk_ids': [cid]}
        for cid in q.evidence_chunk_ids
    ]
    return clean


GENERATION_ROUNDS = 4
MAX_BATCH = 8
WRITE_PRIORITY = ['numerical', 'scenario', 'descriptive', 'short_answer', 'mcq']


def _evidence_rejections(questions: list[dict], evidence: list[dict]) -> dict[int, str]:
    """Run the independent evidence check; return {0-based index: reason} for rejected items."""
    for attempt in range(2):
        try:
            validate_evidence_batch(questions, evidence)
            return {}
        except EvidenceVerificationError as exc:
            rejected = {}
            for index, reason in exc.rejected:
                if not 1 <= index <= len(questions):
                    raise ValueError('Evidence verifier returned an invalid question index') from exc
                rejected[index - 1] = reason
            return rejected
        except ValueError:
            # Malformed verifier output rather than a verdict; ask once more before giving up.
            if attempt == 1:
                raise
    return {}


def generate_group(config: dict, evidence: list[dict], qtype: str, level: str | None, number: int,
                   existing: list[str] | None = None, focus: str | None = None,
                   notes: list[str] | None = None, previously_asked: list[str] | None = None,
                   allow_partial: bool = False) -> list[dict]:
    """Write `number` verified, mutually distinct questions of one format, difficulty and topic.

    Each round over-generates; candidates that fail structural checks, duplicate earlier items,
    or fail the independent evidence check are dropped, and the shortfall is requested again with
    every accepted and rejected question text listed as off-limits.

    `previously_asked` holds questions from the student's earlier attempts: the writer is told to
    avoid them and verbatim repeats are rejected, but re-testing the same concept is the point of
    targeted practice, so they are not subject to the near-duplicate check.
    """
    accepted: list[dict] = []
    rejected_texts: list[str] = []
    reasons: list[str] = []
    seen = list(existing or [])
    earlier = list(previously_asked or [])
    earlier_exact = {clean_text(text) for text in earlier}
    label = f'{qtype.replace("_", " ")} question'
    for _ in range(GENERATION_ROUNDS):
        need = number - len(accepted)
        if need <= 0:
            break
        ask = min(MAX_BATCH, need + max(1, need // 2))
        avoid = seen + [q['question_text'] for q in accepted] + rejected_texts
        kwargs = {'notes': notes} if notes else {}
        try:
            raw_batch = question_batch(config, evidence, qtype, ask, difficulty=level, existing=earlier + avoid,
                                       focus=focus, **kwargs)
        except InsufficientEvidenceError as exc:
            if allow_partial:
                return accepted
            raise InsufficientEvidenceError(
                f'The selected PDFs do not cover "{focus or config.get("subject") or "the requested subject"}" '
                f'well enough to write questions: {exc} Change the topics in the configuration or add material that covers them.'
            ) from exc
        except ValueError as exc:
            reasons.append(str(exc).splitlines()[0][:160])
            continue
        candidates = []
        for raw in raw_batch:
            try:
                candidate = validate_question(raw, config, evidence, qtype, level,
                                              avoid + [q['question_text'] for q in candidates], focus=focus)
                validate_question_topic(candidate, config)
                if clean_text(candidate['question_text']) in earlier_exact:
                    raise ValueError('Repeats a question from an earlier attempt')
                candidates.append(candidate)
            except ValueError as exc:
                reasons.append(str(exc).splitlines()[0][:160])
                if isinstance(raw, dict) and isinstance(raw.get('question_text'), str):
                    rejected_texts.append(raw['question_text'])
        if not candidates:
            continue
        rejections = _evidence_rejections(candidates, evidence)
        for index, candidate in enumerate(candidates):
            if index in rejections:
                reasons.append(f'evidence check: {rejections[index][:160]}')
                rejected_texts.append(candidate['question_text'])
            elif len(accepted) < number:
                accepted.append(candidate)
    if len(accepted) < number and not allow_partial:
        summary = '; '.join(f'{reason} (x{n})' if n > 1 else reason for reason, n in Counter(reasons).most_common(4))
        raise ValueError(
            f'Only {len(accepted)} of {number} distinct, source-verified {label}s'
            + (f' on "{focus}"' if focus else '')
            + f' could be written from the selected material after {GENERATION_ROUNDS} attempts. '
            'Reduce the question count, broaden the topics, or add more material.'
            + (f' Most common rejection reasons: {summary}.' if summary else '')
        )
    return accepted


def generate_set(db: Session, draft: Draft, config: dict, target: list[dict] | None = None, existing: list[str] | None = None) -> list[dict]:
    """Generate and verify ALL questions first; caller persists only on complete success."""
    validate_configuration(config)
    docs = get_documents(db, draft, config['document_ids'])
    quota = Counter(config['type_distribution']) if target is None else Counter(t['type'] for t in target)
    difficulty_quota = Counter(config.get('difficulty_distribution') or {}) if target is None else Counter(t.get('difficulty') for t in target if t.get('difficulty'))
    text_seen = list(existing or [])
    questions = []
    included = config.get('included_topics') or []
    topics = included or [config.get('subject') or 'Main concepts']
    plan = config.get('focus_plan') if target is None else None
    plan_areas = {a['name']: a for a in (plan or {}).get('areas', []) if a.get('questions')}
    allocations = []
    # A weak-area plan fixes each area's question count, formats and difficulty explicitly.
    for area in plan_areas.values():
        for qtype in TYPES:
            allocations.extend([(qtype, area['difficulty'], area['name'])] * (area['types'].get(qtype) or 0))
    for qtype, number in ({} if plan_areas else quota).items():
        if number < 1:
            continue
        if target is not None:
            specs = [t for t in target if t['type'] == qtype]
        elif difficulty_quota:
            specs = []
            for _ in range(number):
                level = next((d for d in LEVELS if difficulty_quota[d] > 0), None)
                if not level:
                    raise ValueError('Could not satisfy fixed difficulty distribution')
                specs.append({'type': qtype, 'difficulty': level})
                difficulty_quota[level] -= 1
        else:
            specs = [{'type': qtype, 'difficulty': None} for _ in range(number)]
        for spec in specs:
            focus = topics[len(allocations) % len(topics)]
            allocations.append((qtype, spec.get('difficulty'), focus))
    if target is None and len(topics) > len(allocations):
        raise ValueError('Question count is smaller than the number of explicitly requested topics')
    # Batch by format/topic/difficulty to reduce provider round trips, but keep targeted retrieval.
    # Write the most constrained formats first so flexible MCQs do not use up the few worked
    # problems a short source offers; the set is reassembled in the original order afterwards.
    groups = list(Counter(allocations).items())
    written = defaultdict(list)
    evidence_by_focus = {}

    def write(qtype, level, focus, number, partial=False):
        area = plan_areas.get(focus) or {}
        if focus not in evidence_by_focus:
            top_k = max(settings.adaptiveai_retrieval_top_k, min(number + 2, 12))
            # For targeted practice, also retrieve around the questions the student missed.
            queries = [focus, f'{config["subject"]}: {focus}', *(area.get('queries') or [])[:3]]
            evidence_by_focus[focus] = retrieve(db, docs, queries, top_k=top_k)[:max(top_k, 12)]
        evidence = evidence_by_focus[focus]
        if not evidence:
            if partial:
                return []
            raise ValueError(f'Insufficient source evidence for {focus}; upload more materials')
        batch = generate_group(config, evidence, qtype, level, number, existing=text_seen,
                               focus=focus if included else None, notes=area.get('weak_points'),
                               previously_asked=(plan or {}).get('previous_questions'), allow_partial=partial)
        text_seen.extend(question['question_text'] for question in batch)
        written[(qtype, level, focus)].extend(batch)
        return batch

    shortfalls = []
    for (qtype, level, focus), number in sorted(groups, key=lambda g: WRITE_PRIORITY.index(g[0][0])):
        # A weak-area plan should not fail outright because one concept is thin in the source.
        missing = number - len(write(qtype, level, focus, number, partial=bool(plan_areas)))
        if missing:
            shortfalls.append((qtype, focus, missing))
    for qtype, failed_focus, missing in shortfalls:
        # Hand the missing questions to the next-weakest areas, keeping the format.
        for other in sorted(plan_areas.values(), key=lambda a: a['mastery']):
            if missing and other['name'] != failed_focus:
                missing -= len(write(qtype, other['difficulty'], other['name'], missing, partial=True))
        if missing:
            raise ValueError(f'The selected material could not support {missing} more {qtype.replace("_", " ")} '
                             f'question(s) for this weak-area plan (starting with "{failed_focus}"). '
                             'Try a smaller question count or add more material.')
    for key in dict.fromkeys([key for key, _ in groups] + list(written)):
        questions.extend(written[key])
    if len(questions) != sum(quota.values()):
        raise ValueError('Incomplete generated question set')
    if target is None:
        if Counter(q['question_type'] for q in questions) != quota:
            raise ValueError('Question-type distribution violated')
        fixed = config.get('difficulty_distribution') or {}
        if fixed and Counter(q['difficulty'] for q in questions) != Counter({k:v for k,v in fixed.items() if v}):
            raise ValueError('Fixed difficulty distribution violated')
    return questions


def sync_focus_plan(config: dict, questions: list[dict]) -> dict:
    """Record how many questions each weak area actually received (thin areas hand theirs on)."""
    plan = config.get('focus_plan')
    if not plan:
        return config
    by_area = defaultdict(Counter)
    for q in questions:
        by_area[q['topic']][q['question_type']] += 1
    areas = []
    for area in plan['areas']:
        types = by_area.get(area['name'], Counter())
        updated = {**area, 'questions': sum(types.values()), 'types': dict(types)}
        if area['questions'] and updated['questions'] != area['questions']:
            if not updated['questions']:
                updated['status'] = 'deferred'
                updated['reason'] = 'The material could not support new questions here this time; its share went to other weak areas.'
            elif updated['questions'] < area['questions']:
                updated['reason'] += f' (Planned {area["questions"]}; the material supported {updated["questions"]}.)'
            else:
                updated['reason'] += f' (Planned {area["questions"]}; took over questions from an area the material could not cover.)'
        areas.append(updated)
    return {**config, 'included_topics': [a['name'] for a in areas if a['questions']], 'focus_plan': {**plan, 'areas': areas}}


def draft_preview(db: Session, draft_id: str) -> list[dict]:
    entries = db.scalars(select(DraftQuestion).where(DraftQuestion.draft_id == draft_id, DraftQuestion.active == True).order_by(DraftQuestion.slot)).all()
    return [{'id': row.id, 'slot': row.slot, 'revision': row.revision, **safe_question(row.payload)} for row in entries]
def safe_question(payload: dict) -> dict:
    return {k: payload.get(k) for k in ['question_text','question_type','topic','subtopics','difficulty','cognitive_level','options','max_marks','units','source_references']}
def grade(db: Session, question: dict, answer: str) -> dict:
    if question['question_type'] == 'mcq':
        is_correct = answer.strip() == question['correct_answer']
        marks = question['max_marks'] if is_correct else 0
        return {'marks_awarded': marks, 'feedback': 'Correct.' if is_correct else 'Incorrect.', 'explanation': question['explanation'], 'correct': is_correct, 'method': 'deterministic'}
    if question['question_type'] == 'numerical':
        try:
            numeric = re.fullmatch(r'\s*([-+]?(?:\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*(.*?)\s*', answer)
            if numeric is None:
                raise ValueError('Could not parse a numerical answer')
            number = float(numeric.group(1).replace(',', ''))
            target = float(str(question['correct_answer']).replace(',', ''))
            units = ' '.join((question.get('units') or '').casefold().split())
            supplied_units = ' '.join(numeric.group(2).casefold().split())
            # Omitting the unit is fine; supplying a different one is not.
            correct = supplied_units in ('', units) and isclose(number, target, abs_tol=question.get('tolerance', .0001), rel_tol=1e-8)
        except ValueError:
            correct = False
        return {'marks_awarded': question['max_marks'] if correct else 0, 'feedback': 'Correct calculation.' if correct else 'Incorrect numerical result.', 'explanation': question['explanation'], 'correct': correct, 'method': 'deterministic'}
    ids = [cid for r in question['source_references'] for cid in r['chunk_ids']]
    passages = [c.text for c in db.scalars(select(Chunk).where(Chunk.id.in_(ids))).all()]
    evaluated = FreeTextEvaluation.model_validate(grade_text(question, answer, passages)).model_dump()
    if not isclose(evaluated['max_marks'], question['max_marks'], abs_tol=.0001):
        raise ValueError('Evaluator changed the maximum marks')
    awarded = evaluated.get('marks_awarded')
    if not isinstance(awarded, (int, float)) or type(awarded) is bool or not 0 <= awarded <= question['max_marks']:
        raise ValueError('Invalid marks from evaluator; response remains pending')
    if not isinstance(evaluated.get('feedback'), str) or not evaluated['feedback'].strip():
        raise ValueError('Missing evaluator feedback')
    return {**evaluated, 'method': 'llm_rubric'}
_session_locks: dict[str, threading.RLock] = {}
_lock_guard = threading.Lock()
def session_lock(session_id):
    with _lock_guard:
        return _session_locks.setdefault(session_id, threading.RLock())
def next_question(db, session_id):
    answered = set(db.scalars(select(Response.slot).where(Response.session_id == session_id)).all())
    return next((row for row in db.scalars(select(SessionQuestion).where(SessionQuestion.session_id == session_id, SessionQuestion.active == True).order_by(SessionQuestion.slot)).all() if row.slot not in answered), None)
def progress(db, session):
    rows = db.scalars(select(Response).where(Response.session_id == session.id)).all()
    graded = [x for x in rows if x.state == 'graded']
    return {'total': session.configuration['question_count'], 'answered': len(graded), 'pending': len(rows) - len(graded), 'status': session.status}
def adapt(db: Session, session: Assessment, trigger: Response) -> None:
    """Only active unsubmitted slots are eligible; fail-safe retains existing question."""
    if db.scalar(select(Adaptation.id).where(Adaptation.trigger_response_id == trigger.id)):
        return
    graded = db.scalars(select(Response).where(Response.session_id == session.id, Response.state == 'graded').order_by(Response.submitted_at, Response.slot)).all()
    min_n = settings.adaptiveai_adapt_min_answers
    decision, reason, desired = 'unchanged', 'Not enough graded answers for a reliable change.', None
    triggering_question = db.get(SessionQuestion, trigger.question_revision_id)
    trigger_topic = triggering_question.payload['topic']
    on_topic = [r for r in graded if db.get(SessionQuestion, r.question_revision_id).payload['topic'] == trigger_topic]
    if len(on_topic) >= min_n:
        window = on_topic[-min_n:]
        average = sum(x.marks / x.max_marks for x in window) / len(window)
        if average >= settings.adaptiveai_adapt_high:
            desired, decision = 'up', 'increase'
        elif average <= settings.adaptiveai_adapt_low:
            desired, decision = 'down', 'decrease'
        else:
            decision = 'unchanged'
        reason = f'Recent {len(window)} graded answers in topic {trigger_topic} averaged {average:.0%}; policy decision: {decision}.'
    previous, replacement = None, None
    old_level, new_level = None, None
    if desired and session.configuration.get('difficulty_distribution'):
        decision, reason = 'constrained', reason + ' Explicit difficulty counts prohibit changing any slot difficulty.'
    elif desired:
        answered = set(db.scalars(select(Response.slot).where(Response.session_id == session.id)).all())
        items = db.scalars(select(SessionQuestion).where(SessionQuestion.session_id == session.id, SessionQuestion.active == True).order_by(SessionQuestion.slot)).all()
        eligible = [x for x in items if x.slot not in answered and x.revision == 1]
        config = session.configuration
        candidate = next((x for x in eligible if x.payload['topic'] == trigger_topic and 0 <= LEVELS.index(x.payload['difficulty']) + (1 if desired == 'up' else -1) < 3 and LEVELS[LEVELS.index(x.payload['difficulty']) + (1 if desired == 'up' else -1)] in config['allowed_difficulties']), None)
        if candidate:
            old_level = candidate.payload['difficulty']
            new_level = LEVELS[LEVELS.index(old_level) + (1 if desired == 'up' else -1)]
            try:
                draft = db.get(Draft, session.draft_id)
                topic_config = {**config, 'included_topics': [candidate.payload['topic']], 'focus_plan': None}
                prior_text = [x.payload['question_text'] for x in items]
                generated = generate_set(db, draft, topic_config, target=[{'type': candidate.payload['question_type'], 'difficulty': new_level}], existing=prior_text)[0]
                if generated['max_marks'] != candidate.payload['max_marks'] or generated['topic'].casefold() != candidate.payload['topic'].casefold():
                    raise ValueError('Replacement changed the mark allocation or approved topic')
                previous = candidate.id
                rev = SessionQuestion(session_id=session.id, slot=candidate.slot, revision=candidate.revision + 1, payload=generated, replaced_from=previous, active=True)
                db.add(rev)
                db.flush()
                candidate.active = False
                replacement = rev.id
                decision = 'replaced'
                reason += f' Replaced unanswered slot {candidate.slot} from {old_level} to {new_level} while preserving format and sources.'
            except Exception as exc:
                db.rollback()
                # Caller may have already committed the grading; isolate replacement failure.
                reason += f' Replacement failed, kept original question: {str(exc)[:160]}'
                decision, previous, replacement = 'replacement_failed', None, None
        else:
            decision, reason = 'constrained', reason + ' No eligible unanswered question within approved difficulty bounds.'
    db.add(Adaptation(session_id=session.id, trigger_response_id=trigger.id, previous_question_id=previous, replacement_question_id=replacement, old_difficulty=old_level, new_difficulty=new_level if replacement else None, decision=decision, reason=reason))
    db.commit()
def assessment_report(db: Session, session: Assessment) -> dict:
    all_questions = db.scalars(select(SessionQuestion).where(SessionQuestion.session_id == session.id)).all()
    by_id = {q.id: q for q in all_questions}
    answers = db.scalars(select(Response).where(Response.session_id == session.id).order_by(Response.slot)).all()
    graded = [r for r in answers if r.state == 'graded']
    earned = sum(r.marks or 0 for r in graded)
    max_total = sum(x['max_marks'] for x in session.initial_snapshot)
    objectives = [r for r in graded if by_id[r.question_revision_id].payload['question_type'] in ['mcq', 'numerical']]
    correct_count = sum(1 for r in objectives if r.evaluation and r.evaluation.get('correct'))
    topic_map = defaultdict(list)
    difficulty_map = defaultdict(list)
    results = []
    recommendations = []
    for r in answers:
        q = by_id[r.question_revision_id]
        p = q.payload
        detail = {'slot': r.slot, 'question_revision_id': q.id, 'revision': q.revision, 'question': p['question_text'], 'question_type': p['question_type'], 'topic': p['topic'], 'difficulty': p['difficulty'], 'answer': r.answer, 'reference_answer': p['correct_answer'] + (' ' + p['units'] if p.get('units') else ''), 'rubric': p['evaluation_rubric'], 'marks': r.marks, 'max_marks': r.max_marks, 'feedback': r.feedback, 'explanation': p['explanation'], 'sources': p['source_references'], 'state': r.state}
        results.append(detail)
        if r.state == 'graded':
            topic_map[p['topic']].append((r, p))
            difficulty_map[p['difficulty']].append((r, p))
            if (r.marks or 0) / r.max_marks < .6:
                recommendations.append({'topic': p['topic'], 'question': p['question_text'], 'sources': p['source_references'], 'suggestion': f'Review {p["topic"]} in the cited PDF pages and retry a {p["question_type"]} question.'})
    def summarize(items):
        maximum = sum(r.max_marks for r, _ in items)
        obtained = sum(r.marks or 0 for r, _ in items)
        objective = [(r,p) for r,p in items if p['question_type'] in ('mcq','numerical')]
        return {'attempted': len(items), 'marks': obtained, 'max_marks': maximum, 'score_percent': round(100*obtained/maximum,1) if maximum else None,
                'objective_accuracy': round(100*sum(bool(r.evaluation.get('correct')) for r,p in objective)/len(objective),1) if objective else None,
                'classification': 'provisional' if len(items)<3 else 'strong' if obtained/maximum >= .8 else 'needs_revision' if obtained/maximum < .6 else 'developing'}
    changes = db.scalars(select(Adaptation).where(Adaptation.session_id == session.id).order_by(Adaptation.created_at)).all()
    return {'id': session.id, 'status': session.status, 'original_prompt': session.prompt,
            'overall': {'total_questions': session.configuration['question_count'], 'attempted': len(graded), 'unanswered': session.configuration['question_count']-len(graded), 'marks': earned, 'max_marks': max_total, 'percent': round(100*earned/max_total,1) if max_total else None, 'objective_accuracy': round(100*correct_count/len(objectives),1) if objectives else None, 'descriptive_marks': sum(r.marks or 0 for r in graded if by_id[r.question_revision_id].payload['question_type'] in ('short_answer','descriptive','scenario'))},
            'topics': {key:summarize(vals) for key,vals in topic_map.items()}, 'difficulties': {key:summarize(vals) for key,vals in difficulty_map.items()},
            'results': results, 'recommendations': recommendations,
            'adaptations': [{'decision': e.decision, 'reason': e.reason, 'old_difficulty': e.old_difficulty, 'new_difficulty': e.new_difficulty, 'previous_question_id': e.previous_question_id, 'replacement_question_id': e.replacement_question_id, 'at': e.created_at.isoformat()} for e in changes]}
