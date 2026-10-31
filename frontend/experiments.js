/* Native experiment page. Chat stays mounted; no run is tied to this connection. */
(() => {
  const V=window.ExperimentsView, $e=id=>document.getElementById(id);
  const page=$e('experiment-page');
  let active=false, selected='', serial=0, stream='events', cursors={events:0,requests:0};
  let items=[], busy=false, updating=false, detailCache=null;
  const text=(tag,value,cls)=>{const e=document.createElement(tag); e.textContent=value??'—'; if(cls)e.className=cls; return e;};
  const button=(label,fn)=>{const b=text('button',label); b.type='button'; b.onclick=fn; return b;};
  function error(e){$e('exp-message').textContent=e.message||String(e);}
  const url=id=>'/api/experiments/'+encodeURIComponent(id);
  function nav(id=''){location.hash=id?'experiments/'+id:'experiments';}
  function resetLogs(){cursors={events:0,requests:0};items=[];$e('exp-log').replaceChildren();}
  function close(){active=false;serial++;poll.stop();page.hidden=true;page.classList.add('hidden');$e('layout').inert=false;
    for(const id of ['exp-list','exp-graph','exp-checks','exp-usage','exp-artifacts','exp-preview','exp-errors','exp-log','exp-new'])$e(id).replaceChildren();
    $e('exp-preview').hidden=true;detailCache=null;}
  async function update(valid=()=>true) {
    if(updating)return; updating=true;
    const generation=serial, rid=selected;
    try {
      if(!rid){const data=await api('/api/experiments'); if(!valid()||generation!==serial)return; list(data.experiments);}
      else {
        const summary=await api(url(rid)+'/summary'); if(!valid()||generation!==serial)return;
        const signature=JSON.stringify([summary.status,summary.nodes]);
        if(!detailCache || detailCache.signature!==signature){
          const full=await api(url(rid)); if(!valid()||generation!==serial)return;
          detailCache={signature,data:full};
        }
        detail({...detailCache.data,...summary});
        const response=await api(`${url(rid)}/${stream}?cursor=${cursors[stream]}&limit=50`);
        if(!valid()||generation!==serial)return;
        cursors[stream]=response.cursor;
        for(const item of response.items){items.push(item); log(item);}
        // Keep the visible window bounded; full logs stay on disk and can be paged again.
        while($e('exp-log').children.length>150)$e('exp-log').firstChild.remove();
        items=items.slice(-150);
        $e('exp-more').hidden=!response.more;
      }
      $e('exp-message').textContent='';
    } catch(e){if(valid()&&generation===serial)error(e);} finally {updating=false;}
  }
  const poll=V.poller(update);
  function route(){
    const match=location.hash.match(/^#experiments(?:\/([a-f0-9]{32}))?$/);
    if(!match){close();return;}
    if(!$e('layout')||$e('layout').classList.contains('hidden'))return;
    detailCache=null;active=true;$e('layout').inert=true;serial++;selected=match[1]||'';page.hidden=false;page.classList.remove('hidden');
    $e('exp-list-area').hidden=!!selected;$e('exp-detail-area').hidden=!selected;
    $e('exp-new').hidden=true;$e('exp-preview').hidden=true;
    for(const id of ['exp-list','exp-graph','exp-checks','exp-usage','exp-artifacts','exp-errors'])$e(id).replaceChildren(text('p','正在加载…'));
    $e('exp-title').textContent=selected;
    $e('exp-status').textContent='正在加载…';$e('exp-stop').disabled=true;resetLogs();poll.start();
  }
  function list(rows){
    const root=$e('exp-list'); root.replaceChildren();
    if(!rows.length){root.append(text('p','暂无可访问的实验。无归属的历史命令行实验需由本机操作人员明确关联账号。'));return;}
    for(const r of rows){
      const row=text('article','', 'exp-card');
      row.append(button(r.run_id,()=>nav(r.run_id)),text('p',`${r.case||'订单报告'} · ${V.state(r.status)}`,V.tone(r.status)),
        text('p',r.created_at?new Date(r.created_at).toLocaleString():'开始时间未知'),
        text('p',(r.members||[]).map(m=>`${m.id}: ${m.provider_id} / ${m.model}`).join('；')),
        text('p',`最终评价：${finalText(r)} · Token：${V.tokens(r.usage)}`));
      if(r.error)row.append(text('p',r.error,'warning'));root.append(row);
    }
  }
  function finalText(d){const results=Object.values(d.final||{});return !results.length||results.some(r=>r.status!=='evaluated')?'未评价':results.every(r=>r.passed===true)?'业务评价通过':'业务评价未通过';}
  function detail(d){
    $e('exp-title').textContent=d.run_id;
    $e('exp-status').textContent=`${V.state(d.status)} · 最终评价：${finalText(d)} · 证据：${d.evidence_complete===false?'不完整':d.evidence_complete===true?'完整':'记录中/未知'}${d.execution_backend==='injected_client_factory'?' · 模拟客户端运行':''}`;
    $e('exp-stop').disabled=!['queued','running','stopping'].includes(d.status)||d.status==='stopping';
    const graph=$e('exp-graph');graph.replaceChildren();
    for(const nid of ['A','B','C','D']){
      const task=d.tasks?.find(t=>t.id===nid), member=d.members?.find(m=>m.id===task?.agent_id);
      const card=text('article','',`exp-node node-${nid} ${V.tone(d.nodes[nid]||'pending')}`);
      card.append(text('h3',`${nid} · ${{A:'清洗数据',B:'渠道统计',C:'质量汇总',D:'汇总报告'}[nid]}`),
        text('p',V.state(d.nodes[nid]||'pending')),text('p',`${member?.id||task?.agent_id||'—'} · ${member?.provider_id||'—'} / ${member?.model||'—'}`),
        text('p','实际返回模型：'+(d.node_models?.[nid]?.join(', ')||'未知')),text('p',V.evaluation(d.final?.[nid])));
      graph.append(card);
    }
    const usage=$e('exp-usage');usage.replaceChildren(text('p','Token：'+V.tokens(d.usage)));
    for(const g of d.usage||[]){usage.append(text('p',`${g.agent_id} · ${g.provider} / ${g.model} · 实际返回：${g.returned_models?.join(', ')||'未知'} · ${g.requests}次请求 · ${g.unknown_usage_requests}次用量未知`));
      const pre=text('pre',JSON.stringify(g.tokens||{},null,2));usage.append(pre);}
    const checks=$e('exp-checks');checks.replaceChildren();
    for(const nid of ['A','B','C','D']){
      const r=d.runtime?.[nid];checks.append(text('p',`${nid} 运行时检查：${r?r.passed?'产物契约通过':'产物契约失败':'未检查'}；${V.evaluation(d.final?.[nid])}`));
      if(r?.errors?.length)checks.append(text('pre',r.errors.join('\n')));
      if(d.final?.[nid]?.errors?.length)checks.append(text('pre',d.final[nid].errors.join('\n')));
    }
    const artifacts=$e('exp-artifacts');artifacts.replaceChildren();
    for(const [id,a] of Object.entries(d.artifacts||{})){
      const row=text('article','', 'exp-card'); row.append(text('strong',id),text('p',`SHA256：${a.sha256}`));
      for(const input of a.inputs||[])row.append(text('p',`输入 ${input.source} · SHA256 ${input.sha256}`));
      row.append(button('预览',()=>preview(id)),button('下载',()=>download(id)));artifacts.append(row);
    }
    if(!artifacts.children.length)artifacts.append(text('p','尚无通过产物契约检查的交付物。'));
    const faults=$e('exp-errors');faults.replaceChildren();
    if(d.error)faults.append(text('p',d.error));
    for(const r of d.request_errors||[])faults.append(text('p',`${r.node_id} · ${r.request_id} · ${r.error_type||r.status} · HTTP ${r.http_status??'未知'}`));
  }
  function log(item){
    const r=item.record, row=text('div','', 'exp-log-row');
    row.append(text('span',[r.timestamp,r.node_id,r.event_type,r.name,r.status,r.error_type,
      r.transport_attempt&&`尝试 ${r.transport_attempt}`,r.request_id&&`请求 ${r.request_id}`,r.tool_call_id&&`工具 ${r.tool_call_id}`].filter(Boolean).join(' · ')));
    row.append(button('展开记录',async()=>{
      try{const data=await api(`${url(selected)}/${stream}?cursor=${item.offset}&limit=1&raw=1`);
        const pre=text('pre',JSON.stringify(data.items[0]?.record,null,2));row.append(pre);
      }catch(e){error(e);}
    }));$e('exp-log').append(row);
  }
  async function preview(id){try{const d=await api(`${url(selected)}/artifact?id=${encodeURIComponent(id)}`);
    const panel=$e('exp-preview');panel.replaceChildren(text('h3',id),button('关闭预览',()=>{panel.hidden=true;}));
    if(id.endsWith('.md')){const div=document.createElement('div');div.append(renderMarkdown(d.content));panel.append(div);}
    else panel.append(text('pre',d.content));panel.hidden=false;
  }catch(e){error(e);}}
  async function download(id){try{
    const response=await fetch(`${url(selected)}/download?id=${encodeURIComponent(id)}`,{headers:authHeaders()});
    if(!response.ok)throw new Error((await response.json()).error||'下载失败');
    const blob=await response.blob(), address=URL.createObjectURL(blob), a=document.createElement('a');
    a.href=address;a.download=id.split('/').pop();a.click();setTimeout(()=>URL.revokeObjectURL(address),1000);
  }catch(e){error(e);}}
  async function newForm(){try{
    const d=await api('/api/models');const form=$e('exp-new');form.replaceChildren(text('h2','新建订单报告实验'));
    form.append(text('p','固定场景：清洗 → 渠道统计 / 质量汇总 → 报告。模型按明确配置执行，不自动替换。'));
    for(const id of ['data_worker','report_worker']){
      const field=document.createElement('fieldset');field.append(text('legend',id));
      const label=text('label','供应商 / 模型 '), select=document.createElement('select');select.name=id;
      for(const [i,m] of d.models.entries()){const option=text('option',`${m.provider_name} / ${m.model}`);option.value=String(i);select.append(option);}
      label.append(select);field.append(label);
      for(const [name,title,value,max] of [['max_rounds','最大轮数',12,100],['timeout','请求超时（秒）',60,600]]){
        const l=text('label',title+' '),input=document.createElement('input');input.type='number';input.min=1;input.max=max;input.value=value;input.name=`${id}-${name}`;input.required=true;l.append(input);field.append(l);
      }form.append(field);
    }
    const submit=text('button','启动实验');submit.type='submit';submit.disabled=!d.models.length;
    form.append(submit,button('取消',()=>form.hidden=true));if(!d.models.length)form.append(text('p','没有启用的模型，请先在聊天页配置模型。'));
    form.onsubmit=async event=>{event.preventDefault();if(busy)return;busy=true;submit.disabled=true;
      try{const agents=['data_worker','report_worker'].map(id=>{
        const m=d.models[Number(form.elements[id].value)];return {id,provider_id:m.provider_id,model:m.model,
          max_rounds:Number(form.elements[`${id}-max_rounds`].value),timeout:Number(form.elements[`${id}-timeout`].value)};
      });const result=await api('/api/experiments',{method:'POST',body:JSON.stringify({case:'order_report',agents})});nav(result.run_id);
      }catch(e){error(e);}finally{busy=false;submit.disabled=false;}};form.hidden=false;
  }catch(e){error(e);}}
  $e('exp-open').onclick=()=>nav();$e('exp-open-mobile').onclick=()=>nav();
  $e('exp-chat').onclick=()=>{location.hash='';};$e('exp-home').onclick=()=>nav();
  $e('exp-create').onclick=newForm;$e('exp-refresh').onclick=()=>update();$e('exp-more').onclick=()=>update();
  $e('exp-stream').onchange=e=>{stream=e.target.value;serial++;resetLogs();poll.start();};
  $e('exp-stop').onclick=async()=>{try{await api(`${url(selected)}/stop`,{method:'POST',body:'{}'});await update();}catch(e){error(e);}};
  addEventListener('hashchange',route);addEventListener('pagehide',close);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)poll.stop();else if(active)poll.start();});
  window.ExperimentsUI={close,route};route();
})();
