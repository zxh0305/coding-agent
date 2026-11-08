/* Project-scoped read-only database workbench. */
(() => {
  const chip=document.createElement('button');chip.className='git-chip';chip.innerHTML='<svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/></svg><span class="chip-label">数据库</span>';chip.id='database-chip';chip.type='button';chip.title='项目数据库';chip.setAttribute('aria-label','项目数据库');chip.setAttribute('aria-haspopup','dialog');
  document.getElementById('project-context-open').after(chip);
  const dialog=document.createElement('dialog');dialog.className='database-dialog';
  dialog.setAttribute('aria-label','项目数据库');
  dialog.innerHTML=`
    <header class="db-header"><div><span class="db-eyebrow">PROJECT WORKSPACE</span><h2>数据库<span class="db-badge">只读模式</span></h2></div><button id="db-close" aria-label="关闭数据库">✕</button></header>
    <div class="db-workbench">
      <aside class="db-sidebar">
        <div class="db-section-heading"><h3>项目连接</h3><span>仅当前项目可见</span></div>
        <select id="db-list" aria-label="数据库连接"></select>
        <div class="db-connection-actions"><button id="db-edit">编辑</button><button id="db-test">测试连接</button><button id="db-remove" class="db-danger">移除</button></div>
        <div class="db-divider"></div>
        <div class="db-section-heading"><h3>连接配置</h3></div>
        <form id="db-connect">
          <input type="hidden" name="id">
          <label>连接名称<input name="name" placeholder="例如：开发环境" required></label>
          <label>数据库类型<select name="kind"><option value="sqlite">SQLite</option><option value="mysql">MySQL</option></select></label>
          <div id="db-sqlite"><label>数据库文件<input name="path" placeholder="例如：data/demo.db"></label><p class="db-help">支持项目相对路径或绝对路径，不会创建新文件。</p></div>
          <div id="db-mysql" hidden>
            <div class="db-host-row"><label>主机地址<input name="host" placeholder="127.0.0.1" spellcheck="false"></label><label>端口<input name="port" type="number" value="3306"></label></div>
            <label>数据库名称<input name="database" placeholder="数据库名" spellcheck="false"></label>
            <label>用户名<input name="user" placeholder="建议使用只读账号" autocomplete="off"></label>
            <label>密码<input name="password" type="password" placeholder="输入连接密码" autocomplete="new-password"></label>
            <p class="db-help">编辑连接时，密码留空会保留原密码。</p>
          </div>
          <div class="db-form-actions"><button type="submit" class="db-primary">保存连接</button><button type="button" id="db-new">新建连接</button></div>
        </form>
      </aside>
      <main class="db-main">
        <section class="db-query-section"><div class="db-section-heading"><div><h3>SQL 查询</h3><p class="db-help">只读执行 · 最多返回 200 行</p></div><button id="db-tables">查看表结构</button></div>
          <div class="db-editor"><div class="db-editor-label">SQL <span>SELECT / WITH / EXPLAIN</span></div><textarea id="db-sql" aria-label="SQL 查询" spellcheck="false" placeholder="SELECT * FROM your_table LIMIT 20"></textarea></div>
          <div class="db-query-footer"><span class="db-help">查询不会修改数据库内容</span><button id="db-run" class="db-primary">执行查询 <span aria-hidden="true">↗</span></button></div>
        </section>
        <section class="db-results-section"><div class="db-section-heading"><h3>查询结果</h3></div><p id="db-state" role="status"></p><div id="db-result"><div class="db-empty"><span class="db-empty-icon">▤</span><strong>从一次查询开始</strong><p>选择或添加连接，查看表结构后执行 SQL。</p></div></div></section>
      </main>
    </div>`;
  document.body.append(dialog);let sid=null,revision=0,busy=false,connections=[];
  const el=id=>dialog.querySelector('#'+id);
  function valid(){return sid && sid===currentSession;}
  async function request(body){if(['test','tables','query','remove'].includes(body.action) && !body.id)throw new Error('请先添加并选择一个数据库连接');if(body.action==='query' && !body.sql.trim())throw new Error('请先输入 SQL 查询');return api('/api/databases',{method:'POST',body:JSON.stringify({session_id:sid,...body})});}
  function show(data){
    const result=el('db-result');result.replaceChildren();const table=document.createElement('table');const head=document.createElement('tr');
    for(const name of data.columns){const th=document.createElement('th');th.textContent=name;head.append(th);}table.append(head);
    for(const row of data.rows){const tr=document.createElement('tr');for(const value of row){const td=document.createElement('td');td.textContent=value===null?'NULL':String(value);tr.append(td);}table.append(tr);}
    result.append(table);el('db-state').textContent=`显示 ${data.rows.length} 行`+(data.limited?' · 已达到结果限制，请缩小查询范围':'');
  }
  async function run(fn){if(busy || !valid())return;busy=true;for(const button of dialog.querySelectorAll('button:not(#db-close)'))button.disabled=true;const version=++revision;el('db-state').dataset.tone='loading';el('db-state').textContent='处理中…';
    try{const data=await fn();if(version===revision && valid() && data)show(data);}catch(error){if(version===revision && valid()){el('db-state').dataset.tone='error';el('db-state').textContent=error.message;}}finally{busy=false;for(const button of dialog.querySelectorAll('button'))button.disabled=false;}}
  async function load(selected){const data=await api('/api/databases?session_id='+encodeURIComponent(sid));if(!valid())return;connections=data.connections;el('db-list').replaceChildren();if(!connections.length){const option=document.createElement('option');option.value='';option.textContent='尚未添加连接';el('db-list').append(option);}for(const item of data.connections){const option=document.createElement('option');option.value=item.id;option.textContent=item.name+' · '+item.kind;el('db-list').append(option);}if(selected)el('db-list').value=selected;}
  chip.onclick=()=>{if(!currentSession)return toast('请先打开已保存且绑定项目的会话');sid=currentSession;el('db-connect').reset();dialog.querySelector('[name=id]').value='';dialog.querySelector('[name=kind]').dispatchEvent(new Event('change'));el('db-result').innerHTML='<div class="db-empty"><span class="db-empty-icon">▤</span><strong>从一次查询开始</strong><p>选择或添加连接，查看表结构后执行 SQL。</p></div>';el('db-state').textContent='';dialog.showModal();void run(async()=>{await load();});};
  el('db-close').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{revision++;sid=null;dialog.querySelector('[name=password]').value='';});
  dialog.querySelector('[name=kind]').onchange=event=>{el('db-sqlite').hidden=event.target.value!=='sqlite';el('db-mysql').hidden=event.target.value!=='mysql';};
  el('db-connect').onsubmit=event=>{event.preventDefault();void run(async()=>{const body=Object.fromEntries(new FormData(event.target));const saved=await request({action:'save',...body});dialog.querySelector('[name=id]').value=saved.id;dialog.querySelector('[name=password]').value='';await load(saved.id);el('db-state').textContent='连接已保存，点击查看表结构可测试连接';});};
  el('db-edit').onclick=()=>{const item=connections.find(x=>x.id===el('db-list').value);if(!item)return;for(const name of ['id','name','kind','path','host','port','database','user']){const input=dialog.querySelector('[name='+name+']');input.value=item[name] || (name==='port'?'3306':'');}dialog.querySelector('[name=password]').value='';dialog.querySelector('[name=kind]').dispatchEvent(new Event('change'));el('db-state').textContent='编辑后保存；密码留空会保留原密码';};
  el('db-new').onclick=()=>{el('db-connect').reset();dialog.querySelector('[name=id]').value='';dialog.querySelector('[name=kind]').dispatchEvent(new Event('change'));el('db-state').textContent='填写新连接信息后保存';};
  el('db-test').onclick=()=>void run(async()=>{const result=await request({action:'test',id:el('db-list').value});el('db-state').textContent=result.message;});
  el('db-tables').onclick=()=>void run(()=>{el('db-result').replaceChildren();return request({action:'tables',id:el('db-list').value});});
  el('db-run').onclick=()=>void run(()=>{el('db-result').replaceChildren();return request({action:'query',id:el('db-list').value,sql:el('db-sql').value});});
  el('db-remove').onclick=()=>void run(async()=>{await request({action:'remove',id:el('db-list').value});await load();el('db-result').replaceChildren();el('db-state').textContent='已移除连接配置，原数据库不受影响';});
})();
