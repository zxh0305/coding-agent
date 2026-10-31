/* Pure presentation/poll lifecycle helpers, shared by browser and node tests. */
(function(root) {
  const names = {pending:'待执行', ready:'已就绪', running:'执行中', produced:'已产出，待检查',
    validated:'产物契约通过', execution_failed:'执行失败', validation_failed:'产物契约失败',
    blocked:'依赖阻塞', stopped:'已停止', interrupted:'已中断', queued:'排队中',
    stopping:'正在停止', completed:'执行已结束', corrupt:'记录损坏'};
  function state(value) { return names[value] || '未知状态'; }
  function evaluation(result) {
    return result?.status !== 'evaluated' ? '未评价' : result.passed === true ? '业务评价通过' : '业务评价未通过';
  }
  function tokens(groups) {
    if (!groups?.length) return '未知';
    let sum = 0, reported = false, unknown = false;
    for (const g of groups) {
      const t = g.tokens || {};
      const input = t.prompt_tokens ?? t.input_tokens;
      const output = t.completion_tokens ?? t.output_tokens;
      if (Number.isFinite(input) && Number.isFinite(output)) { sum += input + output; reported = true; }
      else unknown = true;
      if (g.unknown_usage_requests > 0) unknown = true;
    }
    return reported ? `${sum.toLocaleString()}${unknown ? '（已报告小计；其余未知）' : '（输入＋输出已报告小计）'}` : '未知';
  }
  function tone(status) { return status === 'validated' ? 'contract' : /failed|corrupt|interrupted|blocked|stopped/.test(status) ? 'warning' : ''; }
  function poller(work, delay=2000, clock=globalThis) {
    let timer=null, generation=0;
    async function tick(g) {
      try { await work(() => g === generation); }
      finally { if(g === generation) timer=clock.setTimeout(() => tick(g), delay); }
    }
    return {start() { this.stop(); tick(generation); }, stop() { generation++; if(timer!==null) clock.clearTimeout(timer); timer=null; }};
  }
  const value = {state,evaluation,tokens,tone,poller};
  if(typeof module !== 'undefined') module.exports=value;
  else root.ExperimentsView=value;
})(globalThis);
