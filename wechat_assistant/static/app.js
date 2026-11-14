const $ = s => document.querySelector(s);
let state, lastJob = '', lastAnswer = '', lastMedia = '', settingsLoaded = false, lastProjectView = '';
let previewURL, showArchived = false;
const token = $('meta[name="local-token"]').content;
const names = {task:'待办',decision:'决定',question:'待确认',risk:'阻碍',open:'进行中',reported_done:'聊天称已完成 · 待你确认',done:'已完成',dismissed:'已忽略',candidate:'待确认项目',tracked:'跟踪中',ignored:'已忽略'};
function el(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
function notice(text){$('#notice').textContent=text;clearTimeout(notice.timer);notice.timer=setTimeout(()=>$('#notice').textContent='',6000);}
async function api(path,body){const r=await fetch('/api/'+path,{method:body?'POST':'GET',headers:{'X-Local-Token':token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const v=await r.json();if(!r.ok)throw Error(v.error);return v;}
function button(text,fn,cls='small'){const b=el('button',text,cls);b.onclick=()=>Promise.resolve().then(fn).catch(e=>notice(e.message));return b;}
function date(ts){return ts?new Date((ts+8*3600)*1000).toISOString().slice(0,19).replace('T',' '):'尚未运行';}
function empty(parent,text){parent.append(el('div',text,'empty'));}
async function mutate(route,body){await api(route,body);await refresh();}
function handoff(r){showDialog('在当前客户端更新');const b=$('#dialog-body'),text=el('textarea');text.value=r.prompt;text.readOnly=true;b.append(el('p',r.note),text,button('复制到对话',async()=>{await navigator.clipboard.writeText(r.prompt);notice('已复制，请粘贴到 WorkBuddy / Codex 对话。');}));}
async function job(action,body={}){const r=await api('job',{action,...body});if(r.handoff){handoff(r);return;}notice('已加入后台任务，结果会自动显示。');}
function refs(ids){
 const values=[...new Set(ids||[])],box=el('details',undefined,'source-details');
 if(!values.length){box.hidden=true;return box;}
 box.append(el('summary',`查看出处 · ${values.length}`));const row=el('div',undefined,'row');
 for(const id of values)row.append(button('消息 #'+id,async()=>{const messages=await api('message?id='+id);showDialog('原始消息');for(const m of messages)$('#dialog-body').append(message(m));}));
 box.append(row);return box;
}
function message(m){const c=el('div',undefined,'card');c.append(el('small',`${m.chat_name} · ${m.sender} · ${date(m.ts)} · #${m.id}`),el('p',m.text,'text'));
 if(m.attachment){c.append(el('small','附件：'+mediaStatus(m.attachment.status)),button('读取'+m.kind,()=>job('read_media',{message_id:m.id})));if(m.attachment.text&&m.attachment.status!=='not_read')c.append(button('查看已读内容',async()=>showMedia(await api('media?id='+m.id))));}
 c.append(button('纠正这条消息的理解',()=>feedbackDialog(null,m)));return c;}
function mediaStatus(s){return ({prepared:'图片已准备，等待当前客户端识读',not_read:'尚未读取',ready:'已读取',partial:'部分已读',missing:'本机缺少附件',error:'读取失败',listed:'已列目录，成员正文未读'})[s]||s;}
async function showMedia(r){if(!r)return;showDialog('附件内容 · #'+r.message_id);const b=$('#dialog-body'),d=r.detail||{};
 b.append(el('p',mediaStatus(r.status)+(d.filename?' · '+d.filename:''),'muted'));if(d.quality==='thumbnail')b.append(el('p','当前只有缩略图，文字可能不完整。','muted'));if(d.method){const method=el('details');method.append(el('summary','读取方式'),el('p',d.method,'muted'));b.append(method);}
 b.append(el('div',r.text||d.error||'没有可读正文。','text'));
 if(r.path&&['图片','语音'].includes(r.kind)){const response=await fetch('/api/media-file?id='+r.message_id,{headers:{'X-Local-Token':token}});if(response.ok){if(previewURL)URL.revokeObjectURL(previewURL);previewURL=URL.createObjectURL(await response.blob());const preview=el(r.kind==='图片'?'img':'audio');preview.src=previewURL;preview.style.maxWidth='100%';if(r.kind==='语音')preview.controls=true;b.prepend(preview);}}
 const next=d.next_start??r.next_start;if(next!==undefined&&next!==null)b.append(button('继续读取后面的内容',()=>job('read_media',{message_id:r.message_id,start:next,member:r.member})));
 if(d.archive_members_only){const input=el('input');input.placeholder='输入压缩包中的完整成员名';b.append(input,button('读取这个成员',()=>job('read_media',{message_id:r.message_id,member:input.value})));}
 b.append(button('重新读取',()=>job('read_media',{message_id:r.message_id,start:d.start||0,force:true,member:r.member})));
}
function feedbackDialog(p=null,m=null){showDialog(p?'补充或纠正 · '+p.name:'我的反馈');const body=$('#dialog-body');
 const scope=el('select');const all=el('option','全部工作 / 长期要求');all.value='';scope.append(all);for(const project of state.projects){const option=el('option',project.name);option.value=project.id;scope.append(option);}scope.value=p?.id||'';
 const text=el('textarea');text.placeholder='补充线下进展，或纠正不准确的理解';text.setAttribute('aria-label','反馈内容');scope.setAttribute('aria-label','反馈所属项目');chat.setAttribute('aria-label','反馈所属聊天');
 const chat=el('select');const none=el('option','不限聊天');none.value='';chat.append(none);for(const c of state.chats){const o=el('option',c.name);o.value=c.id;chat.append(o);}chat.value=m?.chat_id||'';
 body.append(scope,chat,text,button('保存反馈',async()=>{await mutate('feedback',{text:text.value,project_id:Number(scope.value)||null,chat_id:chat.value||null,message_id:m?.id||null});notice('已记住，下一次在客户端查询时会带上你的反馈。');feedbackDialog(p,m);},'primary'));
 body.append(el('h3','有效反馈'));for(const f of state.feedback.filter(f=>!p||!f.project_id||f.project_id===p.id)){const row=el('div',undefined,'item');const label=state.projects.find(p=>p.id===f.project_id)?.name||'全部工作';row.append(el('small',label+' · '+date(f.created)+(f.message_id?' · 消息 #'+f.message_id:'')),el('p',f.text,'text'),button('撤销这条反馈',async()=>{await mutate('feedback',{revoke:f.id});feedbackDialog(p,m);}));body.append(row);}
}
async function showAttachments(offset=0){const data=await api('attachments?project='+$('#search-project').value+'&offset='+offset);if(!offset)$('#answers').replaceChildren();for(const m of data.messages)$('#answers').append(message(m));if(data.next_offset!==null)$('#answers').append(button('继续查看附件',e=>showAttachments(data.next_offset)));if(!data.messages.length)empty($('#answers'),'此范围暂无可列出的附件。');}

function showDialog(title){$('#dialog-title').textContent=title;$('#dialog-body').replaceChildren();if(!$('#dialog').open)$('#dialog').showModal();}
function editProject(p){showDialog('项目资料 · '+p.name);const body=$('#dialog-body');const form=el('form');
 for(const [key,title,value] of [['name','项目名称',p.name],['notes','项目说明与分工',p.notes],['paths','本地文档或代码目录（每行一个路径）',p.paths.join('\n')]]){const l=el('label',title);const input=el(key==='name'?'input':'textarea');input.name=key;input.value=value;l.append(input);form.append(l);}
 const paths=form.elements.namedItem('paths').closest('label'),advanced=el('details');advanced.append(el('summary','补充本地文件'),el('p','所填文件会作为独立后台分析的资料。','muted'),paths);form.append(advanced);
 const save=el('button','保存资料','primary');form.append(save);form.onsubmit=async e=>{e.preventDefault();try{const f=new FormData(form);await mutate('project',{id:p.id,name:f.get('name'),notes:f.get('notes'),paths:f.get('paths').split('\n').map(x=>x.trim()).filter(Boolean)});$('#dialog').close();notice('已保存。请在当前客户端按需更新项目资料。');}catch(err){notice(err.message);}};body.append(form);
 body.append(el('h3','关联聊天'));for(const c of p.chats){const row=el('div',undefined,'row');row.append(el('span',(c.name||c.chat_id)+(c.excluded?'（已排除）':'')),button(c.excluded?'重新关联':'排除此聊天',async()=>{await mutate('chat',{project_id:p.id,chat_id:c.chat_id,excluded:!c.excluded});editProject(state.projects.find(x=>x.id===p.id));}));body.append(row);}
 const select=el('select');for(const c of state.chats){const option=el('option',c.name);option.value=c.id;select.append(option);}body.append(select,button('手动关联',async()=>{if(select.value){await mutate('chat',{project_id:p.id,chat_id:select.value,excluded:false});editProject(state.projects.find(x=>x.id===p.id));}}));
}
function projectEntries(p){return Object.values(p.brief?.entries||{}).filter(e=>!e.archived);}
function entrySources(entry){
 const sources=refs([...(entry.evidence_ids||[]),...(entry.attachments||[]).map(a=>a.message_id)]);
 for(const a of entry.attachments||[]){const coverage=a.coverage||{},parts=coverage.indices?.map(i=>i+1).join('、');sources.append(el('p',`附件 #${a.message_id}${coverage.member?' · '+coverage.member:''}${parts?' · 第 '+parts+' 部分':''}`,'muted'));}
 return sources;
}
function projectDetail(p){
 showDialog(p.name);const body=$('#dialog-body'),client=state.settings.provider==='client';
 if(client&&p.brief){
  body.append(el('p','保存于 '+date(p.brief.created)+' · 助手整理','muted'));
  const entries=projectEntries(p),sections={status:'进展',next_step:'下一步',question:'待确认',decision:'已记录的决定',background:'背景'};
  for(const [key,title] of Object.entries(sections)){
   const rows=entries.filter(e=>e.section===key);if(!rows.length)continue;
   const group=el('section',undefined,'brief-section');group.append(el('h3',title));
   for(const entry of rows){const row=el('div',undefined,'brief-entry');if(entry.basis==='inferred')row.append(el('span','推测','badge warning'));else if(entry.basis==='open'&&key!=='question')row.append(el('span','待核实','badge warning'));row.append(el('p',entry.text,'text'),entrySources(entry));group.append(row);}body.append(group);
  }
  const scope=el('details',undefined,'brief-meta');scope.append(el('summary','阅读范围与版本'),el('p',p.brief.scope_note||'未记录阅读范围。','text'),el('p',`版本 ${p.brief.version}。资料截至保存时，后续变化需在对话中核对。`,'muted'));body.append(scope);
 }else if(client){body.append(el('p','还没有整理过这个项目。可在 WorkBuddy / Codex 中查询后保存资料。','muted'));}
 if(p.summary&&(!client||(p.evidence||[]).length||p.items.length)){const legacy=client?el('details'):el('section');legacy.append(el(client?'summary':'h3',client?'此前的后台摘要':'进展'),el('p',p.summary,'text'),refs(p.evidence));body.append(legacy);}
 const chats=(p.overview?.conversations||[]).filter(chat=>!p.chats.some(c=>c.chat_id===chat.chat_id&&c.excluded));
 if(chats.length){const threads=el('details');threads.append(el('summary',client?'此前的聊天概览':'相关聊天'));for(const chat of chats){const row=el('div',undefined,'item'),linked=p.chats.find(c=>c.chat_id===chat.chat_id);row.append(el('h3',linked?.name||'相关会话'),el('p',chat.summary,'text'),refs(chat.evidence));threads.append(row);}body.append(threads);}
 if(p.items.length){const items=el('details');items.append(el('summary',client?'此前记录的事项':`详细事项 · ${p.items.length}`));for(const i of p.items){const row=el('div',undefined,'item');row.append(el('span',names[i.state]||i.state,'badge'),el('h3',i.title));const meta=[i.owner?'负责人：'+i.owner:'',i.due?'截止：'+i.due:''].filter(Boolean);if(meta.length)row.append(el('p',meta.join(' · '),'muted'));if(i.reason)row.append(el('p',i.reason,'text'));row.append(refs(i.evidence));const actions=el('div',undefined,'row');const update=async stateValue=>{await mutate('item',{id:i.id,state:stateValue});projectDetail(state.projects.find(x=>x.id===p.id));};if(!['done','dismissed'].includes(i.state))actions.append(button('确认完成',()=>update('done')),button('忽略',()=>update('dismissed')));else actions.append(button('重新打开',()=>update('open')));row.append(actions);items.append(row);}body.append(items);}
 const actions=el('div',undefined,'row project-options');actions.append(button('补充或纠正',()=>feedbackDialog(p),'primary'),button('项目设置',()=>editProject(p)));if(p.status!=='tracked')actions.append(button('恢复跟踪',async()=>{await mutate('project',{id:p.id,status:'tracked'});$('#dialog').close();}));else actions.append(button('忽略项目',async()=>{await mutate('project',{id:p.id,status:'ignored'});$('#dialog').close();}));body.append(actions);
}
function cardBlock(title,entries,cls=''){
 if(!entries.length)return null;
 const block=el('div',undefined,'card-block '+cls);block.append(el('h3',title));const list=el('ul');
 for(const entry of entries.slice(0,2))list.append(el('li',(entry.basis==='inferred'?'推测：':'')+entry.text));block.append(list);return block;
}
function renderProjects(){
 const target=$('#project-list'),view=JSON.stringify([state.settings.provider,state.projects,showArchived]);if(view===lastProjectView)return;lastProjectView=view;target.replaceChildren();
 const active=state.projects.filter(p=>p.status!=='ignored'),archived=state.projects.filter(p=>p.status==='ignored'),client=state.settings.provider==='client';
 $('#project-count').textContent=active.length?`${active.length} 个项目`:'你的项目资料，集中在这里';
 $('#toggle-archived').hidden=!archived.length&&!showArchived;$('#toggle-archived').textContent=showArchived?'返回正在跟踪':`已忽略项目 · ${archived.length}`;
 $('#project-section-title').textContent=showArchived?'已忽略':'正在跟踪';
 const projects=showArchived?archived:active;
 if(!projects.length){const box=el('div',undefined,'empty');box.append(el('h2',showArchived?'没有已忽略的项目':'从一个项目开始'),el('p',showArchived?'正在跟踪的项目仍保留在首页。':'添加项目后，在对话中查询并保存资料。'));if(!showArchived)box.append(button('添加项目',()=>$('#add-project').click(),'primary'));target.append(box);return;}
 for(const p of projects){
  const entries=client?projectEntries(p):[],main=entries.find(e=>e.section==='status'&&e.basis!=='open')||entries.find(e=>['decision','background'].includes(e.section)&&e.basis!=='open');
  const next=client?entries.filter(e=>e.section==='next_step'&&e.basis!=='open'):(p.overview?.next_steps||p.items.filter(i=>i.state==='open'&&i.kind==='task').map(i=>i.title)).map(text=>({text}));
  const questions=client?entries.filter(e=>e.section==='question'||e.basis==='open'):(p.overview?.uncertainties||p.items.filter(i=>i.state==='open'&&['question','risk'].includes(i.kind)).map(i=>i.title)).map(text=>({text}));
  const card=el('article',undefined,'card project-card'),head=el('div',undefined,'card-heading'),title=el('div');title.append(el('h2',p.name));
  if(client&&p.brief)title.append(el('small','保存于 '+date(p.brief.created).slice(5,16)));else if(!client)title.append(el('small','后台整理'));else title.append(el('small','尚未整理'));
  head.append(title);if(p.status==='ignored')head.append(el('span','已忽略','badge neutral'));else if(questions.length)head.append(el('span',questions.length+' 项待确认','badge warning'));card.append(head);
  const summary=client?(main?(main.basis==='inferred'?'推测：':'')+main.text:entries.length?'已保存项目资料，查看详情了解完整内容。':'还没有项目资料。查询后保存的进展会显示在这里。'):(p.summary||'尚无进展记录。');
  card.append(el('p',summary,'text progress'));
  for(const block of [cardBlock('下一步',next),cardBlock('待确认',questions,'questions')])if(block)card.append(block);
  const actions=el('div',undefined,'card-actions');actions.append(button('查看资料',()=>projectDetail(p),'outline'),button('补充或纠正',()=>feedbackDialog(p),'ghost'));card.append(actions);target.append(card);
 }
}
function selectTab(tab){
 for(const section of document.querySelectorAll('main>section'))section.hidden=section.id!==tab;
 for(const b of document.querySelectorAll('nav button')){b.classList.toggle('active',b.dataset.tab===tab);if(b.dataset.tab===tab)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');}
}
function renderStatus(){
 const s=state.status,client=state.settings.provider==='client',target=$('#status');target.replaceChildren();$('#analyze').textContent=client?'在对话中更新':'更新资料';$('#ask').textContent=client?'在对话中提问':'提问';
 const errors=[s.sync,...(client?[]:[s.analysis])].filter(p=>p?.ok===false),running=s.worker?.running;
 const statusText=errors.length?'同步或整理需要检查':running?({sync:'正在同步',analyze:'正在整理',ask:'正在回答'}[s.worker.action]||'正在处理'):s.sync?.at?'最近同步 '+date(s.sync.at).slice(5,16):'尚未同步';
 target.append(button(statusText,()=>selectTab('settings'),'status-button'+(errors.length?' problem':'')));
 $('#mode').textContent=state.demo?'演示资料':'本机资料';
 $('#sync-summary').textContent=`已收录 ${s.total.toLocaleString()} 条消息`+(s.sync?.at?' · '+date(s.sync.at):'');
 const runtime=$('#runtime');runtime.textContent='同步：'+JSON.stringify(s.sync,null,2)+'\n独立分析'+(client?'（历史状态）':'')+'：'+JSON.stringify(s.analysis,null,2)+'\n提醒：'+JSON.stringify(s.delivery,null,2);
 const old=$('#sync-error');if(old)old.remove();if(errors.length){const error=el('div',errors.map(p=>p.error).join('\n'),'error');error.id='sync-error';$('#sync-summary').after(error);}
}
function renderNotifications(){const target=$('#notification-list');target.replaceChildren();if(!state.notifications.length)empty(target,'暂无提醒。没有变化时不会打扰。');for(const n of state.notifications){const c=el('article',undefined,'card');c.append(el('small',date(n.created)+(n.delivered?' · 已提交系统通知':'')),el('h2',n.title),el('div',n.body,'text'));if(n.error)c.append(el('p',n.error,'error'));target.append(c);}}
function renderProviderFields(){const f=$('#settings-form'),provider=f.elements.namedItem('provider').value;for(const key of ['codex_command','model','api_base','api_key_env','analysis_seconds','batch_size']){const field=f.elements.namedItem(key);field.closest('label').hidden=key==='codex_command'?provider!=='codex':['api_base','api_key_env'].includes(key)?provider!=='api':provider==='client';}const background=f.elements.namedItem('background_analysis');background.closest('label').hidden=provider==='client';background.disabled=provider==='client';if(background.disabled)background.checked=false;}
function renderSettings(){if(settingsLoaded)return;const f=$('#settings-form');for(const [key,value]of Object.entries(state.settings)){const input=f.elements.namedItem(key);if(!input)continue;if(input.type==='checkbox')input.checked=value;else input.value=Array.isArray(value)?value.join(', '):value;}settingsLoaded=true;renderProviderFields();}
async function refresh(){try{state=await api('state');renderStatus();renderProjects();renderNotifications();renderSettings();const select=$('#search-project'),selected=select.value;select.replaceChildren(el('option','全部聊天'));select.firstChild.value='';for(const p of state.projects.filter(p=>p.status==='tracked')){const o=el('option',p.name);o.value=p.id;select.append(o);}select.value=selected;
 const j=state.job;const signature=JSON.stringify(j);if(j&&signature!==lastJob){lastJob=signature;if(!j.ok&&!(state.settings.provider==='client'&&['analyze','ask'].includes(j.action)))notice(j.error);}
 const a=state.answer;if(a&&a.at!==lastAnswer){lastAnswer=a.at;const c=el('div',undefined,'card');c.append(el('h2','根据聊天整理'),el('div',a.result.answer,'text'),refs(a.result.evidence));$('#answers').prepend(c);}
 const m=state.media_result;if(m&&m.at!==lastMedia){const first=lastMedia==='';lastMedia=m.at;if(!first)await showMedia(m.result);}
 if(!m&&lastMedia==='')lastMedia='none';}catch(e){notice('连接本地服务失败：'+e.message);}}
$('#nav').onclick=e=>{const tab=e.target.closest('button[data-tab]')?.dataset.tab;if(tab)selectTab(tab);};
$('#toggle-archived').onclick=()=>{showArchived=!showArchived;renderProjects();};
$('#close-dialog').onclick=()=>{$('#dialog').close();if(previewURL){URL.revokeObjectURL(previewURL);previewURL=null;}};
$('#add-project').onclick=()=>{
 showDialog('添加项目');const body=$('#dialog-body'),name=el('input'),aliases=el('input'),notes=el('textarea');
 name.placeholder='例如：客户门户';aliases.placeholder='多个别名用逗号分隔';notes.placeholder='你的分工或关注重点';
 for(const [input,label] of [[name,'项目名称'],[aliases,'别名（选填）'],[notes,'说明（选填）']]){const field=el('label',label);field.append(input);body.append(field);}body.append(button('保存项目',async()=>{await mutate('project-config',{name:name.value,aliases:aliases.value.split(/[,，]/).map(x=>x.trim()).filter(Boolean),notes:notes.value});$('#dialog').close();notice('项目已保存，可以让智能体按项目查询。');},'primary'));
};
$('#feedback').onclick=()=>feedbackDialog();$('#attachments').onclick=()=>showAttachments().catch(e=>notice(e.message));
$('#sync').onclick=()=>job('sync').catch(e=>notice(e.message));$('#analyze').onclick=()=>job('analyze').catch(e=>notice(e.message));$('#test-notify').onclick=()=>job('notify_test').catch(e=>notice(e.message));$('#backfill').onclick=()=>job('backfill',{days:Number($('#backfill-days').value)}).catch(e=>notice(e.message));
$('#ask').onclick=()=>{const q=$('#question').value.trim();if(q)job('ask',{question:q,project_id:Number($('#search-project').value)||null}).catch(e=>notice(e.message));};
$('#find').onclick=async()=>{try{const rows=await api('search?q='+encodeURIComponent($('#question').value)+'&project='+$('#search-project').value);$('#answers').replaceChildren();for(const m of rows)$('#answers').append(message(m));if(!rows.length)empty($('#answers'),'没有找到匹配的消息。');}catch(e){notice(e.message);}};
$('#settings-form').elements.namedItem('provider').onchange=renderProviderFields;
$('#settings-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const data=Object.fromEntries(f);for(const k of ['poll_seconds','analysis_seconds','batch_size'])data[k]=Number(data[k]);data.schedule=data.schedule.split(/[,，]/).map(x=>x.trim()).filter(Boolean);data.notifications=f.has('notifications');data.background_analysis=f.has('background_analysis');try{await api('settings',data);notice('设置已保存。');settingsLoaded=false;await refresh();}catch(e){notice(e.message);}};
refresh();setInterval(refresh,5000);
