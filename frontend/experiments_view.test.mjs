import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import fs from 'node:fs';
const V=createRequire(import.meta.url)('./experiments_view.js');
test('validated is a contract, never business success',()=>{
 assert.equal(V.state('validated'),'产物契约通过');
 assert.equal(V.evaluation({passed:true}),'未评价');
 assert.equal(V.evaluation({status:'evaluated',passed:false}),'业务评价未通过');
 for(const s of ['stopped','interrupted','corrupt','execution_failed'])assert.equal(V.tone(s),'warning');
});
test('unknown token counters do not become zero',()=>{
 assert.equal(V.tokens([]),'未知');
 assert.equal(V.tokens([{tokens:{},unknown_usage_requests:1}]),'未知');
 assert.match(V.tokens([{tokens:{prompt_tokens:10,completion_tokens:4},unknown_usage_requests:1}]),/小计.*未知/);
 assert.match(V.tokens([{tokens:{input_tokens:10,output_tokens:4},unknown_usage_requests:0}]),/14/);
});
test('poll cleanup prevents stale task from scheduling a second loop',async()=>{
 let resolve,scheduled=0;const clock={setTimeout(){scheduled++;return 1;},clearTimeout(){}};
 const poll=V.poller(()=>new Promise(r=>resolve=r),2000,clock);
 poll.start();poll.stop();resolve();await Promise.resolve();await Promise.resolve();assert.equal(scheduled,0);
});
test('artifact markdown reuses sanitization, HTML stays text, download uses auth header',()=>{
 const source=fs.readFileSync(new URL('./experiments.js',import.meta.url),'utf8');
 assert.match(source,/renderMarkdown\(d.content\)/);assert.match(source,/text\('pre',d.content\)/);
 assert.match(source,/headers:authHeaders\(\)/);assert.doesNotMatch(source,/innerHTML|iframe|srcdoc/);
 const md=fs.readFileSync(new URL('./md.js',import.meta.url),'utf8');assert.match(md,/sanitizeMarkdownHtml/);
});
