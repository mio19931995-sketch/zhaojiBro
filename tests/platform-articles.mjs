// Platform windows, real IPC/worker/archive/fullscreen. Offline pages contain no user data.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { _electron as electron } from 'playwright';
const root=process.cwd(), data=await fs.mkdtemp(path.join(root,'.test-data/platform-articles-'));
const env={...process.env,LULU_DATA_DIR:data,LULU_USER_DATA:process.env.LULU_ARTICLE_TEST_PROFILE || path.join(data,'profile')};
const liveUrls=process.env.LULU_ARTICLE_TEST_URLS ? JSON.parse(process.env.LULU_ARTICLE_TEST_URLS) : process.env.LULU_ARTICLE_TEST_URL ? [process.env.LULU_ARTICLE_TEST_URL] : [];
const live=liveUrls.length>0;
const urls=live?liveUrls:['https://zhuanlan.zhihu.com/p/123456','https://www.toutiao.com/article/123456/','https://www.xiaohongshu.com/explore/1234567890abcdef12345678?xsec_token=synthetic'];
const png='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMuoAAAAASUVORK5CYII=';
let app,page,base;
const results=[],errors=[];
async function waitFor(fn,label,timeout=30000){const end=Date.now()+timeout;while(Date.now()<end){if(await fn())return;await new Promise(r=>setTimeout(r,150));}throw Error('Timed out: '+label);}
async function api(url,method='GET',body){const r=await fetch(base+'/api'+url,{method,headers:{'X-Lulu-Client':'desktop','Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});assert(r.ok,await r.clone().text());return r.json();}
try{
 app=await electron.launch({args:['electron/main.cjs'],env});page=await app.firstWindow();page.on('pageerror',e=>errors.push(e.message));await page.getByLabel('作品链接').waitFor();base=new URL(page.url()).origin;
 if(!live)await app.evaluate(({session},png)=>{
  for(const key of ['zhihu','toutiao','xiaohongshu'])session.fromPartition('persist:lulu-article-'+key).protocol.handle('https',()=>{
   const content='<h1 id="detail-title">测试文章</h1><p id="detail-desc" style="font-family:Georgia;font-size:23px;line-height:37px;color:rgb(12,34,56)">第一段繁體原文。<br>第二行内容。<img id="inline-emoji" src="'+png+'" style="width:18px;height:18px;display:inline"></p><p style="height:1500px">长文滚动。</p>';
   const image='<img src="'+png+'">';
   const body=key==='zhihu'?'<div><picture>'+image+'</picture></div><article class="Post-Main">'+content+image+'</article>':key==='toutiao'?'<div class="article-content">'+content+image+'</div>':'<div id="noteContainer"><div class="media-container"><div class="swiper-slide" data-swiper-slide-index="0"><img class="note-slider-img" src="https://ci.xhscdn.com/test1.png"></div><div class="swiper-slide" data-swiper-slide-index="1"><img class="note-slider-img" src="https://ci.xhscdn.com/test2.png"></div></div><div class="interaction-container">'+content+'</div></div>';
   return new Response('<!doctype html><html><head><title>测试文章</title></head><body>'+body+'</body></html>',{headers:{'content-type':'text/html'}});
  });
 },png);
 for(const url of urls){
  const provider=url.includes('zhihu')?'zhihu':url.includes('toutiao')?'toutiao':'xiaohongshu';
  await page.getByLabel('作品链接').fill(url);const imported=page.waitForResponse(r=>r.url().endsWith('/api/links')&&r.request().method()==='POST');await page.getByRole('button',{name:'抓取',exact:true}).click();const item=(await(await imported).json())[0],id=item.id;
  const row=page.locator('.task-row').filter({hasText:item.title}).first();
  const windowEvent=app.waitForEvent('window');await row.getByRole('button',{name:'在平台窗口抓取',exact:true}).click();const platformPage=await windowEvent;
  if(!live&&provider==='xiaohongshu'){
   // Only the isolated test worker serves synthetic image URLs via a capture rewrite.
   await platformPage.waitForLoadState('domcontentloaded');
   await platformPage.evaluate(png=>document.querySelectorAll('img').forEach((img,i)=>{img.src=i?png.replace('image/png;', 'image/png;charset=utf-8;'):png;}),png);
  }
  try{await waitFor(async()=>{const v=await api('/items/'+id);if(v.status==='error')throw Error(v.error);return v.status==='done';},'capture and archive',live?240000:45000);}catch(e){
   if(!platformPage.isClosed()){await platformPage.screenshot({path:path.join(data,provider+'-blocked.png')});console.log('Platform state',provider,await platformPage.evaluate(()=>({url:location.pathname,title:document.title,excerpt:document.body.innerText.slice(0,180)})));await platformPage.close();}throw e;
  }
  const saved=await api('/items/'+id),view=saved.metadata.article_view;assert(view?.saved_images>0,JSON.stringify(view));
  assert.equal(view.missing_images,0,'All source images must be saved');
  assert.equal(view.missing_styles,0,'All source styles must be saved');
  assert.equal(view.missing_resources,0,'No unresolved source resources');
  assert(saved.transcript.length > 10, 'The captured body must retain the visible article text');
  await page.locator('.task-row').filter({hasText:saved.title}).first().getByRole('button',{name:'查看',exact:true}).click();
  const frame=page.frameLocator('iframe[title="文章原文图文"]');await frame.locator('#lulu-article-root').waitFor();
  await waitFor(()=>frame.locator('img').evaluateAll(images=>images.length>0&&images.every(img=>img.complete&&img.naturalWidth>0)),'decoded local images');
  if(!live){const style=await frame.locator('#detail-desc').evaluate(el=>{const s=getComputedStyle(el);return [s.fontSize,s.lineHeight,s.color];});assert.deepEqual(style,['23px','37px','rgb(12, 34, 56)']);assert.equal(await frame.locator('#inline-emoji').evaluate(el=>getComputedStyle(el).width),'18px');}
  if(!live && provider==='zhihu')assert.equal(await frame.locator('[data-lulu-cover]').count(),1);
  await page.getByRole('button',{name:'全屏浏览',exact:true}).click();await waitFor(()=>app.evaluate(({BrowserWindow,screen})=>{const w=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http://127.0.0.1')),b=w.getBounds(),d=screen.getDisplayMatching(b).bounds;return w.isFullScreen()&&b.width>=d.width&&b.height>=d.height;}),'fullscreen monitor');
  await page.screenshot({path:path.join(data,provider+'-fullscreen.png')});
  await frame.locator('#lulu-article-root').click({position:{x:5,y:5}});await page.keyboard.press('PageDown');await page.keyboard.press('Escape');await page.getByRole('button',{name:'全屏浏览',exact:true}).waitFor();
  if(provider==='xiaohongshu'&&await frame.getByRole('button',{name:'下一张'}).count()){await frame.getByRole('button',{name:'下一张'}).click();assert((await frame.locator('[data-lulu-gallery]').innerText()).includes('2 /'));}
  results.push({provider,images:view.image_count,saved:view.saved_images,missing_styles:view.missing_styles,missing_resources:view.missing_resources,fullscreen:true});
 }
 assert.deepEqual(errors,[]);console.log(JSON.stringify({live:!!live,results,errors,evidence_dir:data},null,2));await fs.writeFile(path.join(data,'results.json'),JSON.stringify({live:!!live,results,errors},null,2));
}finally{if(app)await app.close();}
