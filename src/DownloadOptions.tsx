import { Folder } from '@phosphor-icons/react';
import { Button } from './components';
import type { Settings } from './api';
import type { Shared } from './App';
export type Options={mode:string;keep:boolean;cover:boolean;subtitles:boolean;directory:string;media:boolean};
export function initialOptions(s:Settings):Options{return {mode:String(s.download_mode||'audio'),keep:s.keep_media==='true',cover:s.download_cover==='true',subtitles:s.prefer_subtitles!=='false',directory:String(s.save_directory||''),media:true};}
export function DownloadOptions({value,change,notify,collection=false,codexCache=false}:{codexCache?:boolean;value:Options;change:(v:Options)=>void;notify:Shared['notify'];collection?:boolean}){
  const retained=codexCache&&value.media;
  const shown=retained?{...value,mode:'video',keep:true}:value;
  const set=(k:keyof Options,v:string|boolean)=>change({...value,[k]:v});
  return <div className="download-options"><div className="toolbar">{collection&&<label className="check-label"><input type="checkbox" checked={value.media} onChange={e=>set('media',e.target.checked)}/>音视频文件</label>}<div className="segmented"><button disabled={retained} className={shown.mode==='audio'?'selected':''} onClick={()=>set('mode','audio')}>仅音频</button><button disabled={retained} className={shown.mode==='video'?'selected':''} onClick={()=>set('mode','video')}>音视频</button></div><label className="check-label"><input type="checkbox" checked={value.cover} onChange={e=>set('cover',e.target.checked)}/>封面文件</label><label className="check-label"><input type="checkbox" checked={value.subtitles} onChange={e=>set('subtitles',e.target.checked)}/>优先字幕文件</label>{!collection&&<label className="check-label"><input type="checkbox" disabled={retained} checked={shown.keep} onChange={e=>set('keep',e.target.checked)}/>保留音视频</label>}<Button onClick={async()=>{try{if(!window.desktop)throw new Error('请在桌面版选择文件夹');const directory=await window.desktop.chooseFolder();if(directory)change({...value,directory,keep:true});}catch(e){notify((e as Error).message,true);}}}><Folder size={15}/>保存到</Button><span className="save-location" title={value.directory}>{value.directory||'程序数据目录'}</span></div>{!collection&&<p className="footnote">{retained?'已启用 Codex 视频缓存，完成后可直接读取画面与文稿。':value.keep?'转录结束后保留素材，可在右侧定位。':'转录成功后清理下载的临时音视频；本地导入的原文件会保留。'} 下载选项在点击「开始」时生效。</p>}</div>;
}
