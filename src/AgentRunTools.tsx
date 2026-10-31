import { Button } from './components';
import type { AgentAction, ToolResult } from './agentRuntimeTypes';
import type { Run } from './aiWorkspaceTypes';

export function AgentRunTools({ run, busy, confirm, handoff, openDocument }: { run: Run; busy: boolean; confirm: (action: AgentAction, dismiss: boolean) => void; handoff: (result: ToolResult) => void; openDocument: (id: string) => void }) {
  const names: Record<string, string> = { read_targets: '读取当前材料', search_documents: '查找文稿', read_document: '阅读文稿', load_skill: '加载技能', read_skill_reference: '阅读技能参考', list_tasks: '查询任务', inspect_link: '识别链接', prepare_voice: '准备配音', prepare_transcription: '准备转录', prepare_collection: '准备采集', search_skills: '查找技能', prepare_skill_install: '准备技能安装', create_skill_draft: '准备技能草稿', list_skill_drafts: '读取技能草稿', install_skill: '准备安装技能', create_document: '准备新文稿' };
  const states: Record<string, string> = { running: '执行中', completed: '已完成', done: '已完成', needs_input: '等待操作', pending: '等待确认', confirmed: '已执行', dismissed: '已忽略', cancelled: '已取消', error: '失败', interrupted: '已中断' };
  return <>
    {run.events?.map((event, index) => {
      const result = event.result, name = event.tool || event.name || '', state = event.state || event.status || result?.state || '';
      return <details className="agent-tool-event" key={event.id || `${name}-${index}`}><summary>{event.kind === 'compaction' ? '上下文压缩' : names[name] || name || '工具执行'} · {states[state] || state}</summary><p>{result?.message || event.error || event.message || '已记录本次工具请求。'}</p>
        {result?.text && <pre>{result.text}</pre>}
        {Array.isArray(result?.documents) && <ul>{(result.documents as { id?: string; title?: string }[]).map((doc, number) => <li key={doc.id || number}>{doc.title || doc.id}</li>)}</ul>}
        {result?.state === 'needs_input' && ['voice', 'transcription', 'collection', 'skills'].includes(result.action || '') && <Button disabled={busy || run.status !== 'done'} onClick={() => handoff(result)}>{({ voice: '前往配音', transcription: '前往文案提取', collection: '前往主页采集', skills: '打开技能管理' } as Record<string, string>)[result.action!]}</Button>}
      </details>;
    })}
    {run.actions?.map(action => <article key={action.id} className="agent-pending-action" data-state={action.state}>
      <header><strong>{action.label}</strong><span>{states[action.state] || action.state}</span></header>
      <p>{action.preview.title || action.preview.name}{action.preview.description ? ` · ${action.preview.description}` : ''}</p>
      {(action.preview.text || action.preview.instructions) && <pre>{action.preview.text || action.preview.instructions}</pre>}
      {action.state === 'pending' && <><p>查看上述内容后确认执行。当前文稿保持不变。</p><div className="agent-inline-actions"><Button primary disabled={busy || run.status !== 'done'} onClick={() => confirm(action, false)}>确认执行</Button><Button disabled={busy || run.status !== 'done'} onClick={() => confirm(action, true)}>忽略此操作</Button></div></>}
      {action.result?.document_id && <Button onClick={() => openDocument(action.result!.document_id!)}>打开生成文稿</Button>}
    </article>)}
  </>;
}
