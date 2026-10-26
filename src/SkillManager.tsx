import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { Button } from './components';
import type { Shared } from './App';
import type { SkillDetail, SkillEntry, SkillPreview, SkillVersion } from './agentRuntimeTypes';

export function SkillManager({ notify, changed, useSkill }: { notify: Shared['notify']; changed: (entries: SkillEntry[]) => void; useSkill: (name: string) => void }) {
  const [entries, setEntries] = useState<SkillEntry[]>([]), [issues, setIssues] = useState<string[]>([]);
  const [preview, setPreview] = useState<SkillPreview | null>(null), [detail, setDetail] = useState<SkillDetail | null>(null);
  const [drafts, setDrafts] = useState<SkillPreview[]>([]), [replace, setReplace] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [search, setSearch] = useState('');
  const [name, setName] = useState(''), [description, setDescription] = useState(''), [instructions, setInstructions] = useState('');
  const [preferences, setPreferences] = useState<{ strict?: boolean; language?: string }>({});
  const [versions, setVersions] = useState<SkillVersion[]>([]), [installedGeneration, setInstalledGeneration] = useState('');
  const file = useRef<HTMLInputElement>(null), folder = useRef<HTMLInputElement>(null);
  async function reload() {
    const [catalog, pending] = await Promise.all([api<{ entries: SkillEntry[]; issues: string[] }>('/ai/skills'), api<{ entries: SkillPreview[] }>('/ai/skills/drafts')]);
    setEntries(catalog.entries); setIssues(catalog.issues || []); setDrafts(pending.entries); changed(catalog.entries);
  }
  useEffect(() => { reload().catch(e => setError(e.message)); }, []);
  async function action(work: () => Promise<void>) {
    setBusy(true); setError('');
    try { await work(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function importFiles(files: FileList | null) {
    if (!files?.length) return;
    await action(async () => {
      const data = new FormData();
      for (const item of Array.from(files)) data.append('files', item, item.webkitRelativePath || item.name);
      const value = await api<SkillPreview>('/ai/skills/preview', 'POST', data);
      setPreview(value); setDetail(null); setReplace(false);
    });
    if (file.current) file.current.value = '';
    if (folder.current) folder.current.value = '';
  }
  async function install() {
    if (!preview) return;
    await action(async () => {
      await api('/ai/skills/install', 'POST', { token: preview.token, replace });
      setPreview(null); setReplace(false); await reload(); notify('技能已安装，可在本轮技能中选择');
    });
  }
  async function inspect(entry: SkillEntry) {
    await action(async () => { const [value, history] = await Promise.all([api<SkillDetail>(`/ai/skills/${entry.name}`), api<{ entries: SkillVersion[] }>(`/ai/skills/${entry.name}/versions`)]); setDetail(value); setPreferences(value.config || {}); setInstalledGeneration(value.generation); setVersions(history.entries); setPreview(null); });
  }
  async function inspectVersion(id: string) {
    if (!detail) return;
    await action(async () => { const value = await api<SkillDetail>(`/ai/skills/${detail.name}${id ? `/versions/${id}` : ''}`); setDetail(value); setPreferences(value.config || {}); if (!id) setInstalledGeneration(value.generation); });
  }
  async function restoreVersion() {
    if (!detail?.version_id) return;
    await action(async () => {
      const value = await api<SkillDetail>(`/ai/skills/${detail.name}/versions/${detail.version_id}/restore`, 'POST', { generation: installedGeneration });
      setDetail(value); setPreferences(value.config || {}); setInstalledGeneration(value.generation);
      const history = await api<{ entries: SkillVersion[] }>(`/ai/skills/${detail.name}/versions`); setVersions(history.entries);
      await reload(); notify('技能版本已恢复，替换前的版本也已保留');
    });
  }
  async function mutate(entry: SkillEntry, operation: 'toggle' | 'remove' | 'restore') {
    await action(async () => {
      const path = `/ai/skills/${entry.name}`;
      await api(path + (operation === 'restore' ? '/restore' : ''), operation === 'toggle' ? 'PATCH' : operation === 'remove' ? 'DELETE' : 'POST', operation === 'toggle' ? { enabled: !entry.enabled } : undefined);
      await reload(); notify(operation === 'remove' ? '技能已卸载，可从列表恢复' : operation === 'restore' ? '技能已恢复' : entry.enabled ? '技能已停用' : '技能已启用');
    });
  }
  async function exportSkill(entry: SkillEntry) {
    await action(async () => {
      const response = await fetch(`/api/ai/skills/${entry.name}/export`, { headers: { 'X-Lulu-Client': 'desktop' } });
      if (!response.ok) throw Error((await response.json()).detail || '技能导出失败');
      const url = URL.createObjectURL(await response.blob()), link = document.createElement('a');
      link.href = url; link.download = `${entry.name}.zip`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 30000);
    });
  }
  const back = () => { setPreview(null); setDetail(null); setError(''); };
  return <div className="agent-skills-settings">
    {error && <p className="inline-error" role="alert">{error}</p>}
    {preview ? <section className="agent-skill-preview" aria-label="安装预览">
      <div className="agent-section-heading"><h3>安装预览</h3><Button onClick={back}>返回技能列表</Button></div>
      <strong>{preview.entry.title || preview.entry.name}</strong><p>{preview.entry.description}</p>
      <small>{preview.files.length} 个文件 · 技能说明和参考资料只作方法与素材，不执行脚本。</small>
      <pre>{preview.content}</pre>
      {!!preview.warnings?.length && <ul>{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
      <details><summary>查看文件清单</summary><ul>{preview.files.map(item => <li key={item.path}>{item.path} · {item.size} 字节</li>)}</ul></details>
      {preview.existing && <label className="check-label"><input type="checkbox" aria-label="替换现有技能" checked={replace} onChange={e => setReplace(e.target.checked)}/>替换现有技能（同名包已存在）</label>}
      <div className="agent-inline-actions"><Button primary disabled={busy || (!!preview.existing && !replace)} onClick={install}>确认安装</Button><Button disabled={busy} onClick={back}>取消安装</Button></div>
    </section> : detail ? <section className="agent-skill-preview">
      <div className="agent-section-heading"><h3>{detail.title || detail.name}</h3><Button onClick={back}>返回技能列表</Button></div><p>{detail.description}</p><small>{detail.automatic ? '允许模型按需加载' : '仅限手动选择或 /名称 调用'} · {detail.compatibility}</small>
      {!!versions.length && <label>历史版本<select aria-label="技能历史版本" disabled={busy} value={detail.version_id || ''} onChange={e => inspectVersion(e.target.value)}><option value="">当前版本</option>{versions.map(version => <option key={version.id} value={version.id}>{new Date(version.created_at * 1000).toLocaleString('zh-CN')} · {version.digest.slice(0, 8)}</option>)}</select></label>}
      {detail.version_id && <p role="status">正在预览历史版本。恢复后会保存当前版本，可再次回退。</p>}<pre>{detail.content}</pre>
      <details><summary>查看文件清单</summary><ul>{detail.files.map(item => <li key={item.path}>{item.path} · {item.size} 字节</li>)}</ul></details>
      <label>默认翻译语言<input aria-label="技能翻译语言" disabled={!!detail.version_id} value={preferences.language || ''} maxLength={80} placeholder="例如：英语；本轮要求优先" onChange={e => setPreferences({ ...preferences, language: e.target.value })}/></label>
      <label className="check-label"><input type="checkbox" aria-label="保守校对偏好" disabled={!!detail.version_id} checked={preferences.strict ?? true} onChange={e => setPreferences({ ...preferences, strict: e.target.checked })}/>校对偏好：尽量保留原句</label>
      {detail.version_id ? <div className="agent-inline-actions"><Button primary disabled={busy} onClick={restoreVersion}>恢复此版本</Button><Button disabled={busy} onClick={() => inspectVersion('')}>返回当前版本</Button></div> : <div className="agent-inline-actions"><Button disabled={busy || !detail.installed} onClick={() => action(async () => { const config = { ...preferences }; if (!config.language?.trim()) delete config.language; else config.language = config.language.trim(); const value = await api<SkillDetail>(`/ai/skills/${detail.name}`, 'PATCH', { config }); setDetail(value); setInstalledGeneration(value.generation); await reload(); notify('技能偏好已保存，从下次发送生效'); })}>保存技能偏好</Button><Button disabled={busy || !detail.installed} onClick={() => exportSkill(detail)}>导出技能包</Button><Button primary disabled={!detail.enabled || !detail.installed} onClick={() => useSkill(detail.name)}>使用此技能</Button></div>}
    </section> : <>
      <p>技能为 AI 提供处理方法。可以选择随包技能，或导入本地 SKILL.md、ZIP 和技能文件夹；安装前会先预览。</p>
      <div className="agent-inline-actions"><Button disabled={busy} onClick={() => file.current?.click()}>导入技能包</Button><Button disabled={busy} onClick={() => folder.current?.click()}>导入技能文件夹</Button><Button disabled={busy} onClick={() => action(reload)}>刷新技能</Button></div>
      <input ref={file} hidden type="file" aria-label="导入技能文件" accept=".md,.zip" onChange={e => importFiles(e.target.files)}/>
      <input ref={folder} hidden type="file" aria-label="导入技能文件夹" {...{ webkitdirectory: '', directory: '' }} multiple onChange={e => importFiles(e.target.files)}/>
      {issues.map((issue, index) => <p className="inline-error" key={index}>{String(issue)}</p>)}
      <input aria-label="搜索技能" placeholder="搜索技能名称或用途" value={search} onChange={e => setSearch(e.target.value)}/>
      <div className="agent-skill-list">{entries.filter(entry => `${entry.name} ${entry.title} ${entry.description}`.toLowerCase().includes(search.toLowerCase())).map(entry => <article className="agent-skill-card" data-name={entry.name} key={entry.name}>
        <header><strong>{entry.title || entry.name}</strong><small>{entry.origin === 'system' ? '随包' : '本地'} · {!entry.installed ? '已卸载' : entry.enabled ? '已启用' : '已停用'}</small></header><p>{entry.description}</p><code>/{entry.name}</code>
        <div className="agent-inline-actions"><Button disabled={busy} onClick={() => inspect(entry)}>查看技能</Button>{entry.installed ? <><Button disabled={busy} onClick={() => mutate(entry, 'toggle')}>{entry.enabled ? '停用' : '启用'}</Button><Button disabled={busy} onClick={() => mutate(entry, 'remove')}>卸载</Button><Button disabled={busy || !entry.enabled} onClick={() => useSkill(entry.name)}>使用此技能</Button></> : <Button disabled={busy} onClick={() => mutate(entry, 'restore')}>恢复</Button>}</div>
      </article>)}</div>
      {!!drafts.length && <details><summary>待安装草稿（{drafts.length}）</summary>{drafts.map(value => <div className="agent-section-heading" key={value.token}><span>{value.entry.title || value.entry.name}</span><Button disabled={busy} onClick={() => { setPreview(value); setReplace(false); }}>预览安装</Button></div>)}</details>}
      <details className="agent-new-skill"><summary>编写新技能</summary><label>技能名称<input aria-label="技能名称" placeholder="例如：short-video-outline" value={name} maxLength={64} onChange={e => setName(e.target.value)}/></label><label>技能简介<input aria-label="技能简介" value={description} maxLength={1600} onChange={e => setDescription(e.target.value)}/></label><label>技能指令<textarea aria-label="技能指令" value={instructions} maxLength={100000} placeholder="描述方法、步骤和需要保留的信息…" onChange={e => setInstructions(e.target.value)}/></label><Button disabled={busy || !name.trim() || !description.trim() || !instructions.trim()} onClick={() => action(async () => { setPreview(await api<SkillPreview>('/ai/skills/drafts', 'POST', { name, description, instructions })); setReplace(false); await reload(); })}>生成安装预览</Button></details>
    </>}
  </div>;
}
