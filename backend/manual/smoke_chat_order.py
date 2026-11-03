"""Deterministic browser regressions for replay and session navigation races.
Runs against an isolated temporary DB; no model API calls or user-data changes.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright, expect

FAKE_SSE = '''
window.sources = []; window.autoCatchup = true;
window.EventSource = class {
  constructor(url) { this.url=url; this.closed=false; sources.push(this);
    if(window.autoCatchup) setTimeout(()=>{if(!this.closed)this.emit({type:'caught_up',running:false});},0);
  }
  addEventListener() {} close(){this.closed=true;}
  emit(evt, seq) {if(!this.closed)this.onmessage?.({data:JSON.stringify(evt),lastEventId:seq==null?'':String(seq)});}
};
window.emit = (evt,seq) => sources.filter(s=>!s.closed).at(-1).emit(evt,seq);
'''
case = ApiTestBase(); case.setUp()
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        def new_page():
            pg = browser.new_page(viewport={'width':1300,'height':900})
            pg.add_init_script(FAKE_SSE)
            pg.add_init_script('localStorage.setItem("auth_token", '+repr(case.token)+');localStorage.setItem("auth_username","tester");')
            pg.goto(case.base)
            pg.wait_for_function('typeof SkillUI !== "undefined" && !document.querySelector("#layout").classList.contains("hidden")')
            return pg
        def emit(pg, evt, seq): pg.evaluate('([e,s])=>window.emit(e,s)',[evt,seq])
        failures = []
        # An offline turn finishes while B is queued. A must finish rendering before B dispatches.
        page = new_page(); errors=[]; page.on('pageerror', lambda e: errors.append(str(e)))
        page.evaluate('switchSession("s1")')
        page.wait_for_function('bootBuffer === null')
        sent=[]
        def submit(route):
            sent.append(route.request.post_data_json)
            route.fulfill(status=200,json={'session_id':'s1'})
        page.route('**/api/sessions/s1/messages', submit)
        page.locator('#input').fill('提问A')
        with page.expect_response(lambda r:r.request.method=='POST' and '/s1/messages' in r.url): page.locator('#input').press('Enter')
        nonce=sent[0]['nonce']
        start={'type':'turn_start','nonce':nonce,'user_mid':'uA','input':'提问A','started_at':1793692981}
        emit(page,start,1);emit(page,{'type':'round','round':1,'mid':'aA'},2)
        emit(page,{'type':'answer_delta','mid':'aA','delta':'回答A'},3)
        page.evaluate('flushStreamBuffers()')
        page.locator('#input').fill('提问B');page.locator('#input').press('Enter')
        expect(page.locator('#queue-bar')).to_contain_text('提问B')
        page.evaluate('autoCatchup=false;openEvents("s1",true)')
        for seq, evt in enumerate([start,{'type':'round','round':1,'mid':'aA'}, {'type':'answer_delta','mid':'aA','delta':'回答A'}, {'type':'done','mid':'aA','answer':'回答A','elapsed_s':1,'usage':{}}, {'type':'turn_end','user_mid':'uA'}],1): emit(page,evt,seq)
        emit(page,{'type':'caught_up','running':False},None)
        page.wait_for_function('pendingQueue.length === 0')
        observed=page.locator('#chat .bubble').all_text_contents()
        print('reconnect DOM:',observed)
        users=page.locator('#chat .bubble.user').all_text_contents()
        if users.count('提问A')!=1: failures.append('replay duplicates optimistic user A')
        order=[t.strip() for t in observed if t.strip() in ('提问A','提问B','回答A')]
        if order!=['提问A','回答A','提问B']: failures.append('queued B rendered before recovered A answer')
        if '连接中断，本回合未完成' in page.locator('#chat').inner_text(): failures.append('false interruption displayed for completed turn')
        if page.locator('#chat details.trace').count()!=2: failures.append('reconnect creates duplicate trace cards')
        page.close()
        # A lost reply must retain the entire queued submission and leave the next draft alone.
        page = new_page()
        page.evaluate('switchSession("s1")')
        page.wait_for_function('bootBuffer === null && !workspacePending')
        page.evaluate("""async () => {
          await queueMessage('队列中的消息', [{kind:'file', name:'a.txt', data:'YWJj'}],
                             [{id:'review', version:'original'}]);
        }""")
        page.locator('#input').fill('新草稿')
        requests = []
        def lose_reply(route):
            requests.append(route.request.post_data_json)
            route.abort()
        page.route('**/api/sessions/s1/messages', lose_reply)
        page.evaluate('dispatchNextQueued()')
        expect(page.locator('#queue-bar')).to_contain_text('待确认')
        expect(page.locator('#input')).to_have_value('新草稿')
        page.evaluate('saveDraft(currentSession)')
        page.reload()
        page.wait_for_function('!document.querySelector("#layout").classList.contains("hidden")')
        page.evaluate('switchSession("s1")')
        expect(page.locator('#queue-bar')).to_contain_text('待确认')
        expect(page.locator('#input')).to_have_value('新草稿')
        page.unroute('**/api/sessions/s1/messages', lose_reply)
        def acknowledge(route):
            requests.append(route.request.post_data_json)
            route.fulfill(json={'session_id':'s1', 'replayed':True})
        page.route('**/api/sessions/s1/messages', acknowledge)
        page.locator('#queue-bar').get_by_text('重试').click()
        page.wait_for_function('inFlight.size === 0 && pendingQueue.length === 0')
        assert len(requests) == 2 and requests[0] == requests[1]
        assert requests[1]['attachments'][0]['data'] == 'YWJj'
        assert requests[1]['skills'][0]['version'] == 'original'
        expect(page.locator('#input')).to_have_value('新草稿')
        page.close()
        # A running reconnect must retain already displayed deltas and append only new ones.
        page=new_page();page.evaluate('switchSession("s1")');page.wait_for_function('bootBuffer === null');sent=[]
        page.route('**/api/sessions/s1/messages',submit)
        page.locator('#input').fill('进行中的提问')
        with page.expect_response(lambda r:r.request.method=='POST' and '/s1/messages' in r.url): page.locator('#input').press('Enter')
        start={'type':'turn_start','nonce':sent[0]['nonce'],'user_mid':'uRunning','input':'进行中的提问'}
        first=[start,{'type':'round','round':1,'mid':'aRunning'},{'type':'answer_delta','mid':'aRunning','delta':'前半'}]
        for seq,evt in enumerate(first,1):emit(page,evt,seq)
        page.evaluate('flushStreamBuffers();autoCatchup=false;openEvents("s1",true)')
        for seq,evt in enumerate(first,1):emit(page,evt,seq)
        emit(page,{'type':'answer_delta','mid':'aRunning','delta':'后半'},4)
        emit(page,{'type':'caught_up','running':True},None);page.evaluate('flushStreamBuffers()')
        if page.locator('#chat .process-text').all_text_contents()!=['前半后半']: failures.append('running reconnect duplicates answer prefix')
        if page.locator('#chat .bubble.user').count()!=1:failures.append('running reconnect duplicates user')
        if page.locator('#chat details.trace').count()!=1:failures.append('running reconnect duplicates trace')
        emit(page,{'type':'done','mid':'aRunning','answer':'前半后半','usage':{},'elapsed_s':1},5)
        emit(page,{'type':'turn_end','user_mid':'uRunning'},6)
        page.close()
        # History may contain the input while its answer was completed just after the fetch.
        page=new_page()
        page.route('**/api/sessions/s1/messages?*',lambda route:route.fulfill(json={'messages':[{'role':'user','mid':'uSaved','ord':1024,'content':'已保存提问','turn':{'state':'running'}}],'has_more':False,'user_index':[]}))
        page.evaluate('autoCatchup=false;switchSession("s1")')
        for seq,evt in enumerate([{'type':'turn_start','user_mid':'uSaved','input':'已保存提问'}, {'type':'round','round':1,'mid':'aSaved'}, {'type':'done','mid':'aSaved','answer':'稍后完成的回答','usage':{},'elapsed_s':1}, {'type':'turn_end','user_mid':'uSaved'}],1):emit(page,evt,seq)
        emit(page,{'type':'caught_up','running':False},None)
        if page.locator('#chat .bubble.user').count()!=1:failures.append('saved input duplicated on replay')
        if '稍后完成的回答' not in page.locator('#chat').inner_text():failures.append('saved input incorrectly suppresses completed answer')
        page.close()
        # A slow workspace response cannot enable sending or relabel another project.
        page = new_page()
        held = []
        page.route('**/api/workspace?session_id=s1', lambda route: held.append(route))
        page.route('**/api/workspace?session_id=s2', lambda route: route.fulfill(
            json={'custom': True, 'path': '/project/B'}))
        page.evaluate('void (window.navA = switchSession("s1"))')
        page.wait_for_function('workspacePending && !chatLoading')
        expect(page.locator('#send')).to_be_disabled()
        page.locator('#input').fill('A 的草稿')
        page.evaluate('switchSession("s2")')
        expect(page.locator('#ws-short')).to_have_text('B')
        held[0].fulfill(json={'custom': True, 'path': '/project/A'})
        page.evaluate('window.navA')
        expect(page.locator('#ws-short')).to_have_text('B')
        page.close()
        # A slow history request must not overwrite B or reopen A's event source.
        page=new_page();held=[]
        def history(route):
            if '/s1/' in route.request.url: held.append(route)
            else: route.fulfill(json={'messages':[{'role':'user','mid':'uB','ord':1024,'content':'历史B'}], 'has_more':False,'user_index':[]})
        page.route('**/api/sessions/*/messages?*',history)
        page.evaluate('void (window.navA = switchSession("s1"))')
        page.wait_for_function('currentSession === "s1"')
        # Pump browser events until Playwright has intercepted the request.
        page.locator('#input').input_value()
        assert held, 'history A was not intercepted'
        page.evaluate('switchSession("s2")')
        expect(page.locator('#chat')).to_contain_text('历史B')
        held[0].fulfill(json={'messages':[{'role':'user','mid':'uA','ord':1024,'content':'历史A'}], 'has_more':False,'user_index':[]})
        page.evaluate('window.navA')
        page.locator('#input').input_value()
        print('navigation DOM:',page.locator('#chat .bubble').all_text_contents())
        page.wait_for_function('sources.some(s=>!s.closed)')
        current=page.evaluate('sources.filter(s=>!s.closed).at(-1).url')
        if '历史A' in page.locator('#chat').inner_text(): failures.append('late A history contaminates B')
        if '/s2/events' not in current: failures.append('late A navigation replaces B event source')
        page.close()
        # Failed old history requests are stale too: no error bubble in the selected view.
        page=new_page(); held=[]
        page.route('**/api/sessions/*/messages?*',history)
        page.evaluate('void (window.navA = switchSession("s1"))')
        page.evaluate('switchSession("s2")');assert held
        held[0].fulfill(status=500,json={'error':'OLD_HISTORY_FAILURE'})
        page.evaluate('window.navA')
        if 'OLD_HISTORY_FAILURE' in page.locator('#chat').inner_text():failures.append('late history error contaminates new view')
        page.close()
        # A rejected old submission must not append an error or reset the new view.
        page=new_page();page.evaluate('switchSession("s1")');page.wait_for_function('bootBuffer === null')
        held_posts=[]
        page.route('**/api/sessions/s1/messages', lambda route: held_posts.append(route))
        page.locator('#input').fill('旧会话正在发送')
        with page.expect_request(lambda r: r.method == 'POST' and '/s1/messages' in r.url):
            page.locator('#input').press('Enter')
        page.evaluate('switchSession("s2")')
        assert held_posts
        with page.expect_response(lambda r:r.request.method=='POST' and '/s1/messages' in r.url):
            held_posts[0].fulfill(status=400,json={'error':'OLD_SUBMIT_FAILURE'})
        page.evaluate('new Promise(resolve => requestAnimationFrame(resolve))')
        page.wait_for_function('currentSession === "s2" && !streaming')
        page.locator('#input').input_value()
        if 'OLD_SUBMIT_FAILURE' in page.locator('#chat').inner_text(): failures.append('old POST error contaminates new view')
        if page.locator('#input').input_value(): failures.append('old POST restores text into another session')
        page.close()
        # A newly-created session response cannot hijack a view selected meanwhile.
        page=new_page();page.evaluate('newTask()');held_create=[]
        def create(route):
            if route.request.method=='POST': held_create.append(route)
            else: route.continue_()
        page.route('**/api/sessions',create)
        page.locator('#input').fill('创建中的提问')
        with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/sessions')):
            page.locator('#input').press('Enter')
        page.evaluate('switchSession("s2")');assert held_create
        with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/api/sessions')):
            held_create[0].fulfill(json={'session_id':'s1'})
        # Wait for the response continuation, not an arbitrary timeout.
        page.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        if page.evaluate('currentSession')!='s2': failures.append('late create response hijacks selected session')
        if '/s2/events' not in page.evaluate('sources.filter(s=>!s.closed).at(-1).url'): failures.append('late create response replaces selected stream')
        page.close()
        browser.close()
        assert not errors, errors
        assert not failures, failures
        print('PASS: reconnect ordering, duplicate suppression, trace reuse, navigation isolation, late POST/create isolation')
finally:
    case.tearDown();case.doCleanups()
