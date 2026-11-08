/* Project-scoped read-only database workbench. */
(() => {
  const chip=document.createElement('button');chip.className='git-chip';chip.innerHTML='<svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/></svg><span class="chip-label">数据库</span>';chip.id='database-chip';chip.type='button';chip.title='项目数据库';chip.setAttribute('aria-label','项目数据库');chip.setAttribute('aria-haspopup','dialog');
  document.getElementById('project-context-open').after(chip);
  const dialog=document.createElement('dialog');dialog.className='database-dialog';
  dialog.setAttribute('aria-label','项目数据库');
  dialog.innerHTML=`
    <header class="db-header"><div><h2>数据库<span class="db-badge">只读模式</span></h2><button id="db-project" class="db-project">选择项目文件夹</button></div><button id="db-close" aria-label="关闭数据库">✕</button></header>
    <div class="db-workbench">
      <aside class="db-sidebar">
        <div class="db-section-heading"><h3>项目连接</h3><button id="db-add" title="添加连接" aria-label="添加连接">＋</button></div>
        <select id="db-list" aria-label="数据库连接"></select>
        <div class="db-connection-actions"><button id="db-edit">编辑</button><button id="db-test">测试连接</button><button id="db-remove" class="db-danger">移除</button></div>
        <div class="db-divider"></div><details id="db-tree" open><summary><span aria-hidden="true">▱</span> <strong id="db-database-name">数据库</strong></summary><div class="db-section-heading"><span>表与视图</span><button id="db-refresh">刷新</button></div><input id="db-search" placeholder="搜索表名" aria-label="搜索表名"><div id="db-table-list"></div><div class="db-tree-pages"><button id="db-table-prev">上一页</button><span id="db-table-page"></span><button id="db-table-next">下一页</button></div></details>
        <details id="db-config" open><summary>连接配置</summary>
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
        </form></details>
      </aside>
      <main class="db-main"><div id="db-project-guide" class="db-empty" hidden><strong>先选择一个项目</strong><p>配置数据库不需要发送消息，也不会创建空会话。</p><button id="db-choose-project" class="db-primary">选择项目文件夹</button></div><div id="db-content"><div id="db-breadcrumb">选择数据库连接，再选择一张表</div><div class="db-view-tabs"><button id="db-fields">字段结构</button><button id="db-data">表数据</button></div>
        <details class="db-query-section"><summary>SQL 编辑器</summary><div class="db-section-heading"><div><h3>SQL 查询</h3><p class="db-help">只读执行 · 最多返回 200 行</p></div><button id="db-tables">查看表结构</button></div>
          <div class="db-editor"><div class="db-editor-label">SQL <span>SELECT / WITH / EXPLAIN</span></div><textarea id="db-sql" aria-label="SQL 查询" spellcheck="false" placeholder="SELECT * FROM your_table LIMIT 20"></textarea></div>
          <div class="db-query-footer"><span class="db-help">查询不会修改数据库内容</span><button id="db-run" class="db-primary">执行查询 <span aria-hidden="true">↗</span></button></div>
        </details>
        <section class="db-results-section"><div class="db-section-heading"><h3 id="db-result-title">字段与数据</h3></div><p id="db-state" role="status"></p><div id="db-result"><div class="db-empty"><span class="db-empty-icon">▤</span><strong>从一次查询开始</strong><p>选择或添加连接，查看表结构后执行 SQL。</p></div></div><div class="db-row-pages"><button id="db-row-prev">上一页</button><span id="db-row-page"></span><button id="db-row-next">下一页</button></div></section></div>
      </main>
    </div>`;
  document.body.append(dialog);
  const el=id=>dialog.querySelector('#'+id);
  let context=null,generation=0,saving=false,connections=[],selectedTable=null,tablePage=0,rowPage=0,hasTables=false,hasRows=false,mode='schema',search='',reopen=false,searchTimer;
  const project=()=>currentSession ? (wsCustom ? confirmedWorkspace : null) : pendingProjectPath;
  const key=()=>JSON.stringify([authToken,currentSession,project()]);
  const valid=version=>dialog.open && context && context.key===key() && (version===undefined || version===generation);
  function empty(text){el('db-result').replaceChildren();const node=document.createElement('div');node.className='db-empty';node.textContent=text;el('db-result').append(node);}
  function state(text,error=false){el('db-state').textContent=text;el('db-state').dataset.tone=error?'error':'';}
  function clearTable(){selectedTable=null;rowPage=0;hasRows=false;mode='schema';el('db-fields').disabled=true;el('db-data').disabled=true;el('db-fields').classList.remove('active');el('db-data').classList.remove('active');el('db-breadcrumb').textContent='选择数据库连接，再选择一张表';el('db-row-page').textContent='';el('db-row-prev').disabled=true;el('db-row-next').disabled=true;empty('选择左侧的表，查看字段结构或表数据');}
  async function request(body,version){
    if(!valid(version))throw new Error('项目已切换，请重新打开数据库');
    const scope=context;
    const data=await api('/api/databases',{method:'POST',body:JSON.stringify({session_id:scope.sid,workspace:scope.workspace,...body})});
    if(!valid(version))throw new Error('项目已切换');return data;
  }
  async function run(fn){const version=++generation;if(!valid())return;state('处理中…');try{await fn(version);}catch(error){if(valid(version))state(error.message,true);}}
  function show(data){const result=el('db-result');result.replaceChildren();const table=document.createElement('table');const head=document.createElement('tr');const numberHead=document.createElement('th');numberHead.textContent='#';numberHead.className='db-row-number';head.append(numberHead);for(const name of data.columns){const th=document.createElement('th');th.textContent=name;head.append(th);}table.append(head);for(const [index,row] of data.rows.entries()){const tr=document.createElement('tr');const number=document.createElement('td');number.textContent=(mode==='rows' && selectedTable ? rowPage*100 : 0)+index+1;number.className='db-row-number';tr.append(number);for(const value of row){const td=document.createElement('td');td.textContent=value===null?'NULL':String(value);td.classList.toggle('db-null',value===null);td.title=td.textContent;tr.append(td);}table.append(tr);}result.append(table);state(`显示 ${data.rows.length} 行`+(data.limited?' · 结果已限制，请缩小范围':''));}
  function setKind(){const kind=dialog.querySelector('[name=kind]').value;el('db-sqlite').hidden=kind!=='sqlite';el('db-mysql').hidden=kind!=='mysql';}
  async function catalog(version){
    const id=el('db-list').value;el('db-table-list').replaceChildren();if(!id)return;
    const data=await request({action:'catalog',id,page:tablePage,search},version);hasTables=data.has_more;el('db-database-name').textContent=data.database;el('db-database-name').title=data.database;
    for(const item of data.items){const button=document.createElement('button');button.textContent=item.name;button.title=item.comment || item.name;button.className='db-table-item';button.classList.toggle('active',selectedTable===item.name);button.onclick=()=>{selectedTable=item.name;rowPage=0;mode='schema';void run(viewTable);};el('db-table-list').append(button);}
    if(!data.items.length){const note=document.createElement('p');note.className='db-help';note.textContent='没有匹配的表';el('db-table-list').append(note);}
    el('db-table-page').textContent=`第 ${tablePage+1} 页`;el('db-table-prev').disabled=!tablePage;el('db-table-next').disabled=!hasTables;state('选择一张表查看详情');
  }
  async function viewTable(version){
    if(!selectedTable)throw new Error('请先选择一张表');empty('正在读取…');const name=selectedTable;
    const data=await request({action:mode,id:el('db-list').value,table:name,page:rowPage},version);
    el('db-fields').disabled=false;el('db-data').disabled=false;const connection=connections.find(x=>x.id===el('db-list').value);el('db-breadcrumb').textContent=`${connection?.database || connection?.name || '数据库'}  /  ${name}`;
    el('db-result-title').textContent=mode==='schema'?'字段结构':'表数据';el('db-fields').classList.toggle('active',mode==='schema');el('db-data').classList.toggle('active',mode==='rows');
    for(const node of el('db-table-list').children)node.classList.toggle('active',node.textContent===name);
    show(data);hasRows=!!data.has_more;el('db-row-page').textContent=mode==='rows'?`第 ${rowPage+1} 页 · 每页 100 行`:'';el('db-row-prev').disabled=mode!=='rows'||!rowPage;el('db-row-next').disabled=mode!=='rows'||!hasRows;
  }
  async function load(version,selected){
    const scope=context;const params=new URLSearchParams(scope.sid?{session_id:scope.sid}:{workspace:scope.workspace});const data=await api('/api/databases?'+params);
    if(!valid(version))return;connections=data.connections;el('db-list').replaceChildren();
    if(!connections.length){const option=document.createElement('option');option.value='';option.textContent='尚未添加连接';el('db-list').append(option);}
    for(const item of connections){const option=document.createElement('option');option.value=item.id;option.textContent=item.name+' · '+item.kind;el('db-list').append(option);}
    if(selected)el('db-list').value=selected;el('db-config').open=!connections.length;tablePage=0;search='';el('db-search').value='';clearTable();await catalog(version);
  }
  function open(){
    generation++;const workspace=project();context={key:key(),sid:currentSession,workspace};
    el('db-connect').reset();dialog.querySelector('[name=id]').value='';setKind();el('db-sql').value='';clearTable();el('db-table-list').replaceChildren();state('');
    el('db-project').textContent=workspace ? '项目 · '+workspace.split(/[\\/]/).filter(Boolean).pop() : '选择项目文件夹';el('db-project').title=workspace || '';el('db-project-guide').hidden=!!workspace;el('db-content').hidden=!workspace;dialog.querySelector('.db-sidebar').hidden=!workspace;
    if(!dialog.open)dialog.showModal();if(workspace)void run(version=>load(version));
  }
  function chooseProject(){reopen=true;dialog.close();void openPicker();}
  chip.onclick=open;el('db-project').onclick=chooseProject;el('db-choose-project').onclick=chooseProject;
  window.DatabaseWorkbench={projectPickerClosed(){reopen=false;},projectChanged(){if(reopen){reopen=false;open();}else if(dialog.open)open();}};
  el('db-close').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{generation++;context=null;clearTimeout(searchTimer);dialog.querySelector('[name=password]').value='';});
  dialog.querySelector('[name=kind]').onchange=setKind;
  el('db-list').onchange=()=>{clearTable();search='';el('db-search').value='';tablePage=0;void run(catalog);};
  el('db-connect').onsubmit=async event=>{event.preventDefault();if(saving)return;saving=true;const button=event.target.querySelector('[type=submit]');button.disabled=true;const body=Object.fromEntries(new FormData(event.target));try{await run(async version=>{const saved=await request({action:'save',...body},version);dialog.querySelector('[name=id]').value=saved.id;dialog.querySelector('[name=password]').value='';await load(version,saved.id);if(valid(version))state('连接已保存，可选择表或测试连接');});}finally{saving=false;button.disabled=false;}};
  el('db-edit').onclick=()=>{const item=connections.find(x=>x.id===el('db-list').value);if(!item)return;for(const name of ['id','name','kind','path','host','port','database','user'])dialog.querySelector('[name='+name+']').value=item[name] || (name==='port'?'3306':'');dialog.querySelector('[name=password]').value='';setKind();el('db-config').open=true;};
  el('db-new').onclick=()=>{el('db-connect').reset();dialog.querySelector('[name=id]').value='';setKind();el('db-config').open=true;};
  el('db-add').onclick=el('db-new').onclick;
  el('db-test').onclick=()=>void run(async version=>{const result=await request({action:'test',id:el('db-list').value},version);state(result.message);});
  el('db-remove').onclick=()=>void run(async version=>{await request({action:'remove',id:el('db-list').value},version);await load(version);});
  el('db-tables').onclick=el('db-refresh').onclick=()=>{tablePage=0;clearTable();void run(catalog);};
  el('db-search').oninput=()=>{clearTimeout(searchTimer);generation++;searchTimer=setTimeout(()=>{search=el('db-search').value;tablePage=0;clearTable();void run(catalog);},250);};
  el('db-table-prev').onclick=()=>{if(tablePage){tablePage--;void run(catalog);}};el('db-table-next').onclick=()=>{if(hasTables){tablePage++;void run(catalog);}};
  el('db-fields').onclick=()=>{mode='schema';void run(viewTable);};el('db-data').onclick=()=>{mode='rows';rowPage=0;void run(viewTable);};
  el('db-row-prev').onclick=()=>{if(rowPage){rowPage--;void run(viewTable);}};el('db-row-next').onclick=()=>{if(hasRows){rowPage++;void run(viewTable);}};
  el('db-run').onclick=()=>void run(async version=>{if(!el('db-sql').value.trim())throw new Error('请先输入 SQL');clearTable();const data=await request({action:'query',id:el('db-list').value,sql:el('db-sql').value},version);el('db-result-title').textContent='SQL 查询结果';show(data);});
})();
