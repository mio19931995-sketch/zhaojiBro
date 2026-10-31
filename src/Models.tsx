import { useState } from 'react';
import type { Shared } from './App';
import { TranscriptionModels } from './TranscriptionModels';
import { TextModelSettings } from './TextModelSettings';
import { JevSettings } from './Jev';

export function Models(props: Shared) {
  const [tab, setTab] = useState('transcription');
  const { state, refresh, notify } = props;
  return <section className="panel models-panel">
    <div className="settings-tabs" role="tablist" aria-label="模型设置">
      {[['transcription', '本地转录模型'], ['text', '文本处理模型'], ['jev', 'JEV 内容检查']].map(([id, label]) =>
        <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? 'selected' : ''} onClick={() => setTab(id)}>{label}</button>)}
    </div>
    {tab === 'transcription' ? <TranscriptionModels {...props}/> : tab === 'jev'
      ? <JevSettings settings={state.settings} refresh={refresh} notify={notify}/>
      : <TextModelSettings settings={state.settings} refresh={refresh} notify={notify}/>}
  </section>;
}
