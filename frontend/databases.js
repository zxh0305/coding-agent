/* Project-scoped read-only database workbench. */
(() => {
  const chip=document.createElement('button');chip.className='git-chip';chip.innerHTML='<svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/></svg><span class="chip-label">数据库</span>';chip.id='database-chip';chip.type='button';chip.title='项目数据库';chip.setAttribute('aria-label','项目数据库');chip.setAttribute('aria-haspopup','dialog');
  document.getElementById('project-context-open').after(chip);
  const icons={bolt:'<path d="m13 2-9 12h7l-1 8 10-12h-7l1-8Z"/>',database:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',table:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 9h18M9 9v12"/>',search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',copy:'<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M15 8V4H4v11h4"/>'};
  const icon=name=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">'+icons[name]+'</svg>';
  const dialog=document.createElement('dialog');dialog.className='database-dialog';
  dialog.setAttribute('aria-label','项目数据库');
  dialog.innerHTML=`
    <header class="db-header"><div><span class="db-brand-icon">${icon("database")}</span><h2>数据库工作台<span class="db-badge">账号权限</span></h2><button id="db-project" class="db-project">选择项目文件夹</button></div><button id="db-close" aria-label="关闭数据库">✕</button></header>
    <div class="db-workbench">
      <aside class="db-sidebar">
        <div class="db-section-heading"><h3>项目连接</h3><button id="db-add" title="添加连接" aria-label="添加连接">＋</button></div>
        <div class="db-connection-card"><span class="db-card-caption">已保存的连接</span><select id="db-list" aria-label="数据库连接"></select><div id="db-connections" aria-label="项目连接列表"></div><div id="db-connection-meta" class="db-connection-meta"></div>
        <div class="db-connection-actions"><button id="db-connect-toggle" title="连接或断开数据库">ϟ 连接</button><button id="db-edit">编辑</button><button id="db-test">测试连接</button><button id="db-remove" class="db-danger">移除</button></div></div>
        <div class="db-divider"></div><section id="db-tree"><button id="db-tree-toggle" aria-expanded="true"><span class="db-tree-chevron" aria-hidden="true">⌄</span>${icon("database")}<strong id="db-database-name">数据库</strong></button><div id="db-tree-body"><div class="db-section-heading"><span>表与视图</span><button id="db-refresh">刷新</button></div><div class="db-search-field">${icon("search")}<input id="db-search" placeholder="搜索表名…" aria-label="搜索表名"></div><div id="db-table-list"></div><div class="db-tree-pages"><button id="db-table-prev">上一页</button><span id="db-table-page"></span><button id="db-table-next">下一页</button></div></div></section>
        <details id="db-config" open><summary>连接配置</summary>
        <form id="db-connect">
          <input type="hidden" name="id">
          <label>连接名称<input name="name" placeholder="例如：开发环境" required></label>
          <label>数据库类型<select name="kind"><option value="sqlite">SQLite</option><option value="mysql">MySQL</option></select></label>
          <div id="db-sqlite"><label>数据库文件<input name="path" placeholder="例如：data/demo.db"></label><p class="db-help">支持项目相对路径或绝对路径，不会创建新文件。</p></div>
          <div id="db-mysql" hidden>
            <div class="db-host-row"><label>主机地址<input name="host" placeholder="127.0.0.1" spellcheck="false"></label><label>端口<input name="port" type="number" value="3306"></label></div>
            <label>数据库名称<input name="database" placeholder="数据库名" spellcheck="false"></label>
            <label>用户名<input name="user" placeholder="数据库用户名" autocomplete="off"></label>
            <label>密码<input name="password" type="password" placeholder="输入连接密码" autocomplete="new-password"></label>
            <p class="db-help">编辑连接时，密码留空会保留原密码。</p>
          </div>
          <div class="db-form-actions"><button type="submit" class="db-primary">保存连接</button><button type="button" id="db-new">新建连接</button></div>
        </form></details>
      </aside>
      <main class="db-main"><div id="db-project-guide" class="db-empty" hidden><strong>先选择一个项目</strong><p>配置数据库不需要发送消息，也不会创建空会话。</p><button id="db-choose-project" class="db-primary">选择项目文件夹</button></div><div id="db-content"><div id="db-breadcrumb">选择数据库连接，再选择一张表</div><div class="db-view-tabs"><button id="db-fields">字段结构</button><button id="db-data">表数据</button></div>
        <details class="db-query-section"><summary>SQL 编辑器</summary><div class="db-section-heading"><div><h3>SQL 查询</h3><p class="db-help">按数据库账号权限执行 · 查询最多返回 200 行</p></div><button id="db-tables">查看表结构</button></div>
          <div class="db-snippet-bar"><select id="db-snippet-list" aria-label="常用SQL"><option value="">常用 SQL</option></select><input id="db-snippet-name" placeholder="常用 SQL 名称" aria-label="SQL名称"><button id="db-snippet-save">保存</button><button id="db-snippet-new">新建</button><button id="db-snippet-delete">删除</button></div><div class="db-editor"><div class="db-editor-label">SQL <span>SQL</span></div><textarea id="db-sql" aria-label="SQL 查询" spellcheck="false" placeholder="SELECT * FROM your_table LIMIT 20"></textarea></div>
          <div class="db-query-footer"><span class="db-help">选中语句优先执行 · 修改立即提交</span><button id="db-run" class="db-primary">执行 SQL <span aria-hidden="true">↗</span></button></div>
        </details>
        <section class="db-results-section"><div class="db-section-heading"><h3 id="db-result-title">字段与数据</h3></div><div class="db-grid-tools"><p id="db-state" role="status"></p><div><button id="db-wrap" aria-pressed="false">换行</button><button id="db-cell-open" disabled>${icon("eye")}查看单元格</button></div></div><div id="db-result"><div class="db-empty"><span class="db-empty-icon">▤</span><strong>从一次查询开始</strong><p>选择或添加连接，查看表结构后执行 SQL。</p></div></div><div class="db-row-pages"><button id="db-row-prev">上一页</button><span id="db-row-page"></span><button id="db-row-next">下一页</button></div></section></div>
      </main>
    </div>`;
  document.body.append(dialog);
  const inspector=document.createElement('dialog');inspector.className='db-cell-dialog';inspector.setAttribute('aria-label','单元格详情');
  inspector.innerHTML='<header><div><span class="db-card-caption">单元格内容 · 只读</span><h3 id="db-cell-title"></h3></div><button id="db-cell-close" aria-label="关闭单元格详情">✕</button></header><div class="db-cell-caption"><span id="db-cell-type"></span><button id="db-cell-copy">'+icon('copy')+'复制原值</button></div><pre id="db-cell-value"></pre>';
  document.body.append(inspector);
  let selectedCell=null;

  const el=id=>dialog.querySelector('#'+id);
  const cellEl=id=>inspector.querySelector('#'+id);
  let context=null,generation=0,saving=false,executing=false,connectionStates={},statusRevision=0,preferredConnection=null,snippets=[],snippetId=null,connections=[],selectedTable=null,tablePage=0,rowPage=0,hasTables=false,hasRows=false,mode='schema',search='',reopen=false,searchTimer;
  const project=()=>currentSession ? (wsCustom ? confirmedWorkspace : null) : pendingProjectPath;
  const key=()=>JSON.stringify([authToken,currentSession,project()]);
  const valid=version=>dialog.open && context && context.key===key() && (version===undefined || version===generation);
  function empty(text){resetCell();const result=el('db-result');result.dataset.empty='true';result.replaceChildren();const node=document.createElement('div');node.className='db-empty';const glyph=document.createElement('span');glyph.className='db-empty-icon';glyph.innerHTML=icon('table');const title=document.createElement('strong');title.textContent=text;const hint=document.createElement('p');hint.textContent='字段结构与表数据分开查看，也可以展开 SQL 编辑器查询。';node.append(glyph,title,hint);result.append(node);}
  function connectionMeta(){const item=connections.find(x=>x.id===el('db-list').value);const meta=el('db-connection-meta');meta.textContent=item ? (item.kind==='sqlite'?'SQLite · '+item.path.split(/[\\/]/).pop():'MySQL · '+item.host+':'+item.port+' / '+item.database) : '添加连接后即可浏览表和数据';meta.title=item?.path || meta.textContent;}

  function state(text,error=false){dialog.dataset.loading=text==='处理中…'?'true':'false';el('db-state').textContent=text;el('db-state').dataset.tone=error?'error':'';}
  function clearTable(){selectedTable=null;rowPage=0;hasRows=false;mode='schema';el('db-fields').disabled=true;el('db-data').disabled=true;el('db-fields').classList.remove('active');el('db-data').classList.remove('active');el('db-breadcrumb').textContent='选择数据库连接，再选择一张表';el('db-row-page').textContent='';el('db-row-prev').disabled=true;el('db-row-next').disabled=true;empty('选择左侧的表，查看字段结构或表数据');}
  async function request(body,version){
    if(!valid(version))throw new Error('项目已切换，请重新打开数据库');
    const scope=context;
    const data=await api('/api/databases',{method:'POST',body:JSON.stringify({session_id:scope.sid,workspace:scope.workspace,...body})});
    if(!valid(version))throw new Error('项目已切换');return data;
  }
  async function run(fn){const version=++generation;if(!valid())return;state('处理中…');try{await fn(version);}catch(error){if(valid(version))state(error.message,true);}}
  function resetCell(){if(selectedCell)selectedCell.node.classList.remove('db-cell-selected');selectedCell=null;el('db-cell-open').disabled=true;if(inspector.open)inspector.close();}
  function valueKind(value){if(value===null)return 'null';if(typeof value==='number')return 'number';if(typeof value==='boolean')return 'boolean';if(typeof value==='string' && /^[{[]/.test(value.trim())){try{const parsed=JSON.parse(value);if(parsed!==null && typeof parsed==='object')return 'json';}catch{}}return 'text';}
  function selectCell(node,value,column){if(selectedCell){selectedCell.node.classList.remove('db-cell-selected');selectedCell.node.tabIndex=-1;}selectedCell={node,value,column};node.classList.add('db-cell-selected');node.tabIndex=0;el('db-cell-open').disabled=false;}
  function formatJson(source){
    const tokens=source.match(/"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null|[{}\[\]:,]/g) || [];
    let depth=0,output='';const line=()=>"\n"+'  '.repeat(Math.min(depth,16));
    for(let i=0;i<tokens.length;i++){const token=tokens[i];if(token==='{'||token==='['){output+=token;if(tokens[i+1]!== (token==='{'?'}':']')){depth++;output+=line();}}else if(token==='}'||token===']'){if(tokens[i-1]!== (token==='}'?'{':'[')){depth--;output+=line();}output+=token;}else if(token===',')output+=','+line();else if(token===':')output+=': ';else output+=token;if(output.length>1000000)return source;}
    return output;
  }
  function renderJson(pre,text){const regex=/("(?:[^"\\]|\\.)*"(?=\s*:))|("(?:[^"\\]|\\.)*")|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b/g;let last=0;for(const match of text.matchAll(regex)){pre.append(document.createTextNode(text.slice(last,match.index)));const token=document.createElement('span');token.className='db-json-'+(match[1]?'key':match[2]?'string':match[3]?'number':'literal');token.textContent=match[0];pre.append(token);last=match.index+match[0].length;}pre.append(document.createTextNode(text.slice(last)));}
  function inspectCell(){if(!selectedCell)return;const {value,column}=selectedCell;const kind=valueKind(value);cellEl('db-cell-title').textContent=column;cellEl('db-cell-type').textContent=kind.toUpperCase();const pre=cellEl('db-cell-value');pre.replaceChildren();if(kind==='json')renderJson(pre,formatJson(value));else pre.textContent=value===null?'NULL':String(value);if(!inspector.open)inspector.showModal();}
  cellEl('db-cell-close').onclick=()=>inspector.close();cellEl('db-cell-copy').onclick=async()=>{if(!selectedCell)return;const value=selectedCell.value;const ok=await copyText(value===null?'NULL':String(value));cellEl('db-cell-copy').textContent=ok?'已复制':'复制失败';};
  el('db-cell-open').onclick=inspectCell;inspector.addEventListener('close',()=>{cellEl('db-cell-copy').innerHTML=icon('copy')+'复制原值';});
  el('db-tree-toggle').onclick=()=>{const expanded=el('db-tree-toggle').getAttribute('aria-expanded')!=='true';el('db-tree-toggle').setAttribute('aria-expanded',String(expanded));el('db-tree-body').hidden=!expanded;};
  el('db-wrap').onclick=()=>{const wrap=el('db-wrap').getAttribute('aria-pressed')!=='true';el('db-wrap').setAttribute('aria-pressed',String(wrap));el('db-result').classList.toggle('db-wrap-values',wrap);};
  function show(data){
    resetCell();const result=el('db-result');result.dataset.empty='false';result.replaceChildren();
    if(!data.columns.length && data.affected_rows!==null && data.affected_rows!==undefined){const box=document.createElement('div');box.className='db-execution-result';const title=document.createElement('strong');title.textContent='SQL 执行成功';const detail=document.createElement('p');detail.textContent=`影响 ${data.affected_rows} 行 · 已提交 · ${data.elapsed_ms} ms`;box.append(title,detail);result.append(box);state(`执行成功 · 影响 ${data.affected_rows} 行`);return;}
    const table=document.createElement('table');table.setAttribute('aria-label','数据库查询结果');const thead=document.createElement('thead');const head=document.createElement('tr');const numberHead=document.createElement('th');numberHead.textContent='#';numberHead.className='db-row-number';head.append(numberHead);
    for(const [index,name] of data.columns.entries()){const th=document.createElement('th');th.scope='col';const sample=data.rows.find(row=>row[index]!==null)?.[index];const type=document.createElement('span');type.className='db-column-kind';const kind=valueKind(sample);type.textContent=kind==='number'?'#':kind==='json'?'{}':kind==='boolean'?'◐':'Aa';type.title='当前页数据类型';const label=document.createElement('span');label.textContent=name;th.append(type,label);head.append(th);}thead.append(head);table.append(thead);
    const tbody=document.createElement('tbody');const cells=[];
    for(const [index,row] of data.rows.entries()){const tr=document.createElement('tr');const number=document.createElement('td');number.textContent=(mode==='rows' && selectedTable ? rowPage*100 : 0)+index+1;number.className='db-row-number';tr.append(number);
      for(const [column,value] of row.entries()){const td=document.createElement('td');const kind=valueKind(value);td.className='db-value-'+kind;td.tabIndex=cells.length===0?0:-1;td.title=value===null?'NULL':String(value);td.setAttribute('aria-label',data.columns[column]+': '+td.title);
        if(kind==='json'){const token=document.createElement('span');token.className='db-json-preview';token.textContent=value;td.append(token);}else{const text=document.createElement('span');text.textContent=value===null?'NULL':String(value);if(kind==='null'||kind==='boolean'||(mode==='schema' && data.columns[column]==='类型'))text.className='db-value-tag';td.append(text);}
        const position=cells.length;cells.push(td);td.onclick=()=>selectCell(td,value,data.columns[column]);td.ondblclick=()=>{selectCell(td,value,data.columns[column]);inspectCell();};
        td.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();selectCell(td,value,data.columns[column]);inspectCell();return;}const offset={ArrowRight:1,ArrowLeft:-1,ArrowDown:data.columns.length,ArrowUp:-data.columns.length}[event.key];if(offset && cells[position+offset]){event.preventDefault();cells[position+offset].click();cells[position+offset].focus();}};tr.append(td);
      }tbody.append(tr);
    }
    if(!data.rows.length){const tr=document.createElement('tr');const td=document.createElement('td');td.colSpan=data.columns.length+1;td.className='db-no-rows';td.textContent='没有返回记录';tr.append(td);tbody.append(tr);}table.append(tbody);result.append(table);
    state(data.affected_rows!==null && data.affected_rows!==undefined?`执行成功 · 影响 ${data.affected_rows} 行 · ${data.elapsed_ms} ms`:`${data.rows.length} 行 · ${data.columns.length} 列`+(data.limited?' · 已达到结果限制':''));
  }
  function setKind(){const kind=dialog.querySelector('[name=kind]').value;el('db-sqlite').hidden=kind!=='sqlite';el('db-mysql').hidden=kind!=='mysql';}
  async function catalog(version){
    const id=el('db-list').value;el('db-table-list').replaceChildren();if(!id){state('添加连接后开始浏览');return;}
    if(connectionStates[id]===false){state('连接已断开，点击闪电重新连接');return;}
    if(connectionStates[id]!==false){await request({action:'connect',id},version);connectionStates[id]=true;renderConnections();}
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
    const preferred=selected || preferredConnection;if(preferred && connections.some(x=>x.id===preferred))el('db-list').value=preferred;preferredConnection=el('db-list').value;saveSqlDraft();connectionMeta();renderConnections();el('db-config').open=!connections.length;tablePage=0;search='';el('db-search').value='';clearTable();await loadSnippets(version);await catalog(version);void refreshStatuses();
  }
  function renderConnections(){
    const list=el('db-connections');list.replaceChildren();
    for(const item of connections){const row=document.createElement('div');row.className='db-source-row';row.classList.toggle('active',item.id===el('db-list').value);const select=document.createElement('button');select.className='db-source-select';const dot=document.createElement('i');dot.className='db-status-dot';dot.dataset.connected=String(connectionStates[item.id]===true);dot.title=connectionStates[item.id]?'已连接':'已断开';dot.setAttribute('role','img');dot.setAttribute('aria-label',dot.title);const name=document.createElement('span');name.textContent=item.name;const kind=document.createElement('small');kind.className='db-source-kind';kind.textContent=item.kind;select.append(dot,name,kind);select.disabled=executing;select.title=item.kind+' · '+(item.database || item.path);select.onclick=()=>{el('db-list').value=item.id;el('db-list').dispatchEvent(new Event('change'));};const toggle=document.createElement('button');toggle.className='db-source-power';toggle.disabled=executing;toggle.innerHTML=icon('bolt');toggle.title=connectionStates[item.id]?'断开 '+item.name:'连接 '+item.name;toggle.onclick=()=>toggleConnection(item.id);row.append(select,toggle);list.append(row);}
    const connected=connectionStates[el('db-list').value]===true;el('db-connect-toggle').innerHTML=icon('bolt')+(connected?'断开':'连接');for(const id of ['db-connect-toggle','db-edit','db-test','db-remove','db-run','db-refresh'])el(id).disabled=!el('db-list').value || executing;
  }
  function toggleConnection(id){statusRevision++;void run(async version=>{const connected=connectionStates[id]===true;const result=await request({action:connected?'disconnect':'connect',id},version);connectionStates[id]=result.connected;renderConnections();if(id===el('db-list').value){clearTable();if(result.connected)await catalog(version);else{el('db-table-list').replaceChildren();state('连接已断开，点击闪电重新连接');}}else state(result.connected?'连接已建立':'连接已断开');});}
  el('db-connect-toggle').onclick=()=>toggleConnection(el('db-list').value);
  async function refreshStatuses(){if(!valid() || !context.workspace)return;const stamp=statusRevision,scope=context;try{const result=await request({action:'status'});if(!valid() || scope!==context || stamp!==statusRevision)return;for(const item of connections)connectionStates[item.id]=result.statuses[item.id]?.connected===true;renderConnections();}catch{}}
  setInterval(()=>{if(!document.hidden && dialog.open)void refreshStatuses();},10000);
  const draftKey=()=>context?'database-sql-'+who+'-'+context.workspace:null;
  function saveSqlDraft(){const key=draftKey();if(!key)return;try{localStorage.setItem(key,JSON.stringify({sql:el('db-sql').value,name:el('db-snippet-name').value,id:snippetId,connection:el('db-list').value}));}catch{}}
  async function loadSnippets(version){const data=await request({action:'snippets'},version);snippets=data.items;el('db-snippet-list').replaceChildren();const option=document.createElement('option');option.value='';option.textContent='选择常用 SQL';el('db-snippet-list').append(option);for(const item of snippets){const option=document.createElement('option');option.value=item.id;option.textContent=item.name;el('db-snippet-list').append(option);}if(snippetId && snippets.some(x=>x.id===snippetId))el('db-snippet-list').value=snippetId;}
  el('db-snippet-list').onchange=()=>{const item=snippets.find(x=>x.id===el('db-snippet-list').value);if(!item)return;snippetId=item.id;el('db-snippet-name').value=item.name;el('db-sql').value=item.sql;saveSqlDraft();};
  el('db-snippet-name').oninput=saveSqlDraft;
  el('db-snippet-new').onclick=()=>{snippetId=null;el('db-snippet-list').value='';el('db-snippet-name').value='';el('db-sql').value='';saveSqlDraft();};
  el('db-snippet-save').onclick=()=>void run(async version=>{const result=await request({action:'snippet_save',snippet_id:snippetId,name:el('db-snippet-name').value,sql:el('db-sql').value},version);snippetId=result.id;await loadSnippets(version);saveSqlDraft();state('常用 SQL 已保存，刷新或重新登录后仍可读取');});
  el('db-snippet-delete').onclick=()=>void run(async version=>{if(!snippetId)throw new Error('请先选择常用 SQL');await request({action:'snippet_remove',snippet_id:snippetId},version);snippetId=null;el('db-snippet-name').value='';await loadSnippets(version);saveSqlDraft();state('常用 SQL 已删除');});
  function open(){
    generation++;statusRevision++;connectionStates={};preferredConnection=null;const workspace=project();context={key:key(),sid:currentSession,workspace};
    el('db-connect').reset();dialog.querySelector('[name=id]').value='';setKind();el('db-sql').value='';snippetId=null;el('db-snippet-name').value='';try{const draft=JSON.parse(localStorage.getItem(draftKey())||'null');if(draft){el('db-sql').value=draft.sql || '';el('db-snippet-name').value=draft.name || '';snippetId=draft.id || null;preferredConnection=draft.connection || null;}}catch{}clearTable();el('db-table-list').replaceChildren();state('');
    el('db-project').textContent=workspace ? '项目 · '+workspace.split(/[\\/]/).filter(Boolean).pop() : '选择项目文件夹';el('db-project').title=workspace || '';el('db-project-guide').hidden=!!workspace;el('db-content').hidden=!workspace;dialog.querySelector('.db-sidebar').hidden=!workspace;
    if(!dialog.open)dialog.showModal();if(workspace)void run(version=>load(version));
  }
  function chooseProject(){reopen=true;dialog.close();void openPicker();}
  chip.onclick=open;el('db-project').onclick=chooseProject;el('db-choose-project').onclick=chooseProject;
  window.DatabaseWorkbench={projectPickerClosed(){reopen=false;},projectChanged(){if(reopen){reopen=false;open();}else if(dialog.open)open();}};
  el('db-close').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{resetCell();generation++;context=null;clearTimeout(searchTimer);dialog.querySelector('[name=password]').value='';});
  dialog.querySelector('[name=kind]').onchange=setKind;
  el('db-sql').addEventListener('input',saveSqlDraft);el('db-sql').addEventListener('keydown',event=>{if(event.key==='Enter' && (event.ctrlKey || event.metaKey)){event.preventDefault();el('db-run').click();}});
  el('db-list').onchange=()=>{if(executing)return;statusRevision++;preferredConnection=el('db-list').value;saveSqlDraft();connectionMeta();renderConnections();clearTable();search='';el('db-search').value='';tablePage=0;void run(catalog);};
  el('db-connect').onsubmit=async event=>{event.preventDefault();if(saving)return;saving=true;const button=event.target.querySelector('[type=submit]');button.disabled=true;const body=Object.fromEntries(new FormData(event.target));try{await run(async version=>{const saved=await request({action:'save',...body},version);dialog.querySelector('[name=id]').value=saved.id;dialog.querySelector('[name=password]').value='';await load(version,saved.id);if(valid(version))state('连接已保存，可选择表或测试连接');});}finally{saving=false;button.disabled=false;}};
  el('db-edit').onclick=()=>{const item=connections.find(x=>x.id===el('db-list').value);if(!item)return;for(const name of ['id','name','kind','path','host','port','database','user'])dialog.querySelector('[name='+name+']').value=item[name] || (name==='port'?'3306':'');dialog.querySelector('[name=password]').value='';setKind();el('db-config').open=true;el('db-config').scrollIntoView({block:'nearest'});};
  el('db-new').onclick=()=>{el('db-connect').reset();dialog.querySelector('[name=id]').value='';setKind();el('db-config').open=true;el('db-config').scrollIntoView({block:'nearest'});};
  el('db-add').onclick=el('db-new').onclick;
  el('db-test').onclick=()=>void run(async version=>{await request({action:'connect',id:el('db-list').value},version);connectionStates[el('db-list').value]=true;statusRevision++;renderConnections();state('连接成功');});
  el('db-remove').onclick=()=>void run(async version=>{await request({action:'remove',id:el('db-list').value},version);await load(version);});
  el('db-tables').onclick=el('db-refresh').onclick=()=>{tablePage=0;clearTable();void run(catalog);};
  el('db-search').oninput=()=>{clearTimeout(searchTimer);generation++;searchTimer=setTimeout(()=>{search=el('db-search').value;tablePage=0;clearTable();void run(catalog);},250);};
  el('db-table-prev').onclick=()=>{if(tablePage){tablePage--;void run(catalog);}};el('db-table-next').onclick=()=>{if(hasTables){tablePage++;void run(catalog);}};
  el('db-fields').onclick=()=>{mode='schema';void run(viewTable);};el('db-data').onclick=()=>{mode='rows';rowPage=0;void run(viewTable);};
  el('db-row-prev').onclick=()=>{if(rowPage){rowPage--;void run(viewTable);}};el('db-row-next').onclick=()=>{if(hasRows){rowPage++;void run(viewTable);}};
  el('db-run').onclick=async()=>{if(executing)return;executing=true;el('db-run').disabled=true;try{await run(async version=>{const editor=el('db-sql');const sql=editor.selectionEnd>editor.selectionStart?editor.value.slice(editor.selectionStart,editor.selectionEnd):editor.value;if(!sql.trim())throw new Error('请先输入 SQL');clearTable();statusRevision++;try{const data=await request({action:'execute',id:el('db-list').value,sql},version);connectionStates[el('db-list').value]=data.connected===true;renderConnections();el('db-result-title').textContent='SQL 执行结果';if(data.affected_rows!==null && data.affected_rows!==undefined)await catalog(version);show(data);}finally{void refreshStatuses();}});}finally{executing=false;renderConnections();}};
})();
