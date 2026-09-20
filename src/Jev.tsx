import { useEffect, useState } from 'react';
import { api } from './api';
import type { Item, Settings } from './api';
import { Button } from './components';
import './jev.css';

type Notify = (message: string, error?: boolean) => void;
export function JevSettings({ settings, refresh, notify }: { settings: Settings; refresh: () => Promise<void>; notify: Notify }) {
  const [model, setModel] = useState(String(settings.jev_model || 'jev-1.13.0'));
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState('');
  const [result, setResult] = useState('');
  async function submit(test: boolean) {
    setBusy(test ? 'test' : 'save'); setResult('');
    try {
      const response = await api(test ? '/jev/test' : '/jev/settings', test ? 'POST' : 'PUT', { model, api_key: key });
      if (test) setResult(`连接成功 · ${response.model} 已返回有效判断`);
      else { setKey(''); await refresh(); notify('Jev 配置已保存'); }
    } catch (e) { notify((e as Error).message, true); } finally { setBusy(''); }
  }
  return <div className="jev-settings">
    <h2>Jev 创作检查助手</h2><p>检查口播表达、对照原稿查找不一致；修正建议由已配置的文本模型生成。</p>
    <label>模型<input value={model} disabled={!!busy} onChange={e => { setModel(e.target.value); setResult(''); }}/></label>
    <label>TypeSafe API Key<input type="password" autoComplete="off" value={key} disabled={!!busy} placeholder={settings.has_jev_key ? '已保存，留空保留原密钥' : '填写独立的 TypeSafe API Key'} onChange={e => { setKey(e.target.value); setResult(''); }}/></label>
    <p className="muted">使用 TypeSafe 官方接口，密钥加密保存在本机。点击文稿中的“检查”才会发送正文与对照原稿。</p>
    <div className="jev-actions"><Button disabled={!!busy || !model.trim() || (!key && !settings.has_jev_key)} onClick={() => submit(true)}>{busy === 'test' ? '正在验证…' : '验证连接'}</Button><Button primary disabled={!!busy || !model.trim()} onClick={() => submit(false)}>{busy === 'save' ? '保存中…' : '保存配置'}</Button></div>
    {result && <p role="status">{result}</p>}
    <p className="muted">使用方式：打开一份文稿 → 检查 → 一键检查 → 查看问题与修正建议。中文判断仍需结合原文核对。</p>
  </div>;
}

export type ReviewIssue = { id: string; start: number; end: number; text: string; label: string; uncertain: boolean; suggestion?: string };
type Report = { id: string; fingerprint: string; created_at: number; model: string; issues: ReviewIssue[]; original: string; original_title: string; text: string; checked_paragraphs: number; stale: boolean };
export function DocumentReview({ item, items, settings, notify, updated, locate, configure }: {
  item: Item; items: Item[]; settings: Settings; notify: Notify; updated: () => Promise<void>;
  locate: (issue: ReviewIssue) => void; configure: () => void;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [hasOriginal, setHasOriginal] = useState(false);
  const [source, setSource] = useState('');
  const [busy, setBusy] = useState('');
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    api(`/items/${item.id}/review`).then(r => { if (!cancelled) { setReport(r.report); setHasOriginal(r.has_original); setLoaded(true); } }).catch(e => { if (!cancelled) notify(e.message, true); });
    return () => { cancelled = true; };
  }, [item.id, item.updated_at, notify]);
  async function check() {
    setBusy('check');
    try { setReport(await api(`/items/${item.id}/review`, 'POST', { source_id: source })); }
    catch (e) { notify((e as Error).message, true); } finally { setBusy(''); }
  }
  async function act(issue: ReviewIssue, apply: boolean) {
    if (!report) return;
    setBusy(issue.id);
    try {
      const response = await api(`/items/${item.id}/review/${apply ? 'apply' : 'suggest'}`, 'POST', { report_id: report.id, issue_id: issue.id });
      if (apply) { setReport({ ...report, stale: true }); await updated(); notify('已采纳，原稿已存入历史版本。请重新检查修改后的文稿'); }
      else setReport({ ...report, issues: report.issues.map(i => i.id === issue.id ? { ...i, suggestion: response.suggestion } : i) });
    } catch (e) { notify((e as Error).message, true); } finally { setBusy(''); }
  }
  return <div className="document-review">
    <h3>创作检查</h3>
    <p className="muted">检查表达与原稿一致性，结果供你核对。音画同步、外部事实真伪不在本次检查范围内。</p>
    {!settings.has_jev_key && <div className="review-notice">先配置 Jev 的独立 API Key。<Button onClick={configure}>配置检查模型</Button></div>}
    <label>对照原稿<select aria-label="检查对照原稿" value={source} disabled={!!busy} onChange={e => setSource(e.target.value)}>
      <option value="">{hasOriginal ? '使用处理时保存的原文' : '暂无原稿，仅检查表达'}</option>
      {items.filter(i => i.id !== item.id && i.has_transcript).map(i => <option value={i.id} key={i.id}>{i.title}</option>)}
    </select></label>
    <p className="muted">点击后将这份正文和对照原稿发送至 TypeSafe。</p>
    <Button primary disabled={!!busy || !loaded || !settings.has_jev_key || !item.transcript.trim()} onClick={check}>{busy === 'check' ? '正在逐段检查…' : report ? '重新检查' : '一键检查'}</Button>
    {report && <>
      <div className="review-summary" role="status"><strong>{report.stale ? '文稿已修改，检查结果已过期' : report.issues.length ? `${report.issues.length} 项需要核对` : '本次未发现明确问题'}</strong><p>已检查 {report.checked_paragraphs} 段 · {new Date(report.created_at * 1000).toLocaleString('zh-CN')}</p><small>{report.model}</small></div>
      {!report.original && <p className="review-notice">本次没有对照原稿，未检查事实一致性。</p>}
      {report.original && <details className="review-comparison"><summary>原稿与本次检查正文对照</summary><h4>{report.original_title}</h4><pre>{report.original}</pre><h4>本次检查正文</h4><pre>{report.text}</pre></details>}
      {report.issues.map((issue, index) => <article className={`review-issue ${issue.uncertain ? 'uncertain' : ''}`} key={issue.id}>
        <h4>{index + 1}. {issue.label}</h4><mark>{issue.text}</mark>
        <div className="jev-actions"><Button disabled={!!busy || report.stale} onClick={() => locate(issue)}>定位正文</Button>
          {!issue.uncertain && <Button disabled={!!busy || report.stale || !settings.llm_model} onClick={() => act(issue, false)}>{busy === issue.id ? '处理中…' : issue.suggestion ? '重新生成建议' : '生成修正建议'}</Button>}</div>
        {issue.uncertain && <p className="muted">模型判断不确定，请结合上下文人工核对。</p>}
        {!issue.uncertain && !settings.llm_model && <p className="muted">生成建议前，请配置 Agnes 等文本处理模型。</p>}
        {issue.suggestion && <div className="review-suggestion"><h4>建议替换为</h4><p>{issue.suggestion}</p><Button primary disabled={!!busy || report.stale} onClick={() => act(issue, true)}>采纳这一处</Button><small>保留修改前版本及原时间戳；采纳后需重新检查。</small></div>}
      </article>)}
    </>}
  </div>;
}
