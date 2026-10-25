import { useEffect, useRef, useState } from 'react';
import { ArrowCounterClockwise, Check, Quotes } from '@phosphor-icons/react';
import { api } from './api';
import type { Item } from './api';
import type { Shared } from './App';
import { Button } from './components';
import { Modal } from './Feishu';
import { selectedRange } from './aiWorkspaceTypes';
import type { Proposal, ProposalList, Reference, TextRange, Workspace } from './aiWorkspaceTypes';
import './ai-agent-tools.css';

export function ReferencePicker({ items, busy, add, close }: { items: Item[]; busy: boolean; add: (id: string, range?: TextRange) => Promise<void>; close: () => void }) {
  const [id, setId] = useState(''), [document, setDocument] = useState<Item | null>(null);
  const [range, setRange] = useState<TextRange | null>(null), [error, setError] = useState('');
  useEffect(() => {
    let valid = true; setDocument(null); setRange(null); setError('');
    if (id) api<Item>(`/items/${id}`).then(value => { if (valid) setDocument(value); }).catch(e => { if (valid) setError(e.message); });
    return () => { valid = false; };
  }, [id]);
  async function confirm(part?: TextRange) {
    if (!document) return;
    const snapshot: TextRange = part || { start: 0, end: new TextEncoder().encode(document.transcript).length, text: document.transcript, offset_unit: 'utf8' };
    try { await add(id, snapshot); close(); }
    catch (e) { setError((e as Error).message); }
  }
  return <Modal title="添加参考素材" close={close}>
    <div className="ai-reference-picker">
      <p>参考素材只提供上下文，建议仍只修改当前文稿。可引用全文，或在下方选择一段。</p>
      <label>参考文稿<select aria-label="选择参考文稿" value={id} disabled={busy} onChange={e => setId(e.target.value)}><option value="">请选择文稿</option>{items.filter(i => i.has_transcript && !['queued', 'processing'].includes(i.status)).map(i => <option key={i.id} value={i.id}>{i.title}</option>)}</select></label>
      {document && <><textarea aria-label="参考文稿内容" readOnly value={document.transcript} onSelect={e => setRange(selectedRange(e.currentTarget))}/><small>{document.transcript.length.toLocaleString()} 字；长文请选取所需片段。</small></>}
      {error && <p role="alert" className="inline-error">{error}</p>}
      <div className="form-actions"><Button disabled={busy || !document} onClick={() => confirm()}>引用全文</Button><Button primary disabled={busy || !range} onClick={() => range && confirm(range)}>引用选中内容</Button></div>
    </div>
  </Modal>;
}

export function ReferencePreview({ reference, locate, close }: { reference: Reference; locate?: () => void; close: () => void }) {
  return <Modal title="查看引用" close={close}><div className="ai-reference-picker"><h3>{reference.title}</h3><p>{reference.kind === 'draft' ? '工作区草稿片段' : '来源文稿快照'} · 只读参考</p>{reference.stale && <p role="alert" className="inline-error">{reference.stale_reason || '原文已变化，请重新添加引用。'}</p>}<pre>{reference.text}</pre>{locate && <Button disabled={reference.stale} onClick={() => { close(); requestAnimationFrame(locate); }}>定位到草稿</Button>}</div></Modal>;
}

export function ReferenceChips({ references, busy, preview, remove }: { references: Reference[]; busy: boolean; preview: (value: Reference) => void; remove: (value: Reference) => void }) {
  if (!references.length) return null;
  return <div className="ai-reference-chips" aria-label="本轮参考素材">{references.map((reference, index) => <div key={reference.id} className={reference.stale ? 'ai-reference-chip stale' : 'ai-reference-chip'}>
    <button title={`查看引用：${reference.title}`} onClick={() => preview(reference)}><Quotes size={13}/><span>{index + 1}. {reference.title}</span>{reference.stale && <small>已变化</small>}</button>
    <button aria-label={`移除引用：${reference.title}`} disabled={busy} onClick={() => remove(reference)}>×</button>
  </div>)}</div>;
}

export function ProposalDialog({ workspace, runId, busy, apply, locate, close, notify }: { workspace: Workspace; runId: string; busy: boolean; apply: (id: string, action: 'accept' | 'dismiss' | 'undo') => Promise<void>; locate: (proposal: Proposal) => void; close: () => void; notify: Shared['notify'] }) {
  const [plan, setPlan] = useState<ProposalList | null>(null), [error, setError] = useState('');
  const [loading, setLoading] = useState(true), [pending, setPending] = useState('');
  const alive = useRef(true);
  const request = useRef(0);
  const url = `/ai/workspaces/${workspace.id}/results/${runId}/proposals`;
  async function reload() {
    const generation = ++request.current;
    try { const result = await api<ProposalList>(url); if (alive.current && generation === request.current) { setPlan(result); setError(''); } }
    catch (e) { if (alive.current && generation === request.current) setError((e as Error).message); }
    finally { if (alive.current && generation === request.current) setLoading(false); }
  }
  useEffect(() => { alive.current = true; void reload(); return () => { alive.current = false; request.current++; }; }, [url, workspace.revision, workspace.source_hash]);
  async function act(id: string, action: 'accept' | 'dismiss' | 'undo') {
    setPending(id || 'undo');
    try { await apply(id, action); }
    catch (e) { notify((e as Error).message, true); }
    finally { if (alive.current) { setPending(''); void reload(); } }
  }
  const locked = busy || !!pending || loading;
  return <Modal title="逐项修改建议" close={close}>
    <div className="ai-proposal-header"><p>采纳先写入工作区草稿；确认后点击“保存文稿”，原文历史会保留。</p><Button disabled={locked || !plan?.can_undo} onClick={() => act('', 'undo')}><ArrowCounterClockwise size={14}/>撤销最近采纳</Button></div>
    {error && <p role="alert" className="inline-error">{error} <button onClick={reload}>重试</button></p>}
    {plan?.reason && <p className="ai-proposal-notice" role="status">{plan.reason}</p>}
    <div className="ai-proposal-list" aria-label="修改建议列表">
      {loading && <p>正在读取建议…</p>}
      {!loading && !error && !plan?.proposals.length && <p>结果与原文一致，没有需要采纳的修改。</p>}
      {plan?.proposals.map((proposal, index) => <article className="ai-proposal" data-state={proposal.state} key={proposal.id}>
        <header><strong>建议 {index + 1}</strong><span>{{ pending: '待确认', accepted: '已采纳到草稿', dismissed: '已忽略', reverted: '已撤销', superseded: '已有新结果' }[proposal.state] || proposal.state}</span><button disabled={locked || !plan.can_apply} onClick={() => { close(); requestAnimationFrame(() => locate(proposal)); }}>定位原文</button></header>
        <div className="ai-proposal-diff"><div><small>原文</small><pre>{proposal.before || '（插入位置）'}</pre></div><div><small>建议内容</small><pre>{proposal.after || '（删除这段内容）'}</pre></div></div>
        {proposal.blocked_reason && <p className="ai-proposal-notice">{proposal.blocked_reason}</p>}
        {['pending', 'reverted'].includes(proposal.state) && <div className="ai-proposal-actions"><Button primary disabled={locked || !plan.can_apply || proposal.can_apply === false} onClick={() => act(proposal.id, 'accept')}><Check size={14}/>采纳到草稿</Button><Button disabled={locked || !plan.can_apply} onClick={() => act(proposal.id, 'dismiss')}>忽略</Button></div>}
      </article>)}
    </div>
    <div className="form-actions"><Button onClick={close}>返回文稿</Button></div>
  </Modal>;
}
