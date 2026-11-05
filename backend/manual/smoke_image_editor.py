"""Browser regression for image annotations, export pixels and modal isolation."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_session_model import ApiTestBase
from playwright.sync_api import sync_playwright, expect

case = ApiTestBase(); case.setUp()
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width":1280,"height":900})
        errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""
          window.EventSource=class {
            constructor(){setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'caught_up',running:false}),lastEventId:''}),0);}
            close(){} addEventListener(){}
          };
          Object.defineProperty(navigator,'clipboard',{value:{write:async items=>{
            window.copiedImage=await items[0].getType('image/png');
          }}});
        """)
        page.add_init_script("localStorage.setItem('auth_token'," + repr(case.token) + ");")
        page.goto(case.base); expect(page.locator("#layout")).to_be_visible()
        page.evaluate("""() => {
          const source=document.createElement('canvas');source.width=400;source.height=300;
          const c=source.getContext('2d');c.fillStyle='white';c.fillRect(0,0,400,300);
          window.originalImage=source.toDataURL();c.fillStyle='#00ff00';c.fillRect(0,0,400,300);
          window.secondImage=source.toDataURL();
          window.openTest=()=>openLightbox(originalImage,[originalImage,secondImage],0);
          openTest();
        }""")
        canvas = page.locator(".lb-canvas")
        expect(canvas).to_be_visible()
        page.wait_for_function("document.querySelector('.lb-canvas').width===400")
        assert page.locator("#layout").evaluate("el=>el.inert")
        page.get_by_role("button", name="方框", exact=True).click()
        def stroke(start, end):
            bounds = canvas.bounding_box()
            page.mouse.move(bounds["x"]+start[0],bounds["y"]+start[1]);page.mouse.down()
            page.mouse.move(bounds["x"]+end[0],bounds["y"]+end[1],steps=6);page.mouse.up()
        def pixel(x, y):
            return canvas.evaluate("(el,p)=>Array.from(el.getContext('2d').getImageData(p[0],p[1],1,1).data)", [x,y])
        stroke((80,60),(200,150))
        assert pixel(80,100)[:3] == [239,68,68]
        page.get_by_role("button",name="撤销标注",exact=True).click()
        assert pixel(80,100)[:3] == [255,255,255]
        page.get_by_role("button",name="重做标注",exact=True).click()
        assert pixel(80,100)[:3] == [239,68,68]
        page.get_by_role("button",name="复制图片（含标注）",exact=True).click()
        page.wait_for_function("window.copiedImage instanceof Blob")
        result = page.evaluate("""async()=>{
          const image=await createImageBitmap(copiedImage);const out=document.createElement('canvas');
          out.width=image.width;out.height=image.height;const c=out.getContext('2d');c.drawImage(image,0,0);
          return {width:out.width,pixel:Array.from(c.getImageData(80,100,1,1).data)};
        }""")
        assert result == {"width":400,"pixel":[239,68,68,255]}, result
        page.get_by_role("button",name="下一张图片",exact=True).click()
        expect(page.locator(".lb-status")).to_contain_text("2 / 2")
        assert pixel(80,100)[:3] == [0,255,0]
        page.get_by_role("button",name="上一张图片",exact=True).click()
        expect(page.locator(".lb-status")).to_contain_text("1 / 2")
        assert pixel(80,100)[:3] == [239,68,68]
        page.get_by_role("button",name="放大图片",exact=True).click()
        page.get_by_role("button",name="箭头",exact=True).click()
        stroke((125,225),(250,250))
        assert pixel(150,190)[:3] == [239,68,68], "zoomed coordinates must map to image pixels"
        page.get_by_role("button",name="恢复适合窗口大小",exact=True).click()
        page.get_by_role("button",name="文字",exact=True).click()
        bounds = canvas.bounding_box(); page.mouse.click(bounds["x"]+210,bounds["y"]+50)
        entry = page.get_by_role("textbox",name="标注文字")
        entry.fill("确认这里");entry.press("Enter")
        expect(entry).to_have_count(0)
        page.get_by_role("button",name="擦除标注（保留原图）",exact=True).click()
        bounds = canvas.bounding_box();page.mouse.click(bounds["x"]+80,bounds["y"]+100)
        assert pixel(80,100)[:3] == [255,255,255]
        page.get_by_role("button",name="撤销标注",exact=True).click()
        assert pixel(80,100)[:3] == [239,68,68]
        with page.expect_download() as download:
            page.get_by_role("button",name="下载图片（含标注）",exact=True).click()
        assert download.value.suggested_filename.endswith(".png")
        page.screenshot(path="/tmp/image-editor-desktop.png")
        page.set_viewport_size({"width":390,"height":844})
        expect(canvas).to_be_visible()
        page.wait_for_function("document.querySelector('.lb-canvas').getBoundingClientRect().width<=document.querySelector('.lb-viewport').clientWidth")
        assert page.locator(".lightbox").evaluate("el=>el.scrollWidth<=el.clientWidth")
        assert page.locator(".lb-toolbar").bounding_box()["x"] > canvas.bounding_box()["x"]
        page.screenshot(path="/tmp/image-editor-mobile.png")
        page.keyboard.press("Escape");expect(page.locator(".lightbox")).to_have_count(0)
        assert not page.locator("#layout").evaluate("el=>el.inert")
        page.evaluate("openTest()")
        expect(canvas).to_be_visible()
        assert pixel(80,100)[:3] == [255,255,255], "reopening must not modify original"
        page.keyboard.press("Escape")
        page.set_viewport_size({"width":1280,"height":900})
        page.evaluate("openTest()")
        expect(canvas).to_be_visible()
        page.get_by_role("button",name="画笔",exact=True).click()
        bounds = canvas.bounding_box()
        page.mouse.move(bounds["x"]+30,bounds["y"]+30);page.mouse.down()
        page.mouse.move(bounds["x"]+100,bounds["y"]+30,steps=5)
        assert pixel(50,30)[:3] == [239,68,68]
        canvas.dispatch_event("pointercancel", {"pointerId":1})
        page.mouse.up()
        assert pixel(50,30)[:3] == [255,255,255], "cancel must restore uncommitted stroke"
        stroke((30,30),(100,30))
        assert pixel(50,30)[:3] == [239,68,68]
        page.get_by_role("button",name="圆形",exact=True).click()
        stroke((150,100),(250,200))
        assert pixel(150,150)[:3] == [239,68,68]
        page.get_by_role("button",name="文字",exact=True).click()
        bounds = canvas.bounding_box();page.mouse.click(bounds["x"]+30,bounds["y"]+220)
        entry = page.get_by_role("textbox",name="标注文字")
        entry.fill("不保存这段");entry.press("Escape")
        expect(entry).to_have_count(0);expect(canvas).to_be_visible()
        page.evaluate("()=>{navigator.clipboard.write=async()=>{throw new Error('denied')}}")
        page.get_by_role("button",name="复制图片（含标注）",exact=True).click()
        expect(page.locator("#toast")).to_contain_text("复制图片失败")
        page.evaluate("window.backgroundEscape=0;document.addEventListener('keydown',e=>{if(e.key==='Escape')window.backgroundEscape++})")
        page.keyboard.press("Escape")
        assert page.evaluate("backgroundEscape") == 0, "modal shortcuts must not reach background"
        page.keyboard.press("Escape")
        assert page.evaluate("backgroundEscape") == 1, "modal listener must be removed on close"
        page.evaluate("openLightbox('data:image/png;base64,invalid')")
        expect(page.locator(".lb-status")).to_contain_text("图片加载失败")
        expect(page.get_by_role("button",name="复制图片（含标注）",exact=True)).to_be_disabled()
        page.keyboard.press("Escape")
        assert not errors, errors
        browser.close()
    print("PASS: annotate, undo/redo, image export pixels, gallery drafts, text, erase, zoom, modal and mobile")
finally:
    case.tearDown();case.doCleanups()
