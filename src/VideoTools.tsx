import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent } from 'react';
import { ArrowCounterClockwise, CheckCircle, CircleNotch, DownloadSimple, FilmStrip, ImageSquare, Play, Stop, UploadSimple, X } from '@phosphor-icons/react';
import { api, duration } from './api';
import type { Item } from './api';
import type { Shared } from './App';
import { Button, Empty } from './components';
import './video-tools.css';

type Watermark = { type: 'none' | 'text' | 'image'; text: string; image_id: string; x: number; y: number; size: number; opacity: number; color: string };
type SourceInfo = { item_id: string; duration: number; width: number; height: number; fps: number; base_fps: number; has_audio: boolean; normalized?: boolean };
type Job = { id: string; status: 'queued' | 'running' | 'done' | 'error' | 'cancelled' | 'interrupted'; progress: number; phase: string; error?: string; title: string; created_at: number; output_item_id?: string; download_url?: string; source_info?: SourceInfo; options?: { drop_per_second: number; watermark: Watermark } };
type Draft = { source: string; drop: string; watermark: Watermark };
const emptyWatermark: Watermark = { type: 'none', text: '', image_id: '', x: .95, y: .95, size: 5, opacity: .8, color: '#ffffff' };
const draftKey = 'lulu-video-tools-draft-v1';
const activeStatuses = ['queued', 'running'];
const statusNames: Record<Job['status'], string> = { queued: '等待处理', running: '正在导出', done: '已完成', error: '导出失败', cancelled: '已取消', interrupted: '已中断' };
const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

function loadDraft(): Draft {
  try {
    const value = JSON.parse(localStorage.getItem(draftKey) || '{}');
    const saved = value.watermark || {};
    const number = (key: keyof Watermark, min: number, max: number) => typeof saved[key] === 'number' && Number.isFinite(saved[key]) ? clamp(saved[key], min, max) : emptyWatermark[key];
    return { source: typeof value.source === 'string' ? value.source : '', drop: typeof value.drop === 'string' ? value.drop : '0', watermark: {
      ...emptyWatermark, type: ['none', 'text', 'image'].includes(saved.type) ? saved.type : 'none', text: typeof saved.text === 'string' ? saved.text : '',
      image_id: typeof saved.image_id === 'string' ? saved.image_id : '', x: number('x', 0, 1) as number, y: number('y', 0, 1) as number,
      size: number('size', 1, 40) as number, opacity: number('opacity', 0, 1) as number, color: /^#[0-9a-f]{6}$/i.test(saved.color || '') ? saved.color : '#ffffff',
    } };
  } catch { return { source: '', drop: '0', watermark: { ...emptyWatermark } }; }
}

function Slider({ title, value, min, max, unit, change }: { title: string; value: number; min: number; max: number; unit: string; change: (value: number) => void }) {
  return <label className="vt-slider"><span>{title}<strong>{Math.round(value)}{unit}</strong></span><input type="range" aria-label={title} min={min} max={max} step="1" value={value} onChange={e => change(Number(e.target.value))}/></label>;
}

export default function VideoTools({ state, selected, refresh, select, notify, navigate }: Shared) {
  const [initial] = useState(loadDraft);
  const [source, setSource] = useState(initial.source || (state.items.find(i => i.id === selected)?.media_path ? selected : ''));
  const [drop, setDrop] = useState(initial.drop);
  const [watermark, setWatermark] = useState<Watermark>(initial.watermark);
  const [info, setInfo] = useState<SourceInfo | null>(null);
  const [sourceError, setSourceError] = useState('');
  const [loadingInfo, setLoadingInfo] = useState(false);
  const [importing, setImporting] = useState(false);
  const [uploadingMark, setUploadingMark] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobsError, setJobsError] = useState('');
  const [cancelling, setCancelling] = useState<string[]>([]);
  const [previewTime, setPreviewTime] = useState('0');
  const [previewUrl, setPreviewUrl] = useState('');
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewStamp, setPreviewStamp] = useState(0);
  const [playError, setPlayError] = useState(false);
  const [viewing, setViewing] = useState<Job | null>(null);
  const [stageHeight, setStageHeight] = useState(300);
  const fileInput = useRef<HTMLInputElement>(null);
  const watermarkInput = useRef<HTMLInputElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const overlay = useRef<HTMLDivElement>(null);
  const previewRequest = useRef<AbortController | null>(null);
  const jobStatus = useRef(new Map<string, Job['status']>());
  const draggingMark = useRef<{ pointerId: number; offsetX: number; offsetY: number } | null>(null);
  const mediaItems = state.items.filter(i => i.media_path && i.kind !== 'voice');
  const item = state.items.find(i => i.id === source);
  const fps = info?.base_fps || 0;
  const dropCount = Number(drop);
  const validDrop = drop.trim() !== '' && Number.isInteger(dropCount) && dropCount >= 0 && dropCount < fps;
  const watermarkValid = watermark.type === 'none' || (watermark.type === 'text' ? !!watermark.text.trim() && watermark.text.length <= 200 : !!watermark.image_id);
  const canExport = !!source && !!info && info.item_id === source && !loadingInfo && !sourceError && validDrop && watermarkValid && !importing && !uploadingMark;
  const payload = useMemo(() => ({ item_id: source, drop_per_second: dropCount, watermark }), [source, dropCount, watermark]);
  const setMark = (values: Partial<Watermark>) => setWatermark(old => ({ ...old, ...values }));

  useEffect(() => { try { localStorage.setItem(draftKey, JSON.stringify({ source, drop, watermark })); } catch { /* Preferences are optional. */ } }, [source, drop, watermark]);
  useEffect(() => {
    let cancelled = false;
    setInfo(null); setSourceError(''); setPreviewTime('0'); setPlayError(false); setLoadingInfo(!!source);
    if (source) api<SourceInfo>(`/video-tools/source/${encodeURIComponent(source)}`).then(value => { if (!cancelled) setInfo(value); }).catch(error => { if (!cancelled) setSourceError(error.message); }).finally(() => { if (!cancelled) setLoadingInfo(false); });
    return () => { cancelled = true; };
  }, [source]);
  useEffect(() => {
    previewRequest.current?.abort(); setPreviewBusy(false); setPreviewUrl('');
  }, [source, drop, watermark]);
  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl); }, [previewUrl]);
  useEffect(() => () => previewRequest.current?.abort(), []);
  useEffect(() => {
    if (!stage.current) return;
    const observer = new ResizeObserver(entries => setStageHeight(entries[0].contentRect.height));
    observer.observe(stage.current);
    return () => observer.disconnect();
  }, [info]);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const list = await api<Job[]>('/video-tools/jobs');
        if (cancelled) return;
        setJobs(list); setJobsError('');
        let changed = false;
        for (const job of list) {
          const before = jobStatus.current.get(job.id);
          if (before && activeStatuses.includes(before) && !activeStatuses.includes(job.status)) {
            changed = true;
            if (job.status === 'done') notify('视频处理完成，可以预览或下载');
            else if (job.status === 'error') notify(job.error || '视频导出失败，请查看任务详情', true);
          }
          jobStatus.current.set(job.id, job.status);
        }
        if (changed) await refresh();
      } catch (error) { if (!cancelled) setJobsError((error as Error).message); }
      finally { if (!cancelled) timer = setTimeout(poll, 1500); }
    }
    poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [notify, refresh]);

  async function importVideo(files: FileList | File[]) {
    if (!files.length || importing) return;
    if (files.length !== 1) { notify('请每次导入一个视频，完成后可继续处理其他视频', true); return; }
    const file = files[0];
    if (!/\.(mp4|mov|mkv|webm|avi|m4v|ts|mts|m2ts|flv|wmv)$/i.test(file.name)) { notify('请选择视频文件，例如 MP4、MOV、MKV 或 WebM', true); return; }
    setImporting(true);
    try {
      const form = new FormData(); form.append('files', file);
      const imported = await api<Item[]>('/import', 'POST', form);
      if (!imported[0]) throw new Error('没有导入成功的视频');
      await refresh(); setSource(imported[0].id); setViewing(null); notify('视频已导入，可直接设置删帧和水印');
    } catch (error) { notify((error as Error).message, true); }
    finally { setImporting(false); if (fileInput.current) fileInput.current.value = ''; }
  }
  async function uploadWatermark(file?: File) {
    if (!file || uploadingMark) return;
    setUploadingMark(true);
    try {
      const form = new FormData(); form.append('file', file);
      const result = await api<{ id: string; url: string }>('/video-tools/watermark', 'POST', form);
      setMark({ image_id: result.id, type: 'image' }); notify('图片水印已载入');
    } catch (error) { notify((error as Error).message, true); }
    finally { setUploadingMark(false); if (watermarkInput.current) watermarkInput.current.value = ''; }
  }
  async function createPreview() {
    const seconds = Number(previewTime);
    if (!canExport || !Number.isFinite(seconds) || seconds < 0 || !info || seconds >= info.duration) { notify('请填写视频时长范围内的预览时间', true); return; }
    previewRequest.current?.abort();
    const controller = new AbortController(); previewRequest.current = controller; setPreviewBusy(true);
    try {
      const response = await fetch('/api/video-tools/preview', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Lulu-Client': 'desktop' }, body: JSON.stringify({ ...payload, seconds }), signal: controller.signal });
      if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(typeof error.detail === 'string' ? error.detail : '效果图生成失败'); }
      const blob = await response.blob();
      if (!controller.signal.aborted) { video.current?.pause(); setPreviewStamp(seconds); setPreviewUrl(URL.createObjectURL(blob)); }
    } catch (error) { if (!controller.signal.aborted) notify((error as Error).message, true); }
    finally { if (previewRequest.current === controller) setPreviewBusy(false); }
  }
  async function submit() {
    if (!canExport || submitting) return;
    setSubmitting(true);
    try {
      const job = await api<Job>('/video-tools/jobs', 'POST', payload);
      jobStatus.current.set(job.id, job.status); setJobs(old => [job, ...old.filter(j => j.id !== job.id)]); notify('已加入导出队列，原视频会保留');
    } catch (error) { notify((error as Error).message, true); }
    finally { setSubmitting(false); }
  }
  async function cancel(job: Job) {
    setCancelling(old => [...old, job.id]);
    try { const result = await api<Job>(`/video-tools/jobs/${job.id}/cancel`, 'POST'); setJobs(old => old.map(j => j.id === job.id ? result : j)); }
    catch (error) { notify((error as Error).message, true); }
    finally { setCancelling(old => old.filter(id => id !== job.id)); }
  }
  function beginDrag(event: PointerEvent<HTMLDivElement>) {
    if (!stage.current || !overlay.current) return;
    const rect = overlay.current.getBoundingClientRect();
    draggingMark.current = { pointerId: event.pointerId, offsetX: event.clientX - rect.left, offsetY: event.clientY - rect.top };
    event.currentTarget.setPointerCapture(event.pointerId); event.preventDefault();
  }
  function moveDrag(event: PointerEvent<HTMLDivElement>) {
    if (!draggingMark.current || !stage.current || !overlay.current) return;
    const rect = stage.current.getBoundingClientRect(), mark = overlay.current.getBoundingClientRect();
    const spaceX = rect.width - mark.width, spaceY = rect.height - mark.height;
    setMark({ x: spaceX > 0 ? clamp((event.clientX - rect.left - draggingMark.current.offsetX) / spaceX, 0, 1) : 0, y: spaceY > 0 ? clamp((event.clientY - rect.top - draggingMark.current.offsetY) / spaceY, 0, 1) : 0 });
  }
  const overlayStyle: CSSProperties = { left: `${watermark.x * 100}%`, top: `${watermark.y * 100}%`, transform: `translate(-${watermark.x * 100}%, -${watermark.y * 100}%)`, opacity: watermark.opacity, color: watermark.color, fontSize: Math.max(1, stageHeight * watermark.size / 100) };
  const orderedJobs = [...jobs].sort((a, b) => b.created_at - a.created_at).slice(0, 20);

  return <div className="video-tools">
    <section className="panel vt-source">
      <div className="vt-section-title"><span><FilmStrip size={21}/><h2>导入与选择视频</h2></span><small>本地处理 · 原文件保留</small></div>
      <div className="vt-source-row"><label>已有素材<select data-testid="video-tools-source" aria-label="视频处理源素材" value={source} onChange={e => { setSource(e.target.value); setViewing(null); }}><option value="">选择已有视频，或导入新文件</option>{source && !mediaItems.some(i => i.id === source) && <option value={source}>{loadingInfo ? '正在读取上次的素材…' : '上次选择的素材'}</option>}{mediaItems.map(i => <option key={i.id} value={i.id}>{i.title} · {duration(i.duration)}</option>)}</select></label><Button disabled={importing} onClick={() => fileInput.current?.click()}>{importing ? <CircleNotch className="spin" size={16}/> : <UploadSimple size={16}/>} {importing ? '正在导入…' : '导入视频'}</Button></div>
      <button className={`vt-drop ${dragOver ? 'is-over' : ''}`} disabled={importing} onClick={() => fileInput.current?.click()} onDragOver={event => { event.preventDefault(); setDragOver(true); }} onDragLeave={() => setDragOver(false)} onDrop={event => { event.preventDefault(); setDragOver(false); importVideo(event.dataTransfer.files); }}>也可以将一个视频拖到这里 · MP4 / MOV / MKV / WebM</button>
      <input data-testid="video-tools-import" hidden type="file" ref={fileInput} accept="video/*,.mkv,.m4v,.ts,.mts,.m2ts,.flv,.wmv" aria-label="导入处理视频" onChange={event => event.target.files && importVideo(event.target.files)}/>
      {loadingInfo && <p className="vt-note"><CircleNotch className="spin" size={14}/> 正在读取视频参数…</p>}
      {sourceError && <p className="vt-error" role="alert">{sourceError}</p>}
      {info && <div className="vt-source-details"><span>{info.width} × {info.height}</span><span>{Number(info.fps).toFixed(2).replace(/\.00$/, '')} fps</span><span>{duration(info.duration)}</span><span>{info.has_audio ? '包含音轨' : '无音轨'}</span></div>}
    </section>

    <div className="vt-editor-grid">
      <section className="panel vt-preview-panel">
        <div className="vt-section-title"><h2>{previewUrl ? '实际水印效果图' : '视频与水印预览'}</h2>{previewUrl && <Button onClick={() => setPreviewUrl('')}><ArrowCounterClockwise size={14}/> 返回视频</Button>}</div>
        {info ? <>
          <div className="vt-stage-wrap"><div ref={stage} className="vt-stage" style={{ aspectRatio: `${info.width}/${info.height}`, width: `min(100%, ${440 * info.width / info.height}px)` }}>
            <video ref={video} key={source} controls playsInline hidden={!!previewUrl} src={`/api/items/${source}/media`} onError={() => setPlayError(true)} onTimeUpdate={event => setPreviewTime(event.currentTarget.currentTime.toFixed(2))} onLoadedMetadata={() => setPlayError(false)} aria-label="源视频预览"/>
            {previewUrl ? <img className="vt-effect-frame" src={previewUrl} alt={`第 ${previewStamp.toFixed(2)} 秒的实际水印效果`}/> : watermark.type !== 'none' && (watermark.type === 'text' ? watermark.text : watermark.image_id) ? <div ref={overlay} className={`vt-watermark ${watermark.type}`} style={overlayStyle} onPointerDown={beginDrag} onPointerMove={moveDrag} onPointerUp={() => { draggingMark.current = null; }} onPointerCancel={() => { draggingMark.current = null; }} title="拖动水印调整位置">{watermark.type === 'text' ? watermark.text : <img draggable={false} src={`/api/video-tools/watermark/${watermark.image_id}`} style={{ height: stageHeight * watermark.size / 100 }} alt="图片水印"/>}</div> : null}
          </div></div>
          {playError && <p className="vt-note">当前播放器不能预览这个编码。仍可指定时间生成效果图，并导出为 MP4。</p>}
          <div className="vt-preview-actions"><label>效果图时间（秒）<input aria-label="效果图时间" type="number" min="0" max={Math.max(0, info.duration - .01)} step="0.01" value={previewTime} onChange={event => { setPreviewTime(event.target.value); const seconds = Number(event.target.value); if (video.current && Number.isFinite(seconds) && seconds >= 0 && seconds < info.duration) video.current.currentTime = seconds; }}/></label><button data-testid="video-tools-preview" className="button" disabled={!canExport || previewBusy} onClick={createPreview}>{previewBusy ? <CircleNotch className="spin" size={15}/> : <ImageSquare size={15}/>} {previewBusy ? '生成中…' : '生成实际效果图'}</button></div>
          <p className="vt-note">{previewUrl ? `第 ${previewStamp.toFixed(2)} 秒的实际水印效果；它是静态画面。删帧后的流畅度请在导出视频中检查。` : '正在播放原视频。可拖动水印调整位置；这里是位置示意，实际字体和尺寸请生成效果图查看。'}</p>
        </> : <Empty icon={<FilmStrip size={34}/>} title="先选择一个视频" text="导入后可自定义每秒删帧数量、添加水印，并导出完整视频。"/>}
      </section>

      <section className="panel vt-controls">
        <div className="vt-section-title"><h2>每秒删帧</h2><small>保持原时长与音画同步</small></div>
        <label className="vt-drop-field">每秒删除的帧数<div><input data-testid="video-tools-drop" aria-label="每秒删除的帧数" type="number" min="0" max={Math.max(0, fps - 1)} step="1" value={drop} disabled={!info} onChange={event => setDrop(event.target.value)}/><span>帧 / 秒</span></div></label>
        {info && <div className="vt-fps-summary"><span>基准<strong>{fps} fps</strong></span><span aria-hidden="true">→</span><span>导出<strong>{validDrop ? `${fps - dropCount} fps` : '—'}</strong></span></div>}
        <p className="vt-note">每秒均匀删除指定数量的帧，整段内容和音轨保持原时长。填 0 可只添加水印。删帧越多，画面越不流畅。</p>
        {info?.normalized && <p className="vt-note">此素材将先规范为 {fps} fps，再执行每秒删帧。</p>}
        {info && !validDrop && <p className="vt-error" role="alert">请输入 0 至 {fps - 1} 的整数，至少保留 1 帧 / 秒。</p>}
        <div className="vt-watermark-heading"><h2>自定义水印</h2><select data-testid="video-tools-watermark-type" aria-label="水印类型" value={watermark.type} onChange={event => setMark({type:event.target.value as Watermark['type']})}><option value="none">不添加水印</option><option value="text">文字水印</option><option value="image">图片水印</option></select></div>
        {watermark.type === 'text' && <><label className="vt-field">水印内容<textarea data-testid="video-tools-watermark-text" aria-label="水印内容" value={watermark.text} maxLength={200} rows={2} placeholder="例如：满满超人 · 原创记录" onChange={event => setMark({ text: event.target.value })}/></label><label className="vt-color">文字颜色<input aria-label="水印文字颜色" type="color" value={watermark.color} onChange={event => setMark({ color: event.target.value })}/><span>{watermark.text.length} / 200 字</span></label></>}
        {watermark.type === 'image' && <div className="vt-image-upload">{watermark.image_id && <img src={`/api/video-tools/watermark/${watermark.image_id}`} alt="已上传的水印图片"/>}<div><Button disabled={uploadingMark} onClick={() => watermarkInput.current?.click()}>{uploadingMark ? <CircleNotch className="spin" size={15}/> : <UploadSimple size={15}/>} {watermark.image_id ? '更换水印图片' : '上传水印图片'}</Button><p className="vt-note">PNG / JPEG / WebP，透明 PNG 可保留透明背景。</p></div></div>}
        <input data-testid="video-tools-watermark-file" hidden type="file" ref={watermarkInput} accept="image/png,image/jpeg,image/webp" aria-label="上传水印图片" onChange={event => uploadWatermark(event.target.files?.[0])}/>
        {watermark.type !== 'none' && <div className="vt-mark-controls">
          <div className="vt-presets" aria-label="水印预设位置">{[['左上', .05, .05], ['居中', .5, .5], ['右下', .95, .95]].map(([title, x, y]) => <button key={title} onClick={() => setMark({ x: Number(x), y: Number(y) })}>{title}</button>)}</div>
          <Slider title="水平位置" min={0} max={100} unit="%" value={watermark.x * 100} change={x => setMark({ x: x / 100 })}/>
          <Slider title="垂直位置" min={0} max={100} unit="%" value={watermark.y * 100} change={y => setMark({ y: y / 100 })}/>
          <Slider title={watermark.type === 'text' ? '字号占画面高度' : '图片占画面高度'} min={1} max={40} unit="%" value={watermark.size} change={size => setMark({ size })}/>
          <Slider title="不透明度" min={5} max={100} unit="%" value={watermark.opacity * 100} change={opacity => setMark({ opacity: opacity / 100 })}/>
          {!watermarkValid && <p className="vt-error">{watermark.type === 'text' ? '请填写水印文字。' : '请先上传水印图片。'}</p>}
        </div>}
      </section>
    </div>

    <section className="panel vt-export-bar"><div><strong>导出完整视频</strong><p>保留原分辨率，生成独立 MP4 文件；需要重新编码，原文件不会被覆盖。已开始的任务使用提交时的设置。</p></div><button data-testid="video-tools-export" className="button primary" disabled={!canExport || submitting} onClick={submit}>{submitting ? <CircleNotch className="spin" size={17}/> : <FilmStrip size={17}/>} {submitting ? '正在提交…' : '开始导出视频'}</button></section>

    {viewing && <section className="panel vt-output-preview"><div className="vt-section-title"><h2>处理后的视频</h2><Button title="关闭输出预览" onClick={() => setViewing(null)}><X size={16}/></Button></div><p>{viewing.title}</p><video key={viewing.id} controls playsInline src={viewing.output_item_id ? `/api/items/${viewing.output_item_id}/media` : `/api/video-tools/jobs/${viewing.id}/download`} aria-label="处理后的视频预览"/><p className="vt-note">播放检查画面流畅度、水印和音画同步后，再下载使用。</p></section>}
    <section className="panel vt-jobs"><div className="vt-section-title"><h2>导出记录</h2><small>{jobs.length > 20 ? '最近 20 项' : `${jobs.length} 项`}</small></div>{jobsError && <p className="vt-error" role="alert">导出状态读取失败：{jobsError}。稍后会自动重试。</p>}
      {!jobs.length ? <p className="vt-note">导出进度和结果会保存在这里，完成后也可在切片库找到视频。</p> : <div className="vt-job-list">{orderedJobs.map(job => <article data-testid="video-tools-job" data-job-id={job.id} key={job.id} className={`vt-job ${job.status}`}>
        <div className="vt-job-top"><div><strong>{job.title || item?.title || '视频处理'}</strong><p>{job.phase || statusNames[job.status]}</p></div><span className="vt-job-status">{job.status === 'done' && <CheckCircle size={15}/>} {statusNames[job.status]}</span></div>
        {activeStatuses.includes(job.status) && <progress aria-label={`${job.title}导出进度`} value={clamp(job.progress || 0, 0, 100)} max={100}/>}
        {job.error && <p className="vt-error" role="alert">{job.error}</p>}
        {job.status === 'interrupted' && <p className="vt-note">上次退出时未完成。选择源视频后，可用当前设置重新导出。</p>}
        <div className="vt-job-bottom"><small>{new Date(job.created_at * 1000).toLocaleString('zh-CN')}{job.options && ` · 每秒删 ${job.options.drop_per_second} 帧`}</small><div>{activeStatuses.includes(job.status) && <Button disabled={cancelling.includes(job.id)} onClick={() => cancel(job)}><Stop size={14}/>{cancelling.includes(job.id) ? '正在取消…' : '取消导出'}</Button>}{job.status === 'done' && <><Button onClick={() => setViewing(job)}><Play size={14}/>预览结果</Button><a className="button primary" href={`/api/video-tools/jobs/${job.id}/download`} download><DownloadSimple size={14}/>下载视频</a>{job.output_item_id && <Button onClick={() => { select(job.output_item_id!); navigate('clip-library'); }}>切片库查看</Button>}</>}</div></div>
      </article>)}</div>}
    </section>
  </div>;
}
