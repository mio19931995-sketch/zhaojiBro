import { useEffect, useRef, useState } from 'react';
import { ArrowsInSimple, ArrowsOutSimple, ArrowSquareOut, BookOpen, CloudArrowUp, Copy, DownloadSimple, FileText, FloppyDisk, Folder, List, PencilSimple, Trash, X } from '@phosphor-icons/react';
import type { Shared } from './App';
import { api, duration, reveal } from './api';
import type { Item, Segment } from './api';
import { Button, Empty } from './components';
import { clearDraft, readDraft, writeDraft } from './drafts';
import { requestFeishu } from './Feishu';
import { HistoryDialog } from './Recovery';
import './inspector.css';

type Draft = { text: string; segments: Segment[]; tab: string };

// App keys this component by item ID so drafts and playback never leak to another item.
export function Inspector({ item, close, notify, refresh, navigate, select, state }: Shared & { item?: Item; close: () => void }) {
  const [detail, setDetail] = useState<Item | null>(null);
  const [draft, setDraft] = useState('');
  const [segments, setSegments] = useState<Segment[]>([]);
  const [dirty, setDirty] = useState(false);
  const [tab, setTab] = useState('text');
  const [saving, setSaving] = useState(false);
  const [videoFullscreen, setVideoFullscreen] = useState(false);
  const [playbackTime, setPlaybackTime] = useState(0);
  const [editingText, setEditingText] = useState(false);
  const [following, setFollowing] = useState(true);
  const [loopIndex, setLoopIndex] = useState<number | null>(null);
  const [speed, setSpeed] = useState(1);
  const [historyOpen, setHistoryOpen] = useState(false);
  const player = useRef<HTMLMediaElement | null>(null);
  const videoFrame = useRef<HTMLDivElement | null>(null);
  const documentBody = useRef<HTMLDivElement | null>(null);
  const syncedLines = useRef<Array<HTMLButtonElement | null>>([]);
  const timelineLines = useRef<Array<HTMLDivElement | null>>([]);
  const fullscreenRef = useRef(false);
  const loopRef = useRef<Segment | null>(null);
  fullscreenRef.current = videoFullscreen;

  function loadDetail(value: Item) {
    setDetail(value); setDraft(value.transcript); setSegments(value.segments);
  }

  useEffect(() => {
    let ignore = false;
    if (item && !dirty && !saving) {
      Promise.all([api<Item>(`/items/${item.id}`), readDraft<Draft>(item.id)]).then(([value, saved]) => {
        if (ignore) return;
        setDetail(value); setDraft(saved?.text ?? value.transcript); setSegments(saved?.segments ?? value.segments);
        if (saved) { setTab(saved.tab); setDirty(true); setEditingText(saved.tab === 'text'); setFollowing(false); }
      }).catch(e => { if (!ignore) notify(e.message, true); });
    }
    return () => { ignore = true; };
  }, [item?.id, item?.updated_at, dirty, saving, notify]);

  const active = !!item && ['processing', 'queued'].includes(item.status);
  const timed = !!detail?.segments.length;
  const lineCount = draft.replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n').length;
  const changedStructure = dirty && tab === 'text' && timed && lineCount !== detail?.segments.length;

  async function save() {
    if (!detail || saving) return;
    setSaving(true);
    try {
      const saved = await api<Item>(`/items/${detail.id}`, 'PATCH', tab === 'timeline' ? { segments } : { transcript: draft });
      await clearDraft(detail.id); loadDetail(saved); setDirty(false); setEditingText(false);
      await refresh(); notify('文稿已保存，修改前的版本已保留');
    } catch (e) { notify((e as Error).message, true); } finally { setSaving(false); }
  }

  function editText(text: string) {
    setDraft(text); setDirty(true);
    if (detail) writeDraft(detail.id, { text, segments, tab }).catch(e => notify('草稿保存失败：' + e.message, true));
  }

  function editSegments(value: Segment[]) {
    const text = value.map(s => s.text).join('\n');
    setSegments(value); setDraft(text); setDirty(true); setFollowing(false);
    if (detail) writeDraft(detail.id, { text, segments: value, tab }).catch(e => notify('草稿保存失败：' + e.message, true));
  }

  async function discard() {
    if (!detail || saving) return;
    if (!window.confirm('放弃尚未保存的修改，恢复到上次保存的文稿？')) return;
    setSaving(true);
    try { await clearDraft(detail.id); loadDetail(await api<Item>(`/items/${detail.id}`)); setDirty(false); setEditingText(false); }
    catch (e) { notify((e as Error).message, true); } finally { setSaving(false); }
  }

  async function saveScript() {
    if (!detail || !draft.trim() || saving) return;
    setSaving(true);
    try {
      const result = await api<Item>('/documents', 'POST', { title: detail.title + ' · 口播稿', text: draft, folder: detail.folder });
      await clearDraft(detail.id); setDirty(false); await refresh(); select(result.id);
      notify('已另存为口播稿，原字幕和时间轴已保留');
    } catch (e) { notify((e as Error).message, true); } finally { setSaving(false); }
  }

  async function restored() {
    if (!detail) return;
    await clearDraft(detail.id);
    loadDetail(await api<Item>(`/items/${detail.id}`));
    setDirty(false); setEditingText(false); setLoopIndex(null); setFollowing(true);
    await refresh();
  }

  async function exportIntegration(kind: string) {
    if (!detail) return;
    if (kind === 'feishu') { requestFeishu([detail.id]); return; }
    try { const r = await api(`/${kind}/export`, 'POST', { ids: [detail.id] }); notify(`已导出 ${r.count} 份文稿`); }
    catch (e) { notify((e as Error).message, true); }
  }

  useEffect(() => {
    const changed = () => setVideoFullscreen(document.fullscreenElement === videoFrame.current);
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && fullscreenRef.current && !document.fullscreenElement) {
        setVideoFullscreen(false); window.desktop?.setFullscreen?.(false).catch(() => {});
      }
    };
    const unsubscribe = window.desktop?.onFullscreenChange?.(setVideoFullscreen);
    document.addEventListener('fullscreenchange', changed); document.addEventListener('keydown', escape);
    return () => {
      unsubscribe?.(); document.removeEventListener('fullscreenchange', changed); document.removeEventListener('keydown', escape);
      if (fullscreenRef.current) window.desktop?.setFullscreen?.(false).catch(() => {});
    };
  }, []);

  async function toggleVideoFullscreen() {
    if (!videoFrame.current) return;
    try {
      if (window.desktop?.setFullscreen) {
        if (document.fullscreenElement) await document.exitFullscreen();
        setVideoFullscreen(await window.desktop.setFullscreen(!videoFullscreen));
      } else if (document.fullscreenElement) await document.exitFullscreen();
      else await videoFrame.current.requestFullscreen();
    } catch { notify('进入或退出全屏失败，请重试', true); }
  }

  const activeSegment = segments.findIndex(s => playbackTime >= s.start && playbackTime < s.end);
  const loop = loopIndex === null ? null : segments[loopIndex];
  loopRef.current = loop || null;
  useEffect(() => {
    if (!following || activeSegment < 0 || editingText || !['text', 'timeline'].includes(tab)) return;
    const container = documentBody.current;
    const line = tab === 'text' ? syncedLines.current[activeSegment] : timelineLines.current[activeSegment];
    if (!container || !line) return;
    const top = container.scrollTop + line.getBoundingClientRect().top - container.getBoundingClientRect().top;
    container.scrollTo({ top: Math.max(0, top), behavior: 'auto' });
  }, [following, activeSegment, playbackTime, editingText, tab, detail?.id, segments.length]);

  // Use animation frames while playing so a short sentence loops at its end,
  // rather than waiting for the browser's relatively infrequent timeupdate event.
  useEffect(() => {
    if (!loop) return;
    let frame = 0;
    const tick = () => {
      const media = player.current;
      const currentLoop = loopRef.current;
      if (media && currentLoop && !media.paused && !media.seeking && media.currentTime >= currentLoop.end) {
        media.currentTime = currentLoop.start; setPlaybackTime(currentLoop.start);
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [loop?.start, loop?.end]);

  function syncPlayback(media: HTMLMediaElement) { setPlaybackTime(media.currentTime); }
  function mediaEnded() {
    if (loop && player.current) { player.current.currentTime = loop.start; player.current.play().catch(() => {}); }
  }
  function mediaSeeked(media: HTMLMediaElement) {
    syncPlayback(media);
    const currentLoop = loopRef.current;
    if (currentLoop && (media.currentTime < currentLoop.start - 0.05 || media.currentTime > currentLoop.end + 0.05)) { loopRef.current = null; setLoopIndex(null); }
  }
  function playSegment(index: number) {
    const segment = segments[index];
    if (!player.current || !segment) return;
    if (loopIndex !== null) { loopRef.current = segment; setLoopIndex(index); }
    player.current.currentTime = segment.start; setPlaybackTime(segment.start);
    player.current.play().catch(() => notify('无法播放，请检查本地媒体文件', true));
  }
  function toggleLoop() {
    if (loopIndex !== null) { loopRef.current = null; setLoopIndex(null); return; }
    const next = activeSegment >= 0 ? activeSegment : Math.max(0, segments.findIndex(s => s.start >= playbackTime));
    if (!segments[next] || segments[next].end <= segments[next].start) return;
    loopRef.current = segments[next]; setLoopIndex(next); playSegment(next);
  }
  function beginEditing() {
    if (!editingText) {
      // Each visible line maps to one original caption; embedded newlines are normalized.
      if (segments.length) setDraft(segments.map(s => s.text.replace(/\r?\n/g, ' ')).join('\n'));
      setFollowing(false);
    }
    setEditingText(!editingText);
  }
  function stopFollowing() { if (segments.length && !editingText) setFollowing(false); }

  const video = detail && /\.(mp4|mov|webm|mkv|avi|m4v)$/i.test(detail.media_path);
  return <aside className="inspector">
    <div className="inspector-heading"><h2>文稿预览</h2><div className="inspector-heading-actions">
      {detail && <Button disabled={dirty || saving || active} title={dirty ? '先保存或放弃草稿，再查看历史版本' : '查看和恢复历史版本'} onClick={() => setHistoryOpen(true)}>历史版本</Button>}
      <Button title="关闭预览" onClick={close}><X size={16}/></Button>
    </div></div>
    {!detail ? <Empty icon={<FileText size={32}/>} title="在素材库打开任意一条" text="这里会显示完整的文稿、来源和试听。"/> : <>
      <div className="document-title"><h2>{detail.title}</h2><p>{detail.folder} · {new Date(detail.created_at * 1000).toLocaleDateString('zh-CN')}</p></div>
      {detail.metadata.cover_path && !video && <img className="inspector-cover" src={`/api/items/${detail.id}/cover`} alt="作品封面" onDoubleClick={() => reveal(detail.id, 'cover').catch(e => notify(e.message, true))}/>}
      {detail.media_path && <div className="media-player">{video ?
        <div ref={videoFrame} className={`video-frame ${videoFullscreen && !document.fullscreenElement ? 'window-fullscreen' : ''}`}>
          <video ref={el => { player.current = el; }} onTimeUpdate={e => syncPlayback(e.currentTarget)} onSeeked={e => mediaSeeked(e.currentTarget)} onEnded={mediaEnded} controls preload="metadata" poster={`/api/items/${detail.id}/cover`} src={`/api/items/${detail.id}/media`}/>
          <button className="video-fullscreen" title={videoFullscreen ? '退出全屏' : '全屏播放'} aria-label={videoFullscreen ? '退出全屏' : '全屏播放'} onClick={toggleVideoFullscreen}>{videoFullscreen ? <ArrowsInSimple size={18}/> : <ArrowsOutSimple size={18}/>}</button>
        </div> : <audio ref={el => { player.current = el; }} onTimeUpdate={e => syncPlayback(e.currentTarget)} onSeeked={e => mediaSeeked(e.currentTarget)} onEnded={mediaEnded} controls preload="metadata" src={`/api/items/${detail.id}/media`}/>}
      </div>}
      <div className="document-tabs"><div className="segmented">{[['text', '文稿'], ['timeline', '时间轴'], ['info', '信息']].map(([value, label]) =>
        <button disabled={dirty && value !== tab} className={tab === value ? 'selected' : ''} key={value} onClick={() => { setTab(value); if (value !== 'text') setEditingText(false); }}>{label}</button>
      )}</div><div className="transcript-tools">
        {tab === 'text' && segments.length > 0 && detail.media_path && <button disabled={editingText && dirty || saving || active} onClick={beginEditing}>{editingText ? '返回字幕' : '编辑全文'}</button>}
        <span>{(draft || '').length.toLocaleString()} 字</span>
      </div></div>
      {!!segments.length && detail.media_path && tab !== 'info' && <div className="caption-toolbar" aria-label="字幕播放控制">
        <button className="button" disabled={editingText} aria-pressed={following} onClick={() => setFollowing(!following)}>{following ? '跟随中' : '回到当前句'}</button>
        <button className="button" aria-pressed={loopIndex !== null} onClick={toggleLoop}>{loopIndex === null ? '单句循环' : '停止循环'}</button>
        <label>倍速<select aria-label="播放速度" value={speed} onChange={e => { const value = Number(e.target.value); setSpeed(value); if (player.current) player.current.playbackRate = value; }}>{[0.5, 0.75, 1, 1.25, 1.5, 2].map(value => <option key={value} value={value}>{value}×</option>)}</select></label>
      </div>}
      {editingText && timed && <p className="caption-edit-hint">一行对应一句字幕，校正文字时保留换行即可保留时间轴。增删句子或大幅改写请另存口播稿。</p>}
      <div className="document-body" ref={documentBody} onWheel={stopFollowing} onTouchMove={stopFollowing}
        onPointerDown={e => { if (e.target === e.currentTarget) stopFollowing(); }}
        onKeyDown={e => { if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(e.key)) stopFollowing(); }}>
        {tab === 'text' ? detail.transcript || dirty ? segments.length > 0 && detail.media_path && !editingText ?
          <div className="synced-transcript" aria-label="同步字幕">{segments.map((segment, index) =>
            <button ref={node => { syncedLines.current[index] = node; }} className={`synced-line ${index === activeSegment ? 'current' : ''} ${index === loopIndex ? 'looping' : ''}`} aria-current={index === activeSegment ? 'true' : undefined} title={`跳转到 ${duration(segment.start)}`} onClick={() => playSegment(index)} key={`${segment.start}-${index}`}><span>{duration(segment.start)}</span><strong>{segment.text}</strong></button>
          )}</div> : <textarea aria-label="文稿正文" value={draft} readOnly={active || saving} onChange={e => editText(e.target.value)} spellCheck={false}/> :
          <Empty icon={<FileText size={26}/>} title={active ? '正在生成文稿' : '还没有文稿'} text={active ? '识别出的文字会持续出现在这里。' : '开始转录后，在这里查看和编辑全文。'}/> :
          tab === 'timeline' ? segments.length ? <div className="segments">{segments.map((segment, index) =>
            <div ref={node => { timelineLines.current[index] = node; }} className={`segment ${index === activeSegment ? 'current' : ''} ${index === loopIndex ? 'looping' : ''}`} key={index}>
              <button title="播放这一段" onClick={() => playSegment(index)}>{duration(segment.start)}</button>
              <textarea aria-label={`第 ${index + 1} 段字幕`} readOnly={active || saving} value={segment.text} onFocus={stopFollowing} onChange={e => editSegments(segments.map((s, i) => i === index ? { ...s, text: e.target.value } : s))}/>
            </div>
          )}</div> : <Empty icon={<List size={26}/>} title="没有时间戳" text="转录音视频或导入 SRT 后，可以按段编辑和定位播放。"/> :
          <div className="info-list">
            <label>标题<input defaultValue={detail.title} key={detail.title} disabled={active} onBlur={async e => { const title = e.target.value.trim(); if (title && title !== detail.title) { try { await api(`/items/${detail.id}`, 'PATCH', { title }); await refresh(); } catch (err) { notify((err as Error).message, true); } } }}/></label>
            <label>文件夹<input defaultValue={detail.folder} key={detail.folder} disabled={active} onBlur={async e => { if (e.target.value.trim()) { await api(`/items/${detail.id}`, 'PATCH', { folder: e.target.value.trim() }).catch(err => notify(err.message, true)); await refresh(); } }}/></label>
            <label>来源<span>{detail.source_url || (detail.kind === 'voice' ? detail.metadata.source_id ? '文稿配音' : '手动输入配音' : '本地导入')}</span></label><label>状态<span>{detail.phase}</span></label><label>本地文件<span>{detail.media_path || '文稿保存在本地数据库中'}</span></label>
            {detail.kind === 'voice' && detail.metadata.rate !== undefined && <label>配音语速<span>{Number(detail.metadata.rate) > 0 ? '稍快' : Number(detail.metadata.rate) < 0 ? '稍慢' : '正常'}</span></label>}
            {detail.metadata.source_id && <Button disabled={!state.items.some(i => i.id === detail.metadata.source_id)} onClick={() => select(detail.metadata.source_id)}>{state.items.some(i => i.id === detail.metadata.source_id) ? '打开来源文稿' : '来源文稿已移除，可在回收站恢复'}</Button>}
            {detail.source_url && <Button onClick={() => window.desktop?.openExternal(detail.source_url)}>打开原作品<ArrowSquareOut size={15}/></Button>}
            <Button onClick={() => reveal(detail.id, detail.media_path ? 'media' : 'document').catch(e => notify(e.message, true))}>在文件夹中显示<Folder size={15}/></Button>
            <Button disabled={active} onClick={async () => { if (!window.confirm('将这份素材移入回收站？可以恢复，原始文件会保留。')) return; try { await api(`/items/${detail.id}`, 'DELETE'); await refresh(); notify('已移入回收站，可在左侧回收站恢复'); close(); } catch (e) { notify((e as Error).message, true); } }}><Trash size={15}/>移入回收站</Button>
          </div>}
      </div>
      <footer className="document-footer">{dirty ? <div className="caption-save-area">
        <p className={changedStructure ? 'caption-edit-warning' : ''}>{changedStructure ? '字幕句数已改变，无法直接保留时间轴。请恢复原句数，或另存口播稿。' : timed ? '草稿自动保留；保存字幕时保留时间轴' : '草稿自动保留在本地'}</p>
        <div className="save-row"><Button disabled={saving} onClick={discard}>放弃</Button>{tab === 'text' && timed && <Button disabled={saving || !draft.trim()} onClick={saveScript}>另存口播稿</Button>}<Button primary disabled={saving || active || changedStructure} onClick={save}><FloppyDisk size={15}/>保存</Button></div>
      </div> : <>
        <div className="export-row"><Button disabled={!detail.transcript} onClick={() => navigator.clipboard.writeText(draft).then(() => notify('已复制全文')).catch(() => notify('复制失败，请手动选中复制', true))}><Copy size={15}/>复制</Button>{['txt', 'md', 'srt'].map(fmt => <a className={`button ${(!detail.transcript || (fmt === 'srt' && !detail.segments.length)) ? 'disabled' : ''}`} key={fmt} href={`/api/items/${detail.id}/export/${fmt}`} download>{fmt.toUpperCase()}<DownloadSimple size={13}/></a>)}</div>
        <div className="export-row secondary"><Button disabled={!detail.transcript} onClick={() => navigate('process', detail.id)}><PencilSimple size={14}/>继续处理</Button><Button disabled={!detail.transcript} onClick={() => exportIntegration('feishu')}><CloudArrowUp size={14}/>存入飞书</Button><Button disabled={!detail.transcript} title="存入 Obsidian" onClick={() => exportIntegration('obsidian')}><BookOpen size={15}/></Button></div>
      </>}</footer>
      {historyOpen && <HistoryDialog itemId={detail.id} close={() => setHistoryOpen(false)} restored={restored} notify={notify}/>}
    </>}
  </aside>;
}
