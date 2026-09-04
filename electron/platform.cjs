// A dedicated, visible browser session. Only responses from the page the user opens are collected.
const { BrowserWindow, session } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
let active = null;
function platformURL(value) {
  const u = new URL(value);
  if (u.protocol !== 'https:' || !(u.hostname === 'douyin.com' || u.hostname.endsWith('.douyin.com'))) throw new Error('平台浏览器目前用于抖音主页和作品链接');
  return u.href;
}
function normalize(a) {
  const stats = a.statistics || {}, video = a.video || {};
  const address = video.play_addr || video.bit_rate?.[0]?.play_addr || {};
  return { key: a.aweme_id, title: a.desc || '未命名作品', url: `https://www.douyin.com/video/${a.aweme_id}`,
    author: a.author?.nickname || '', date: a.create_time ? new Date(a.create_time * 1000).toISOString().slice(0,10).replaceAll('-','') : '',
    duration: (video.duration || a.duration || 0) / 1000, likes: stats.digg_count, views: stats.play_count,
    collections: stats.collect_count, comments: stats.comment_count, shares: stats.share_count,
    direct_url: address.url_list?.[0] || '', cover_url: (video.cover || video.origin_cover)?.url_list?.[0] || '', provider: 'douyin' };
}
async function capture({ url, limit = 100, collectionId = '', loginOnly = false }, baseURL, dataDir) {
  platformURL(url);
  if (active && !active.isDestroyed()) { active.focus(); throw new Error('已有平台窗口，请先完成或关闭它'); }
  limit = Math.max(1, Math.min(10000, Number(limit) || 100));
  if (collectionId && !/^[a-f0-9]{32}$/.test(collectionId)) throw new Error('无效采集任务');
  const ses = session.fromPartition('persist:lulu-platform-douyin');
  ses.setPermissionRequestHandler((_c,_p,cb) => cb(false));
  const win = new BrowserWindow({ width:1100,height:800,show:true,autoHideMenuBar:true,title:'Lulu · 抖音登录与采集（关闭窗口结束）',
    webPreferences:{session:ses,nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true} });
  active=win;win.removeMenu();
  win.webContents.setWindowOpenHandler(() => ({action:'deny'}));
  win.webContents.on('will-navigate',(e,destination) => { try {platformURL(destination);} catch {e.preventDefault();} });
  const entries=new Map(), requests=new Set(), savedKeys=new Set(); let ended=false, serverDone=false, lastNew=Date.now(), saveChain=Promise.resolve(), saveError='', pages=0, awaitingLogin=false;
  async function persist(status='running') {
    if (!collectionId) return;
    const list=[...entries.values()].filter(e=>!savedKeys.has(e.key));
    for(let offset=0;offset<Math.max(1,list.length);offset+=500){
      const batch=list.slice(offset,offset+500);
      const r=await fetch(`${baseURL}/api/collections/${collectionId}/capture`,{method:'POST',headers:{'Content-Type':'application/json','X-Lulu-Client':'desktop'},
        body:JSON.stringify({entries:batch,status,error:saveError,title:list[0]?.author ? list[0].author+' · 主页采集' : undefined})});
      if(!r.ok)throw new Error('保存采集结果失败，请保持窗口打开并重试');
      batch.forEach(e=>savedKeys.add(e.key));
    }
  }
  async function saveCookies() {
    const cookies=await ses.cookies.get({});
    const allowed=cookies.filter(c=>c.domain==='douyin.com'||c.domain.endsWith('.douyin.com'));
    if(!allowed.length)return;
    const body='# Netscape HTTP Cookie File\n'+allowed.map(c=>[c.httpOnly?'#HttpOnly_'+c.domain:c.domain,c.domain.startsWith('.')?'TRUE':'FALSE',c.path||'/',c.secure?'TRUE':'FALSE',Math.floor(c.expirationDate||Date.now()/1000+86400),c.name,c.value].join('\t')).join('\n');
    const form=new FormData();form.append('file',new Blob([body]),'cookies.txt');
    const r=await fetch(baseURL+'/api/cookies',{method:'POST',headers:{'X-Lulu-Client':'desktop'},body:form});
    if(!r.ok)throw new Error('平台登录文件保存失败');
  }
  if(!loginOnly){
    // A BrowserWindow initially has no initialized renderer target on Windows.
    await win.loadURL('about:blank');
    try {
      win.webContents.debugger.attach('1.3');
      await win.webContents.debugger.sendCommand('Network.enable');
    } catch(error) {
      if(!win.isDestroyed())win.close();active=null;
      if(collectionId){saveError='平台窗口初始化失败，请重新打开';await persist('error').catch(()=>{});}
      throw new Error('平台窗口初始化失败：'+error.message);
    }
    win.webContents.debugger.on('message',async(_e,method,params)=>{
      if(method==='Network.responseReceived'){
        try {const u=new URL(params.response.url);if(u.hostname.endsWith('douyin.com') && /\/aweme\/v\d+\/web\/aweme\/(post|detail)\//.test(u.pathname)) requests.add(params.requestId);}catch{}
      }
      if(method!=='Network.loadingFinished'||!requests.delete(params.requestId)||ended)return;
      try {
        const response=await win.webContents.debugger.sendCommand('Network.getResponseBody',{requestId:params.requestId});
        const data=JSON.parse(response.base64Encoded?Buffer.from(response.body,'base64').toString('utf8'):response.body);
        const list=data.aweme_list || (data.aweme_detail?[data.aweme_detail]:[]);
        const currentURL = new URL(win.webContents.getURL());
        const expectedAuthor = currentURL.pathname.match(/^\/user\/([^/]+)/)?.[1];
        const expectedVideo = currentURL.pathname.match(/^\/video\/(\d+)/)?.[1];
        const before=entries.size;
        for(const a of list){
          if(expectedAuthor && a.author?.sec_uid && a.author.sec_uid!==expectedAuthor)continue;
          if(expectedVideo && a.aweme_id!==expectedVideo)continue;
          if(a.aweme_id && !entries.has(a.aweme_id) && entries.size<limit){entries.set(a.aweme_id,normalize(a));lastNew=Date.now();}
        }
        if(entries.size>before)pages++;
        if(entries.size && saveError==='未能读取平台返回的作品信息，请刷新页面重试')saveError='';
        if(Object.hasOwn(data,'has_more')) serverDone=!data.has_more;
        saveChain=saveChain.then(()=>persist()).catch(e=>{saveError=e.message;});
        if(entries.size>=limit || (serverDone && entries.size) || (!collectionId&&entries.size)) setTimeout(()=>{if(!win.isDestroyed())win.close();},500);
      }catch(e){if(!ended&&!entries.size)saveError='未能读取平台返回的作品信息，请刷新页面重试';}
    });
  }
  const timer=setInterval(async()=>{
    if(ended||win.isDestroyed()||loginOnly)return;
    try {
      const login=await win.webContents.executeJavaScript(`document.body.innerText.includes('扫码登录') && (document.body.innerText.includes('验证码登录') || document.body.innerText.includes('登录后'))`);
      if(login){
        lastNew=Date.now();
        if(!awaitingLogin){awaitingLogin=true;saveError='平台要求登录。请在打开的抖音窗口扫码或登录后继续采集。';saveChain=saveChain.then(()=>persist('waiting-login')).catch(e=>{saveError=e.message;});}
        return;
      }
      if(awaitingLogin){awaitingLogin=false;saveError='';lastNew=Date.now();saveChain=saveChain.then(()=>persist()).catch(e=>{saveError=e.message;});}
    } catch {}
    // Ordinary visible-page scrolling requests subsequent pages; no signature spoofing or challenge bypass.
    if(entries.size && !serverDone){
      try{await win.webContents.executeJavaScript(`(() => { const list = [...document.querySelectorAll('*')].filter(e => e.scrollHeight > e.clientHeight + 300 && e.clientHeight > 200 && /auto|scroll/.test(getComputedStyle(e).overflowY)); const e = list.sort((a,b) => b.clientHeight-a.clientHeight)[0] || document.scrollingElement; e.scrollTo(0,e.scrollHeight); })()`);}catch{}
    }
    if(Date.now()-lastNew>90000 && entries.size){saveError='平台未继续返回作品；已保存当前结果，可重新采集补充。';win.close();}
  },2500);
  const result=new Promise(resolve=>win.on('closed',async()=>{
    ended=true;clearInterval(timer);active=null;
    try{await saveCookies();await saveChain;await persist(serverDone||entries.size>=limit?'done':'paused');}catch(e){saveError=e.message;}
    resolve({entries:[...entries.values()],complete:serverDone||entries.size>=limit,pages,error:saveError||(!entries.size&&!loginOnly?'没有获取到作品。请在平台窗口完成登录或验证后重试。':'')});
  }));
  win.loadURL(url).catch(()=>{saveError='平台页面加载失败，请检查网络后重试';});
  return result;
}
function stop(){if(active&&!active.isDestroyed())active.close();}
module.exports={capture,stop,normalize};
