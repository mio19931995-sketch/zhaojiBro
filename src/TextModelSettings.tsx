import { useEffect, useRef, useState } from 'react';
import { ArrowSquareOut, Check, CircleNotch, FloppyDisk } from '@phosphor-icons/react';
import { api, openExternal } from './api';
import type { Settings } from './api';
import { Button } from './components';

type Provider = 'agnes' | 'deepseek' | 'custom';
type Draft = { llm_url: string; llm_model: string; llm_api_key: string };
type TestResult = { ok: boolean; model: string; base_url: string; message?: string };
type Props = { settings: Settings; refresh: () => Promise<void>; notify: (text: string, error?: boolean) => void };
const presets = {
  agnes: { llm_url: 'https://apihub.agnes-ai.com/v1', llm_model: 'agnes-3.0-flash' },
  deepseek: { llm_url: 'https://api.deepseek.com', llm_model: 'deepseek-flash' },
  custom: { llm_url: '', llm_model: '' },
};
const origin = (value: string) => { try { const parsed = new URL(value.trim()); return ['http:', 'https:'].includes(parsed.protocol) ? parsed.origin : ''; } catch { return ''; } };
function providerFor(value: string): Provider {
  try {
    const host = new URL(value.trim()).hostname.toLowerCase();
    if (['apihub.agnes-ai.com', 'platform.agnes-ai.com', 'apihub.agnes-ai.cn', 'api.agnes-ai.cn'].includes(host)) return 'agnes';
    if (host === 'api.deepseek.com' || host === 'platform.deepseek.com') return 'deepseek';
  } catch { /* An incomplete address remains an editable custom service. */ }
  return 'custom';
}
const enabled = (value: Settings[string]) => value === true || value === 'true';

export function TextModelSettings({ settings, refresh, notify }: Props) {
  const [draft, setDraft] = useState<Draft>({ llm_url: '', llm_model: '', llm_api_key: '' });
  const [provider, setProvider] = useState<Provider>('custom');
  const [knownSettings, setKnownSettings] = useState(settings);
  const [loaded, setLoaded] = useState(false);
  const [pending, setPending] = useState<'test' | 'save' | ''>('');
  const [testStatus, setTestStatus] = useState<{ ok: boolean; text: string } | null>(null);
  const initialized = useRef(false);
  const working = useRef(false);

  useEffect(() => {
    setKnownSettings(settings);
    if (initialized.current || !Object.keys(settings).length) return;
    initialized.current = true;
    const next = { llm_url: String(settings.llm_url || ''), llm_model: String(settings.llm_model || ''), llm_api_key: '' };
    setDraft(next);
    setProvider(providerFor(next.llm_url));
    setLoaded(true);
  }, [settings]);

  const draftOrigin = origin(draft.llm_url);
  const savedKey = !!draftOrigin && (
    draftOrigin === origin(presets.agnes.llm_url) ? enabled(knownSettings.has_agnes_key) :
    draftOrigin === origin(presets.deepseek.llm_url) ? enabled(knownSettings.has_deepseek_key) :
    draftOrigin === origin(String(knownSettings.llm_url || '')) && enabled(knownSettings.has_llm_key)
  );
  const remoteProvider = providerFor(draft.llm_url);
  const missingKey = remoteProvider !== 'custom' && !draft.llm_api_key.trim() && !savedKey;
  const providerName = remoteProvider === 'agnes' ? 'Agnes AI' : remoteProvider === 'deepseek' ? 'DeepSeek' : '此服务';
  const ready = loaded && !!draftOrigin && !!draft.llm_model.trim() && !missingKey;
  const locked = !loaded || !!pending;

  function chooseProvider(value: Provider) {
    setProvider(value);
    setDraft({ ...presets[value], llm_api_key: '' });
    setTestStatus(null);
  }

  function edit(key: keyof Draft, value: string) {
    setTestStatus(null);
    setDraft(current => ({
      ...current, [key]: value,
      ...(key === 'llm_url' && origin(value) !== origin(current.llm_url) ? { llm_api_key: '' } : {}),
    }));
    if (key === 'llm_url') setProvider(providerFor(value));
  }

  async function run(operation: 'test' | 'save') {
    if (working.current || !ready) return;
    working.current = true;
    setPending(operation);
    setTestStatus(null);
    const payload = { llm_url: draft.llm_url.trim(), llm_model: draft.llm_model.trim(), llm_api_key: draft.llm_api_key.trim() };
    try {
      if (operation === 'test') {
        const result = await api<TestResult>('/llm/test', 'POST', payload);
        if (!result.ok) throw new Error(result.message || '连接测试失败，请检查服务地址、模型和 API Key');
        setTestStatus({ ok: true, text: '固定短句测试已通过，当前模型可以使用。' });
      } else {
        const saved = await api<Settings>('/settings', 'PATCH', payload);
        const next = { llm_url: String(saved.llm_url ?? payload.llm_url), llm_model: String(saved.llm_model ?? payload.llm_model), llm_api_key: '' };
        setKnownSettings(saved);
        setDraft(next);
        setProvider(providerFor(next.llm_url));
        await refresh();
        notify('文本模型配置已保存');
      }
    } catch (error) {
      const message = (error as Error).message;
      if (operation === 'test') setTestStatus({ ok: false, text: message });
      else notify(message, true);
    } finally {
      working.current = false;
      setPending('');
    }
  }

  return <div className="settings-form">
    <h2>连接文本模型</h2>
    <p>选择模型服务，处理摘要、改写、校正与翻译。也支持 Ollama 等 OpenAI 兼容服务。</p>
    <label>服务商<select aria-label="文本模型服务商" disabled={locked} value={provider} onChange={e => chooseProvider(e.target.value as Provider)}>
      <option value="agnes">Agnes AI</option><option value="deepseek">DeepSeek</option><option value="custom">自定义（OpenAI 兼容 / Ollama）</option>
    </select></label>
    <label>服务地址<input aria-label="文本模型服务地址" disabled={locked} value={draft.llm_url} onChange={e => edit('llm_url', e.target.value)} placeholder="例如：http://127.0.0.1:11434/v1"/></label>
    <label>模型名称<input aria-label="文本模型名称" disabled={locked} value={draft.llm_model} onChange={e => edit('llm_model', e.target.value)} placeholder="填写服务支持的模型 ID" list={provider === 'agnes' ? 'agnes-model-options' : undefined}/>
    </label>
    {provider === 'agnes' && <datalist id="agnes-model-options"><option value="agnes-3.0-flash"/><option value="agnes-2.5-flash"/></datalist>}
    <label>API Key{remoteProvider === 'custom' ? '（本地服务可留空）' : ''}<input aria-label="文本模型 API Key" type="password" autoComplete="off" disabled={locked} value={draft.llm_api_key} onChange={e => edit('llm_api_key', e.target.value)} placeholder={savedKey ? `${providerName} 的密钥已保存，留空保持不变` : remoteProvider === 'custom' ? '此地址尚未保存密钥，本地服务可留空' : `请填写 ${providerName} 的 API Key`}/></label>
    {missingKey && <p className="footnote">尚未保存 {providerName} 的密钥，请先填写 API Key。</p>}
    {provider === 'agnes' && <Button disabled={locked} onClick={() => openExternal('https://platform.agnes-ai.com/settings/apiKeys').catch(error => notify(error.message, true))}>获取 / 管理 Agnes API Key<ArrowSquareOut size={14}/></Button>}
    {provider === 'deepseek' && <p className="footnote">DeepSeek 请填写 api.deepseek.com，不要粘贴密钥管理页面。</p>}
    <p className="footnote">测试只发送固定短句，不读取文稿，也不会保存配置。确认后点击“保存配置”生效。</p>
    {testStatus && <p role="status" data-testid="llm-test-status" className={testStatus.ok ? 'footnote' : 'inline-error'}>{testStatus.ok && <Check size={14}/>} {testStatus.text}</p>}
    <div className="form-actions">
      <Button disabled={locked || !ready} onClick={() => run('test')}>{pending === 'test' && <CircleNotch size={16} className="spin"/>}{pending === 'test' ? '正在测试…' : '测试连接'}</Button>
      <Button primary disabled={locked || !ready} onClick={() => run('save')}>{pending === 'save' ? <CircleNotch size={16} className="spin"/> : <FloppyDisk size={16}/>} {pending === 'save' ? '正在保存…' : '保存配置'}</Button>
    </div>
  </div>;
}
