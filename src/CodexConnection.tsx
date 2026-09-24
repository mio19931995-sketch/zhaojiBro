import { useEffect, useState } from 'react';
import type { Shared } from './App';
import { api } from './api';
import { Button } from './components';

type Status = { registered: boolean; available_media: number; cache_enabled: boolean; message: string };
export function CodexConnection({ refresh, notify }: Shared) {
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  async function load() { try { setStatus(await api('/codex/status')); } catch (e) { notify((e as Error).message, true); } }
  useEffect(() => { load(); }, []);
  async function connect() {
    setBusy(true);
    try { setStatus(await api('/codex/connect', 'POST')); notify('连接配置已保存，请重新打开 Codex 以加载 Lulu 工具'); }
    catch (e) { notify((e as Error).message, true); } finally { setBusy(false); }
  }
  async function cache(value: boolean) {
    const previous = status;
    if (status) setStatus({ ...status, cache_enabled: value });
    setBusy(true);
    try { await api('/settings', 'PATCH', { codex_keep_video: String(value) }); await refresh(); await load(); }
    catch (e) { setStatus(previous); notify((e as Error).message, true); } finally { setBusy(false); }
  }
  return <section className="panel writing-panel">
    <h2>Lulu 素材直连</h2><p>抓取完成后，在 Codex 中直接查找素材、读取文稿与时间轴、查看视频画面，无需手动找文件或重复上传。</p>
    <div className="service-note"><span>{status?.message || '正在读取连接状态…'}{status && ` · ${status.available_media} 份素材有可用媒体`}</span></div>
    <div className="form-actions"><Button disabled={busy} onClick={load}>刷新状态</Button><Button primary disabled={busy} onClick={connect}>{busy ? '处理中…' : status?.registered ? '重新配置连接' : '连接 Codex'}</Button></div>
    <h3>抓取后自动保留视频</h3>
    <label className="check-label"><input type="checkbox" checked={!!status?.cache_enabled} disabled={busy || !status} onChange={e => cache(e.target.checked)}/>为 Codex 保留视频缓存</label>
    <p className="muted">启用后，新的音视频抓取任务会保留完整视频，覆盖“仅音频”和临时清理选项。缓存由 Lulu 自动管理，会占用磁盘；关闭后恢复原来的下载选项。已经清理的视频需要重新抓取。</p>
    <h3>怎么使用</h3>
    <ol><li>首次连接后重新打开 Codex，加载 Lulu 工具。</li><li>在 Lulu 抓取视频，等待完成。</li><li>在 Codex 说：“读取 Lulu 最近抓取的视频，帮我分析内容。”</li></ol>
    <p>指定某一份素材时，在右侧预览点击“复制到 Codex”，或在该预览中按 Ctrl+Alt+C，内容会自动进入系统剪贴板。自行切换到 Codex，选好项目与对话，再按 Ctrl+V 粘贴并发送；不会自动跳转或发送。视频本身无需上传。</p>
    <p className="muted">“配置已写入”不代表当前 Codex 对话已加载连接。连接适用于这台电脑上的 Codex；关闭 Lulu 后仍可读取已经保存的素材。</p>
  </section>;
}
