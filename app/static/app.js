const root = document.getElementById('app');
const state = {
  token: sessionStorage.getItem('adaptive_token') || '', email: sessionStorage.getItem('adaptive_email') || '',
  view: 'setup', docs: [], selected: new Set(), draft: null, sessionId: null, assessment: null, feedback: null, report: null,
  prompt: '', count: '', type: '', difficulty: '', topics: '', busy: false, authMode: 'login', toast: null,
};
const prompts = [
  ['Operating Systems', 'Generate 20 medium-to-hard questions from the uploaded Operating Systems PDF. Focus on deadlocks, semaphores, process synchronization, and classical synchronization problems. Include 10 MCQs, 5 descriptive questions, and 5 scenario-based questions. Prioritize application and exam-oriented questions. Do not include content that is not covered in the PDF.'],
  ['Computer Networks', 'Generate 15 medium-difficulty questions about TCP congestion control, routing algorithms, subnetting, and network-layer protocols. Include MCQs, short answers and numerical problems from the uploaded material.'],
  ['Compiler Design', 'Generate 25 medium-to-hard questions covering lexical analysis, parsing, syntax-directed translation, intermediate code generation, and optimization. Emphasize application and analytical reasoning. Stay within the uploaded material.'],
  ['Quick revision', 'Generate 10 easy-to-medium short-answer and MCQ questions testing the most important concepts in the uploaded notes. Focus on understanding and recall.'],
];
const $ = (tag, attrs={}, ...children) => {
  const el = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs)) {
    if (v === undefined || v === null) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = String(v);
    else if (k === 'checked') el.checked = !!v;
    else if (k === 'value') el.value = v;
    else if (k === 'disabled') el.disabled = !!v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k,v);
  }
  children.flat(Infinity).forEach(c => {if (c != null) el.append(typeof c === 'string' ? document.createTextNode(c) : c)});
  return el;
};
const text = (tag, value, cls) => $(tag, {text: value, class:cls});
const btn = (title, fn, cls='') => $('button',{type:'button',class:cls,onclick:fn,disabled:state.busy},title);
const panel = (title,...children) => $('section',{class:'paper'},text('h2',title),...children);
const row = (...children) => $('div',{class:'row'},...children);
let fieldCounter=0;
const formField = (label, control) => {const id=control.id||`adaptive-field-${++fieldCounter}`;control.id=id;return $('div',{},$('label',{for:id,text:label}),control);};
const notice = (message,cls='notice') => $('div',{class:cls,text:message});
const statusTag = s => $('span',{class:`status ${s}`,text:s});
const errorMessage = e => e?.message || String(e);
async function api(path,method='GET',body=null,files=false){
  const headers = {...(state.token ? {Authorization:`Bearer ${state.token}`} : {})};
  if(body && !files) headers['Content-Type']='application/json';
  let r;
  try {r=await fetch('/api'+path,{method,headers,body:files?body:body?JSON.stringify(body):undefined});}
  catch {throw new Error('Cannot connect to the server. Please check your connection.');}
  let data = await r.json().catch(()=>({detail:'Invalid server response'}));
  if (!r.ok){if(r.status===401){state.token='';sessionStorage.removeItem('adaptive_token');}
    throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail || data));}
  return data;
}
function flash(message,bad=false){state.toast={message,bad};render();clearTimeout(state.toastTimeout);state.toastTimeout=setTimeout(()=>{state.toast=null;render();},5200)}
async function action(task){if(state.busy)return;state.busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
const loading=notice('Working…');loading.classList.add('fixed-status');loading.setAttribute('role','status');root.append(loading);
try{await task();}catch(e){flash(errorMessage(e),true);}finally{state.busy=false;render();}}
async function loadDocuments(){state.docs=await api('/documents');state.selected=new Set([...state.selected].filter(id=>state.docs.some(d=>d.id===id)));render();}
async function loadDraft(id){state.draft=await api('/drafts/'+id);state.prompt=state.draft.prompt;sessionStorage.setItem('draft_id',id);state.view='setup';render();}
async function loadAssessment(id){state.sessionId=id;sessionStorage.setItem('session_id',id);state.assessment=await api('/assessments/'+id);state.view='assessment';render();}
async function loadReport(id){state.report=await api(`/assessments/${id}/analytics`);state.sessionId=id;state.view='analytics';render();}
function header(){return $('header',{class:'site'},$('div',{class:'brand'},'Adaptive', $('span',{},'AI')),state.token?row(btn('Create assessment',()=>{state.view='setup';state.draft=null;state.feedback=null;sessionStorage.removeItem('session_id');sessionStorage.removeItem('draft_id');render()},'ghost small'),btn('My attempts',()=>action(showHistory),'ghost small'),text('span',state.email,'small'),btn('Sign out',()=>{state.token='';state.email='';sessionStorage.clear();state.draft=null;state.view='setup';render()},'ghost small')):text('span','Learn from your own course material','small'))}
function page(...children){root.replaceChildren(header(),$('main',{class:'container'},...children),$('footer',{},'AdaptiveAI · Questions and grades are grounded in your uploaded material. AI grading may be imperfect.'));}
function authPage(){const email=$('input',{type:'email',required:true,placeholder:'you@university.edu',autocomplete:'email'}),pass=$('input',{type:'password',required:true,minlength:'10',placeholder:'At least 10 characters',autocomplete:state.authMode==='login'?'current-password':'new-password'});
const auth=$('form',{onsubmit:e=>{e.preventDefault();action(async()=>{const output=await api(`/auth/${state.authMode}`,'POST',{email:email.value,password:pass.value});state.token=output.token;state.email=output.email;sessionStorage.setItem('adaptive_token',state.token);sessionStorage.setItem('adaptive_email',state.email);await loadDocuments();});}},text('h1',state.authMode==='login'?'Welcome back':'Create an account'),text('p','Access your saved study materials, assessments and results.'),formField('University email',email),formField('Password',pass),$('button',{type:'submit'},state.authMode==='login'?'Sign in':'Create account'),btn(state.authMode==='login'?'New here? Register':'Already registered? Sign in',()=>{state.authMode=state.authMode==='login'?'register':'login';render()},'ghost'));
root.replaceChildren(header(),$('main',{class:'container'},$('div',{class:'auth-panel'},auth)));}
function uploadBox(){const input=$('input',{type:'file',accept:'application/pdf,.pdf',multiple:true});input.style.display='none';const box=$('div',{class:'upload',role:'button',tabindex:'0'},$('div',{},text('strong','Drop PDF files here or click to browse'),text('p','Multiple PDFs · Text-based PDFs only · Max 20 MB per document','small')));
box.onclick=()=>input.click();box.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();input.click();}}; box.ondragover=e=>{e.preventDefault();box.style.borderColor='#17618b'};
box.ondragleave=()=>box.style.borderColor='';box.ondrop=e=>{e.preventDefault();box.style.borderColor='';uploadFiles(e.dataTransfer.files)};input.onchange=()=>uploadFiles(input.files);
return $('div',{},box,input);}
function uploadFiles(fileList){if(!fileList?.length)return;action(async()=>{const data=new FormData();for(const f of fileList)data.append('files',f);const out=await api('/documents','POST',data,true);out.forEach(doc=>state.selected.add(doc.id));await loadDocuments();flash('Documents received. Processing status updates automatically.');});}
function docList(){if(!state.docs.length)return text('p','No uploaded PDFs yet. Upload at least one text-based PDF to begin.');const out=$('div',{});
state.docs.forEach(doc=>{const check=$('input',{type:'checkbox',checked:state.selected.has(doc.id),disabled:doc.status!=='ready',onchange:()=>{check.checked?state.selected.add(doc.id):state.selected.delete(doc.id)}});check.style.width='auto';const buttons=row(check,statusTag(doc.status));if(doc.status==='failed')buttons.append(btn('Retry',()=>action(async()=>{await api(`/documents/${doc.id}/retry`,'POST');await loadDocuments()}),'subtle small'));
buttons.append(btn('Delete',()=>action(async()=>{if(!confirm('Permanently delete this unreferenced document?'))return;await api(`/documents/${doc.id}`,'DELETE');state.selected.delete(doc.id);await loadDocuments()}),'ghost small'));
out.append($('div',{class:'doc-row'},$('div',{},text('div',doc.filename,'filename'),text('div',`${(doc.size_bytes/1024/1024).toFixed(2)} MB · ${doc.page_count} pages`,'small'),doc.error?notice(doc.error,'error'):null),buttons));});return out;}
function setupPage(){const intro=$('div',{class:'intro'},$('div',{},text('div','PERSONALIZED ASSESSMENT','kicker'),text('h1','Turn course PDFs into an adaptive assessment'),text('p','Upload your notes, describe the questions you need, review the AI interpretation, and take an assessment grounded in your actual material.','subhead')),$('div',{class:'metric'},text('h3','How it works'),text('p','1. Index source PDFs. 2. Explain what you want. 3. Confirm the interpretation. 4. Review questions. 5. Start answering.')));
const upload=panel('1. Course material',uploadBox(),$('div',{style:'margin-top:15px'},docList()));
const input=$('textarea',{placeholder:'Example: Generate exactly 20 Operating Systems questions on deadlocks and synchronization…'});input.value=state.prompt;input.oninput=()=>{state.prompt=input.value};
const ex=$('div',{class:'examples'});prompts.forEach(([label,value])=>ex.append(btn(label,()=>{state.prompt=value;input.value=value},'ghost small')));
const count=$('input',{type:'number',min:'1',max:'40',placeholder:'20'});count.value=state.count;count.oninput=()=>state.count=count.value;
const type=$('select',{});[['','No preference (mixed)'],['mixed','Mixed'],['mcq','MCQ'],['short_answer','Short answer'],['descriptive','Descriptive'],['numerical','Numerical'],['scenario','Scenario']].forEach(([v,t])=>type.append($('option',{value:v,text:t})));type.value=state.type;type.onchange=()=>state.type=type.value;
const diff=$('select',{});[['','No preference (medium)'],['easy','Easy'],['medium','Medium'],['hard','Hard']].forEach(([v,t])=>diff.append($('option',{value:v,text:t})));diff.value=state.difficulty;diff.onchange=()=>state.difficulty=diff.value;
const topics=$('input',{placeholder:'Optional: deadlocks, semaphores, routing'});topics.value=state.topics;topics.oninput=()=>state.topics=topics.value;
const generateSetup=btn('Interpret my prompt →',()=>action(async()=>{
 const controls={};if(state.count)controls.question_count=Number(state.count);if(state.type)controls.question_type=state.type;if(state.difficulty)controls.difficulty=state.difficulty;if(state.topics)controls.topics=state.topics.split(',').map(s=>s.trim()).filter(Boolean);
 const draft=await api('/drafts','POST',{prompt:state.prompt,document_ids:[...state.selected],controls});await loadDraft(draft.id);
 await loadDraft((await api(`/drafts/${draft.id}/interpret`,'POST')).id);
}));
const setup=panel('2. Describe the questions you want',formField('Describe the Questions You Want',input),text('p','Try an example, then customize it. Explicit instructions take priority; conflicting selectors will be flagged.','small'),ex,$('div',{class:'sep'}),$('div',{class:'grid three'},formField('Question count',count),formField('Format',type),formField('Difficulty',diff)),formField('Topics (optional)',topics),row(generateSetup));
const sections=[intro,$('div',{class:'step-track'},...['Upload','Interpret','Review','Generate','Assess','Analyze'].map((v,i)=>text('span',v,'step '+(state.view==='setup' && i<2?'active':'')))),upload,setup];
if(state.draft)sections.push(draftSection());page(...sections);
}
function draftSection(){const d=state.draft;const sec=panel('3. Interpretation and review',row(text('span',`Draft: ${d.id.slice(0,8)}…`,'small'),statusTag(d.state)),d.error?notice(d.error,'error'):null);
if(d.conflicts?.length)sec.append(notice('Resolve these conflicts before approving:','warning'),...d.conflicts.map(x=>notice(x,'warning')));
if((d.state==='created'||(d.state==='failed'&&!d.configuration))&&d.document_ids?.length)sec.append(row(btn(d.state==='failed'?'Retry interpretation':'Interpret this saved draft',()=>action(async()=>{await loadDraft((await api(`/drafts/${d.id}/interpret`,'POST')).id)}),'subtle')));
const plan=d.configuration?.focus_plan;
if(plan)sec.append(focusPlanTable(plan.areas,`Built from ${plan.based_on.length} attempt(s) on this material. More questions go to the concepts you scored lowest on; difficulty steps down where you struggled and up where you did well. Questions you already saw are not repeated.`));
if(d.configuration && d.state!=='generating' && (!plan || d.state==='failed'))sec.append(configurationEditor(d));
if(d.document_ids?.length && d.state!=='generated' && d.state!=='generating'){
 const selection=$('div',{class:'row'});d.document_ids.forEach(id=>{const file=state.docs.find(x=>x.id===id);selection.append(btn(`Remove ${file?.filename||id} from draft`,()=>action(async()=>{await loadDraft((await api(`/drafts/${d.id}/documents/${id}`,'DELETE')).id)}),'ghost small'));});
 sec.append(text('p','Draft sources · Removing a PDF here does not delete the stored file.','small'),selection);}

if(d.state==='generating')sec.append(notice('Generating and verifying the question set. This may take several minutes; you can leave this page and return to the draft.'));
if(['approved','generated','failed'].includes(d.state)&&d.configuration)sec.append(row(btn(d.state==='generated'?'Regenerate all questions':'Generate questions',()=>action(async()=>{if(d.state==='generated'&&!confirm('Regenerate the draft? Existing assessment sessions will not change.'))return;await loadDraft((await api(`/drafts/${d.id}/generate`,'POST')).id)}))));
if (d.questions?.length) {
  const startButton = btn('Start Assessment →', () => action(async () => {
    let key = sessionStorage.getItem('startkey_' + d.id);
    if (!key) { key = crypto.randomUUID(); sessionStorage.setItem('startkey_' + d.id, key); }
    const started = await api(`/drafts/${d.id}/start`, 'POST', {idempotency_key:key});
    state.feedback = null;
    await loadAssessment(started.session_id);
  }));
  if (d.state !== 'generated') startButton.disabled = true;
  sec.append($('div',{class:'sep'}), text('h2',`Question preview (${d.questions.length})`),
    text('p','Answer keys and rubrics are hidden in student assessment mode.','small'),
    ...d.questions.map(q=>questionPreview(q,d)), row(startButton));
}
return sec;}
function configurationEditor(d){const config=structuredClone(d.configuration), n=$('input',{type:'number',min:1,max:40,value:config.question_count});n.oninput=()=>{config.question_count=Number(n.value)};
const topic=$('input',{value:(config.included_topics||[]).join(', ')});topic.oninput=()=>config.included_topics=topic.value.split(',').map(x=>x.trim()).filter(Boolean);
const excl=$('input',{value:(config.excluded_topics||[]).join(', ')});excl.oninput=()=>config.excluded_topics=excl.value.split(',').map(x=>x.trim()).filter(Boolean);
const distro=$('div',{class:'grid three'});['mcq','short_answer','descriptive','numerical','scenario'].forEach(t=>{const input=$('input',{type:'number',min:0,max:40,value:config.type_distribution[t]||0});input.oninput=()=>{config.type_distribution[t]=Number(input.value)};distro.append(formField(t.replace('_',' '),input));});
const levels=$('div',{class:'row'});['easy','medium','hard'].forEach(level=>{const cb=$('input',{type:'checkbox',checked:config.allowed_difficulties.includes(level)});cb.style.width='auto';cb.onchange=()=>config.allowed_difficulties=cb.checked?[...new Set([...config.allowed_difficulties,level])]:config.allowed_difficulties.filter(x=>x!==level);levels.append($('label',{class:'row'},cb,level));});
const fixed=$('div',{class:'grid three'});['easy','medium','hard'].forEach(level=>{const inp=$('input',{type:'number',min:0,value:config.difficulty_distribution[level]||0});inp.oninput=()=>{config.difficulty_distribution[level]=Number(inp.value)};fixed.append(formField(level,inp));});
const details=$('details',{},$('summary',{},'Special requirements and assumptions'),text('pre',JSON.stringify({requirements:config.requirements,cognitive_levels:config.cognitive_levels,assumptions:config.assumptions,subject:config.subject,documents:config.document_ids},null,2),'small'));
return $('div',{},text('p','This is the interpreted assessment configuration. Edit any misunderstood values and approve explicitly. Prompt and approved configuration are both saved.'),$('div',{class:'grid'},formField('Total questions',n),formField('Included topics',topic),formField('Excluded topics',excl)),text('h3','Question formats (must add up to total)'),distro,text('h3','Permitted difficulties'),levels,text('h3','Fixed difficulty counts (optional; all zero means adaptive range)'),fixed,details,row(btn('Approve configuration',()=>action(async()=>{
 config.type_distribution=Object.fromEntries(Object.entries(config.type_distribution).filter(([k,v])=>v>0));config.difficulty_distribution=Object.fromEntries(Object.entries(config.difficulty_distribution).filter(([k,v])=>v>0));
 if(Object.keys(config.difficulty_distribution).length===0)config.difficulty_distribution={};
 await loadDraft((await api(`/drafts/${d.id}/approve`,'POST',{configuration:config})).id);flash('Configuration approved');
}),'subtle')));
}
const pct=v=>v==null?'—':`${v}%`;
const delta=v=>v==null?text('span','—'):text('span',`${v>0?'+':''}${v} pts`,`delta ${v>0?'up':v<0?'down':''}`);
const areaStatus=s=>$('span',{class:`status ${s}`,text:s.replace('_',' ')});
const tableHead=cols=>$('thead',{},$('tr',{},...cols.map(t=>text('th',t))));
function focusPlanTable(areas,intro){return $('div',{class:'callout'},text('h3','Weak-area plan'),text('p',intro,'small'),
  $('div',{class:'tablewrap'},$('table',{},tableHead(['Concept area','Score so far','Questions','Difficulty','Why']),
  $('tbody',{},...(areas||[]).map(a=>$('tr',{},text('td',a.name),text('td',`${Math.round(a.mastery*100)}%`),text('td',String(a.questions)),text('td',a.questions?a.difficulty:'—'),$('td',{},areaStatus(a.status),' ',a.reason)))))));}
function comparisonPanel(c){const o=c.overall;
  const cards=$('div',{class:'grid three'},$('div',{class:'metric'},text('span','Previous attempt'),text('strong',pct(o.before?.percent))),$('div',{class:'metric'},text('span','This attempt'),text('strong',pct(o.now?.percent))),$('div',{class:'metric'},text('span','Change'),$('strong',{},delta(o.change))));
  const summary=c.summary.length?$('div',{class:'callout'},text('strong','What changed'),$('ul',{},...c.summary.map(s=>text('li',s)))):null;
  const table=$('div',{class:'tablewrap'},$('table',{},tableHead(['Concept area','Previous','This attempt','Change','Result']),$('tbody',{},...c.areas.map(a=>$('tr',{},text('td',a.area),
    $('td',{},pct(a.before?.percent),a.before?text('div',`${a.before.difficulty}${a.before_source==='an earlier attempt'?' · earlier attempt':''}`,'small'):null),
    $('td',{},pct(a.now?.percent),a.now?text('div',`${a.now.count} q · ${a.now.difficulty}`,'small'):null),
    $('td',{},delta(a.change),a.difficulty_note?text('div',a.difficulty_note,'small'):null),$('td',{},areaStatus(a.status)))))));
  const work=c.needs_work.length?$('div',{},text('h3','What still needs work'),...c.needs_work.map(n=>$('div',{class:'callout'},text('strong',`${n.area} — ${n.percent}%`),n.missed.length?$('ul',{},...n.missed.map(m=>text('li',m))):null))):null;
  const trend=$('div',{},text('h3','Your progress on this material'),$('div',{class:'trend'},...c.trend.map((t,i)=>$('div',{class:'metric'+(t.current?' current':'')},text('span',`Attempt ${i+1}${t.focused?' · weak-area':''}`),text('strong',pct(t.percent)),text('span',new Date(t.at).toLocaleDateString())))));
  return panel('Compared with your previous attempt',text('p',`Previous attempt on this material: ${new Date(c.previous_at).toLocaleString()}`,'small'),cards,summary,$('div',{class:'sep'}),table,work,$('div',{class:'sep'}),trend,notice(c.next_step));}
function practicePanel(r){const count=$('input',{type:'number',min:1,max:40,placeholder:String(r.overall.total_questions)});
  const field=formField('Questions (optional)',count);field.style.maxWidth='190px';
  return panel('Practice your weak areas',text('p','Builds your next attempt from this and your earlier attempts on the same PDFs: more questions on the concepts you missed, easier where you struggled, harder where you did well. When you finish it, you get a side-by-side comparison with this attempt.'),
    $('div',{class:'row',style:'align-items:end'},field,btn('Practice my weak areas →',()=>action(async()=>{const out=await api(`/assessments/${state.sessionId}/focus`,'POST',count.value?{question_count:Number(count.value)}:{});
      state.report=null;await loadDocuments();await loadDraft(out.draft_id);flash('Building your weak-area attempt. Questions appear here when they are ready.');
      document.querySelector('.callout')?.scrollIntoView({block:'start'});}))));}
function chips(q){return $('div',{class:'tags'},...['question_type','topic','difficulty','cognitive_level'].map(k=>text('span',q[k],'tag')));}
async function openSource(ref){
  const pdfWindow=window.open('', '_blank');
  if(!pdfWindow)throw new Error('Please allow popups to view the source PDF.');
  try{
    pdfWindow.document.title='Loading source PDF…';
    const response=await fetch(`/api/documents/${ref.document_id}/file`,{headers:{Authorization:`Bearer ${state.token}`}});
    if(!response.ok)throw new Error('Could not access this private source document.');
    const blob=await response.blob();
    const url=URL.createObjectURL(blob);
    pdfWindow.location.href=url+`#page=${ref.pages?.[0]||1}`;
    setTimeout(()=>URL.revokeObjectURL(url),5*60*1000);
  }catch(e){pdfWindow.close();flash(errorMessage(e),true);}
}
function sources(q){const output=$('div',{class:'row'});
  (q.source_references||[]).forEach(ref=>output.append(btn(`Open ${ref.filename} · p. ${ref.pages?.join(', ')}`,()=>openSource(ref),'ghost small')));
  return output;}

function questionPreview(q,d){return $('article',{class:'question-card'},$('header',{},text('strong',`Question ${q.slot}`),chips(q)),text('div',q.question_text,'question-text'),q.options?.length?$ ('ol',{},...q.options.map(opt=>text('li',opt.text))):null,sources(q),row(btn('Regenerate this question',()=>action(async()=>{await loadDraft((await api(`/drafts/${d.id}/questions/${q.id}/regenerate`,'POST')).id)}),'ghost small')));}
function assessmentPage(){const a=state.assessment;if(!a){page(notice('No assessment loaded.'));return}const p=a.progress;const out=[text('div','ADAPTIVE ASSESSMENT','kicker'),text('h1',`Question ${Math.min(p.answered+1,p.total)} of ${p.total}`),text('p',`${p.answered} evaluated · ${p.pending} pending · ${p.status}`),$('div',{class:'progress'},$('div',{style:`width:${100*p.answered/p.total}%`}))];
if(state.feedback){const f=state.feedback;out.push(panel('Evaluation feedback',text('div',`${f.marks} / ${f.max_marks} marks`,'score'),text('p',f.feedback),text('p',`Reference answer: ${f.correct_answer}`),text('p',f.explanation),row(btn(p.status==='completed'?'View final analytics →':'Next question →',()=>action(async()=>{state.feedback=null;await loadAssessment(state.sessionId);if(state.assessment.progress.status==='completed')await loadReport(state.sessionId)})))));}
else if(a.pending){out.push(panel('Evaluation pending',notice('Your answer is saved, but grading could not complete. Retry the exact answer to prevent duplicate submissions.','warning'),row(btn('Retry evaluation',()=>action(async()=>{const result=await api(`/assessments/${state.sessionId}/answer`,'POST',{question_revision_id:a.pending.question_revision_id,answer:a.pending.answer});if(result.state==='graded'){state.feedback=result;await loadAssessment(state.sessionId);}else flash(result.error,true)})))));}
else if(a.question){const q=a.question;const input=$('textarea',{placeholder:'Write your answer here…'});let selection='';let answerSection;
if(q.question_type==='mcq') {answerSection=$('fieldset',{},$('legend',{},'Choose one answer'),...(q.options||[]).map(opt=>{const r=$('input',{type:'radio',name:'answer',value:opt.id});r.onchange=()=>selection=opt.id;return $('label',{class:'option'},r,`${opt.id}. ${opt.text}`)}));}
else {if(q.question_type==='numerical')input.placeholder='Enter a numerical answer (e.g. 42.5)';answerSection=formField('Your answer',input);}
const submit=btn('Submit answer',()=>action(async()=>{const answer=q.question_type==='mcq'?selection:input.value.trim();if(!answer)throw new Error('Please enter or select an answer.');const result=await api(`/assessments/${state.sessionId}/answer`,'POST',{question_revision_id:q.id,answer});if(result.state==='graded'){state.feedback=result;await loadAssessment(state.sessionId);}else{flash(result.error,true);await loadAssessment(state.sessionId)}}));
out.push(panel('',chips(q),$('div',{class:'sep'}),text('div',q.question_text,'question-text'),sources(q),$('div',{class:'sep'}),answerSection,row(submit)));}
else {out.push(panel('Assessment complete',text('p','All questions have been evaluated.'),btn('View final analytics',()=>action(()=>loadReport(state.sessionId)))));}
page(...out);}
function analyticsPage(){const r=state.report;if(!r){page(notice('No results.'));return;}const o=r.overall;const cards=$('div',{class:'grid three'},...[
 ['Score',o.percent==null?'—':`${o.percent}%`],['Marks',`${o.marks} / ${o.max_marks}`],['Questions',`${o.attempted} / ${o.total_questions}`],['Objective accuracy',o.objective_accuracy==null?'N/A':`${o.objective_accuracy}%`],['Descriptive marks',o.descriptive_marks],['Unanswered',o.unanswered],
].map(([name,val])=>$('div',{class:'metric'},text('span',name),text('strong',val))));
const out=[text('div',r.focus_plan?'WEAK-AREA ATTEMPT RESULTS':'ASSESSMENT RESULTS','kicker'),text('h1','Your performance overview'),text('p',`Assessment status: ${r.status}`),cards];
if(r.comparison)out.push(comparisonPanel(r.comparison));
if(r.overall.attempted)out.push(practicePanel(r));
if(r.focus_plan)out.push(panel('What this attempt focused on',focusPlanTable(r.focus_plan,'The plan this attempt was built from.')));
function performanceTable(title,obj){const entries=Object.entries(obj||{});return panel(title,entries.length?$('div',{class:'tablewrap'},$('table',{},$('thead',{},$('tr',{},...['Category','Attempted','Marks','Score','Objective accuracy','Interpretation'].map(t=>text('th',t)))),$('tbody',{},...entries.map(([name,val])=>$('tr',{},...[name,val.attempted,`${val.marks}/${val.max_marks}`,val.score_percent==null?'—':`${val.score_percent}%`,val.objective_accuracy==null?'N/A':`${val.objective_accuracy}%`,val.classification].map(t=>text('td',t))))))):text('p','No graded data yet.'));}
out.push(performanceTable('Topic-wise performance',r.topics),performanceTable('Difficulty-wise performance',r.difficulties));
out.push(panel('Questions and explanations',...(r.results||[]).map(q=>$('details',{},$('summary',{},`${q.slot}. ${q.question.slice(0,120)} · ${q.marks??'pending'}/${q.max_marks}`),text('p',`Your answer: ${q.answer}`),text('p',`Reference: ${q.reference_answer}`),text('p',q.feedback||'Evaluation pending'),text('p',q.explanation),text('p',`Rubric: ${JSON.stringify(q.rubric)}`),text('div',q.sources.map(s=>`${s.filename} p. ${s.pages.join(', ')}`).join(' | '),'source'),sources({source_references:q.sources})))));
out.push(panel('Revision suggestions',r.recommendations?.length?$('div',{class:'list'},...r.recommendations.map(a=>$('div',{},text('strong',a.topic),text('p',a.suggestion),text('p',a.sources.map(s=>`${s.filename} p. ${s.pages.join(', ')}`).join(' | '),'source'),sources({source_references:a.sources})))):text('p','No low-scoring graded responses identified.')));
out.push(panel('Adaptive decision history',r.adaptations?.length?$('div',{class:'list'},...r.adaptations.map(a=>$('div',{},statusTag(a.decision),text('span',' '+a.reason)))):text('p','No difficulty decisions recorded.')));
out.push(row(btn('Repeat using this prompt',()=>action(async()=>{const d=await api(`/assessments/${state.sessionId}/repeat`,'POST');await loadDraft(d.draft_id);state.prompt=state.draft.prompt;await loadDocuments()})),btn('My attempts',()=>action(showHistory),'ghost')));page(...out);}
async function showHistory(){const list=await api('/assessments');state.view='history';state.history=list;render();}
function historyPage(){page(text('div','MY LEARNING','kicker'),text('h1','Assessment history'),panel('Saved attempts',...(state.history?.length?state.history.map(s=>$('div',{class:'doc-row'},$('div',{},text('strong',new Date(s.created_at).toLocaleString()),row(statusTag(s.status),s.focused?$('span',{class:'status focused',text:'weak-area'}):null,text('span',`${s.total} questions${s.percent!=null?` · ${s.percent}%`:''}${s.subject?` · ${s.subject}`:''}`,'small'))),row(btn('Resume',()=>action(()=>loadAssessment(s.id)),'ghost small'),btn('Analytics',()=>action(()=>loadReport(s.id)),'subtle small')))):[text('p','No attempts yet. Start a new assessment to create your first record.')])));}
function render(){if(!state.token){authPage();}else if(state.view==='assessment')assessmentPage();else if(state.view==='analytics')analyticsPage();else if(state.view==='history')historyPage();else setupPage();
 if(state.toast){const n=notice(state.toast.message,state.toast.bad?'error':'notice');n.classList.add('fixed-status');n.setAttribute('role','alert');root.append(n);}}
render();
if(state.token){loadDocuments().then(async()=>{const sid=sessionStorage.getItem('session_id'), did=sessionStorage.getItem('draft_id');
 try{if(sid)await loadAssessment(sid);else if(did)await loadDraft(did);}catch{sessionStorage.removeItem('session_id');sessionStorage.removeItem('draft_id');}
}).catch(e=>{flash(errorMessage(e),true);});}
setInterval(()=>{if(state.token && state.view==='setup' && state.docs.some(d=>!['ready','failed'].includes(d.status)))loadDocuments().catch(()=>{});
  if(state.token && state.view==='setup' && state.draft?.state==='generating')loadDraft(state.draft.id).catch(()=>{});},2800);
