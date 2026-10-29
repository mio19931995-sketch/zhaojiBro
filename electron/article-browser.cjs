const { BrowserWindow, Menu, session, dialog } = require('electron');
const { snapshot } = require('./article-page.cjs');
const active = new Map();
const platforms = { zhihu:['zhihu.com'], toutiao:['toutiao.com'], xiaohongshu:['xiaohongshu.com','xhslink.com'] };
function provider(url) {
  const u = new URL(url);
  if(u.protocol !== 'https:' || u.username || u.password || u.port) throw Error('无效文章链接');
  const key = Object.keys(platforms).find(k=>platforms[k].some(h=>u.hostname===h||u.hostname.endsWith('.'+h)));
  if(!key) throw Error('不支持的文章平台'); return key;
}
async function capture(id, viewOnly, baseURL) {
  if(!/^[a-f0-9]{32}$/.test(id)) throw Error('无效素材');
  const response=await fetch(baseURL+'/api/items/'+id);
  if(!response.ok)throw Error('找不到文章');
  const item=await response.json(), key=provider(item.source_url);
  const existing=active.get(key);
  if(existing&&!existing.isDestroyed()){existing.focus();throw Error('请先完成或关闭该平台已经打开的文章窗口');}
  const ses=session.fromPartition('persist:lulu-article-'+key);
  ses.setPermissionRequestHandler((_c,_p,cb)=>cb(false));ses.setPermissionCheckHandler(()=>false);
  const win=new BrowserWindow({width:1180,height:860,show:true,autoHideMenuBar:false,title:'Lulu · 打开文章后自动保存；需要登录时请自行完成',
    webPreferences:{session:ses,nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true}});
  active.set(key,win);
  // Use the embedded Chromium browser identity; site login pages need a normal desktop UA.
  win.webContents.setUserAgent(win.webContents.getUserAgent().replace(/\sElectron\/\S+/g,'').replace(/\slulu-workbench\/\S+/ig,''));
  win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  const guard=(e,url)=>{try{if(provider(url)!==key)e.preventDefault();}catch{e.preventDefault();}};
  win.webContents.on('will-navigate',guard);win.webContents.on('will-redirect',guard);
  const denyDownload=e=>e.preventDefault();ses.on('will-download',denyDownload);
  let done=false,busy=false,message='文章窗口已关闭，未保存内容',timer,attempts=0;
  let settle;
  const result=new Promise(resolve=>{settle=resolve;});
  async function save(manual=false){
    if(done||busy||win.isDestroyed())return;
    busy=true;
    try{
      if(provider(win.webContents.getURL())!==key)throw Error('请打开原文章');
      const value=await win.webContents.executeJavaScript(`(${snapshot.toString()})(${JSON.stringify(key)})`);
      if(value.error){message=value.error;if(manual)await dialog.showMessageBox(win,{type:'info',message});return;}
      if(value.media&&viewOnly)throw Error('这是视频笔记，请从任务队列按视频方式处理');
      const r=await fetch(`${baseURL}/api/items/${id}/article-capture`,{method:'POST',headers:{'Content-Type':'application/json','X-Lulu-Client':'desktop'},body:JSON.stringify({...value,view_only:viewOnly})});
      if(!r.ok)throw Error((await r.json()).detail||'保存文章失败');
      done=true;settle({ok:true,media:!!value.media});win.close();
    }catch(e){message=e.message;if(manual&&!win.isDestroyed())await dialog.showMessageBox(win,{type:'error',message});}
    finally{busy=false;}
  }
  win.setMenu(Menu.buildFromTemplate([{label:'保存当前文章',click:()=>save(true)},
    {label:'登录平台',click:()=>{void win.loadURL('https://'+(key==='zhihu'?'www.zhihu.com/signin':key==='toutiao'?'www.toutiao.com':'www.xiaohongshu.com')).catch(()=>{});}},
    {label:'返回原文',click:()=>{attempts=0;void win.loadURL(item.source_url).catch(()=>{});}},
    {label:'重新加载',click:()=>{attempts=0;win.webContents.reload();}},{label:'取消',click:()=>win.close()}]));
  win.on('closed',()=>{clearInterval(timer);ses.removeListener('will-download',denyDownload);active.delete(key);if(!done)settle({ok:false,error:message});});
  win.webContents.on('did-finish-load',()=>{attempts=0;});
  timer=setInterval(()=>{if(++attempts<=30)void save();},2000);
  void win.loadURL(item.source_url).catch(()=>{message='平台页面加载失败，请检查网络或点击重新加载';});
  return result;
}
module.exports={capture,provider};
