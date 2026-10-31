import { useEffect, useState } from 'react';
import { Check, CircleNotch, Cube, Folder, UploadSimple } from '@phosphor-icons/react';
import { api, openExternal } from './api';
import type { Model } from './api';
import type { Shared } from './App';
import { Button } from './components';
import './TranscriptionModels.css';

type NativeModel = Model & { native?: boolean; backend?: string; external?: boolean; filename?: string; source_url?: string; model_ready?: boolean; runtime_ready?: boolean; supported?: boolean; path?: string; timestamps?: boolean; speakers?: boolean };
type NativeStatus = { runtime: { ready: boolean; state: string; message: string; supported: boolean; reason: string }; models: NativeModel[]; candidates: { id: string; path: string; verified: boolean }[]; runtime_candidate: string };

export function TranscriptionModels({ state, refresh, notify }: Shared) {
  const [status, setStatus] = useState<NativeStatus>();
  const [paths, setPaths] = useState<Record<string, string>>({});
  const [runtimePath, setRuntimePath] = useState('');
  const [busy, setBusy] = useState('');
  async function load() { setStatus(await api<NativeStatus>('/asr/status')); }
  useEffect(() => {
    let live = true;
    const poll = () => api<NativeStatus>('/asr/status').then(value => { if (live) setStatus(value); }).catch(() => {});
    poll(); const timer = setInterval(poll, 2500);
    return () => { live = false; clearInterval(timer); };
  }, []);
  async function action(key: string, run: () => Promise<unknown>, message: string) {
    if (busy) return;
    setBusy(key);
    try { await run(); await load(); await refresh(); notify(message); }
    catch (error) { notify((error as Error).message, true); }
    finally { setBusy(''); }
  }
  function importModel(model: NativeModel, path: string) {
    return action(model.id, () => api(`/asr/models/${model.id}/import`, 'POST', { path }), '已校验并引用原有模型，源文件保留在原位置');
  }
  async function chooseFolder(id: string) {
    const path = await window.desktop?.chooseFolder();
    if (path) setPaths(value => ({ ...value, [id]: path }));
  }
  const currentModels = state.models as NativeModel[];
  const models = currentModels.filter(model => !model.native);
  const native = status?.models || currentModels.filter(model => model.native);
  const turbo = models.find(model => model.id === 'large-v3-turbo');
  const featured = [native.find(model => model.id === 'sensevoice-q8'), turbo,
    native.find(model => model.id === 'whisper-turbo-q4'), native.find(model => model.id === 'moss-q5')].filter(Boolean) as NativeModel[];
  function card(model: NativeModel) {
    const selected = state.settings.model === model.id;
    const candidates = status?.candidates.filter(candidate => candidate.id === model.id) || [];
    return <article className="asr-model" key={model.id}>
      <div className="asr-model-heading"><Cube size={24}/><div><h3>{model.name}</h3><small>{model.size} · {model.backend || 'CTranslate2 · CPU INT8'}</small></div>{selected && <span className="status-pill done">当前使用</span>}</div>
      <p>{model.note}</p>
      {model.native && <div className="asr-capabilities"><span>{model.timestamps ? '真实分段时间轴' : '仅文稿，无精确时间轴'}</span><span>{model.speakers ? '区分匿名发言人' : '不区分发言人'}</span></div>}
      <p className="asr-status" role="status">{model.native ? `模型文件：${model.model_ready ? '已校验' : '未导入'} · 运行工具：${model.runtime_ready ? '已验证' : '未准备'}` : model.installed ? '已有模型可用' : '尚未安装'}</p>
      {model.message && <small>{model.message}</small>}
      {model.state === 'downloading' && <progress aria-label={`${model.name} 下载中`}/>}
      <div className="asr-card-actions">
        <Button primary disabled={!model.installed || selected || !!busy} onClick={() => action(model.id, () => api('/settings', 'PATCH', { model: model.id }), `已切换到 ${model.name}`)}><Check size={15}/>{selected ? '使用中' : '使用此模型'}</Button>
        {!model.native && !model.installed && <Button disabled={model.state === 'downloading' || !!busy} onClick={() => action(model.id, () => api(`/models/${model.id}/install`, 'POST'), '已开始下载所选模型')}>{model.state === 'downloading' ? '下载中…' : '下载模型'}</Button>}
        {(model.model_ready || model.external) && <Button disabled={!!busy} onClick={() => action(model.id, () => api(`/asr/models/${model.id}/binding`, 'DELETE'), '已移除模型引用，原有模型文件保留')}>移除引用</Button>}
        {!model.native && model.installed && !model.external && <Button disabled={!!busy} onClick={() => action(model.id, () => api(`/models/${model.id}`, 'DELETE'), '已卸载所选模型')}>卸载模型</Button>}
      </div>
      <details className="asr-import"><summary>{model.native ? '导入已有模型' : '复用其他目录的已有模型'}</summary>
        {model.filename && <p>需要 {model.filename}。导入时校验完整性，不复制或删除源文件。</p>}
        {!!model.source_url && <button className="text-button" onClick={() => openExternal(model.source_url!)}>查看模型来源与文件</button>}
        {candidates.length > 0 && <div className="asr-candidates">{candidates.map(candidate => <div key={candidate.path}><span title={candidate.path}>{candidate.path}</span><Button disabled={!!busy} onClick={() => importModel(model, candidate.path)}>校验并复用</Button></div>)}</div>}
        <label>模型文件或目录<input aria-label={`${model.name} 模型路径`} value={paths[model.id] || ''} placeholder={model.native ? '粘贴 GGUF 文件完整路径，或选择所在文件夹' : '选择 CTranslate2 模型所在目录'} onChange={event => setPaths(value => ({ ...value, [model.id]: event.target.value }))}/></label>
        <div className="asr-card-actions">{window.desktop && <Button disabled={!!busy} onClick={() => chooseFolder(model.id)}><Folder size={15}/>选择目录</Button>}<Button disabled={!paths[model.id]?.trim() || !!busy} onClick={() => importModel(model, paths[model.id].trim())}>{busy === model.id ? <CircleNotch className="spin" size={15}/> : <UploadSimple size={15}/>}校验并导入</Button></div>
      </details>
    </article>;
  }
  return <div className="transcription-models">
    <div className="model-intro"><div><h2>本地语音转录</h2><p>保留已有模型和默认选择。新增 Windows 适配模型需手动导入；不会自动下载模型权重。</p></div><label>识别语言<select aria-label="识别语言" value={String(state.settings.language || 'auto')} disabled={!!busy} onChange={event => action('language', () => api('/settings', 'PATCH', { language: event.target.value }), '识别语言已更新')}><option value="auto">自动识别</option><option value="zh">中文</option><option value="en">英语</option><option value="ja">日语</option><option value="ko">韩语</option></select></label></div>
    <div className="asr-runtime"><div><strong>Windows 原生转录工具</strong><p>{status?.runtime.ready ? 'transcribe.cpp 0.4.0 · CPU 接口验证通过' : status?.runtime.reason || 'SenseVoice、Whisper Q4、MOSS 共用。约 18 MB，包含运行工具，不含模型。'}</p>{status?.runtime.message && <small role="status">{status.runtime.message}</small>}</div>
      <Button disabled={!status || !status.runtime.supported || status.runtime.ready || status.runtime.state === 'downloading' || !!busy} onClick={() => action('runtime', () => api('/asr/runtime/install', 'POST'), '开始准备运行工具，不会下载模型')}>{status?.runtime.state === 'downloading' ? <CircleNotch className="spin" size={15}/> : <Cube size={15}/>} {status?.runtime.ready ? '运行工具已就绪' : status?.runtime_candidate ? '验证已安装工具' : '准备运行工具'}</Button>
      {status?.runtime.state === 'downloading' && <Button onClick={() => action('cancel', () => api('/asr/runtime/cancel', 'POST'), '已请求取消下载')}>取消</Button>}
      <details><summary>导入已下载的运行工具</summary><label>运行工具目录<input aria-label="转录运行工具目录" value={runtimePath} placeholder="包含 transcribe.dll 及配套 DLL 的目录" onChange={event => setRuntimePath(event.target.value)}/></label><div className="asr-card-actions">{window.desktop && <Button onClick={async () => { const path = await window.desktop?.chooseFolder(); if (path) setRuntimePath(path); }}><Folder size={15}/>选择目录</Button>}<Button disabled={!runtimePath.trim() || !!busy} onClick={() => action('runtime-import', () => api('/asr/runtime/import', 'POST', { path: runtimePath.trim() }), '运行工具版本和 CPU 接口已验证')}>验证并导入</Button></div></details>
    </div>
    <div className="asr-model-grid">{featured.map(card)}</div>
    <details className="asr-legacy" open><summary>原有轻量 Whisper 模型</summary><div className="asr-model-grid">{models.filter(model => model.id !== 'large-v3-turbo').map(card)}</div></details>
    <p className="footnote">中文统一输出简体，英文保留英文。MOSS 支持中英文；SenseVoice 不生成精确字幕时间轴。模型文件校验和运行接口验证不代表本机识别质量已实测。</p>
  </div>;
}
