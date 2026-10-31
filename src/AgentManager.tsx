import { useEffect, useState } from 'react';
import { api } from './api';
import { Button } from './components';
import { Modal } from './Feishu';
import { SkillManager } from './SkillManager';
import type { Shared } from './App';
import type { Workspace } from './aiWorkspaceTypes';
import { defaultAgentConfig } from './agentRuntimeTypes';
import type { AgentConfig, AgentTool, SkillEntry } from './agentRuntimeTypes';
import './agent-runtime.css';

export type AgentTab = 'context' | 'tools' | 'skills';
export function AgentManager({ workspace, initialTab, compact, cancel, changed, chooseSkill, skillsChanged, close, notify }: {
  workspace: Workspace; initialTab: AgentTab; compact: () => Promise<void>; cancel: () => Promise<void>; changed: () => Promise<void>; chooseSkill: (name: string) => void; skillsChanged: (entries: SkillEntry[]) => void; close: () => void; notify: Shared['notify'];
}) {
  const [tab, setTab] = useState(initialTab), [config, setConfig] = useState<AgentConfig>(workspace.agent_config || defaultAgentConfig);
  const [tools, setTools] = useState<AgentTool[]>([]), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const context = workspace.context, compactRun = workspace.compact_run;
  const active = [workspace.run, compactRun].some(run => run && ['queued', 'running'].includes(run.status));
  useEffect(() => {
    let valid = true;
    api<{ entries: AgentTool[] }>('/ai/tools').then(value => { if (valid) setTools(value.entries); }).catch(e => { if (valid) setError(e.message); });
    return () => { valid = false; };
  }, []);
  async function save(part: 'context' | 'tools') {
    setBusy(true); setError('');
    try {
      // Read the other section just before saving so this form cannot silently revert another window's settings.
      const current = await api<AgentConfig>(`/ai/workspaces/${workspace.id}/agent-config`);
      const body = part === 'tools' ? { ...current, tools_enabled: config.tools_enabled, allowed_tools: config.allowed_tools } : { ...current, auto_compact: config.auto_compact, context_window: config.context_window, compact_threshold: config.compact_threshold };
      const result = await api<AgentConfig>(`/ai/workspaces/${workspace.id}/agent-config`, 'PUT', body);
      setConfig(result); await changed(); notify(part === 'tools' ? '工具设置已保存' : '上下文设置已保存');
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  let summary: unknown = context?.checkpoint?.summary;
  if (typeof summary === 'string') { try { summary = JSON.parse(summary); } catch {} }
  return <Modal title="Agent 设置" close={close}>
    <div className="agent-manager">
      <nav aria-label="Agent 设置分类" className="agent-manager-tabs">{([['context', '上下文'], ['tools', '工具'], ['skills', '技能']] as const).map(([key, title]) => <button aria-pressed={tab === key} className={tab === key ? 'selected' : ''} key={key} onClick={() => setTab(key)}>{title}</button>)}</nav>
      {error && <p role="alert" className="inline-error">{error}</p>}
      <div className="agent-manager-content">
        {tab === 'context' && <div className="agent-context-settings">
          <p>长对话自动整理为摘要，保留最近两轮完整交流。完整记录仍可回看，压缩不会改动文稿。</p>
          <div className="agent-context-meter"><strong>{context?.percent ?? 0}%</strong><span>估算约 {(context?.estimate || 0).toLocaleString()} token · 可用输入 {(context?.input_limit || 0).toLocaleString()}</span><progress max={100} value={Math.min(100, context?.percent || 0)}/></div>
          <label className="check-label"><input type="checkbox" aria-label="自动压缩上下文" checked={config.auto_compact} onChange={e => setConfig({ ...config, auto_compact: e.target.checked })}/>自动压缩上下文</label>
          <div className="agent-context-fields"><label>模型上下文窗口<input aria-label="模型上下文窗口" type="number" min={8192} max={2000000} step={1024} value={config.context_window} onChange={e => setConfig({ ...config, context_window: Number(e.target.value) })}/></label><label>自动压缩阈值<select aria-label="自动压缩阈值" value={config.compact_threshold} onChange={e => setConfig({ ...config, compact_threshold: Number(e.target.value) })}>{[.5, .65, .75, .85].map(value => <option key={value} value={value}>{Math.round(value * 100)}%</option>)}</select></label></div>
          <small>按你的服务实际支持容量填写；这是估算，服务端仍可能有自己的限制。压缩会使用当前文本模型。</small>
          <div className="agent-inline-actions"><Button disabled={busy} onClick={() => save('context')}>保存上下文设置</Button><Button disabled={busy || active || !context?.can_compact} onClick={async () => { setBusy(true); setError(''); try { await compact(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }}>立即压缩</Button></div>
          {compactRun && <p role="status" className={compactRun.status === 'error' ? 'inline-error' : ''}>{compactRun.phase}</p>}
          {compactRun && ['queued', 'running'].includes(compactRun.status) && <Button disabled={busy} onClick={cancel}>取消压缩</Button>}
          <section className="agent-summary"><h3>当前摘要</h3>{summary && typeof summary === 'object' ? Object.entries(summary).map(([key, values]) => <div key={key}><strong>{{ goals: '目标', constraints: '约束', decisions: '决定', pending: '待办' }[key] || key}</strong><ul>{(Array.isArray(values) ? values : [values]).map((value, index) => <li key={index}>{String(value)}</li>)}</ul></div>) : <p>{typeof summary === 'string' ? summary : '还没有压缩摘要；历史达到阈值后会自动整理，也可以手动压缩。'}</p>}</section>
        </div>}
        {tab === 'tools' && <div className="agent-tools-settings">
          <p>工具用于读取 Lulu 资料、准备工作台入口，或生成待确认操作。模型需支持工具调用；纯文本模型可关闭此功能。</p>
          <label className="check-label"><input aria-label="启用工具调用" type="checkbox" checked={config.tools_enabled} onChange={e => setConfig({ ...config, tools_enabled: e.target.checked })}/>启用工具调用</label>
          <div className="agent-inline-actions"><Button disabled={busy} onClick={() => setConfig({ ...config, tools_enabled: true, allowed_tools: tools.filter(tool => tool.effect !== 'confirm').map(tool => tool.name) })}>启用常用工具</Button><Button disabled={busy} onClick={() => setConfig({ ...config, allowed_tools: [] })}>清空选择</Button><Button primary disabled={busy} onClick={() => save('tools')}>保存工具设置</Button></div>
          <small>停用的工具会停止后续调用；新启用的能力从下一轮生效。生成文稿、安装技能等操作会先展示内容，等待你确认。</small>
          <div className="agent-tool-list">{tools.map(tool => <label className="agent-tool-option" key={tool.name}><input type="checkbox" aria-label={`允许工具 ${tool.name}`} checked={config.allowed_tools.includes(tool.name)} onChange={e => setConfig({ ...config, allowed_tools: e.target.checked ? [...config.allowed_tools, tool.name] : config.allowed_tools.filter(name => name !== tool.name) })}/><span><strong>{tool.title}</strong><small>{tool.description}</small><em>{{ read: '读取资料', handoff: '准备入口', confirm: '确认后执行' }[tool.effect]}</em></span></label>)}</div>
        </div>}
        {tab === 'skills' && <SkillManager notify={notify} changed={skillsChanged} useSkill={name => { chooseSkill(name); close(); }}/>}
      </div>
      <div className="agent-manager-footer"><Button onClick={close}>返回工作区</Button></div>
    </div>
  </Modal>;
}
