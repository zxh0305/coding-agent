/* Project-scoped read-only database workbench. */
(() => {
  const chip=document.createElement('button');chip.className='chip';chip.textContent='数据库';chip.id='database-chip';
  document.getElementById('browser-chip').after(chip);
  const dialog=document.createElement('dialog');dialog.className='database-dialog';
  dialog.innerHTML=`<header><strong>项目数据库 · 只读</strong><button id="db-close">关闭</button></header><p>连接随项目隔离。SQLite 不会创建文件；MySQL 请使用专用只读账号。</p><form id="db-connect"><input type="hidden" name="id"><input name="name" placeholder="连接名称" required><select name="kind"><option value="sqlite">SQLite</option><option value="mysql">MySQL</option></select><div id="db-sqlite"><input name="path" placeholder="SQLite 文件路径（相对当前项目或绝对路径）"></div><div id="db-mysql" hidden><input name="host" placeholder="主机"><input name="port" type="number" value="3306"><input name="database" placeholder="数据库名"><input name="user" placeholder="只读用户名"><input name="password" type="password" placeholder="密码（编辑时留空则保留原密码）" autocomplete="new-password"></div><button>保存连接</button><button type="button" id="db-new">新建连接</button></form><div class="db-actions"><select id="db-list" aria-label="数据库连接"></select><button id="db-edit">编辑连接</button><button id="db-test">测试连接</button><button id="db-tables">查看表结构</button><button id="db-remove">移除连接</button></div><textarea id="db-sql" aria-label="SQL 查询" placeholder="SELECT * FROM your_table LIMIT 20"></textarea><button id="db-run">执行只读查询</button><p id="db-state" role="status"></p><div id="db-result"></div>`;
  document.body.append(dialog);let sid=null,revision=0,busy=false,connections=[];
  const el=id=>dialog.querySelector('#'+id);
  function valid(){return sid && sid===currentSession;}
  async function request(body){return api('/api/databases',{method:'POST',body:JSON.stringify({session_id:sid,...body})});}
  function show(data){
    const result=el('db-result');result.replaceChildren();const table=document.createElement('table');const head=document.createElement('tr');
    for(const name of data.columns){const th=document.createElement('th');th.textContent=name;head.append(th);}table.append(head);
    for(const row of data.rows){const tr=document.createElement('tr');for(const value of row){const td=document.createElement('td');td.textContent=value===null?'NULL':String(value);tr.append(td);}table.append(tr);}
    result.append(table);el('db-state').textContent=`显示 ${data.rows.length} 行`+(data.limited?' · 已达到结果限制，请缩小查询范围':'');
  }
  async function run(fn){if(busy || !valid())return;busy=true;for(const button of dialog.querySelectorAll('button:not(#db-close)'))button.disabled=true;const version=++revision;el('db-state').textContent='处理中…';
    try{const data=await fn();if(version===revision && valid() && data)show(data);}catch(error){if(version===revision && valid())el('db-state').textContent=error.message;}finally{busy=false;for(const button of dialog.querySelectorAll('button'))button.disabled=false;}}
  async function load(selected){const data=await api('/api/databases?session_id='+encodeURIComponent(sid));if(!valid())return;connections=data.connections;el('db-list').replaceChildren();for(const item of data.connections){const option=document.createElement('option');option.value=item.id;option.textContent=item.name+' · '+item.kind;el('db-list').append(option);}if(selected)el('db-list').value=selected;}
  chip.onclick=()=>{if(!currentSession)return toast('请先打开已保存且绑定项目的会话');sid=currentSession;el('db-result').replaceChildren();el('db-state').textContent='';dialog.showModal();void run(async()=>{await load();});};
  el('db-close').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{revision++;sid=null;dialog.querySelector('[name=password]').value='';});
  dialog.querySelector('[name=kind]').onchange=event=>{el('db-sqlite').hidden=event.target.value!=='sqlite';el('db-mysql').hidden=event.target.value!=='mysql';};
  el('db-connect').onsubmit=event=>{event.preventDefault();void run(async()=>{const body=Object.fromEntries(new FormData(event.target));const saved=await request({action:'save',...body});dialog.querySelector('[name=id]').value=saved.id;dialog.querySelector('[name=password]').value='';await load(saved.id);el('db-state').textContent='连接已保存，点击查看表结构可测试连接';});};
  el('db-edit').onclick=()=>{const item=connections.find(x=>x.id===el('db-list').value);if(!item)return;for(const name of ['id','name','kind','path','host','port','database','user']){const input=dialog.querySelector('[name='+name+']');input.value=item[name] || (name==='port'?'3306':'');}dialog.querySelector('[name=password]').value='';dialog.querySelector('[name=kind]').dispatchEvent(new Event('change'));el('db-state').textContent='编辑后保存；密码留空会保留原密码';};
  el('db-new').onclick=()=>{el('db-connect').reset();dialog.querySelector('[name=id]').value='';dialog.querySelector('[name=kind]').dispatchEvent(new Event('change'));el('db-state').textContent='填写新连接信息后保存';};
  el('db-test').onclick=()=>void run(async()=>{const result=await request({action:'test',id:el('db-list').value});el('db-state').textContent=result.message;});
  el('db-tables').onclick=()=>void run(()=>request({action:'tables',id:el('db-list').value}));
  el('db-run').onclick=()=>void run(()=>request({action:'query',id:el('db-list').value,sql:el('db-sql').value}));
  el('db-remove').onclick=()=>void run(async()=>{await request({action:'remove',id:el('db-list').value});await load();el('db-result').replaceChildren();el('db-state').textContent='已移除连接配置，原数据库不受影响';});
})();
