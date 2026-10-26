import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowClockwise, ArrowRight, Check, ChatCircleText, FloppyDisk, PaperPlaneRight, Plus, Stop } from '@phosphor-icons/react';
import type { Shared } from './App';
import { api } from './api';
import { Button, Empty } from './components';
import { readDraft } from './drafts';
import { ProposalDialog, ReferenceChips, ReferencePicker, ReferencePreview } from './AIAgentTools';
import { locateRange, rangeText, selectedRange } from './aiWorkspaceTypes';
import type { Message, Reference, Run, TargetRange, TextRange, Workspace } from './aiWorkspaceTypes';
import { AgentManager } from './AgentManager';
import type { AgentTab } from './AgentManager';
import { AgentRunTools } from './AgentRunTools';
import type { AgentAction, SkillEntry, ToolResult } from './agentRuntimeTypes';
import { prepareAgentHandoff } from './agentHandoff';
import './ai-workbench.css';

type Session = { id: string; title: string; updated_at: number };
type Preset = { id: string; title: string; instruction: string };

function storedTarget(id: string): TargetRange | null {
  try { const value = JSON.parse(localStorage.getItem(`ai-target-${id}`) || 'null'); return value && typeof value.text === 'string' && Number.isInteger(value.revision) && value.offset_unit === 'utf8' ? value : null; } catch { return null; }
}

export function AIWorkbench({ state, processingSource, navigate, notify, refresh, select }: Shared) {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [title, setTitle] = useState('新文稿');
  const [text, setText] = useState('');
  const [instruction, setInstruction] = useState('');
  const [preset, setPreset] = useState('');
  const [busy, setBusy] = useState(false);
  const [draftStatus, setDraftStatus] = useState('草稿已保存');
  const [loadError, setLoadError] = useState('');
  const [retryLoad, setRetryLoad] = useState(0);
  const [preview, setPreview] = useState<Message | null>(null);
  const [selection, setSelection] = useState<(TextRange & { basis: string }) | null>(null);
  const [target, setTarget] = useState<TargetRange | null>(null);
  const [referencePicker, setReferencePicker] = useState(false);
  const [referencePreview, setReferencePreview] = useState<Reference | null>(null);
  const [proposalRun, setProposalRun] = useState('');
  const [agentTab, setAgentTab] = useState<AgentTab | null>(null);
  const [skills, setSkills] = useState<SkillEntry[]>([]), [skill, setSkill] = useState('');
  const current = useRef<Workspace | null>(null);
  const draft = useRef({ title: '新文稿', text: '', instruction: '', preset: '' });
  const saved = useRef({ title: '', text: '', instruction: '', preset: '' });
  const writes = useRef<Promise<void>>(Promise.resolve());
  const pollVersion = useRef(0);
  const alive = useRef(true);
  const conversation = useRef<HTMLDivElement>(null);
  const editor = useRef<HTMLTextAreaElement>(null);
  draft.current = { title, text, instruction, preset };
  const source = workspace?.source_id || processingSource;
  const run = workspace?.run;
  const compactRun = workspace?.compact_run;
  const active = [run, compactRun].some(value => value && ['queued', 'running'].includes(value.status));
  const dirty = title !== saved.current.title || text !== saved.current.text || instruction !== saved.current.instruction || preset !== saved.current.preset;
  const draftLabel = dirty && draftStatus === '草稿已保存' ? '等待保存…' : draftStatus;
  const staleTarget = !!target && (target.revision !== workspace?.revision || text !== saved.current.text || title !== saved.current.title);

  function changeTarget(value: TargetRange | null) {
    setTarget(value);
    if (current.current) { try { if (value) localStorage.setItem(`ai-target-${current.current.id}`, JSON.stringify(value)); else localStorage.removeItem(`ai-target-${current.current.id}`); } catch {} }
  }

  const load = useCallback((value: Workspace) => {
    pollVersion.current++;
    current.current = value;
    saved.current = { title: value.title, text: value.text, instruction: value.instruction || '', preset: value.preset || '' };
    draft.current = saved.current;
    setWorkspace(value); setText(value.text); setTitle(value.title); setPreview(null);
    setInstruction(value.instruction || ''); setPreset(value.preset || '');
    setDraftStatus('草稿已保存'); setLoadError('');
    setSelection(null); setTarget(storedTarget(value.id));
    try { setSkill(localStorage.getItem(`ai-skill-${value.id}`) || ''); } catch { setSkill(''); }
  }, []);

  const persist = useCallback(async () => {
    const owner = current.current;
    if (!owner) return;
    const value = { ...draft.current };
    writes.current = writes.current.catch(() => {}).then(async () => {
      if (!current.current || current.current.id !== owner.id) return;
      if (value.title === saved.current.title && value.text === saved.current.text && value.instruction === saved.current.instruction && value.preset === saved.current.preset) return;
      if (alive.current) setDraftStatus('正在保存草稿…');
      try {
        if (value.title !== saved.current.title || value.text !== saved.current.text) {
          const result = await api<{ revision: number; text?: string; title?: string }>(`/ai/workspaces/${owner.id}/draft`, 'PUT', { title: value.title.trim() || '新文稿', text: value.text, revision: current.current.revision });
          const canonicalText = result.text ?? value.text, canonicalTitle = result.title ?? value.title;
          // Canonical simplification must not leave browser byte ranges pointing at the pre-save text.
          // Do not overwrite characters typed while this save was in flight.
          if (alive.current && draft.current.text === value.text && canonicalText !== value.text) { draft.current.text = canonicalText; setText(canonicalText); setSelection(null); }
          if (alive.current && draft.current.title === value.title && canonicalTitle !== value.title) { draft.current.title = canonicalTitle; setTitle(canonicalTitle); }
          value.text = canonicalText; value.title = canonicalTitle;
          current.current = { ...current.current, title: value.title, text: value.text, revision: result.revision };
          saved.current = { ...saved.current, title: value.title, text: value.text };
          if (alive.current) setWorkspace(previous => previous?.id === owner.id ? { ...previous, revision: result.revision } : previous);
        }
        if (value.instruction !== saved.current.instruction || value.preset !== saved.current.preset) {
          await api(`/ai/workspaces/${owner.id}/composer`, 'PUT', { instruction: value.instruction, preset: value.preset });
          current.current = { ...current.current, instruction: value.instruction, preset: value.preset };
        }
        saved.current = value;
        if (alive.current) setDraftStatus('草稿已保存');
      } catch (error) {
        if (alive.current) setDraftStatus('草稿保存失败，请重试');
        throw error;
      }
    });
    return writes.current;
  }, []);

  useEffect(() => {
    alive.current = true;
    let cancelled = false;
    (async () => {
      const [list, shortcuts] = await Promise.all([api<Session[]>(`/ai/workspaces?source_id=${encodeURIComponent(processingSource)}`), api<Preset[]>('/ai/presets')]);
      if (cancelled) return;
      setSessions(list); setPresets(shortcuts);
      let value: Workspace;
      if (list.length) value = await api<Workspace>(`/ai/workspaces/${list[0].id}`);
      else {
        const legacy = await readDraft<{ source: string; text: string; title: string; action: string; instruction: string }>(`process-text-${processingSource || 'manual'}`);
        const oldActions: Record<string, string> = { rewrite: '将文稿改写为适合发布的中文文案，去掉口头重复，保持事实、数据、链接和观点。只返回正文。', outline: '从原文提炼结构化大纲：主题、论点、例子、结论。标明原文没有给出的信息，不补造。', translate: '将正文翻译为自然的简体中文；原文为中文时翻译为英文。保留段落、数字和链接，只返回译文。' };
        const restored = legacy && typeof legacy.text === 'string' ? { title: legacy.title || '新文稿', text: legacy.text, instruction: [oldActions[legacy.action] || shortcuts.find(p => p.id === legacy.action)?.instruction, legacy.instruction].filter(Boolean).join('\n'), preset: legacy.action || '' } : undefined;
        value = await api<Workspace>('/ai/workspaces', 'POST', { source_id: legacy?.source || processingSource, draft: restored });
      }
      if (!cancelled) {
        load(value);
        if (!list.length) setSessions([{ id: value.id, title: value.title, updated_at: Date.now() / 1000 }]);
      }
    })().catch(error => { if (!cancelled) setLoadError(error.message); });
    return () => { cancelled = true; alive.current = false; persist().catch(() => {}); };
  }, [processingSource, load, persist, retryLoad]);

  useEffect(() => {
    if (!workspace) return;
    const timer = setTimeout(() => persist().catch(error => notify(error.message, true)), 450);
    return () => clearTimeout(timer);
  }, [text, title, instruction, preset, workspace?.id, persist, notify]);

  useEffect(() => {
    if (!workspace?.id) return;
    let cancelled = false;
    const timer = setInterval(async () => {
      const generation = pollVersion.current;
      try {
        const value = await api<Workspace>(`/ai/workspaces/${workspace.id}`);
        if (cancelled || current.current?.id !== value.id || generation !== pollVersion.current) return;
        const runtime = { references: value.references, messages: value.messages, run: value.run, run_id: value.run_id, compact_run: value.compact_run, context: value.context, agent_config: value.agent_config };
        current.current = { ...current.current!, ...runtime };
        setWorkspace(previous => previous ? { ...previous, ...runtime } : previous);
      } catch {}
    }, active ? 650 : 2000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [workspace?.id, active]);

  useEffect(() => {
    let valid = true;
    api<{ entries: SkillEntry[] }>('/ai/skills').then(value => { if (valid) setSkills(value.entries); }).catch(error => notify(error.message, true));
    return () => { valid = false; };
  }, [workspace?.id, notify]);

  useEffect(() => { conversation.current?.scrollTo({ top: conversation.current.scrollHeight }); }, [workspace?.messages.length, run?.status]);

  async function switchSession(id: string) {
    setBusy(true);
    try { await persist(); load(await api(`/ai/workspaces/${id}`)); }
    catch (error) { notify((error as Error).message, true); } finally { setBusy(false); }
  }
  async function createSession() {
    setBusy(true);
    try {
      await persist();
      const value = await api<Workspace>('/ai/workspaces', 'POST', { source_id: source || '', title, text: source ? '' : text });
      load(value);
      setSessions(await api(`/ai/workspaces?source_id=${encodeURIComponent(value.source_id)}`));
    } catch (error) { notify((error as Error).message, true); } finally { setBusy(false); }
  }
  async function chooseSource(id: string) {
    setBusy(true);
    try {
      await persist();
      if (id === processingSource) {
        const list = await api<Session[]>(`/ai/workspaces?source_id=${encodeURIComponent(id)}`);
        const value = list.length ? await api<Workspace>(`/ai/workspaces/${list[0].id}`) : await api<Workspace>('/ai/workspaces', 'POST', { source_id: id });
        load(value); setSessions(list.length ? list : [{ id: value.id, title: value.title, updated_at: Date.now() / 1000 }]);
      } else navigate('process', id);
    } catch (error) { notify((error as Error).message, true); } finally { setBusy(false); }
  }
  async function send(value = instruction, selectedPreset = preset, retry?: Run) {
    if (!value.trim() || !workspace) return;
    setBusy(true);
    try {
      await persist();
      if (!retry && target && (target.revision !== current.current!.revision || rangeText(current.current!.text, target) !== target.text)) throw Error('选中片段的文稿版本已变化，请重新选择，或点击“处理全文”。');
      await api(`/ai/workspaces/${workspace.id}/runs`, 'POST', { revision: current.current!.revision, instruction: value, preset: selectedPreset, selection: retry ? retry.selection : target, skill: retry ? retry.skill || '' : skill, reference_ids: (retry ? retry.references || [] : current.current!.references || []).map(reference => reference.id), ...(retry ? { retry_of: retry.id } : {}) });
      const update = await api<Workspace>(`/ai/workspaces/${workspace.id}`);
      setWorkspace(previous => previous ? { ...previous, messages: update.messages, run: update.run, run_id: update.run_id } : previous);
      setInstruction(''); setPreset(''); setPreview(null);
    } catch (error) { notify((error as Error).message, true); } finally { setBusy(false); }
  }
  async function cancel() {
    if (!workspace) return;
    try { await api(`/ai/workspaces/${workspace.id}/cancel`, 'POST'); await reloadRuntime(); }
    catch (error) { notify((error as Error).message, true); }
  }
  async function saveDocument() {
    if (!workspace) return;
    setBusy(true);
    try {
      await persist();
      const result = await api<{ item_id: string; workspace: Workspace }>(`/ai/workspaces/${workspace.id}/save-document`, 'POST', { revision: current.current!.revision });
      current.current = { ...current.current!, source_id: result.workspace.source_id, source_hash: result.workspace.source_hash };
      setWorkspace(previous => previous ? { ...previous, source_id: result.workspace.source_id, source_hash: result.workspace.source_hash } : previous);
      await refresh(); notify('文稿已保存，原有版本可在历史版本中恢复');
    } catch (error) { notify((error as Error).message, true); } finally { setBusy(false); }
  }
  async function saveResult(message: Message, apply = false) {
    if (!workspace) return;
    if (apply && !window.confirm('将这份结果应用到来源文稿？现有文稿会保留到历史版本；时间轴不匹配时会阻止覆盖。')) return;
    setBusy(true);
    try {
      await persist();
      const result = await api<{ item_id: string }>(`/ai/workspaces/${workspace.id}/results/${message.run_id}/${apply ? 'apply' : 'save-output'}`, 'POST', apply ? { revision: current.current!.revision } : undefined);
      await refresh();
      if (apply) load(await api(`/ai/workspaces/${workspace.id}`));
      else { navigate('output-library'); select(result.item_id); }
      notify(apply ? '已应用并保存历史版本' : '结果已另存到输出库');
    } catch (error) { notify((error as Error).message, true); } finally { setBusy(false); }
  }

  async function useSelection() {
    if (!selection) return;
    setBusy(true);
    try {
      await persist();
      if (selection.basis !== current.current!.text || rangeText(current.current!.text, selection) !== selection.text) throw Error('正文已变化或已转为简体，请重新选择片段。');
      changeTarget({ start: selection.start, end: selection.end, text: selection.text, offset_unit: 'utf8', revision: current.current!.revision });
    } catch (error) { notify((error as Error).message, true); } finally { setBusy(false); }
  }
  async function addReference(id: string, range?: TextRange) {
    pollVersion.current++;
    setBusy(true);
    try {
      await persist();
      if (!id && selection?.basis !== current.current!.text) throw Error('正文已变化或已转为简体，请重新选择引用。');
      const result = await api<{ workspace: Workspace }>(`/ai/workspaces/${current.current!.id}/references`, 'POST', { revision: current.current!.revision, source_id: id, ...(range ? { start: range.start, end: range.end, expected_text: range.text, offset_unit: 'utf8' } : {}) });
      current.current = { ...current.current!, references: result.workspace.references };
      setWorkspace(previous => previous ? { ...previous, references: result.workspace.references } : previous);
      notify('已添加只读参考；发送时会一起交给模型');
    } finally { pollVersion.current++; setBusy(false); }
  }
  async function removeReference(reference: Reference) {
    if (!workspace) return;
    pollVersion.current++;
    setBusy(true);
    try {
      const result = await api<{ workspace: Workspace }>(`/ai/workspaces/${workspace.id}/references/${reference.id}`, 'DELETE');
      current.current = { ...current.current!, references: result.workspace.references };
      setWorkspace(previous => previous ? { ...previous, references: result.workspace.references } : previous);
    } catch (error) { notify((error as Error).message, true); } finally { pollVersion.current++; setBusy(false); }
  }
  async function proposalAction(id: string, action: 'accept' | 'dismiss' | 'undo') {
    if (!workspace || !proposalRun) return;
    setBusy(true);
    try {
      await persist();
      const result = await api<{ workspace: Workspace }>(`/ai/workspaces/${workspace.id}/results/${proposalRun}/${action === 'undo' ? 'undo' : `proposals/${id}/${action}`}`, 'POST', { revision: current.current!.revision });
      load(result.workspace);
      if (action !== 'dismiss') changeTarget(null);
      notify(action === 'accept' ? '已采纳到草稿，确认后请保存文稿' : action === 'undo' ? '已撤销最近一次采纳' : '已忽略这条建议');
    } finally { setBusy(false); }
  }
  function locate(reference: Reference) {
    try { if (editor.current) locateRange(editor.current, reference); }
    catch (error) { notify((error as Error).message, true); }
  }
  async function reloadRuntime() {
    const owner = current.current?.id;
    if (!owner) return;
    const value = await api<Workspace>(`/ai/workspaces/${owner}`);
    if (owner !== current.current?.id) return;
    pollVersion.current++;
    const runtime = { references: value.references, messages: value.messages, run: value.run, run_id: value.run_id, compact_run: value.compact_run, context: value.context, agent_config: value.agent_config };
    current.current = { ...current.current, ...runtime };
    setWorkspace(previous => previous ? { ...previous, ...runtime } : previous);
  }
  function chooseSkill(name: string) {
    setSkill(name);
    if (current.current) { try { localStorage.setItem(`ai-skill-${current.current.id}`, name); } catch {} }
  }
  async function compact() {
    setBusy(true);
    try { await persist(); await api(`/ai/workspaces/${current.current!.id}/compact`, 'POST', { revision: current.current!.revision }); await reloadRuntime(); }
    finally { setBusy(false); }
  }
  async function confirmAction(action: AgentAction, dismiss: boolean) {
    if (!workspace || !run) return;
    setBusy(true);
    try {
      await persist();
      await api(`/ai/workspaces/${workspace.id}/runs/${run.id}/actions/${action.id}/${dismiss ? 'dismiss' : 'confirm'}`, 'POST', { revision: current.current!.revision });
      await reloadRuntime(); await refresh();
      const listing = await api<{ entries: SkillEntry[] }>('/ai/skills'); setSkills(listing.entries);
      notify(dismiss ? '已忽略此操作' : '操作已完成，可查看执行结果');
    } catch (error) { notify((error as Error).message, true); } finally { setBusy(false); }
  }
  async function handoff(result: ToolResult) {
    if (result.action === 'skills') { setAgentTab('skills'); return; }
    if (!run || !['voice', 'transcription', 'collection'].includes(result.action || '')) return;
    setBusy(true);
    try {
      await persist();
      prepareAgentHandoff({ id: `${run.id}-${Date.now()}`, action: result.action as 'voice' | 'transcription' | 'collection', text: result.text ?? run.input_text, source_id: workspace?.source_id || '', url: result.url, limit: result.limit });
      navigate(result.action === 'voice' ? 'voice' : result.action === 'collection' ? 'collect' : 'extract');
      notify('已带入内容，请在工作台确认配置后开始');
    } catch (error) { notify((error as Error).message, true); } finally { setBusy(false); }
  }

  if (loadError) return <section className="panel ai-load-error"><p>{loadError}</p><Button onClick={() => { setLoadError(''); setRetryLoad(value => value + 1); }}>重试打开</Button>{processingSource && <Button onClick={() => navigate('process', '')}>打开手动文稿</Button>}</section>;
  if (!workspace) return <section className="panel ai-load-error"><p>正在打开文稿工作区…</p></section>;
  return <section className="ai-workbench" aria-label="AI 文稿工作区">
    <div className="ai-document panel">
      <div className="ai-document-heading"><label>当前文稿<select aria-label="选择原文" value={source || ''} disabled={busy} onChange={event => chooseSource(event.target.value)}><option value="">手动输入文稿</option>{state.items.filter(item => item.has_transcript).map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label><Button disabled={busy || !text.trim()} onClick={saveDocument}><FloppyDisk size={15}/>{source ? '保存文稿' : '存为文稿'}</Button></div>
      <input className="ai-document-title" aria-label="文稿标题" value={title} maxLength={500} disabled={busy} onChange={event => setTitle(event.target.value)}/>
      <div className="ai-selection-tools"><Button disabled={busy || active || !selection} onClick={useSelection}>仅处理选中片段</Button><Button disabled={busy || active || !selection || (workspace.references?.length || 0) >= 8} onClick={() => selection && addReference('', selection).catch(error => notify(error.message, true))}>引用选中片段</Button><span>{selection ? `已选 ${selection.text.length} 字` : '在正文中选择文字'}</span></div>
      <textarea ref={editor} className="ai-document-editor" aria-label="待处理文案" placeholder="选择已有文稿，或粘贴需要处理的正文…" value={text} maxLength={150000} disabled={busy} onSelect={event => { const value = selectedRange(event.currentTarget); setSelection(value ? { ...value, basis: event.currentTarget.value } : null); }} onChange={event => { setText(event.target.value); setSelection(null); }}/>
      <div className="ai-document-status"><span>{text.length.toLocaleString()} 字</span><span className={draftStatus.includes('失败') ? 'inline-error' : ''}>{draftLabel}</span>{draftStatus.includes('失败') && <button onClick={() => persist().catch(error => notify(error.message, true))}>重试保存</button>}</div>
      <p className="ai-document-footnote">编辑先保存在工作区草稿；点击“保存文稿”后才更新来源。字幕段数变化时请另存结果。</p>
    </div>
    <div className="ai-assistant panel">
      <div className="ai-assistant-heading"><span><ChatCircleText size={19}/>文稿助手</span><Button disabled={busy} onClick={createSession}><Plus size={14}/>新建对话</Button></div>
      <select aria-label="文稿对话记录" value={workspace.id} disabled={busy} onChange={event => switchSession(event.target.value)}>{sessions.map((session, index) => <option value={session.id} key={session.id}>{session.title} · {new Date(session.updated_at * 1000).toLocaleString('zh-CN')} {index === 0 ? '（最近）' : ''}</option>)}</select>
      <div className="ai-model-line"><span>{state.settings.llm_model || '尚未配置文本模型'}</span><button onClick={() => navigate('models')}>配置模型</button></div>
      <div className="ai-agent-bar"><button onClick={() => setAgentTab('context')}>上下文约 {workspace.context?.percent || 0}%{workspace.context?.checkpoint ? ' · 已有摘要' : ''}</button><button onClick={() => setAgentTab('tools')}>工具与技能{workspace.agent_config?.tools_enabled ? ' · 工具已开启' : ''}</button></div>
      <div className="ai-conversation" ref={conversation} aria-label="文稿助手对话">
        {!workspace.messages.length && <Empty icon={<ChatCircleText size={27}/>} title="先告诉我想怎么处理" text="校正、润色、摘要或拆解。结果先展示，是否写回由你决定。"/>}
        {workspace.messages.map(message => message.role === 'tool' ? <details className="agent-tool-event" key={message.id}><summary>工具执行记录</summary><pre>{message.text}</pre></details> : message.role === 'status' ? <p className="ai-proposal-notice" key={message.id}>{message.text}</p> : <article key={message.id} className={`ai-message ${message.role}`}>
          <span className="ai-message-label">{message.role === 'user' ? '你的要求' : '处理结果'}{message.scope === 'selection' ? ' · 选中片段' : ''}</span><div className="ai-message-text">{message.text}</div>
          {!!message.references?.length && <div className="ai-message-references">{message.references.map(reference => <button title={`查看引用：${reference.title}`} key={reference.id} onClick={() => setReferencePreview(reference)}>引用：{reference.title}</button>)}</div>}
          {message.role === 'assistant' && <div className="ai-result-actions">
            {!message.non_edit_result && <Button disabled={busy || active} onClick={() => setProposalRun(message.run_id)}>逐项建议</Button>}
            {!message.non_edit_result && <Button disabled={busy} onClick={() => setPreview(preview?.id === message.id ? null : message)}>对照原文</Button>}
            <Button disabled={busy} onClick={() => saveResult(message)}><FloppyDisk size={14}/>另存结果</Button>
            {!message.non_edit_result && message.scope !== 'selection' && !(run?.id === message.run_id && run.proposal_used) && <Button disabled={busy || !source || active} onClick={() => saveResult(message, true)}><Check size={14}/>应用到文稿</Button>}
          </div>}
        </article>)}
        {run && <AgentRunTools run={run} busy={busy || active} confirm={confirmAction} handoff={handoff} openDocument={id => { navigate('library'); select(id); }}/>}
        {run && <div className={`ai-run-status ${run.status === 'error' ? 'is-error' : ''}`} role="status"><span>{run.phase}</span>{['queued', 'running'].includes(run.status) && <progress max={run.total || 1} value={Math.max(0, run.current - 1)}/>}<div>{['queued', 'running'].includes(run.status) ? <Button onClick={cancel}><Stop size={13}/>取消处理</Button> : ['error', 'cancelled', 'interrupted'].includes(run.status) && <Button disabled={busy || active} onClick={() => send(run.instruction, run.preset, run)}><ArrowClockwise size={13}/>重试本次要求</Button>}</div></div>}
        {compactRun && <div className={`ai-run-status ${compactRun.status === 'error' ? 'is-error' : ''}`} role="status"><span>{compactRun.phase}</span>{['queued', 'running'].includes(compactRun.status) ? <Button onClick={cancel}><Stop size={13}/>取消压缩</Button> : ['error', 'cancelled', 'interrupted'].includes(compactRun.status) && <Button disabled={busy || active} onClick={() => compact().catch(error => notify(error.message, true))}>重试压缩</Button>}</div>}
      </div>
      {preview && <div className="ai-comparison" aria-label="原文结果对照"><div><strong>当前草稿</strong><pre>{text}</pre></div><div><strong>处理结果</strong><pre>{preview.text}</pre></div><button onClick={() => setPreview(null)}>收起对照</button></div>}
      <div className="ai-composer">
        <div className="ai-context-heading"><span>{target ? '修改范围：选中片段' : '修改范围：全文'} · 参考 {workspace.references?.length || 0}/8</span><Button disabled={busy || active || (workspace.references?.length || 0) >= 8} onClick={() => setReferencePicker(true)}>添加参考素材</Button></div>
        {target && <div className={`ai-target-scope ${staleTarget ? 'stale' : ''}`} role="status"><span title={target.text}>{staleTarget ? '文稿已变化，请重新选取片段' : `仅处理：${target.text}`}</span><button disabled={busy || active} onClick={() => changeTarget(null)}>处理全文</button></div>}
        <ReferenceChips references={workspace.references || []} busy={busy || active} preview={setReferencePreview} remove={removeReference}/>
        <label className="ai-skill-choice">技能<select aria-label="本轮技能" disabled={busy || active} value={skill} onChange={e => chooseSkill(e.target.value)}><option value="">按当前要求处理</option>{skills.filter(entry => entry.installed && entry.enabled).map(entry => <option key={entry.name} value={entry.name}>{entry.title || entry.name}</option>)}{skill && !skills.some(entry => entry.name === skill && entry.installed && entry.enabled) && <option value={skill}>{skill}（已停用或卸载，请重新选择）</option>}</select><button onClick={() => setAgentTab('skills')}>管理</button></label>
        <div className="ai-presets">{presets.map(choice => <button disabled={busy || active} key={choice.id} onClick={() => { setInstruction(choice.instruction); setPreset(choice.id); }}>{choice.title}<ArrowRight size={12}/></button>)}</div><textarea aria-label="文稿处理要求" placeholder="例如：保留全部数据，把这份文稿改得更适合口播。" value={instruction} maxLength={4000} disabled={busy} onChange={event => { setInstruction(event.target.value); setPreset(''); }}/><div className="ai-send-row"><span>携带当前范围、参考和对话上下文。</span><Button primary disabled={busy || active || staleTarget || (workspace.references || []).some(reference => reference.stale) || !text.trim() || !instruction.trim() || !state.settings.llm_model || (!!skill && !skills.some(entry => entry.name === skill && entry.installed && entry.enabled))} onClick={() => send()}><PaperPlaneRight size={15}/>{busy ? '提交中…' : '发送'}</Button></div>
        {(workspace.references || []).some(reference => reference.stale) && <small className="inline-error">引用已变化，请查看并重新添加，或移除后发送。</small>}
      </div>
    </div>
    {referencePicker && <ReferencePicker items={state.items} busy={busy} add={addReference} close={() => setReferencePicker(false)}/>}
    {referencePreview && <ReferencePreview reference={referencePreview} locate={referencePreview.kind === 'draft' ? () => locate(referencePreview) : undefined} close={() => setReferencePreview(null)}/>}
    {proposalRun && <ProposalDialog key={`${workspace.id}-${proposalRun}`} workspace={workspace} runId={proposalRun} busy={busy} apply={proposalAction} locate={proposal => { try { if (editor.current) locateRange(editor.current, { start: proposal.start, end: proposal.end, text: proposal.state === 'accepted' ? proposal.after : proposal.before, offset_unit: 'utf8' }); } catch (error) { notify((error as Error).message, true); } }} notify={notify} close={() => setProposalRun('')}/>}
    {agentTab && <AgentManager key={workspace.id} workspace={workspace} initialTab={agentTab} compact={compact} cancel={cancel} changed={reloadRuntime} chooseSkill={chooseSkill} skillsChanged={setSkills} close={() => setAgentTab(null)} notify={notify}/>}
  </section>;
}
