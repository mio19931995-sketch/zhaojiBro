import { useCallback, useEffect, useState } from 'react';
import { ArrowClockwise, ArrowCounterClockwise, ClockCounterClockwise, FileText, Trash } from '@phosphor-icons/react';
import { api } from './api';
import type { Item, Segment } from './api';
import type { Shared } from './App';
import { Button, Empty } from './components';
import { Modal } from './Feishu';
import './recovery.css';

type TrashItem = Pick<Item, 'id' | 'title' | 'kind' | 'folder' | 'created_at' | 'updated_at'> & { deleted_at?: number; characters?: number };
type Version = { id: string; created_at: number; reason: string; title: string; characters: number; segment_count: number };
type VersionDetail = Version & { transcript: string; segments: Segment[] };
const itemKinds: Record<string, string> = { media: '音视频素材', document: '文稿', output: '处理后的文稿', voice: '配音', clip: '视频切片' };
const reasons: Record<string, string> = { edit: '编辑前', before_edit: '编辑前', transcript_edit: '修改全文前', segments_edit: '修改字幕前', retranscribe: '重新转录前', before_retranscribe: '重新转录前', restore: '恢复历史版本前', before_restore: '恢复历史版本前', delete: '移入回收站前' };
function dateTime(value: number) { return value ? new Date(value * 1000).toLocaleString('zh-CN') : '时间未知'; }

export function TrashView({ refresh, notify }: Pick<Shared, 'refresh' | 'notify'>) {
  const [items, setItems] = useState<TrashItem[]>([]);
  const [checked, setChecked] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [restoring, setRestoring] = useState(false);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    setLoading(true); setError('');
    try { setItems(await api<TrashItem[]>('/trash')); }
    catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const ids = checked.filter(id => items.some(item => item.id === id));
  const allChecked = items.length > 0 && ids.length === items.length;
  async function restore(selected: string[]) {
    if (!selected.length || restoring) return;
    setRestoring(true);
    try {
      const result = await api<{ count?: number }>('/trash/restore', 'POST', { ids: selected });
      setChecked(previous => previous.filter(id => !selected.includes(id)));
      await Promise.all([load(), refresh()]);
      notify(`已恢复 ${result.count ?? selected.length} 项，可在原素材库或任务列表中查看`);
    } catch (e) { notify((e as Error).message, true); }
    finally { setRestoring(false); }
  }
  return <section className="panel recovery-panel">
    <div className="recovery-heading"><div><h2>最近移除的内容</h2><p>恢复后会回到原来的文件夹，文稿和字幕时间轴一并保留。</p></div><Button title="刷新回收站" disabled={loading || restoring} onClick={() => void load()}><ArrowClockwise size={16}/>刷新</Button></div>
    <div className="recovery-toolbar">
      <label className="recovery-check"><input aria-label="全选回收站内容" type="checkbox" checked={allChecked} disabled={!items.length || loading || restoring} onChange={() => setChecked(allChecked ? [] : items.map(item => item.id))}/>全选</label>
      <span>已选 {ids.length} / 共 {items.length} 项</span>
      <Button primary disabled={!ids.length || loading || restoring} onClick={() => void restore(ids)}><ArrowCounterClockwise size={15}/>{restoring ? '正在恢复…' : '恢复所选'}</Button>
    </div>
    {error ? <div className="recovery-error" role="alert"><p>{error}</p><Button disabled={loading} onClick={() => void load()}>重新加载</Button></div> : loading ? <p className="recovery-status" role="status">正在读取回收站…</p> : !items.length ? <Empty icon={<Trash size={30}/>} title="回收站是空的" text="移除的任务和文稿会保留在这里，需要时可以恢复。"/> : <div className="recovery-list">{items.map(item => <article className="recovery-item" key={item.id}>
      <input aria-label={`选择恢复 ${item.title}`} type="checkbox" checked={ids.includes(item.id)} disabled={restoring} onChange={() => setChecked(previous => previous.includes(item.id) ? previous.filter(id => id !== item.id) : [...previous, item.id])}/>
      <div className="recovery-item-copy"><strong>{item.title}</strong><span>{itemKinds[item.kind] || '素材'} · {item.folder || '根目录'}</span><small>移除于 {dateTime(item.deleted_at || item.updated_at)}</small></div>
      <Button disabled={restoring} onClick={() => void restore([item.id])}><ArrowCounterClockwise size={15}/>恢复</Button>
    </article>)}</div>}
  </section>;
}

export function HistoryDialog({ itemId, close, restored, notify }: { itemId: string; close: () => void; restored: () => Promise<void>; notify: Shared['notify'] }) {
  const [versions, setVersions] = useState<Version[]>([]);
  const [selected, setSelected] = useState('');
  const [detail, setDetail] = useState<VersionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [error, setError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setLoading(true); setError(''); setSelected(''); setDetail(null);
    api<Version[]>(`/items/${itemId}/versions`).then(result => {
      if (!cancelled) { setVersions(result); setSelected(result[0]?.id || ''); }
    }).catch(e => { if (!cancelled) setError(e.message); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [itemId, reload]);
  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    let cancelled = false;
    setLoadingDetail(true); setDetail(null); setDetailError('');
    api<VersionDetail>(`/items/${itemId}/versions/${selected}`).then(result => { if (!cancelled) setDetail(result); })
      .catch(e => { if (!cancelled) setDetailError(e.message); }).finally(() => { if (!cancelled) setLoadingDetail(false); });
    return () => { cancelled = true; };
  }, [itemId, selected, reload]);
  async function restore() {
    if (!detail || restoring || loadingDetail) return;
    setRestoring(true);
    try {
      await api(`/items/${itemId}/versions/${selected}/restore`, 'POST');
      await restored();
      notify('历史版本已恢复，恢复前的内容也已保留为一个版本');
      close();
    } catch (e) { notify((e as Error).message, true); }
    finally { setRestoring(false); }
  }
  return <Modal title="文稿历史版本" close={() => { if (!restoring) close(); }}>
    <div className="history-dialog-body">
      <p className="history-intro">选择一个版本查看正文。恢复后会替换当前文稿和字幕时间轴；当前已保存的内容会先保留为一个版本。</p>
      {loading ? <p className="recovery-status" role="status">正在读取历史版本…</p> : error ? <div className="recovery-error" role="alert"><p>{error}</p><Button onClick={() => setReload(value => value + 1)}>重新加载</Button></div> : !versions.length ? <Empty icon={<ClockCounterClockwise size={30}/>} title="还没有历史版本" text="编辑保存、重新转录或恢复旧版本前，会自动保留当前已保存的文稿。"/> : <div className="history-layout">
        <nav className="history-version-list" aria-label="历史版本">{versions.map(version => <button key={version.id} className={selected === version.id ? 'selected' : ''} aria-pressed={selected === version.id} disabled={restoring} onClick={() => setSelected(version.id)}>
          <strong>{reasons[version.reason] || version.reason || '已保存版本'}</strong><span>{dateTime(version.created_at)}</span><small>{version.characters.toLocaleString()} 字 · {version.segment_count} 段字幕</small>
        </button>)}</nav>
        <section className="history-preview" aria-label="版本内容预览">
          {loadingDetail ? <p className="recovery-status" role="status">正在读取版本正文…</p> : detailError ? <div className="recovery-error" role="alert"><p>{detailError}</p><Button onClick={() => setReload(value => value + 1)}>重新加载</Button></div> : detail && <><h3>{detail.title}</h3>{detail.transcript ? <div className="history-preview-text" tabIndex={0}>{detail.transcript}</div> : <Empty icon={<FileText size={24}/>} title="这个版本没有正文" text="恢复它会将文稿还原为当时的状态。"/>}</>}
        </section>
      </div>}
      <div className="form-actions history-actions"><Button disabled={restoring} onClick={close}>关闭</Button><Button primary disabled={!detail || loading || loadingDetail || restoring || !!error || !!detailError} onClick={() => void restore()}><ArrowCounterClockwise size={15}/>{restoring ? '正在恢复…' : '恢复此版本'}</Button></div>
    </div>
  </Modal>;
}
