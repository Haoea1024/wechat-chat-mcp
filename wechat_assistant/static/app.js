const $ = s => document.querySelector(s);
let state, lastJob = '', lastAnswer = '', lastMedia = '', settingsLoaded = false, lastProjectView = '';
let previewURL;
const token = $('meta[name="local-token"]').content;
const names = {task:'待办',decision:'决定',question:'待确认',risk:'阻碍',open:'进行中',reported_done:'聊天称已完成 · 待你确认',done:'已完成',dismissed:'已忽略',candidate:'待确认项目',tracked:'跟踪中',ignored:'已忽略'};
function el(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
function notice(text){$('#notice').textContent=text;clearTimeout(notice.timer);notice.timer=setTimeout(()=>$('#notice').textContent='',6000);}
async function api(path,body){const r=await fetch('/api/'+path,{method:body?'POST':'GET',headers:{'X-Local-Token':token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const v=await r.json();if(!r.ok)throw Error(v.error);return v;}
function button(text,fn,cls='small'){const b=el('button',text,cls);b.onclick=()=>Promise.resolve().then(fn).catch(e=>notice(e.message));return b;}
function date(ts){return ts?new Date((ts+8*3600)*1000).toISOString().slice(0,19).replace('T',' '):'尚未运行';}
function empty(parent,text){parent.append(el('div',text,'empty'));}
async function mutate(route,body){await api(route,body);await refresh();}
async function job(action,body={}){await api('job',{action,...body});notice('已加入后台任务，结果会自动显示。');}
function refs(ids){const row=el('div',undefined,'row');for(const id of ids||[])row.append(button('消息 #'+id,async()=>{const messages=await api('message?id='+id);showDialog('原始消息依据');for(const m of messages)$('#dialog-body').append(message(m));}));return row;}
function message(m){const c=el('div',undefined,'card');c.append(el('small',`${m.chat_name} · ${m.sender} · ${date(m.ts)} · #${m.id}`),el('p',m.text,'text'));
 if(m.attachment){c.append(el('small','附件：'+mediaStatus(m.attachment.status)),button('读取'+m.kind,()=>job('read_media',{message_id:m.id})));if(m.attachment.text&&m.attachment.status!=='not_read')c.append(button('查看已读内容',async()=>showMedia(await api('media?id='+m.id))));}
 c.append(button('纠正这条消息的理解',()=>feedbackDialog(null,m)));return c;}
function mediaStatus(s){return ({not_read:'尚未读取',ready:'已读取',partial:'部分已读',missing:'本机缺少附件',error:'读取失败',listed:'已列目录，成员正文未读'})[s]||s;}
async function showMedia(r){if(!r)return;showDialog('附件内容 · #'+r.message_id);const b=$('#dialog-body'),d=r.detail||{};
 b.append(el('p',mediaStatus(r.status)+(d.filename?' · '+d.filename:''),'muted'));if(d.quality==='thumbnail')b.append(el('p','当前只有缩略图，文字可能不完整。','muted'));if(d.method)b.append(el('small',d.method));
 b.append(el('div',r.text||d.error||'没有可读正文。','text'));
 if(r.path&&['图片','语音'].includes(r.kind)){const response=await fetch('/api/media-file?id='+r.message_id,{headers:{'X-Local-Token':token}});if(response.ok){if(previewURL)URL.revokeObjectURL(previewURL);previewURL=URL.createObjectURL(await response.blob());const preview=el(r.kind==='图片'?'img':'audio');preview.src=previewURL;preview.style.maxWidth='100%';if(r.kind==='语音')preview.controls=true;b.prepend(preview);}}
 const next=d.next_start??r.next_start;if(next!==undefined&&next!==null)b.append(button('继续读取后面的内容',()=>job('read_media',{message_id:r.message_id,start:next,member:r.member})));
 if(d.archive_members_only){const input=el('input');input.placeholder='输入压缩包中的完整成员名';b.append(input,button('读取这个成员',()=>job('read_media',{message_id:r.message_id,member:input.value})));}
 b.append(button('重新读取',()=>job('read_media',{message_id:r.message_id,start:d.start||0,force:true,member:r.member})));
}
function feedbackDialog(p=null,m=null){showDialog(p?'补充或纠正 · '+p.name:'我的反馈');const body=$('#dialog-body');
 const scope=el('select');const all=el('option','全部工作 / 长期要求');all.value='';scope.append(all);for(const project of state.projects){const option=el('option',project.name);option.value=project.id;scope.append(option);}scope.value=p?.id||'';
 const text=el('textarea');text.placeholder='例如：这只是群里的玩笑，不是我的工作；这个任务昨天已在线下完成；我只负责其中的接口部分。';
 const chat=el('select');const none=el('option','不限聊天');none.value='';chat.append(none);for(const c of state.chats){const o=el('option',c.name);o.value=c.id;chat.append(o);}chat.value=m?.chat_id||'';
 body.append(el('p','用自己的话说明即可。有效反馈优先于自动推测，可随时撤销；原始聊天不会改动。','muted'),scope,chat,text,button('保存反馈',async()=>{await mutate('feedback',{text:text.value,project_id:Number(scope.value)||null,chat_id:chat.value||null,message_id:m?.id||null});notice('已记住，下一轮会按你的反馈更新进度。');feedbackDialog(p,m);},'primary'));
 body.append(el('h3','有效反馈'));for(const f of state.feedback.filter(f=>!p||!f.project_id||f.project_id===p.id)){const row=el('div',undefined,'item');const label=state.projects.find(p=>p.id===f.project_id)?.name||'全部工作';row.append(el('small',label+' · '+date(f.created)+(f.message_id?' · 消息 #'+f.message_id:'')),el('p',f.text,'text'),button('撤销这条反馈',async()=>{await mutate('feedback',{revoke:f.id});feedbackDialog(p,m);}));body.append(row);}
}
async function showAttachments(offset=0){const data=await api('attachments?project='+$('#search-project').value+'&offset='+offset);if(!offset)$('#answers').replaceChildren();for(const m of data.messages)$('#answers').append(message(m));if(data.next_offset!==null)$('#answers').append(button('继续查看附件',e=>showAttachments(data.next_offset)));if(!data.messages.length)empty($('#answers'),'此范围暂无可列出的附件。');}

function showDialog(title){$('#dialog-title').textContent=title;$('#dialog-body').replaceChildren();if(!$('#dialog').open)$('#dialog').showModal();}
function editProject(p){showDialog('项目资料 · '+p.name);const body=$('#dialog-body');const form=el('form');
 for(const [key,title,value] of [['name','项目名称',p.name],['notes','你的进度说明 / 纠正 / 个人分工',p.notes],['paths','本地文档或代码目录（每行一个路径）',p.paths.join('\n')]]){const l=el('label',title);const input=el(key==='name'?'input':'textarea');input.name=key;input.value=value;l.append(input);form.append(l);}
 form.append(el('p','只读你填写的文件；目录读取 README / TODO / STATUS 和 Git 近期状态，内容会作为模型分析上下文。','muted'));
 const save=el('button','保存资料','primary');form.append(save);form.onsubmit=async e=>{e.preventDefault();try{const f=new FormData(form);await mutate('project',{id:p.id,name:f.get('name'),notes:f.get('notes'),paths:f.get('paths').split('\n').map(x=>x.trim()).filter(Boolean)});$('#dialog').close();notice('已保存，后台将结合资料重新分析。');}catch(err){notice(err.message);}};body.append(form);
 body.append(el('h3','关联聊天'));for(const c of p.chats){const row=el('div',undefined,'row');row.append(el('span',(c.name||c.chat_id)+(c.excluded?'（已排除）':'')),button(c.excluded?'重新关联':'排除此聊天',async()=>{await mutate('chat',{project_id:p.id,chat_id:c.chat_id,excluded:!c.excluded});editProject(state.projects.find(x=>x.id===p.id));}));body.append(row);}
 const select=el('select');for(const c of state.chats){const option=el('option',c.name);option.value=c.id;select.append(option);}body.append(select,button('手动关联',async()=>{if(select.value){await mutate('chat',{project_id:p.id,chat_id:select.value,excluded:false});editProject(state.projects.find(x=>x.id===p.id));}}));
}
function briefList(title,values,cls=''){if(!values.length)return null;const box=el('div',undefined,'brief '+cls);box.append(el('h3',title));const list=el('ul');for(const value of values.slice(0,3))list.append(el('li',value));box.append(list);return box;}
function renderProjects(){
 const target=$('#project-list'),view=JSON.stringify(state.projects);if(view===lastProjectView)return;lastProjectView=view;
 const opened=new Set([...target.querySelectorAll('details[open]')].map(n=>n.dataset.project));target.replaceChildren();
 if(!state.projects.length)empty(target,'还没有配置项目。点击“添加项目”，或直接让智能体先搜索聊天。');
 for(const p of [...state.projects].sort((a,b)=>a.fixed&&b.fixed?a.id-b.id:0)){
  const c=el('article',undefined,'card project-card'),overview=p.overview||{};
  const head=el('div',undefined,'row between');head.append(el('h2',p.name),el('span',p.fixed?'我的工作':names[p.status],'badge'));c.append(head,el('p',p.summary,'text progress'));
  if(p.status!=='ignored'){
   const chats=(overview.conversations||[]).filter(chat=>!p.chats.some(link=>link.chat_id===chat.chat_id&&link.excluded));
   if(chats.length){const group=el('div',undefined,'conversation-list'),more=el('details');more.append(el('summary',`更多相关聊天（${Math.max(0,chats.length-3)}）`));chats.forEach((chat,index)=>{const row=el('div',undefined,'conversation');const linked=p.chats.find(link=>link.chat_id===chat.chat_id);row.append(el('span',(chat.chat_id.includes('@chatroom')?'群聊 · ':'私聊 · ')+(linked?.name||'相关聊天'),'muted'),el('p',chat.summary,'text'));(index<3?group:more).append(row);});if(chats.length>3)group.append(more);const threads=el('details');threads.append(el('summary',`各群聊与私聊进度（${chats.length}）`),group);c.append(threads);}
   const next=overview.next_steps||p.items.filter(i=>i.state==='open'&&i.kind==='task').slice(0,3).map(i=>i.title);
   const questions=overview.uncertainties||p.items.filter(i=>i.state==='open'&&['question','risk'].includes(i.kind)).slice(0,3).map(i=>i.title);
   const blocks=el('div',undefined,'briefs');for(const b of [briefList('下一步',next),briefList('待确认',questions,'uncertainties')])if(b)blocks.append(b);if(blocks.childElementCount)c.append(blocks);
  }
  const actions=el('div',undefined,'row');if(p.status!=='tracked')actions.append(button('确认跟踪',()=>mutate('project',{id:p.id,status:'tracked'}),'primary'));
  actions.append(button('补充 / 纠正',()=>feedbackDialog(p)),button('资料与关联聊天',()=>editProject(p)));if(p.status!=='ignored')actions.append(button('忽略项目',()=>mutate('project',{id:p.id,status:'ignored'})));c.append(actions);
  const detail=el('details');detail.dataset.project=String(p.id);detail.open=opened.has(String(p.id));detail.append(el('summary',`查看消息依据与详细事项（${p.items.length}）`));
  detail.append(refs([...new Set([...(p.evidence||[]),...(overview.conversations||[]).flatMap(chat=>chat.evidence||[])])]));
  for(const i of p.items){const item=el('div',undefined,'item');item.append(el('span',`${names[i.kind]} · ${names[i.state]}${i.priority==='urgent'?' · 紧急':''}`,'badge'),el('h3',i.title));const meta=[i.owner?'负责人：'+i.owner:'',i.due?'截止：'+i.due:''].filter(Boolean);if(meta.length)item.append(el('small',meta.join('　')));if(i.reason)item.append(el('p',i.reason,'muted'));item.append(refs(i.evidence));const controls=el('div',undefined,'row');if(!['done','dismissed'].includes(i.state))controls.append(button('确认完成',()=>mutate('item',{id:i.id,state:'done'})),button('忽略',()=>mutate('item',{id:i.id,state:'dismissed'})));else controls.append(button('重新打开',()=>mutate('item',{id:i.id,state:'open'})));item.append(controls);detail.append(item);}
  c.append(detail);target.append(c);
 }
}
function renderStatus(){const s=state.status,target=$('#status');target.replaceChildren();const running=s.worker?.running?({sync:'正在采集',analyze:'正在整理进度',ask:'正在回答'}[s.worker.action]||'处理中'):'后台运行中';target.append(el('span',running,'badge'),el('span',`已收录 ${s.total.toLocaleString()} 条 · 待分析 ${s.pending||0} 条`,'muted'),el('span','最近采集 '+date(s.sync?.at),'muted'));for(const part of [s.sync,s.analysis])if(part?.ok===false)target.append(el('span',part.error,'error'));$('#mode').textContent=state.demo?'虚构数据演示':'本机运行 · 北京时间';$('#runtime').textContent='\n采集状态：'+JSON.stringify(s.sync,null,2)+'\n模型状态：'+JSON.stringify(s.analysis,null,2)+'\n桌面提醒：'+JSON.stringify(s.delivery,null,2);}
function renderNotifications(){const target=$('#notification-list');target.replaceChildren();if(!state.notifications.length)empty(target,'暂无提醒。没有变化时不会打扰。');for(const n of state.notifications){const c=el('article',undefined,'card');c.append(el('small',date(n.created)+(n.delivered?' · 已提交系统通知':'')),el('h2',n.title),el('div',n.body,'text'));if(n.error)c.append(el('p',n.error,'error'));target.append(c);}}
function renderSettings(){if(settingsLoaded)return;const f=$('#settings-form');for(const [key,value]of Object.entries(state.settings)){const input=f.elements.namedItem(key);if(!input)continue;if(input.type==='checkbox')input.checked=value;else input.value=Array.isArray(value)?value.join(', '):value;}settingsLoaded=true;}
async function refresh(){try{state=await api('state');renderStatus();renderProjects();renderNotifications();renderSettings();const select=$('#search-project'),selected=select.value;select.replaceChildren(el('option','全部聊天'));select.firstChild.value='';for(const p of state.projects.filter(p=>p.status==='tracked')){const o=el('option',p.name);o.value=p.id;select.append(o);}select.value=selected;
 const j=state.job;const signature=JSON.stringify(j);if(j&&signature!==lastJob){lastJob=signature;if(!j.ok)notice(j.error);}
 const a=state.answer;if(a&&a.at!==lastAnswer){lastAnswer=a.at;const c=el('div',undefined,'card');c.append(el('h2','根据聊天整理'),el('div',a.result.answer,'text'),refs(a.result.evidence));$('#answers').prepend(c);}
 const m=state.media_result;if(m&&m.at!==lastMedia){const first=lastMedia==='';lastMedia=m.at;if(!first)await showMedia(m.result);}
 if(!m&&lastMedia==='')lastMedia='none';}catch(e){notice('连接本地服务失败：'+e.message);}}
$('#nav').onclick=e=>{const tab=e.target.dataset.tab;if(!tab)return;for(const s of document.querySelectorAll('main>section'))s.hidden=s.id!==tab;for(const b of document.querySelectorAll('nav button'))b.classList.toggle('active',b.dataset.tab===tab);};
$('#close-dialog').onclick=()=>{$('#dialog').close();if(previewURL){URL.revokeObjectURL(previewURL);previewURL=null;}};
$('#add-project').onclick=()=>{
 showDialog('添加项目');const body=$('#dialog-body'),name=el('input'),aliases=el('input'),notes=el('textarea');
 name.placeholder='项目名称';aliases.placeholder='别名或简称，用逗号分隔';notes.placeholder='项目说明、你的分工或关注重点';
 body.append(name,aliases,notes,button('保存项目',async()=>{await mutate('project-config',{name:name.value,aliases:aliases.value.split(/[,，]/).map(x=>x.trim()).filter(Boolean),notes:notes.value});$('#dialog').close();notice('项目已保存，可以让智能体按项目查询。');},'primary'));
};
$('#feedback').onclick=()=>feedbackDialog();$('#attachments').onclick=()=>showAttachments().catch(e=>notice(e.message));
$('#sync').onclick=()=>job('sync').catch(e=>notice(e.message));$('#analyze').onclick=()=>job('analyze').catch(e=>notice(e.message));$('#test-notify').onclick=()=>job('notify_test').catch(e=>notice(e.message));$('#backfill').onclick=()=>job('backfill',{days:Number($('#backfill-days').value)}).catch(e=>notice(e.message));
$('#ask').onclick=()=>{const q=$('#question').value.trim();if(q)job('ask',{question:q,project_id:Number($('#search-project').value)||null}).catch(e=>notice(e.message));};
$('#find').onclick=async()=>{try{const rows=await api('search?q='+encodeURIComponent($('#question').value)+'&project='+$('#search-project').value);$('#answers').replaceChildren();for(const m of rows)$('#answers').append(message(m));if(!rows.length)empty($('#answers'),'没有找到匹配的消息。');}catch(e){notice(e.message);}};
$('#settings-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const data=Object.fromEntries(f);for(const k of ['poll_seconds','analysis_seconds','batch_size'])data[k]=Number(data[k]);data.schedule=data.schedule.split(/[,，]/).map(x=>x.trim()).filter(Boolean);data.notifications=f.has('notifications');data.background_analysis=f.has('background_analysis');try{await api('settings',data);notice('设置已保存。');await refresh();}catch(e){notice(e.message);}};
refresh();setInterval(refresh,5000);
