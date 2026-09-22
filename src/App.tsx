import { useState, useEffect, useRef, useCallback } from 'react';
import { Headphones, FileText, Stack, Microphone, List, User, Folder, PencilSimple, Waveform, Scissors, Cube, GearSix, DownloadSimple, Link, Play, Pause, Check, X, SidebarSimple, ArrowClockwise, Trash, CaretRight, FileAudio, CircleNotch, BookOpen, CloudArrowUp, Record, Stop } from '@phosphor-icons/react';
import { api, duration, labels, preparePlatform } from './api';
import type { Item, State } from './api';
import { Button, Empty } from './components';
import { Collect, Library, ProcessText, Voice, Clip, Models, Configuration } from './Panels';
import { FeishuExportHost } from './Feishu';
import { DownloadOptions, initialOptions } from './DownloadOptions';
import type { Options } from './DownloadOptions';
import { Inspector } from './Inspector';
import { TrashView } from './Recovery';
import { CodexConnection } from './CodexConnection';

const NAV = [
  { group: '工作台', items: [['extract','文案提取',Headphones],['process','文案处理',FileText],['outputs','内容输出',Stack],['voice','文案配音',Microphone],['clip','直播切片',Scissors],['collect','主页采集',User]] },
  { group: '素材库', items: [['library','文稿库',Folder],['output-library','输出库',PencilSimple],['voice-library','音频库',Waveform],['clip-library','切片库',List],['trash','回收站',Trash]] },
  { group: '配置', items: [['models','AI 大模型',Cube],['codex','Codex 连接',Link],['obsidian','Obsidian 知识库',BookOpen],['feishu','飞书知识库',CloudArrowUp]] },
] as const;
const TITLES: Record<string, [string, string]> = {
  extract: ['文案提取', '拖入音视频或粘贴作品链接，提取文案与作品信息。'],
  process: ['文案处理', '整理逐字稿，让观点更清楚，让文案更好用。'],
  outputs: ['内容输出', '在这里查看、编辑和导出处理后的文稿。'],
  voice: ['文案配音', '使用 Windows 本地语音，把文稿变成声音。'],
  clip: ['直播切片', '导入直播录播或本地素材，按起止时间保存片段。'],
  collect: ['主页采集', '粘贴公开主页或播放列表链接，选择作品，批量收集与转录。'],
  library: ['文稿库', '转录和导入的文稿都在这里，可以查看、整理、导出和继续处理。'],
  'output-library': ['输出库', '保存文案处理的结果，每一份原文仍然保留。'],
  'voice-library': ['音频库', '你的本地配音作品，随时试听和导出。'],
  'clip-library': ['切片库', '从长素材里留下值得使用的片段。'],
  trash: ['回收站', '恢复误删的文稿、配音和素材，继续之前的创作。'],
  models: ['AI 大模型', '管理本地转录模型，连接你使用的文本模型。'],
  obsidian: ['Obsidian 知识库', '将选择的文稿保存为 Markdown，放进你的本地知识库。'],
  feishu: ['飞书知识库', '连接自己的多维表格，在需要时主动导出文稿。'],
  settings: ['设置', '管理全局偏好、文件存储与服务连接。'],
  codex: ['Codex 连接', '把 Lulu 素材库交给 Codex，继续分析、改稿与剪辑。'],
};

let extractOptionsCache:Options|null=null;
let saveOptionsQueue:Promise<unknown>=Promise.resolve();

export type Shared = { state: State; refresh: () => Promise<void>; notify: (text: string, error?: boolean) => void; select: (id: string) => void; selected: string; processingSource: string; navigate: (page: string, source?: string) => void };

export default function App() {
  const [state, setState] = useState<State>({ items: [], settings: {}, models: [], data_dir: '' });
  const [page, setPage] = useState(localStorage.getItem('lulu-page') || 'extract');
  const [selected, setSelected] = useState('');
  const [processingSource, setProcessingSource] = useState('');
  const [inspector, setInspector] = useState(true);
  const [toast, setToast] = useState<{ text: string; error: boolean } | null>(null);
  const [connected, setConnected] = useState(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const notify = useCallback((text: string, error = false) => {
    setToast({ text, error }); if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), error ? 14000 : 5000);
  }, []);
  const refresh = useCallback(async () => {
    try { const value = await api<State>('/state'); setState(value); setConnected(true); }
    catch { setConnected(false); }
  }, []);
  useEffect(() => { refresh(); const timer = setInterval(refresh, 1500); return () => clearInterval(timer); }, [refresh]);
  useEffect(() => { localStorage.setItem('lulu-page', page); }, [page]);
  useEffect(() => { document.documentElement.dataset.theme = String(state.settings.theme || 'light'); }, [state.settings.theme]);
  const navigate = (value: string, source?: string) => { if(value==='collect')setSelected(''); if (value === 'process' && source !== undefined) setProcessingSource(source); setPage(value); };
  const select = (id: string) => { setSelected(id); setInspector(true); };
  const shared: Shared = { state, refresh, notify, select, selected, processingSource, navigate };
  const model = state.models.find(m => m.id === state.settings.model);
  const showInspector = inspector && !['settings', 'models', 'codex', 'feishu', 'obsidian', 'trash'].includes(page) && (page !== 'collect' || !!selected);
  const current = state.items.find(i => i.id === selected);
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="sidebar-tools"><Button title="设置" onClick={() => navigate('settings')}><GearSix size={17}/></Button><span>LULU</span></div>
      <button className="brand" aria-label="返回文案提取" onClick={() => navigate('extract')}><img src="/app-icon.svg" alt="Lulu"/><span>Lulu<span className="brand-sub">内容工作台</span></span></button>
      <nav aria-label="主导航">{NAV.map(group => <div className="nav-group" key={group.group}><div className="nav-label">{group.group}</div>{group.items.map(([id, label, Icon]) => <button key={id} className={`nav-item ${page === id ? 'active' : ''}`} onClick={() => navigate(id)}><Icon size={18}/><span>{label}</span>{id === 'extract' && state.items.some(i => i.status === 'processing') && <span className="nav-dot"/>}</button>)}</div>)}</nav>
      <button className="model-status" onClick={() => navigate('models')}><Cube size={19}/><span><strong>{model?.name || '本地转录模型'}</strong><small><i className={model?.installed ? 'ready-dot' : 'wait-dot'}/>{model?.installed ? '本地模型 · 已就绪' : '安装模型后开始转录'}</small></span><CaretRight size={13}/></button>
    </aside>
    <div className="app-main">
      <div className="window-bar"><span>Lulu</span><div><span className={`connection ${connected ? '' : 'offline'}`}><i/>{connected ? '本地运行' : '正在连接本地服务'}</span><Button title={inspector ? '收起文稿预览' : '展开文稿预览'} onClick={() => setInspector(!inspector)}><SidebarSimple size={17}/></Button></div></div>
      <div className={`workspace ${showInspector ? 'with-inspector' : ''}`}>
        <main className="content"><header className="page-heading"><h1>{TITLES[page]?.[0] || '文案提取'}</h1><p>{TITLES[page]?.[1]}</p></header>
          {page === 'extract' && <Extract {...shared}/>}
          {page === 'collect' && <Collect {...shared}/>}
          {['library','outputs','output-library','voice-library','clip-library'].includes(page) && <Library {...shared} page={page}/>}
          {page === 'process' && <ProcessText key={processingSource || 'manual'} {...shared}/>}
          {page === 'voice' && <Voice {...shared}/>}
          {page === 'clip' && <Clip {...shared}/>}
          {page === 'trash' && <TrashView {...shared}/>}
          {page === 'models' && <Models {...shared}/>}
          {page === 'codex' && <CodexConnection {...shared}/>}
          {['settings','feishu','obsidian'].includes(page) && <Configuration {...shared} page={page}/>}
        </main>
        {showInspector && <Inspector key={current?.id || 'empty'} item={current} {...shared} close={() => setInspector(false)}/>}
      </div>
    </div>
    <FeishuExportHost {...shared}/>
    {toast && <div role={toast.error ? 'alert' : 'status'} className={`toast ${toast.error ? 'error' : ''}`}><span>{toast.error ? <X size={18}/> : <Check size={18}/>}</span><p>{toast.text}</p><button aria-label="关闭提示" onClick={() => setToast(null)}><X size={17}/></button></div>}
  </div>;
}

function Extract(props: Shared) {
  const { state, refresh, notify, select, navigate } = props;
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [filter, setFilter] = useState('all');
  const [checked, setChecked] = useState<string[]>([]);
  const [recording, setRecording] = useState(false);
  const [recordSource,setRecordSource]=useState('microphone');
  const recordCleanup=useRef<()=>void>(()=>{});
  const [options,setOptions]=useState(extractOptionsCache||initialOptions(state.settings));
  useEffect(()=>{if(!extractOptionsCache&&Object.keys(state.settings).length){extractOptionsCache=initialOptions(state.settings);setOptions(extractOptionsCache);}},[state.settings]);
  async function changeOptions(v:Options){extractOptionsCache=v;setOptions(v);saveOptionsQueue=saveOptionsQueue.catch(()=>{}).then(()=>api('/settings','PATCH',{download_mode:v.mode,keep_media:String(v.keep),download_cover:String(v.cover),prefer_subtitles:String(v.subtitles),save_directory:v.directory}));try{await saveOptionsQueue;await refresh();}catch(e){notify((e as Error).message,true);}}
  const [recordSeconds, setRecordSeconds] = useState(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { if (!recording) return; const timer = setInterval(() => setRecordSeconds(s => s + 1), 1000); return () => clearInterval(timer); }, [recording]);
  useEffect(() => () => { if(recorder.current?.state==='recording')recorder.current.stop();recordCleanup.current(); }, []);
  async function importFiles(files: FileList | File[]) {
    if (!files.length) return; setBusy(true);
    try { const form = new FormData(); Array.from(files).forEach(f => form.append('files', f)); const items = await api<Item[]>('/import', 'POST', form); await refresh(); select(items[0].id); notify(`已导入 ${items.length} 份素材，点击开始进行转录`); }
    catch (e) { notify((e as Error).message, true); } finally { setBusy(false); if (input.current) input.current.value = ''; }
  }
  async function addLink() {
    const urls = url.match(/https?:\/\/[^\s]+/g); if (!urls?.length) { notify('请先粘贴作品链接', true); return; }
    setBusy(true);
    try { await api('/links', 'POST', { urls }); setUrl(''); await refresh(); notify('链接已加入队列，点击开始下载并转录'); } catch (e) { notify((e as Error).message, true); } finally { setBusy(false); }
  }
  async function record() {
    if (recording) { recorder.current?.stop(); setRecording(false); return; }
    try {
      const inputs:MediaStream[]=[];let context:AudioContext|undefined;
      recordCleanup.current=()=>{inputs.forEach(s=>s.getTracks().forEach(t=>t.stop()));context?.close().catch(()=>{});};
      if(recordSource!=='system')inputs.push(await navigator.mediaDevices.getUserMedia({audio:true}));
      if(recordSource!=='microphone'){
        const display=await navigator.mediaDevices.getDisplayMedia({video:true,audio:true});inputs.push(display);
        display.getVideoTracks().forEach(t=>t.stop());
        if(!display.getAudioTracks().length)throw new Error('系统没有返回音频轨道，请在 Windows 桌面版使用系统声音录制');
      }
      let stream:MediaStream;
      if(inputs.length>1){context=new AudioContext();const output=context.createMediaStreamDestination();inputs.forEach(s=>context!.createMediaStreamSource(s).connect(output));stream=output.stream;}
      else stream=new MediaStream(inputs[0].getAudioTracks());
      const rec = new MediaRecorder(stream); const blobs: Blob[] = [];
      rec.ondataavailable = e => { if (e.data.size) blobs.push(e.data); };
      rec.onstop = () => { recordCleanup.current();stream.getTracks().forEach(t => t.stop()); importFiles([new File(blobs, `录音 ${new Date().toLocaleString('zh-CN').replace(/[/:]/g, '-')}.webm`, { type: rec.mimeType })]); };
      rec.start(); recorder.current = rec; setRecordSeconds(0); setRecording(true);
    } catch(e) {recordCleanup.current();notify('录音未开始：'+(e as Error).message, true);}
  }
  const items = state.items.filter(i => !['document','output','voice','clip'].includes(i.kind)).filter(i => filter === 'all' || (filter === 'active' ? i.status !== 'done' : i.status === 'done'));
  const visibleDeletable = items.filter(i => !['processing','queued'].includes(i.status)).map(i => i.id);
  const visibleChecked = checked.filter(id => visibleDeletable.includes(id));
  const allVisibleChecked = visibleDeletable.length > 0 && visibleDeletable.every(id => checked.includes(id));
  useEffect(() => { setChecked(value => value.filter(id => state.items.some(item => item.id === id))); }, [state.items]);
  function toggleChecked(id: string) { setChecked(value => value.includes(id) ? value.filter(current => current !== id) : [...value, id]); }
  function toggleAllVisible() { setChecked(value => allVisibleChecked ? value.filter(id => !visibleDeletable.includes(id)) : [...new Set([...value, ...visibleDeletable])]); }
  async function removeTasks(ids: string[]) {
    if (!ids.length) return;
    const count = ids.length;
    if (!window.confirm(count === 1 ? '将这条任务移入回收站？\n可在左侧回收站恢复，原始文件会保留。' : `将选中的 ${count} 条任务移入回收站？\n可在左侧回收站恢复，原始文件会保留。`)) return;
    try {
      if (count === 1) await api(`/items/${ids[0]}`, 'DELETE');
      else await api('/items/batch', 'POST', { ids, action: 'delete' });
      if (ids.includes(props.selected)) select('');
      setChecked(value => value.filter(id => !ids.includes(id)));
      await refresh();
      notify(`已将 ${count} 条任务移入回收站，可在左侧恢复`);
    } catch (e) { notify((e as Error).message, true); }
  }
  return <>
    <section className="panel import-panel">
      <button className={`drop-zone ${drag ? 'dragging' : ''}`} disabled={busy} onClick={() => input.current?.click()} onDragOver={e => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={e => { e.preventDefault(); setDrag(false); importFiles(e.dataTransfer.files); }}>
        <DownloadSimple size={26}/><strong>{busy ? '正在导入素材…' : '拖入本地音视频文件'}</strong><span>或点击选择</span><small>MP3 · WAV · M4A · MP4 · MOV · 字幕 / 文稿</small>
      </button><input ref={input} data-testid="file-import" type="file" multiple accept="audio/*,video/*,.txt,.md,.srt" hidden onChange={e => e.target.files && importFiles(e.target.files)}/>
      <div className="inset"><h3><Link size={17}/>粘贴链接，自动下载并转录</h3><div className="input-row"><input aria-label="作品链接" value={url} onChange={e => setUrl(e.target.value)} onKeyDown={e => e.key === 'Enter' && addLink()} placeholder="抖音 / 小红书 / B 站 / YouTube / 公开作品链接…"/><Button primary onClick={addLink} disabled={busy}><DownloadSimple size={16}/>抓取</Button></div><DownloadOptions codexCache={state.settings.codex_keep_video==='true'} value={options} change={changeOptions} notify={notify}/><div className="hint-row"><span>先加入队列，点击任务上的「开始」后下载和转录。</span><button onClick={() => navigate('settings')}>登录与采集设置<CaretRight size={12}/></button></div></div>
      <div className="inset recording-row"><div className="section-icon"><Microphone size={21}/></div><div><h3>录一段声音</h3><p>{recording ? `正在录音 · ${duration(recordSeconds)}` : '选择声音来源，结束后自动加入转录队列'}</p></div><select aria-label="录音来源" disabled={recording} value={recordSource} onChange={e=>setRecordSource(e.target.value)}><option value="microphone">麦克风</option><option value="system">系统声音</option><option value="mix">麦克风 + 系统混录</option></select><Button primary onClick={record}>{recording ? <Stop size={16}/> : <Microphone size={16}/>} {recording ? '结束录音' : '开始录音'}</Button></div>
    </section>
    <div className="section-bar"><h2><span className="tiny-dot"/>任务队列 <small>{state.items.filter(i => ['idle','paused','queued','processing'].includes(i.status)).length} 个待处理</small></h2><div className="section-actions"><div className="segmented">{[['all','全部任务'],['active','进行中'],['done','已完成']].map(([v,t]) => <button className={filter === v ? 'selected' : ''} onClick={() => setFilter(v)} key={v}>{t}</button>)}</div><Button primary onClick={async () => { try { for(const i of state.items.filter(i=>['idle','paused','error'].includes(i.status)))await preparePlatform(i); const r = await api('/start-all', 'POST'); await refresh(); notify(`已启动 ${r.count} 个任务`); } catch (e) { notify((e as Error).message, true); } }} disabled={!state.items.some(i => ['idle','error','paused'].includes(i.status))}><Play size={15}/>全部开始</Button></div></div>
    <section className="panel task-panel">{items.length ? <><div className="task-batch"><label><input type="checkbox" checked={allVisibleChecked} onChange={toggleAllVisible}/><span>全选当前可删除任务</span></label><span>已选 {visibleChecked.length} 项</span><Button disabled={!visibleChecked.length} onClick={() => removeTasks(visibleChecked)}><Trash size={14}/>批量删除</Button></div>{items.map(item => <TaskRow item={item} key={item.id} checked={checked.includes(item.id)} toggleChecked={toggleChecked} remove={() => removeTasks([item.id])} {...props}/>)}</> : <Empty icon={<FileAudio size={30}/>} title="从第一份素材开始" text="导入一段音视频，文稿和处理进度会显示在这里。"/>}</section>
  </>;
}

function TaskRow({ item, select, selected, refresh, notify, checked, toggleChecked, remove }: Shared & { item: Item; checked: boolean; toggleChecked: (id: string) => void; remove: () => void }) {
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  useEffect(() => { setThumbnailFailed(false); }, [item.id, item.media_path, item.metadata.cover_path]);
  async function action(command: string) {
    if (command === 'retranscribe' && !window.confirm('确定重新转录这条任务吗？\n现有文稿会先保留到历史版本，可在预览中恢复。')) return;
    try { if(command==='start')await preparePlatform(item);await api(`/items/${item.id}/${command}`, 'POST'); await refresh(); if(command==='retranscribe')notify('已按自动语言识别重新转录'); } catch (e) { notify((e as Error).message, true); }
  }
  const active = ['processing','queued'].includes(item.status);
  const video = /\.(mp4|mov|webm|mkv|avi|m4v)$/i.test(item.media_path);
  const thumbnail = item.status === 'done' && !thumbnailFailed && (!!item.metadata.cover_path || video);
  return <article className={`task-row selectable ${selected === item.id ? 'chosen' : ''} ${checked ? 'batch-selected' : ''}`}>
    <input className="task-select" type="checkbox" aria-label={`选择任务 ${item.title}`} checked={checked} disabled={active} onChange={() => toggleChecked(item.id)}/>
    <button className="task-open" onClick={() => select(item.id)}><span className={`task-icon ${active ? 'working' : ''} ${thumbnail ? 'thumbnail' : ''}`}>{thumbnail ? <img loading="lazy" src={`/api/items/${item.id}/cover`} alt="" onError={() => setThumbnailFailed(true)}/> : active ? <CircleNotch size={23}/> : item.status === 'done' ? <FileText size={23}/> : <FileAudio size={23}/>}</span><span className="task-copy"><strong>{item.title}</strong><small>{item.source_url ? '来自链接' : '本地素材'}{item.duration > 0 && ` · ${duration(item.duration)}`} · {item.phase}</small></span></button>
    <div className="task-actions"><span className={`status-pill ${item.status}`}>{labels[item.status]}</span><div className="task-buttons">{item.status === 'done' ? <><Button onClick={() => select(item.id)}>查看<CaretRight size={13}/></Button><Button title="按当前语言设置重新识别" onClick={() => action('retranscribe')}><ArrowClockwise size={13}/>重转</Button></> : <Button primary={!active} onClick={() => action(active ? 'pause' : 'start')}>{active ? <Pause size={14}/> : <Play size={14}/>} {active ? '暂停' : item.status === 'error' ? '重试' : '开始'}</Button>}<Button title={active ? '请先暂停任务再删除' : '删除任务'} disabled={active} onClick={remove}><Trash size={13}/>删除</Button></div></div>
    {active && <progress aria-label="任务进度" value={item.progress} max={100}/>}
    {item.error && <div className="inline-error">{item.error}</div>}
  </article>;
}
