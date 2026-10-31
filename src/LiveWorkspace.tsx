import { useEffect, useRef, useState } from 'react';
import { ArrowClockwise, Broadcast, DownloadSimple, Pause, Play, Plus, Trash, User } from '@phosphor-icons/react';
import type { Shared } from './App';
import { api, duration } from './api';
import { Button, Empty } from './components';
import './live.css';

type Metrics = { title?: string; viewer_count?: number | null; viewer_count_raw?: string; like_count?: number | null; elapsed_seconds?: number };
type Host = { id: string; source: string; name: string; status: string; message: string; live_state: string; enabled: boolean; metrics: Metrics; checked_at: number | null; auto_record: boolean; segment_seconds: number; comments: boolean };
type Part = { id: string; file: string; duration: number; bytes: number; item_id: string };
type Recording = { id: string; host_id: string; name: string; title: string; started: number; ended: number | null; status: string; duration: number; bytes: number; segments: Part[]; comments_count: number; comments_state: string; warning: string };
type LiveState = { hosts: Host[]; recordings: Recording[]; ffmpeg: boolean; comments_available: boolean; max_active: number };
type Data = { comments: { received_at: string; elapsed_seconds: number; nickname: string; text: string; message_id: string }[]; metrics: Metrics[]; total: number; page: number; comments_message: string };
const initial: LiveState = { hosts: [], recordings: [], ffmpeg: true, comments_available: true, max_active: 3 };
const stamp = (value: number | null) => value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '尚未检查';
const count = (value: number | null | undefined, raw?: string) => value == null ? raw || '未提供' : value.toLocaleString();
const recordingLabels: Record<string, string> = { recording: '录制中', saved: '已保存', interrupted: '中断，原始文件已保留', recovered: '已恢复分段' };

export function LiveWorkspace({ notify, refresh, select, navigate }: Shared) {
  const [snapshot, setSnapshot] = useState<LiveState>(initial), [tab, setTab] = useState<'hosts' | 'recordings'>('hosts');
  const [url, setUrl] = useState(''), [name, setName] = useState(''), [segmentMinutes, setSegmentMinutes] = useState('30');
  const [autoRecord, setAutoRecord] = useState(true), [comments, setComments] = useState(true), [busy, setBusy] = useState('');
  const [recordingId, setRecordingId] = useState(''), [partId, setPartId] = useState(''), [data, setData] = useState<Data | null>(null);
  const [query, setQuery] = useState(''), [page, setPage] = useState(1), [loadError, setLoadError] = useState('');
  const generation = useRef(0);
  async function reload() { setSnapshot(await api<LiveState>('/live/state')); setLoadError(''); }
  useEffect(() => {
    let disposed = false;
    async function poll() {
      try { const result = await api<LiveState>('/live/state'); if (!disposed) { setSnapshot(result); setLoadError(''); } }
      catch (error) { if (!disposed) setLoadError((error as Error).message); }
    }
    void poll(); const timer = setInterval(poll, 3000);
    return () => { disposed = true; clearInterval(timer); };
  }, []);
  useEffect(() => {
    const token = ++generation.current;
    setData(null);
    if (!recordingId) return;
    async function load() {
      try {
        const result = await api<Data>(`/live/recordings/${recordingId}/data?page=${page}&query=${encodeURIComponent(query)}`);
        if (token === generation.current) setData(result);
      } catch (error) { if (token === generation.current) setLoadError((error as Error).message); }
    }
    const debounce = setTimeout(load, 200), timer = setInterval(load, 5000);
    return () => { generation.current++; clearTimeout(debounce); clearInterval(timer); };
  }, [recordingId, query, page]);
  const recording = snapshot.recordings.find(row => row.id === recordingId);
  const part = recording?.segments.find(row => row.id === partId) || recording?.segments[0];
  async function run(key: string, action: () => Promise<unknown>, success?: string) {
    setBusy(key);
    try { await action(); await reload(); if (success) notify(success); }
    catch (error) { notify((error as Error).message, true); }
    finally { setBusy(''); }
  }
  async function add() {
    await api('/live/hosts', 'POST', { source: url, name, segment_seconds: Number(segmentMinutes) * 60, auto_record: autoRecord, comments });
    setUrl(''); setName('');
  }
  function choose(row: Recording) { setRecordingId(row.id); setPartId(''); setQuery(''); setPage(1); }
  async function exportFile(path: string, filename: string) {
    const response = await fetch('/api/live/' + path, { headers: { 'X-Lulu-Client': 'desktop' } });
    if (!response.ok) throw new Error((await response.json()).detail || '导出失败');
    const blob = URL.createObjectURL(await response.blob()), link = document.createElement('a');
    link.href = blob; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(blob), 1000);
  }
  return <div className="live-workspace">
    <div className="live-tabs"><Button primary={tab === 'hosts'} onClick={() => setTab('hosts')}><Broadcast size={17}/>直播间 · {snapshot.hosts.length}</Button><Button primary={tab === 'recordings'} onClick={() => setTab('recordings')}>录像与互动 · {snapshot.recordings.length}</Button><span>同时监控上限 {snapshot.max_active} 位</span></div>
    {loadError && <p className="inline-error" role="alert">直播列表暂未刷新：{loadError}</p>}
    {!snapshot.ffmpeg && <p className="inline-error" role="alert">未检测到 FFmpeg，暂时无法录制。请通过安装脚本补齐运行环境。</p>}
    {!snapshot.comments_available && <p className="inline-error">弹幕组件未安装，录像和平台互动统计仍可使用。</p>}
    {tab === 'hosts' ? <>
      <section className="panel live-add"><div className="panel-heading"><h2>添加抖音直播间</h2><span>支持直播间、主播主页及公开分享链接</span></div>
        <div className="input-row"><input aria-label="抖音直播链接" placeholder="粘贴主播主页或直播间链接" value={url} onChange={event => setUrl(event.target.value)}/><Button primary disabled={!!busy || !url.trim()} onClick={() => run('add', add, '已添加主播，点击检查或开始监控')}><Plus size={17}/>添加</Button></div>
        <div className="live-options"><label>备注名称<input aria-label="主播备注名称" value={name} maxLength={200} onChange={event => setName(event.target.value)} placeholder="自动读取主播名"/></label><label>录像分段<select aria-label="录像分段长度" value={segmentMinutes} onChange={event => setSegmentMinutes(event.target.value)}>{[1,5,10,15,30,60].map(value => <option key={value} value={value}>{value} 分钟</option>)}</select></label><label><input type="checkbox" checked={autoRecord} onChange={event => setAutoRecord(event.target.checked)}/>开播后自动录制</label><label><input type="checkbox" checked={comments} onChange={event => setComments(event.target.checked)}/>录制时保存弹幕</label></div>
        <p className="footnote">开始后每分钟检查一次；关闭程序会停止监控并保留录像，重新打开后需手动开始。录像按分段保存，低于 256 MiB 可用空间时暂停。</p>
        <div className="live-login"><Button disabled={!!busy} onClick={() => run('login', async () => { if (!window.desktop) throw new Error('请在桌面版完成平台登录'); const result = await window.desktop.platform({ url: 'https://www.douyin.com/', loginOnly: true }); if (result.error) throw new Error(result.error); await refresh(); }, '平台窗口已关闭，下次检查会使用已保存会话')}><User size={16}/>登录抖音</Button><span>仅在平台要求登录时使用自己的账号；状态以平台返回为准。</span></div>
      </section>
      <section className="live-hosts" aria-label="主播监控列表">{snapshot.hosts.map(host => <article className="panel live-host" key={host.id}>
        <div className="live-host-title"><span className={`live-dot ${host.live_state === 'live' ? 'on' : ''}`}/><div><h3>{host.name}</h3><span>{host.message} · {host.live_state === 'live' ? '直播中' : host.live_state === 'offline' ? '未开播' : '直播状态未确认'}</span></div></div>
        {host.metrics.title && <p>{host.metrics.title}</p>}
        <dl className="live-metrics"><div><dt>在线人数</dt><dd>{count(host.metrics.viewer_count, host.metrics.viewer_count_raw)}</dd></div><div><dt>点赞</dt><dd>{count(host.metrics.like_count)}</dd></div><div><dt>记录方式</dt><dd>{host.auto_record ? `${host.segment_seconds / 60} 分钟分段` : '仅监控状态'}</dd></div></dl>
        <p className="footnote">上次检查：{stamp(host.checked_at)}</p>
        <div className="toolbar"><Button primary disabled={!!busy || host.enabled} onClick={() => run(host.id, () => api(`/live/hosts/${host.id}/start`, 'POST'))}><Play size={15}/>{host.auto_record ? '监控并录制' : '开始监控'}</Button><Button disabled={!!busy || !host.enabled} onClick={() => run(host.id, () => api(`/live/hosts/${host.id}/pause`, 'POST'))}><Pause size={15}/>暂停</Button><Button disabled={!!busy || host.enabled} onClick={() => run(host.id, () => api(`/live/hosts/${host.id}/check`, 'POST'))}><ArrowClockwise size={15}/>检查一次</Button><Button title="移除主播，保留历史录像" disabled={!!busy || host.enabled} onClick={() => run(host.id, () => api(`/live/hosts/${host.id}`, 'DELETE'), '已移除主播，历史录像保留')}><Trash size={15}/></Button></div>
      </article>)}</section>
      {!snapshot.hosts.length && <Empty icon={<Broadcast size={30}/>} title="从一位主播开始" text="添加公开链接后，先检查状态，再决定是否监控录制。没有返回的统计值显示为未提供。"/>}
    </> : <div className="live-recordings">
      <section className="panel live-history"><h2>已保存录像</h2>{snapshot.recordings.map(row => <button className={row.id === recordingId ? 'live-history-row selected' : 'live-history-row'} key={row.id} onClick={() => choose(row)}><strong>{row.name}</strong><span>{stamp(row.started)}</span><span>{recordingLabels[row.status] || row.status} · {row.segments.length} 段 · {duration(row.duration)}</span></button>)}{!snapshot.recordings.length && <p className="footnote">开播并开始录制后，会在这里显示录像与互动记录。</p>}</section>
      <section className="panel live-detail">{recording ? <><div className="panel-heading"><h2>{recording.title || recording.name}</h2><span>{recordingLabels[recording.status] || recording.status}</span></div>
        <p className="footnote">{stamp(recording.started)} · 已封装 {duration(recording.duration)} · {(recording.bytes / 1024**2).toFixed(1)} MiB</p>
        {recording.warning && <p className="inline-error">{recording.warning}</p>}
        {part ? <><video key={part.id} controls preload="metadata" src={`/api/live/recordings/${recording.id}/segments/${part.id}/media`}/><div className="toolbar"><label>录像分段<select aria-label="选择录像分段" value={part.id} onChange={event => setPartId(event.target.value)}>{recording.segments.map((entry, index) => <option key={entry.id} value={entry.id}>第 {index + 1} 段 · {duration(entry.duration)}</option>)}</select></label><Button disabled={!!busy} onClick={() => run('download', () => exportFile(`recordings/${recording.id}/segments/${part.id}/download`, `${recording.name}-${part.file}`))}><DownloadSimple size={16}/>下载本段</Button><Button disabled={!!busy} onClick={() => run('import', async () => { const item = await api(`/live/recordings/${recording.id}/segments/${part.id}/import`, 'POST'); await refresh(); navigate('extract'); select(item.id); }, '已加入素材库，可开始转录或复制到 Codex')}>加入素材库</Button></div></> : <div className="live-no-segment">{recording.ended ? '暂无可播放分段，可尝试恢复已有原始录像。' : '录制中，首个分段封装完成后即可回放。点击暂停会保存最后一段。'}</div>}
        <div className="toolbar"><Button disabled={!!busy || recording.ended === null} onClick={() => run('recover', () => api(`/live/recordings/${recording.id}/recover`, 'POST'), '已检查并恢复可用分段')}>恢复分段</Button><Button disabled={!!busy} onClick={() => run('metrics', () => exportFile(`recordings/${recording.id}/export/metrics`, '直播互动统计.csv'))}>导出互动统计</Button><Button disabled={!!busy} onClick={() => run('comments', () => exportFile(`recordings/${recording.id}/export/comments`, '直播弹幕.csv'))}>导出弹幕</Button></div>
        <div className="live-data-heading"><h3>互动记录</h3><span>{data?.comments_message || '正在读取'} · 已匹配 {data?.total || 0} 条弹幕</span></div>
        {data && data.metrics.length > 0 && <div className="live-table-scroll"><table><thead><tr><th>录制时间</th><th>在线人数</th><th>点赞</th></tr></thead><tbody>{data.metrics.slice(-6).map((entry, index) => <tr key={index}><td>{duration(entry.elapsed_seconds || 0)}</td><td>{count(entry.viewer_count, entry.viewer_count_raw)}</td><td>{count(entry.like_count)}</td></tr>)}</tbody></table></div>}
        <input aria-label="搜索直播弹幕" placeholder="搜索弹幕或昵称" value={query} onChange={event => { setQuery(event.target.value); setPage(1); }}/>
        <div className="live-comments">{data?.comments.map((entry, index) => <p key={entry.message_id || `${entry.received_at}-${index}`}><time>{duration(entry.elapsed_seconds)}</time><strong>{entry.nickname || '匿名用户'}</strong><span>{entry.text}</span></p>)}{data?.total === 0 && <p className="footnote">没有匹配的弹幕。无弹幕记录不代表直播间无人发言，请查看连接状态。</p>}</div>
        <div className="toolbar"><Button disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</Button><span>第 {page} 页</span><Button disabled={!data || page * 100 >= data.total} onClick={() => setPage(page + 1)}>下一页</Button></div>
      </> : <Empty icon={<Play size={28}/>} title="选择一场录像" text="查看分段回放、在线人数、点赞采样和弹幕。录像片段可以加入素材库继续转录。"/>}</section>
    </div>}
  </div>;
}
