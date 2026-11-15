"""Owner-authenticated Supervisor Chat presentation surface.

The browser renders only escaped/sanitized Markdown and observable execution activity.
No hidden model chain-of-thought is requested, retained, or exposed.
"""

SUPERVISOR_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Plane Alerts · Supervisor Chat</title><style>
:root{color-scheme:dark;--bg:#0b0f14;--panel:#121820;--line:#26313d;--muted:#8fa0b2;--text:#e9eef4;--accent:#7cb8e8;--good:#7bc49b;--bad:#df8585;--activity:#17202a}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line);background:#0e1319}.brand{font-weight:700}.brand small{display:block;color:var(--muted);font-weight:500;font-size:11px}.spacer{flex:1}a,button{color:var(--text)}a{color:var(--accent);text-decoration:none}main{max-width:1440px;margin:auto;padding:18px}.layout{display:grid;grid-template-columns:250px minmax(0,1fr) 330px;gap:12px;align-items:stretch}.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;min-width:0}.sidebar,.tracepanel{height:calc(100vh - 98px);min-height:560px;overflow:hidden;display:flex;flex-direction:column}.sidehead{padding:12px;border-bottom:1px solid var(--line);font-weight:650}.historylist,.trace{overflow:auto;padding:8px}.historyitem{width:100%;text-align:left;border:1px solid transparent;background:transparent;padding:9px;border-radius:6px;margin:0 0 5px;color:var(--text)}.historyitem:hover,.historyitem.active{background:#17202a;border-color:var(--line)}.historytitle{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.historymeta{display:block;font-size:11px;color:var(--muted);margin-top:3px}.toolbar{display:flex;gap:8px;align-items:center;padding:12px;border-bottom:1px solid var(--line);flex-wrap:wrap}.status{color:var(--muted);font-size:12px}.chat{height:calc(100vh - 310px);min-height:380px;overflow:auto;padding:16px;display:grid;gap:10px}.message{max-width:86%;padding:11px 13px;border:1px solid var(--line);border-radius:8px;overflow-wrap:anywhere}.user{justify-self:end;background:#172433;white-space:pre-wrap}.assistant{justify-self:start;background:#10161d}.meta{font-size:11px;color:var(--muted);margin-top:7px}.assistant .md p{margin:.45em 0}.assistant .md p:first-child{margin-top:0}.assistant .md p:last-child{margin-bottom:0}.assistant .md strong{font-weight:750;color:#f4f7fa}.assistant .md em{font-style:italic}.assistant .md code{font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#0a0f14;border:1px solid #202a35;border-radius:4px;padding:1px 4px}.assistant .md pre{margin:8px 0;padding:10px;background:#090d12;border:1px solid var(--line);border-radius:6px;overflow:auto}.assistant .md pre code{border:0;padding:0;background:transparent;white-space:pre}.assistant .md h1,.assistant .md h2,.assistant .md h3,.assistant .md h4{margin:.75em 0 .35em;line-height:1.2}.assistant .md h1{font-size:1.35em}.assistant .md h2{font-size:1.22em}.assistant .md h3{font-size:1.12em}.assistant .md ul,.assistant .md ol{margin:.45em 0;padding-left:1.55em}.assistant .md li{margin:.18em 0}.assistant .md blockquote{margin:.55em 0;padding:3px 0 3px 10px;border-left:3px solid #41556a;color:#c6d1dc}.assistant .md table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}.assistant .md th,.assistant .md td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}.assistant .md th{background:#17202a;font-weight:700}.activity{justify-self:start;max-width:86%;padding:8px 11px;border:1px dashed #344252;border-radius:8px;background:var(--activity);color:#c8d3de}.activity.running{border-style:solid}.activity-title{font-size:12px;font-weight:650}.activity-detail{font-size:11px;color:var(--muted);margin-top:3px;white-space:pre-wrap;overflow-wrap:anywhere}.activity .spinner{display:inline-block;width:8px;height:8px;border:1px solid #6f8295;border-top-color:transparent;border-radius:50%;margin-right:6px;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.composer{display:grid;grid-template-columns:1fr auto;gap:8px;padding:12px;border-top:1px solid var(--line)}textarea{min-height:70px;resize:vertical;background:#0d1218;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:10px;font:inherit}button{border:1px solid var(--line);background:#17202a;border-radius:7px;padding:9px 12px;cursor:pointer}button:disabled{opacity:.55;cursor:default}.note{padding:10px 12px;color:var(--muted);font-size:12px;border-bottom:1px solid var(--line)}.bad{color:var(--bad)}.good{color:var(--good)}.traceitem{border-bottom:1px solid var(--line);padding:9px 4px}.traceitem:last-child{border-bottom:0}.tracekind{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.tracetitle{font-weight:600;margin-top:2px}.tracedetail{font-size:12px;color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere;margin-top:4px}.empty{padding:12px;color:var(--muted);font-size:12px}@media(max-width:1050px){.layout{grid-template-columns:220px minmax(0,1fr)}.tracepanel{grid-column:1/-1;height:auto;min-height:0;max-height:360px}}@media(max-width:700px){main{padding:10px}.layout{grid-template-columns:1fr}.sidebar,.tracepanel{height:auto;min-height:0;max-height:300px}.chat{height:52vh;min-height:340px}.message,.activity{max-width:95%}.composer{grid-template-columns:1fr}.toolbar{align-items:flex-start}}</style></head><body>
<header><div class="brand">Plane Alerts<small>Private AI Operations · Supervisor Chat</small></div><div class="spacer"></div><a href="/">Operations dashboard</a></header>
<main><div class="layout"><aside class="panel sidebar"><div class="sidehead">Chat history</div><div id="historyList" class="historylist"><div class="empty">Loading conversations…</div></div></aside><section class="panel"><div class="toolbar"><button id="new">New conversation</button><label><input id="switch" type="checkbox"> ask another provider</label><span id="status" class="status">Checking owner session…</span></div><div class="note">Grounded read-only operations chat. Observable provider/tool activity is shown inline while a request runs. Missing evidence is reported as unavailable. The Supervisor cannot merge, deploy, change secrets/configuration, or decide flight/alert behavior.</div><div id="chat" class="chat"><div class="status">No conversation loaded.</div></div><form id="form" class="composer"><textarea id="message" maxlength="8192" placeholder="Ask about current tasks, findings, provider health, tests, replays, Git artifacts, or backups…" required></textarea><button id="send" type="submit">Send</button></form></section><aside class="panel tracepanel"><div class="sidehead">Execution trace</div><div class="note">Exact observable execution history: provider attempts/results, failovers, read-only tool calls/results, usage, and persisted continuity. Private hidden chain-of-thought is not stored or exposed.</div><div id="trace" class="trace"><div class="empty">Select a conversation to view its trace.</div></div></aside></div></main>
<script>
let csrf='',conversationId=localStorage.getItem('plane_alerts_supervisor_conversation')||'',working=false,pollTimer=null,lastHistory={messages:[]},lastTrace={};
const chat=document.querySelector('#chat'),statusEl=document.querySelector('#status'),send=document.querySelector('#send'),historyList=document.querySelector('#historyList'),trace=document.querySelector('#trace');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when=v=>{const d=new Date(Number(v||0)*1000);return Number.isFinite(d.getTime())?d.toLocaleString():''};
const shortJson=(v,limit=420)=>{let s='';try{s=JSON.stringify(v)}catch(_){s=String(v??'')}return s.length>limit?s.slice(0,limit)+'…':s};

function inlineMarkdown(raw){
  const code=[];
  let s=String(raw??'').replace(/`([^`\n]+)`/g,(_,value)=>`\u0000CODE${code.push(esc(value))-1}\u0000`);
  s=esc(s);
  s=s.replace(/\*\*([^*\n]+?)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g,'$1<em>$2</em>');
  s=s.replace(/~~([^~\n]+?)~~/g,'<del>$1</del>');
  return s.replace(/\u0000CODE(\d+)\u0000/g,(_,i)=>`<code>${code[Number(i)]||''}</code>`);
}
function markdown(raw){
  const lines=String(raw??'').replace(/\r\n?/g,'\n').split('\n'),out=[];
  let paragraph=[],list=null,listItems=[];
  const flushParagraph=()=>{if(paragraph.length){out.push(`<p>${paragraph.map(inlineMarkdown).join('<br>')}</p>`);paragraph=[]}};
  const flushList=()=>{if(list&&listItems.length){out.push(`<${list}>${listItems.map(x=>`<li>${inlineMarkdown(x)}</li>`).join('')}</${list}>`)}list=null;listItems=[]};
  const isSeparator=line=>/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
  const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(x=>x.trim());
  for(let i=0;i<lines.length;i++){
    const line=lines[i];
    if(/^```/.test(line.trim())){
      flushParagraph();flushList();const lang=line.trim().slice(3).trim(),buf=[];i++;
      while(i<lines.length&&!/^```/.test(lines[i].trim())){buf.push(lines[i]);i++}
      out.push(`<pre${lang?` data-lang="${esc(lang)}"`:''}><code>${esc(buf.join('\n'))}</code></pre>`);continue;
    }
    if(line.includes('|')&&i+1<lines.length&&isSeparator(lines[i+1])){
      flushParagraph();flushList();const head=cells(line);i+=2;const rows=[];
      while(i<lines.length&&lines[i].includes('|')&&lines[i].trim()){rows.push(cells(lines[i]));i++}i--;
      out.push(`<table><thead><tr>${head.map(x=>`<th>${inlineMarkdown(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(x=>`<td>${inlineMarkdown(x)}</td>`).join('')}</tr>`).join('')}</tbody></table>`);continue;
    }
    const heading=line.match(/^(#{1,4})\s+(.+)$/);if(heading){flushParagraph();flushList();const n=heading[1].length;out.push(`<h${n}>${inlineMarkdown(heading[2])}</h${n}>`);continue}
    const bullet=line.match(/^\s*[-+*]\s+(.+)$/),numbered=line.match(/^\s*\d+\.\s+(.+)$/);
    if(bullet||numbered){flushParagraph();const wanted=numbered?'ol':'ul';if(list&&list!==wanted)flushList();list=wanted;listItems.push((bullet||numbered)[1]);continue}
    if(/^\s*>\s?/.test(line)){flushParagraph();flushList();out.push(`<blockquote>${inlineMarkdown(line.replace(/^\s*>\s?/,''))}</blockquote>`);continue}
    if(!line.trim()){flushParagraph();flushList();continue}
    if(list)flushList();paragraph.push(line);
  }
  flushParagraph();flushList();return out.join('')||'<p></p>';
}

async function api(path,opt={}){opt.headers={...(opt.headers||{}),'Accept':'application/json'};if(opt.body&&!opt.headers['Content-Type'])opt.headers['Content-Type']='application/json';if(opt.method&&opt.method!=='GET')opt.headers['X-CSRF-Token']=csrf;const r=await fetch(path,opt);if(r.status===401)throw Error('owner authentication required');const body=await r.text();let value={};try{value=body?JSON.parse(body):{}}catch(_){throw Error('invalid server response')}if(!r.ok)throw Error(value.error||('HTTP '+r.status));return value}
function setStatus(text,cls=''){statusEl.className='status '+cls;statusEl.textContent=text}
function activityItems(data){
  const items=[];
  (data.tools||[]).forEach(x=>items.push({t:Number(x.created||0),k:'tool',title:`Ran read tool · ${x.tool||'unknown'}`,detail:`arguments ${shortJson(x.arguments)}\nresult hash ${String(x.result_hash||'').slice(0,16)}…`}));
  (data.switches||[]).forEach(x=>items.push({t:Number(x.created||0),k:'failover',title:`Provider switch · ${x.from_provider||'none'} → ${x.to_provider||'unknown'}`,detail:x.reason||'route change'}));
  (data.usage||[]).forEach(x=>items.push({t:Number(x.created||0),k:'provider',title:`Provider ${x.success?'completed':'failed'} · ${x.provider||'unknown'} · ${x.model||'unknown'}`,detail:`${x.slot||''}${x.failure_kind?' · '+x.failure_kind:''} · ${x.latency_ms||0} ms · in ${x.input_tokens||0} / out ${x.output_tokens||0}`}));
  return items;
}
function render(history=lastHistory,data=lastTrace,isWorking=working){
  lastHistory=history||{messages:[]};lastTrace=data||{};
  const messages=lastHistory.messages||[],items=[];
  messages.forEach((m,index)=>items.push({t:Number(m.created||0),o:index*10+2,type:'message',value:m}));
  activityItems(lastTrace).forEach((a,index)=>items.push({t:a.t,o:index*10+5,type:'activity',value:a}));
  items.sort((a,b)=>a.t===b.t?a.o-b.o:a.t-b.t);
  if(!items.length&&!isWorking){chat.innerHTML='<div class="status">Start a grounded operations conversation.</div>';return}
  chat.innerHTML=items.map(item=>{
    if(item.type==='message'){
      const m=item.value,body=m.role==='assistant'?`<div class="md">${markdown(m.content)}</div>`:esc(m.content);
      return `<div class="message ${m.role==='user'?'user':'assistant'}">${body}<div class="meta">${esc(m.role)}${m.created?' · '+esc(when(m.created)):''}</div></div>`;
    }
    const a=item.value;return `<div class="activity"><div class="activity-title">${esc(a.title)}</div><div class="activity-detail">${esc(a.detail)}</div></div>`;
  }).join('')+(isWorking?'<div class="activity running"><div class="activity-title"><span class="spinner"></span>Supervisor request in progress</div><div class="activity-detail">Live observable tool activity will appear here as it is persisted.</div></div>':'');
  chat.scrollTop=chat.scrollHeight;
}
function renderHistory(items){if(!items.length){historyList.innerHTML='<div class="empty">No saved conversations yet.</div>';return}historyList.innerHTML=items.map(c=>`<button class="historyitem ${c.conversation_id===conversationId?'active':''}" data-id="${esc(c.conversation_id)}"><span class="historytitle">${esc(c.title||'New conversation')}</span><span class="historymeta">${esc(c.status||'ACTIVE')} · ${esc(c.message_count)} messages${c.updated?' · '+esc(when(c.updated)):''}</span></button>`).join('');historyList.querySelectorAll('.historyitem').forEach(btn=>btn.onclick=async()=>{conversationId=btn.dataset.id||'';localStorage.setItem('plane_alerts_supervisor_conversation',conversationId);await load();await loadHistory()})}
function jsonText(v){try{return JSON.stringify(v,null,2)}catch(_){return String(v??'')}}
function renderTrace(data){const items=[];(data.usage||[]).forEach(x=>items.push({t:x.created,k:'provider',title:`${x.success?'Success':'Failure'} · ${x.provider} · ${x.model}`,detail:`${x.slot||''}${x.failure_kind?' · '+x.failure_kind:''} · ${x.latency_ms} ms · in ${x.input_tokens} / out ${x.output_tokens}${x.estimated_neurons?' · '+x.estimated_neurons+' neurons':''}`}));(data.switches||[]).forEach(x=>items.push({t:x.created,k:'failover',title:`${x.from_provider||'none'} → ${x.to_provider||'unknown'}`,detail:`${x.reason||'route change'}${x.from_model?' · '+x.from_model:''}${x.to_model?' → '+x.to_model:''}`}));(data.tools||[]).forEach(x=>items.push({t:x.created,k:'tool',title:x.tool,detail:`arguments ${jsonText(x.arguments)}\nresult ${jsonText(x.result)}\nhash ${x.result_hash}`}));items.sort((a,b)=>(a.t||0)-(b.t||0));if(data.continuity){items.push({t:data.conversation?.updated||0,k:'continuity',title:'Persisted continuity',detail:jsonText(data.continuity)})}if(!items.length){trace.innerHTML='<div class="empty">No provider/tool activity has been recorded for this conversation yet.</div>';return}trace.innerHTML=items.map(x=>`<div class="traceitem"><div class="tracekind">${esc(x.k)}${x.t?' · '+esc(when(x.t)):''}</div><div class="tracetitle">${esc(x.title)}</div><div class="tracedetail">${esc(x.detail)}</div></div>`).join('')}
async function loadHistory(){const r=await api('/api/supervisor/conversations?limit=50');renderHistory(r.conversations||[])}
async function load(silent=false){if(!conversationId)return;try{const [h,t]=await Promise.all([api('/api/supervisor/history?conversation_id='+encodeURIComponent(conversationId)),api('/api/supervisor/trace?conversation_id='+encodeURIComponent(conversationId))]);lastHistory=h;lastTrace=t;render(h,t,working);renderTrace(t);if(h.conversation?.provider)setStatus(`${working?'Working':'Ready'} · ${h.conversation.provider} · ${h.conversation.model||'model unavailable'}`,working?'':'good')}catch(e){if(!silent){conversationId='';localStorage.removeItem('plane_alerts_supervisor_conversation');lastHistory={messages:[]};lastTrace={};render(lastHistory,lastTrace,false);renderTrace({});setStatus(e.message,'bad')}}}
function startPolling(){stopPolling();pollTimer=setInterval(()=>load(true),650)}
function stopPolling(){if(pollTimer){clearInterval(pollTimer);pollTimer=null}}
async function session(){try{const s=await api('/api/session');csrf=s.csrf;const st=await api('/api/supervisor/status');setStatus(st.enabled?`Ready · ${st.primary_provider} · ${st.model}`:'Supervisor disabled',st.enabled?'good':'bad');await loadHistory();if(conversationId)await load()}catch(e){setStatus(e.message+' · sign in on the Operations dashboard first','bad');send.disabled=true}}
async function createConversation(){const r=await api('/api/supervisor/conversations',{method:'POST',body:'{}'});conversationId=r.conversation_id;localStorage.setItem('plane_alerts_supervisor_conversation',conversationId);lastHistory={messages:[]};lastTrace={};render(lastHistory,lastTrace,false);renderTrace({});await loadHistory();return conversationId}
document.querySelector('#new').onclick=async()=>{try{await createConversation();setStatus('New conversation ready','good')}catch(e){setStatus(e.message,'bad')}};
document.querySelector('#form').onsubmit=async e=>{e.preventDefault();send.disabled=true;try{if(!conversationId)await createConversation();let text=document.querySelector('#message').value.trim();if(!text)return;if(document.querySelector('#switch').checked)text='Switch provider. '+text;document.querySelector('#message').value='';working=true;render(lastHistory,lastTrace,true);setStatus('Working · waiting for provider/tool activity');startPolling();const r=await api('/api/supervisor/chat',{method:'POST',body:JSON.stringify({conversation_id:conversationId,message:text})});working=false;stopPolling();await load();await loadHistory();setStatus(`${r.status} · ${r.provider||'degraded'} · ${r.model||'no model'}${r.slot?' · '+r.slot:''}`,r.status==='OK'?'good':'bad')}catch(e){working=false;stopPolling();setStatus(e.message,'bad');await load(true)}finally{working=false;stopPolling();send.disabled=false;render(lastHistory,lastTrace,false)}};
session();
</script></body></html>'''