"""Weak-area practice plans built from earlier attempts, and attempt-to-attempt comparisons."""
from collections import Counter, defaultdict
import logging
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.ai import ProviderUnavailable, cluster_concepts
from app.assessment import COGNITIVE, LEVELS, TYPES, clean_text
from app.config import settings
from app.models import Assessment, Draft, Response, SessionQuestion

logger = logging.getLogger(__name__)

MAX_HISTORY_ATTEMPTS = 5      # attempts on the same material that inform a plan
MAX_PLAN_AREAS = 8
STRONG = .85                  # mastery at/above this only gets a retention check
WEAK = .6                     # current score below this is reported as "needs work"
CHANGE = 10                   # percentage points that count as a real improvement/regression
GENERIC_WORDS = {'a', 'an', 'and', 'the', 'of', 'in', 'on', 'for', 'to', 'its', 'functionality', 'function',
                 'functions', 'calculation', 'calculations', 'concept', 'concepts', 'role', 'purpose',
                 'definition', 'basics', 'overview', 'introduction'}


def topic_key(label: str) -> str:
    """Normalize free-form topic labels for matching: "PTBR_function" ~ "PTBR Functionality",
    "Probability of Collisions" ~ "Collision Probability" (generic words, plurals, word order)."""
    words = [w for w in clean_text(label or '').split() if w not in GENERIC_WORDS]
    stems = {w[:-1] if len(w) > 3 and w.endswith('s') and not w.endswith('ss') else w for w in words}
    return ' '.join(sorted(stems)) or clean_text(label or '')


def _ratio(marks: float, maximum: float) -> float | None:
    return marks / maximum if maximum else None


def _num(value: float) -> str:
    """30.0 -> "30", 33.3 -> "33.3" for readable sentences."""
    return f'{value:.1f}'.rstrip('0').rstrip('.')


def _pct(marks: float, maximum: float) -> float | None:
    return round(100 * marks / maximum, 1) if maximum else None


def attempt_items(db: Session, session: Assessment) -> list[dict]:
    """Every graded answer of one attempt with the question it answered."""
    rows = db.execute(
        select(Response, SessionQuestion)
        .join(SessionQuestion, SessionQuestion.id == Response.question_revision_id)
        .where(Response.session_id == session.id, Response.state == 'graded')
        .order_by(Response.slot)
    ).all()
    items = []
    for response, question in rows:
        p = question.payload
        evaluation = response.evaluation or {}
        items.append({
            'session_id': session.id, 'question': p['question_text'], 'topic': p.get('topic') or '',
            'type': p['question_type'], 'difficulty': p['difficulty'], 'marks': float(response.marks or 0),
            'max_marks': float(response.max_marks), 'missing': [str(x) for x in evaluation.get('missing_elements') or []][:3],
        })
    return items


def related_attempts(db: Session, session: Assessment, include_later: bool = False) -> list[Assessment]:
    """The owner's attempts on overlapping source PDFs, oldest first, that have graded answers."""
    docs = set(session.configuration.get('document_ids') or [])
    query = select(Assessment).where(Assessment.owner_id == session.owner_id).order_by(Assessment.created_at)
    attempts = []
    for attempt in db.scalars(query).all():
        if not include_later and attempt.created_at > session.created_at:
            continue
        if not docs & set(attempt.configuration.get('document_ids') or []):
            continue
        has_graded = db.scalar(select(Response.id).where(Response.session_id == attempt.id, Response.state == 'graded').limit(1))
        if has_graded or attempt.id == session.id:
            attempts.append(attempt)
    return attempts


def _group_into_areas(items: list[dict]) -> tuple[list[dict], str]:
    """Cluster questions into concept areas with the model; fall back to normalized topic labels."""
    def by_label(indexes):
        groups = defaultdict(list)
        for i in indexes:
            groups[topic_key(items[i]['topic']) or 'general'].append(i)
        def label(text):  # "PTBR_function" -> "PTBR Function", keeping acronyms intact
            return ' '.join(w if any(c.isupper() for c in w) else w.capitalize() for w in text.replace('_', ' ').split()) or 'General'
        return [{'name': label(items[ix[0]]['topic']), 'items': ix} for ix in groups.values()]

    try:
        raw = cluster_concepts([{'index': i, 'topic': it['topic'], 'question': it['question'][:300]} for i, it in enumerate(items)],
                               max_areas=MAX_PLAN_AREAS)
    except ProviderUnavailable:
        raise
    except Exception:
        logger.exception('Concept grouping failed; grouping weak areas by topic label instead')
        return by_label(range(len(items))), 'topic_labels'
    areas, assigned, by_name = [], set(), {}
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not entry['name'].strip():
            continue
        indexes = [i for i in entry.get('items') or [] if type(i) is int and 0 <= i < len(items) and i not in assigned]
        if not indexes:
            continue
        assigned.update(indexes)
        name = ' '.join(entry['name'].split())[:60]
        key = topic_key(name)
        if key in by_name:
            by_name[key]['items'].extend(indexes)
        else:
            by_name[key] = {'name': name, 'items': indexes}
            areas.append(by_name[key])
    leftovers = [i for i in range(len(items)) if i not in assigned]
    for group in by_label(leftovers):
        key = topic_key(group['name'])
        if key in by_name:
            by_name[key]['items'].extend(group['items'])
        else:
            by_name[key] = group
            areas.append(group)
    return areas, 'concepts'


def _largest_remainder(total: int, weights: dict[str, float], minimum: int = 0) -> dict[str, int]:
    counts = {k: minimum for k in weights}
    remaining = total - minimum * len(weights)
    weight_sum = sum(weights.values()) or 1
    shares = {k: remaining * w / weight_sum for k, w in weights.items()}
    for k in counts:
        counts[k] += int(shares[k])
    leftover = total - sum(counts.values())
    for k in sorted(shares, key=lambda k: (shares[k] - int(shares[k]), weights[k]), reverse=True)[:leftover]:
        counts[k] += 1
    return counts


def build_focus_plan(db: Session, session: Assessment, question_count: int | None = None) -> dict:
    """Configuration for a new attempt that concentrates on the student's weakest concept areas."""
    attempts = related_attempts(db, session)[-MAX_HISTORY_ATTEMPTS:]
    recency = {a.id: 0.5 ** rank for rank, a in enumerate(reversed(attempts))}  # latest attempt weighs most
    when = {a.id: a.created_at for a in attempts}
    items = [item for attempt in attempts for item in attempt_items(db, attempt)]
    if not items:
        raise ValueError('Answer at least one question in this attempt before building a weak-area practice set')
    areas, grouping = _group_into_areas(items)

    plan_areas = []
    for area in areas:
        members = [items[i] for i in area['items']]
        weighted_marks = sum(recency[m['session_id']] * m['marks'] for m in members)
        weighted_max = sum(recency[m['session_id']] * m['max_marks'] for m in members)
        history = []
        for attempt in attempts:
            mine = [m for m in members if m['session_id'] == attempt.id]
            if mine:
                history.append({
                    'session_id': attempt.id, 'at': attempt.created_at.isoformat(), 'count': len(mine),
                    'marks': sum(m['marks'] for m in mine), 'max_marks': sum(m['max_marks'] for m in mine),
                    'difficulty': LEVELS[round(sum(LEVELS.index(m['difficulty']) for m in mine) / len(mine))],
                })
        missed = sorted((m for m in members if m['marks'] < m['max_marks']), key=lambda m: when[m['session_id']], reverse=True)
        latest_level = history[-1]['difficulty']
        plan_areas.append({
            'name': area['name'],
            'mastery': round(_ratio(weighted_marks, weighted_max) or 0, 3),
            'history': history,
            'weak_points': [
                (m['question'] + (f" (missed: {'; '.join(m['missing'])})" if m['missing'] else ''))[:260]
                for m in missed[:4]
            ],
            'queries': [m['question'] for m in missed[:3]] or [m['question'] for m in members[:2]],
            'seen_types': dict(Counter(m['type'] for m in members)),
            'previous_difficulty': latest_level,
        })

    n = question_count or session.configuration.get('question_count') or 10
    n = max(1, min(int(n), settings.adaptiveai_max_questions))
    plan_areas.sort(key=lambda a: a['mastery'])
    weak = [a for a in plan_areas if a['mastery'] < STRONG] or list(plan_areas)
    strong = [a for a in plan_areas if a not in weak]
    retention = min(len(strong), n // 5) if len(weak) < n else 0
    weak = weak[:n - retention]
    counts = _largest_remainder(n - retention, {a['name']: (1 - a['mastery']) + .15 for a in weak}, minimum=1)
    for area in strong[:retention]:
        counts[area['name']] = 1

    default_types = [t for t in TYPES if (session.configuration.get('type_distribution') or {}).get(t)] or ['mcq']
    for area in plan_areas:
        area['questions'] = counts.get(area['name'], 0)
        base = LEVELS.index(area['previous_difficulty'])
        latest = area['history'][-1]
        latest_ratio = _ratio(latest['marks'], latest['max_marks']) or 0
        if latest_ratio >= .75:
            level = min(base + 1, 2)
            why = (f'you scored {round(100 * latest_ratio)}% at {latest["difficulty"]} last time, so this attempt '
                   + ('checks it one level harder' if level > base else 'keeps checking it at hard'))
        elif area['mastery'] < .4:
            level = max(base - 1, 0)
            why = ('you scored low here, so this attempt rebuilds it one level easier' if level < base
                   else 'you scored low here, so this attempt stays at easy with fresh questions')
        else:
            level, why = base, 'you are partly there, so this attempt practises it at the same level'
        area['difficulty'] = LEVELS[level]
        if area['questions'] == 0 and area['mastery'] >= STRONG:
            area['status'], area['reason'] = 'skipped', 'Strong already; not retested this time.'
        elif area['questions'] == 0:
            area['status'], area['reason'] = 'deferred', 'Left for a later attempt; weaker areas took the available questions.'
        elif area['mastery'] >= STRONG:
            area['status'], area['reason'] = 'retention', f'Retention check: {why}.'
        else:
            area['status'] = 'weak' if area['mastery'] < WEAK else 'developing'
            area['reason'] = f'Score so far {round(100 * area["mastery"])}%; {why}.'
        seen = {t: c for t, c in area['seen_types'].items() if t in default_types} or {t: 1 for t in default_types}
        area['types'] = {t: c for t, c in _largest_remainder(area['questions'], seen).items() if c} if area['questions'] else {}

    active = [a for a in plan_areas if a['questions']]
    type_totals = Counter()
    for area in active:
        type_totals.update(area['types'])
    previous = [it['question'] for it in items][-60:]
    summary = ', '.join(f'{a["name"]} ({a["questions"]})' for a in active)
    config = {
        'question_count': n,
        'subject': session.configuration.get('subject') or 'Uploaded course materials',
        'included_topics': [a['name'] for a in active],
        'excluded_topics': [],
        'type_distribution': dict(type_totals),
        'requested_types': [],
        'allowed_difficulties': list(LEVELS),
        'difficulty_distribution': {},
        'cognitive_levels': list(COGNITIVE),
        'requirements': {},
        'special_instructions': f'Targeted practice on weak areas: {summary}.',
        'document_ids': list(session.configuration['document_ids']),
        'source_only': True,
        'assumptions': [],
        'explicit_fields': [],
        'focus_plan': {
            'based_on': [a.id for a in attempts],
            'parent_session_id': session.id,
            'grouping': grouping,
            'areas': plan_areas,
            'previous_questions': previous,
        },
    }
    return config


def focus_prompt(config: dict, session: Assessment) -> str:
    areas = [a for a in config['focus_plan']['areas'] if a['questions']]
    listing = '; '.join(f'{a["name"]}: {a["questions"]} {a["difficulty"]}' for a in areas)
    return (f'Weak-area practice built from your attempt on {session.created_at:%d %b %Y %H:%M} '
            f'and {len(config["focus_plan"]["based_on"]) - 1} earlier attempt(s) on the same material. {listing}.')


def _stats(items: list[dict]) -> dict | None:
    if not items:
        return None
    marks, maximum = sum(i['marks'] for i in items), sum(i['max_marks'] for i in items)
    return {'count': len(items), 'marks': marks, 'max_marks': maximum, 'percent': _pct(marks, maximum),
            'difficulty': LEVELS[round(sum(LEVELS.index(i['difficulty']) for i in items) / len(items))]}


def _previous_attempt(db: Session, session: Assessment) -> Assessment | None:
    draft = db.get(Draft, session.draft_id)
    if draft and draft.parent_session_id:
        parent = db.get(Assessment, draft.parent_session_id)
        if parent and parent.owner_id == session.owner_id:
            return parent
    earlier = [a for a in related_attempts(db, session) if a.id != session.id]
    return earlier[-1] if earlier else None


def compare_with_previous(db: Session, session: Assessment) -> dict | None:
    """What changed since the previous attempt on the same material, area by area."""
    previous = _previous_attempt(db, session)
    if not previous:
        return None
    current_items, previous_items = attempt_items(db, session), attempt_items(db, previous)
    if not current_items:
        return None
    plan = session.configuration.get('focus_plan')
    rows = []
    if plan:
        by_area = defaultdict(list)
        names = {a['name'].casefold(): a['name'] for a in plan['areas']}
        for item in current_items:
            by_area[names.get(item['topic'].casefold(), item['topic'])].append(item)
        for area in plan['areas']:
            before = next((h for h in area['history'] if h['session_id'] == previous.id), None)
            source = 'previous attempt'
            if not before and area['history']:
                before, source = area['history'][-1], 'an earlier attempt'
            if before:
                before = {**before, 'percent': _pct(before['marks'], before['max_marks'])}
            rows.append({'area': area['name'], 'before': before, 'before_source': source if before else None,
                         'now': _stats(by_area.pop(area['name'], [])), 'planned_questions': area['questions'],
                         'items': []})
        for name, extra in by_area.items():
            rows.append({'area': name, 'before': None, 'before_source': None, 'now': _stats(extra), 'planned_questions': 0, 'items': []})
        for row in rows:
            row['items'] = [i for i in current_items if i['topic'].casefold() == row['area'].casefold()]
    else:
        prev_groups, cur_groups, labels = defaultdict(list), defaultdict(list), {}
        for item in previous_items:
            prev_groups[topic_key(item['topic'])].append(item)
            labels.setdefault(topic_key(item['topic']), item['topic'])
        for item in current_items:
            cur_groups[topic_key(item['topic'])].append(item)
            labels[topic_key(item['topic'])] = item['topic']
        for key in dict.fromkeys([*cur_groups, *prev_groups]):
            rows.append({'area': labels[key].replace('_', ' '), 'before': _stats(prev_groups.get(key, [])),
                         'before_source': 'previous attempt' if key in prev_groups else None,
                         'now': _stats(cur_groups.get(key, [])), 'planned_questions': None, 'items': cur_groups.get(key, [])})

    improved, regressed, needs_work, strengths, not_retested, new = [], [], [], [], [], []
    for row in rows:
        before, now = row['before'], row['now']
        row['change'] = round(now['percent'] - before['percent'], 1) if before and now else None
        row['difficulty_note'] = None
        if before and now and now['difficulty'] != before['difficulty']:
            easier = LEVELS.index(now['difficulty']) < LEVELS.index(before['difficulty'])
            row['difficulty_note'] = f'tested {"easier" if easier else "harder"} this time ({before["difficulty"]} → {now["difficulty"]})'
        if not now:
            row['status'] = 'not_retested'
            not_retested.append(row['area'])
        elif not before:
            row['status'] = 'new'
            new.append(row['area'])
        elif row['change'] >= CHANGE:
            row['status'] = 'improved'
            improved.append(row)
        elif row['change'] <= -CHANGE:
            row['status'] = 'regressed'
            regressed.append(row)
        elif now['percent'] < 100 * WEAK:
            row['status'] = 'still_weak'
        else:
            row['status'] = 'steady'
        if now and now['percent'] is not None and now['percent'] < 100 * WEAK:
            missed = [i for i in row['items'] if i['marks'] < i['max_marks']]
            needs_work.append({'area': row['area'], 'percent': now['percent'],
                               'missed': [i['question'] + (f" (missed: {'; '.join(i['missing'])})" if i['missing'] else '') for i in missed[:3]]})
        elif now and now['percent'] is not None and now['percent'] >= 80:
            strengths.append(row['area'])
        del row['items']

    before_all, now_all = _stats(previous_items), _stats(current_items)
    overall_change = round(now_all['percent'] - before_all['percent'], 1) if before_all and now_all else None
    summary = []
    if overall_change is not None:
        direction = 'up' if overall_change > 0 else 'down' if overall_change < 0 else 'unchanged'
        summary.append(f'Overall score {direction}: {_num(before_all["percent"])}% → {_num(now_all["percent"])}%'
                       + (f' ({"+" if overall_change > 0 else "-"}{_num(abs(overall_change))} points).' if overall_change else '.'))
    if improved:
        summary.append('Improved: ' + '; '.join(
            f'{r["area"]} ({_num(r["before"]["percent"])}% → {_num(r["now"]["percent"])}%' + (f', {r["difficulty_note"]}' if r['difficulty_note'] else '') + ')'
            for r in improved) + '.')
    if regressed:
        summary.append('Slipped: ' + '; '.join(f'{r["area"]} ({_num(r["before"]["percent"])}% → {_num(r["now"]["percent"])}%)' for r in regressed) + '.')
    if needs_work:
        summary.append('Still needs work: ' + '; '.join(f'{n["area"]} ({_num(n["percent"])}%)' for n in needs_work) + '.')
    if strengths:
        summary.append('Strong now: ' + ', '.join(strengths) + '.')
    easier_gains = [r['area'] for r in improved if r['difficulty_note'] and 'easier' in r['difficulty_note']]
    if easier_gains:
        summary.append('Some gains came on easier questions (' + ', '.join(easier_gains)
                       + '); a weak-area attempt moves an area up a level once you score 75% or more at its current level.')
    if not_retested:
        summary.append('Not retested this time: ' + ', '.join(not_retested) + '.')

    trend = []
    for attempt in related_attempts(db, session):
        stats = _stats(attempt_items(db, attempt))
        trend.append({'session_id': attempt.id, 'at': attempt.created_at.isoformat(),
                      'percent': stats['percent'] if stats else None, 'questions': stats['count'] if stats else 0,
                      'focused': bool(attempt.configuration.get('focus_plan')), 'current': attempt.id == session.id})
    return {
        'previous_session_id': previous.id, 'previous_at': previous.created_at.isoformat(),
        'overall': {'before': before_all, 'now': now_all, 'change': overall_change},
        'areas': rows, 'improved': [r['area'] for r in improved], 'regressed': [r['area'] for r in regressed],
        'needs_work': needs_work, 'strengths': strengths, 'not_retested': not_retested, 'new_areas': new,
        'summary': summary, 'trend': trend,
        'next_step': ('Start another weak-area attempt to work on: ' + ', '.join(n['area'] for n in needs_work) + '.')
        if needs_work else 'No weak areas left at this level; a new weak-area attempt will raise the difficulty.',
    }
