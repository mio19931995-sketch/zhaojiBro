import { useEffect, useRef, useState } from 'react';
import { ArrowSquareOut, ArrowsOutSimple, BookOpen, CaretDown, CaretRight, Check, CircleNotch, CloudArrowUp, Cube, DownloadSimple, FileText, FloppyDisk, Folder, Link, MagnifyingGlass, Microphone, Monitor, PencilSimple, Play, Plus, Scissors, Trash, UploadSimple, User, Waveform } from '@phosphor-icons/react';
import { api, duration, labels } from './api';
import type { Item, Entry, Settings } from './api';
import type { Shared } from './App';
import { Button, Empty } from './components';
import { readDraft, writeDraft } from './drafts';

export { Collect } from './Collection';
export { Library } from './Library';
import { FeishuSettings } from './Feishu';

export function ProcessText({ state, processingSource, refresh, select, notify, navigate }: Shared) {
  const [source, setSource] = useState(processingSource);
  const draftKey = `process-text-${processingSource || 'manual'}`;
  const [text, setText] = useState('');
  const [action, setAction] = useState('summary');
  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState('新文稿');
  const [draftLoaded, setDraftLoaded] = useState(false);
  useEffect(() => { let cancelled = false; readDraft<{source:string;text:string;action:string;instruction:string;title:string}>(draftKey).then(async saved => {
    if (cancelled) return;
    if (saved) { setSource(saved.source); setText(saved.text); setAction(saved.action); setInstruction(saved.instruction); setTitle(saved.title); }
    else if (processingSource) { const i = await api<Item>(`/items/${processingSource}`); if (!cancelled) { setText(i.transcript); setTitle(i.title); } }
    if (!cancelled) setDraftLoaded(true);
  }).catch(e => notify(e.message,true)); return () => { cancelled = true; }; },[notify]);
  useEffect(() => { if (draftLoaded) writeDraft(draftKey, {source,text,action,instruction,title}).catch(e => notify('草稿保存失败：' + e.message,true)); },[source,text,action,instruction,title,draftLoaded,notify,draftKey]);
  function chooseSource(id:string) { navigate('process', id); }
  async function process() { setBusy(true); try { const item = await api<Item>('/process-text','POST',{text, action, instruction, source_id: source || '', title: title + ' · ' + ({summary:'摘要',correct:'校正',rewrite:'改写',translate:'翻译',outline:'大纲'}[action] || '')}); await refresh(); select(item.id); notify('处理结果已保存到输出库'); } catch(e) { notify((e as Error).message,true); } finally { setBusy(false); } }
  return <section className="panel writing-panel"><div className="panel-heading"><h2>选择原文</h2><select aria-label="选择原文" value={source} onChange={e => chooseSource(e.target.value)}><option value="">手动粘贴文稿</option>{state.items.filter(i => i.has_transcript).map(i => <option key={i.id} value={i.id}>{i.title}</option>)}</select></div><textarea aria-label="待处理文案" disabled={!draftLoaded} className="writing-area" placeholder="在这里粘贴正文，或从文稿库选择一份文稿…" value={text} onChange={e => setText(e.target.value)}/><div className="word-count">{text.length.toLocaleString()} 字 · 原始文稿会保留</div><h3>处理方式</h3><div className="action-options">{[['summary','提炼摘要'],['correct','错字校正'],['rewrite','文案改写'],['outline','拆解大纲'],['translate','中英翻译']].map(([v,l]) => <button className={action === v ? 'selected' : ''} key={v} onClick={() => setAction(v)}>{l}</button>)}</div><label className="field-label">补充要求<input placeholder="例如：保留所有数据，整理成适合口播的表达" value={instruction} onChange={e => setInstruction(e.target.value)}/></label><div className="service-note"><Cube size={18}/><span>{state.settings.llm_model ? `使用 ${state.settings.llm_model} · ${state.settings.llm_url}` : '还没有配置文本模型。支持 Ollama 与 OpenAI 兼容服务。'}</span><button onClick={() => navigate('models')}>配置模型</button></div><div className="form-actions"><span className="muted">正文会发送到你配置的模型服务</span><Button primary disabled={busy || !text.trim() || !state.settings.llm_model} onClick={process}>{busy ? <CircleNotch size={16} className="spin"/> : <Play size={16}/>} {busy ? '正在处理全文…' : '开始处理'}</Button></div></section>;
}

export { Voice } from './Voice';

export function Clip({state,refresh,select,notify}:Shared) {
  const [source,setSource] = useState(''); const [start,setStart] = useState('0'); const [end,setEnd] = useState('10'); const [busy,setBusy] = useState(false);
  const selected = state.items.find(i => i.id === source);
  async function clip() { setBusy(true); try { const item = await api<Item>('/clip','POST',{item_id:source,start:Number(start),end:Number(end)}); await refresh(); select(item.id); notify('片段已保存到切片库'); } catch(e) { notify((e as Error).message,true); } finally { setBusy(false); } }
  return <section className="panel writing-panel"><label className="field-label">选择本地录播或素材<select aria-label="切片源素材" value={source} onChange={e => { setSource(e.target.value); const item=state.items.find(i=>i.id===e.target.value); if(item) setEnd(String(Math.min(30,item.duration || 30))); }}><option value="">选择已导入的音视频</option>{state.items.filter(i => i.media_path).map(i => <option key={i.id} value={i.id}>{i.title} · {duration(i.duration)}</option>)}</select></label>{selected?.media_path && <video className="clip-preview" controls src={`/api/items/${selected.id}/media`}/>}<div className="form-grid voice-options"><label>开始时间（秒）<input aria-label="切片开始时间" type="number" min="0" step="0.1" value={start} onChange={e=>setStart(e.target.value)}/></label><label>结束时间（秒）<input aria-label="切片结束时间" type="number" min="0" step="0.1" value={end} onChange={e=>setEnd(e.target.value)}/></label></div><div className="service-note"><Scissors size={19}/><span>按选定时间生成独立文件，保留原始素材。当前支持已下载的录播文件。</span></div><div className="form-actions"><Button primary disabled={!source || busy} onClick={clip}>{busy ? <CircleNotch size={16} className="spin"/> : <Scissors size={16}/>} {busy?'正在生成片段…':'保存片段'}</Button></div></section>;
}

export function Configuration(props:Shared & {page:string}) {
  const {state,refresh,notify,page}=props;
  const [tab,setTab] = useState(page==='feishu'?'feishu':page==='obsidian'?'files':'general'); const [draft,setDraft] = useState<Settings>({}); const [loaded,setLoaded]=useState(false); const [saving,setSaving]=useState(false); const file=useRef<HTMLInputElement>(null);
  useEffect(()=>{setTab(page==='feishu'?'feishu':page==='obsidian'?'files':'general');},[page]);
  useEffect(()=>{if(!loaded && Object.keys(state.settings).length){setDraft(state.settings);setLoaded(true);}},[state.settings,loaded]);
  const set=(k:string,v:string)=>setDraft({...draft,[k]:v});
  async function save(){setSaving(true);try{await api('/settings','PATCH',draft);await refresh();setDraft(d=>({...d,feishu_secret:''}));notify('设置已保存');}catch(e){notify((e as Error).message,true);}finally{setSaving(false);}}
  function field(key:string,title:string,placeholder='',secret=false){return <label key={key}>{title}<input aria-label={title} autoComplete="off" type={secret?'password':'text'} value={String(draft[key]||'')} placeholder={placeholder} onChange={e=>set(key,e.target.value)}/></label>;}
  return <section className="panel settings-layout"><nav className="settings-nav" aria-label="设置分类">{[['general','通用'],['appearance','外观'],['files','文件与存储'],['feishu','飞书知识库'],['about','关于']].map(([v,l])=><button className={tab===v?'selected':''} key={v} onClick={()=>setTab(v)}>{l}</button>)}</nav><div className="settings-form">{tab==='general' && <><h2>采集与登录</h2><p>公开作品可以直接尝试采集。平台需要登录时，手动导入你自己导出的 Netscape 格式 Cookie 文件。</p><div className="setting-row"><div><strong>平台登录文件</strong><p>{state.settings.cookies_path?'已导入 cookies.txt':'尚未导入，使用公开内容模式'}</p></div><Button onClick={()=>file.current?.click()}><UploadSimple size={16}/>导入 cookies.txt</Button>{state.settings.cookies_path && <Button onClick={async()=>{await api('/cookies','DELETE');await refresh();notify('已清除登录文件');}}>清除</Button>}</div><input hidden type="file" accept=".txt" ref={file} onChange={async e=>{if(e.target.files?.[0]){const form=new FormData();form.append('file',e.target.files[0]);try{await api('/cookies','POST',form);await refresh();notify('登录文件已导入');}catch(err){notify((err as Error).message,true);}finally{e.target.value='';}}}}/><div className="service-note"><Monitor size={20}/><span>程序仅连接本地界面服务。音视频转录在这台电脑上完成。</span></div><h2>任务恢复</h2><p>关闭后再次打开，文稿与任务仍会保留。未完成任务会标记为已暂停，点击开始即可重新处理；已下载文件会继续使用。</p></>}{tab==='appearance' && <><h2>外观</h2><p>纸白面板与暖色强调，支持浅色与深色外观。</p><label>颜色主题<select aria-label="颜色主题" value={String(draft.theme||'light')} onChange={e=>set('theme',e.target.value)}><option value="light">浅色 · 米白</option><option value="dark">深色 · 石墨</option></select></label></>}{tab==='files' && <><h2>文件与存储</h2><label>本地数据目录<div className="path-display">{state.data_dir}</div></label><Button onClick={()=>window.desktop?.openData()}><Folder size={16}/>打开数据文件夹</Button><p>素材、转录模型、文稿数据库和生成文件保存在此目录。移除列表条目不会删除源文件。</p><h2>Obsidian 知识库</h2>{field('obsidian_path','已有仓库文件夹','例如 D:\\Notes\\MyVault')}<Button onClick={async()=>{const p=await window.desktop?.chooseFolder();if(p)set('obsidian_path',p);}}><Folder size={16}/>选择仓库</Button><p>在右侧文稿预览中点击书本按钮，即可把文稿保存到仓库内的 Lulu 文件夹。</p></>}{tab==='feishu' && <FeishuSettings {...props}/>}{tab==='about' && <><div className="about-brand"><img src="/app-icon.svg" alt="Lulu"/><h2>Lulu Workbench</h2><span>0.2.0</span></div><p>根据 Lulu 公开工作流程与 v4.3.1 开源实现适配。本项目不是 Lulu 官方 Windows 发行版。</p><p>本地转录：faster-whisper / transcribe.cpp<br/>内容采集：yt-dlp<br/>音视频处理：FFmpeg<br/>界面：React + Electron</p><p>当前支持：本地转录、AI 文稿工作区、直播录制、文稿管理、素材采集、Windows 配音、切片、Markdown 与飞书导出。</p><p>语音克隆和自动更新尚未实现。直播与链接采集结果取决于平台限制、登录状态和网络。</p></>}{!['about','feishu'].includes(tab) && <div className="form-actions"><Button primary disabled={saving} onClick={save}><FloppyDisk size={16}/>{saving?'正在保存…':'保存设置'}</Button></div>}</div></section>;
}
