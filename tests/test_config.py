import pytest
from app.assessment import build_configuration,validate_configuration,validate_question,safe_calculation
from app.models import Document


def doc():
    return Document(id='doc-1',owner_id='u',filename='os.pdf',storage_key='private',sha256='hash',size_bytes=20,status='ready')


def basic():
    return {'question_count':20,'included_topics':['deadlocks'],'type_distribution':{'mcq':10,'descriptive':5,'scenario':5},'allowed_difficulties':['hard'],'difficulty_distribution':{},'cognitive_levels':['application']}


def test_prompt_controls_conflict_and_distribution():
    data, conflicts=build_configuration(basic(),{'question_count':30,'difficulty':'medium'},[doc()])
    assert data['question_count']==20
    assert len(conflicts)>=2
    valid, conflicts=build_configuration(basic(),{},[doc()])
    assert not conflicts
    validate_configuration(valid)


def test_count_distribution_and_source_conflicts():
    r=basic()
    r['type_distribution']={'mcq':17}
    _,err=build_configuration(r,{},[doc()])
    assert any('distribution' in e for e in err)
    r=basic();r['source_names']=['missing.pdf']
    _,err=build_configuration(r,{},[doc()])
    assert any('not included' in e for e in err)


def test_safe_arithmetic_and_mcq_validation():
    assert safe_calculation('(10+4)/2')==7
    with pytest.raises(ValueError):safe_calculation('__import__("os").system("ls")')
    for expression in ('1/0', '10**6**6', '1e308*1e308'):
        with pytest.raises(ValueError):
            safe_calculation(expression)
    cfg={'allowed_difficulties':['hard'],'cognitive_levels':['application'],'excluded_topics':[]}
    evidence=[{'id':'c1','document_id':'d1','filename':'pdf','pages':[8],'text':'The source text supports this.'}]
    item={'question_text':'Which one of these is correct?','question_type':'mcq','topic':'deadlocks','difficulty':'hard','cognitive_level':'application','options':[{'id':v,'text':v+' option'} for v in 'ABCD'],'correct_answer':'A','evaluation_rubric':[{'criterion':'Right answer','marks':1}],'max_marks':1,'explanation':'Answer A is correct.','evidence_chunk_ids':['c1']}
    good=validate_question(item,cfg,evidence,'mcq')
    assert good['source_references'][0]['pages']==[8]
    item['evidence_chunk_ids']=['fake']
    with pytest.raises(ValueError):validate_question(item,cfg,evidence,'mcq')
