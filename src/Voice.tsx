import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { CircleNotch, Microphone, Monitor } from '@phosphor-icons/react';
import { api } from './api';
import type { Item } from './api';
import type { Shared } from './App';
import { Button } from './components';
import { readDraft, writeDraft } from './drafts';

type VoiceOption = { id?: string; name: string; language: string; gender?: string; engine?: string };
type VoiceDraft = { source: string; text: string; voice: string; rate: string };
const blankDraft = (source = ''): VoiceDraft => ({ source, text: '', voice: '', rate: '0' });
const draftKey = (source: string) => `voice-draft-${source || 'manual'}`;
const voiceId = (voice: VoiceOption) => voice.id || voice.name;
// A page change must not make an active generation look idle or submit it twice.
let voiceGeneration: Promise<void> | null = null;
const generationListeners = new Set<() => void>();
const subscribeGeneration = (listener: () => void) => {
  generationListeners.add(listener);
  return () => { generationListeners.delete(listener); };
};
const generationBusy = () => voiceGeneration !== null;
const publishGeneration = () => { generationListeners.forEach(listener => listener()); };

export function Voice({ state, refresh, select, notify }: Shared) {
  const [draft, setDraft] = useState<VoiceDraft>(blankDraft);
  const [source, setSource] = useState('');
  const [voices, setVoices] = useState<VoiceOption[]>([]);
  const [sessionReady, setSessionReady] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const busy = useSyncExternalStore(subscribeGeneration, generationBusy);
  const [loadingError, setLoadingError] = useState('');
  const [voiceNotice, setVoiceNotice] = useState('');
  const [saveStatus, setSaveStatus] = useState('');
  const [saveError, setSaveError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const currentDraft = useRef<VoiceDraft>(blankDraft());
  const request = useRef(0);
  const saveRequest = useRef(0);
  const mounted = useRef(false);

  function persist(next: VoiceDraft) {
    const revision = ++saveRequest.current;
    setSaveStatus('正在保存草稿…');
    setSaveError(false);
    // writeDraft updates its navigation cache synchronously, before the SQLite write.
    const writes = [writeDraft(draftKey(next.source), next), writeDraft('voice-session', { source: next.source })];
    Promise.all(writes).then(() => {
      if (mounted.current && revision === saveRequest.current) setSaveStatus('草稿已保存');
    }).catch(() => {
      if (mounted.current && revision === saveRequest.current) {
        setSaveStatus('草稿保存失败，请重试');
        setSaveError(true);
      }
    });
  }

  async function loadSource(id: string, available: VoiceOption[]) {
    const revision = ++request.current;
    ++saveRequest.current;
    setSource(id);
    setLoaded(false);
    setLoadingError('');
    setSaveStatus('');
    setSaveError(false);
    setVoiceNotice('');
    setDraft(blankDraft(id));
    try {
      const saved = await readDraft<VoiceDraft>(draftKey(id));
      if (!mounted.current || revision !== request.current) return;
      const text = saved?.text ?? (id ? (await api<Item>(`/items/${id}`)).transcript : '');
      if (!mounted.current || revision !== request.current) return;
      const wantedVoice = saved?.voice || '';
      const matching = available.find(v => voiceId(v) === wantedVoice || v.name === wantedVoice);
      const fallback = available.find(v => v.name.includes('Huihui')) || available.find(v => v.language.startsWith('zh')) || available[0];
      const resolved = matching || fallback;
      const next: VoiceDraft = {
        source: id, text, voice: resolved ? voiceId(resolved) : wantedVoice,
        rate: ['-2', '0', '2'].includes(String(saved?.rate)) ? String(saved?.rate) : '0',
      };
      if (wantedVoice && !matching && resolved) setVoiceNotice(`上次选择的音色已不可用，已切换为 ${resolved.name}。`);
      if (!available.length) setVoiceNotice('没有读取到可用音色，草稿仍可编辑和保存。请检查 Windows 语音包后重新进入此页。');
      currentDraft.current = next;
      setDraft(next);
      setLoaded(true);
      persist(next);
    } catch (error) {
      if (mounted.current && revision === request.current) setLoadingError(`草稿读取失败：${(error as Error).message}`);
    }
  }

  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    setSessionReady(false);
    setLoaded(false);
    setLoadingError('');
    const voiceList = api<VoiceOption[]>('/voices').catch(error => {
      if (!cancelled) notify(error.message, true);
      return [];
    });
    Promise.all([readDraft<{ source: string }>('voice-session'), voiceList]).then(([session, available]) => {
      if (cancelled) return;
      setVoices(available);
      setSessionReady(true);
      return loadSource(session?.source || '', available);
    }).catch(error => {
      if (!cancelled) setLoadingError(`草稿读取失败：${error.message}`);
    });
    return () => { cancelled = true; mounted.current = false; ++request.current; ++saveRequest.current; };
  }, [notify, attempt]);

  function updateDraft(patch: Partial<VoiceDraft>) {
    if (!loaded) return;
    const next = { ...currentDraft.current, ...patch };
    currentDraft.current = next;
    setDraft(next);
    if (patch.voice) setVoiceNotice('');
    persist(next);
  }

  function generate() {
    if (!loaded || voiceGeneration) return;
    const operation = (async () => {
      try {
        const item = await api<Item>('/voice', 'POST', {
          text: draft.text, voice: draft.voice, rate: Number(draft.rate),
          source_id: state.items.some(i => i.id === draft.source) ? draft.source : '',
          title: (state.items.find(i => i.id === draft.source)?.title || '本地配音') + ' · 配音',
        });
        await refresh();
        select(item.id);
        notify('配音已保存到音频库');
      } catch (error) { notify((error as Error).message, true); }
    })();
    voiceGeneration = operation;
    publishGeneration();
    const finish = () => {
      if (voiceGeneration === operation) { voiceGeneration = null; publishGeneration(); }
    };
    void operation.then(finish, finish);
  }

  const sources = state.items.filter(i => i.has_transcript);
  return <section className="panel writing-panel">
    <div className="panel-heading"><h2><Microphone size={19}/>配音正文</h2>
      <select aria-label="选择配音文稿" disabled={!sessionReady || busy} value={source} onChange={e => loadSource(e.target.value, voices)}>
        <option value="">手动输入正文</option>
        {source && !sources.some(i => i.id === source) && <option value={source}>上次文稿（保留的配音草稿）</option>}
        {sources.map(i => <option key={i.id} value={i.id}>{i.title}</option>)}
      </select>
    </div>
    <textarea aria-label="配音正文" disabled={!loaded || busy} className="writing-area" value={draft.text} onChange={e => updateDraft({ text: e.target.value })} placeholder="写下希望读出的内容。只会朗读这里的正文。"/>
    <div className="form-grid voice-options">
      <label>本地音色<select aria-label="本地音色" disabled={!loaded || busy || !voices.length} value={draft.voice} onChange={e => updateDraft({ voice: e.target.value })}>
        {!voices.length && <option value={draft.voice}>暂无可用音色</option>}
        {voices.map(v => <option key={voiceId(v)} value={voiceId(v)}>{v.name} · {v.gender === 'male' ? '男声' : v.gender === 'female' ? '女声' : '系统音色'} · {v.language}</option>)}
      </select></label>
      <label>语速<select aria-label="语速" disabled={!loaded || busy} value={draft.rate} onChange={e => updateDraft({ rate: e.target.value })}><option value="-2">稍慢</option><option value="0">正常</option><option value="2">稍快</option></select></label>
    </div>
    <div className="service-note"><Monitor size={19}/><span>{voiceNotice || `已读取 ${voices.length} 个 Windows 音色；全部在本机离线生成 WAV 音频。每份文稿的正文、音色和语速会分别保存。`}</span></div>
    <div className="form-actions">
      <span className="muted" role="status" data-testid="voice-draft-status">{loadingError || (!loaded ? '正在恢复配音草稿…' : `${draft.text.length.toLocaleString()} 字 · ${saveStatus}`)}</span>
      {loadingError && <Button onClick={() => sessionReady ? loadSource(source, voices) : setAttempt(v => v + 1)}>重新读取</Button>}
      {saveError && <Button onClick={() => persist(currentDraft.current)}>重试保存</Button>}
      <Button primary disabled={!loaded || busy || !draft.text.trim() || !voices.length} onClick={generate}>{busy ? <CircleNotch size={16} className="spin"/> : <Microphone size={16}/>} {busy ? '正在生成配音…' : '生成配音'}</Button>
    </div>
  </section>;
}
