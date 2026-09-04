export type Segment = { start: number; end: number; text: string };
export type Item = {
  id: string; title: string; kind: string; status: string; phase: string; error: string; progress: number;
  source_url: string; media_path: string; duration: number; folder: string; created_at: number; updated_at: number;
  has_transcript?: boolean; characters?: number; transcript: string; segments: Segment[];
  metadata: { author?: string; date?: string; likes?: number; views?: number; comments?: number; collections?: number; shares?:number; cover_path?:string; direct_url?:string; provider?:string; [key:string]:any };
};
export type Model = { id: string; name: string; note: string; size: string; installed: boolean; state: string; message: string };
export type Settings = Record<string, string | boolean>;
export type State = { items: Item[]; settings: Settings; models: Model[]; data_dir: string; folders?:string[] };
export type Entry = { key: string; title: string; url: string; author: string; duration: number; date: string; likes?: number; views?: number };

export async function api<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'X-Lulu-Client': 'desktop' };
  const isForm = body instanceof FormData;
  if (body && !isForm) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api' + path, { method, headers, body: body ? (isForm ? body : JSON.stringify(body)) : undefined });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: '本地服务暂时不可用' }));
    throw new Error(typeof error.detail === 'string' ? error.detail : '输入格式不正确，请检查后重试');
  }
  return response.json();
}
export function duration(seconds: number): string {
  const s = Math.floor(seconds || 0);
  return s >= 3600 ? `${Math.floor(s / 3600)}:${String(Math.floor(s % 3600 / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
export const labels: Record<string, string> = { idle: '待开始', queued: '排队中', processing: '处理中', done: '已完成', error: '需重试', paused: '已暂停' };

declare global {
  interface Window { desktop?: { openData: () => Promise<void>; showItem: (id: string,kind?:string) => Promise<void>; chooseFolder: () => Promise<string>; openExternal: (url: string) => Promise<void>; platform:(options:{url:string;limit?:number;collectionId?:string;loginOnly?:boolean})=>Promise<{entries:any[];complete:boolean;error:string}>; stopPlatform:()=>Promise<void> } }
}

export function openExternal(url:string){ if(window.desktop)return window.desktop.openExternal(url);window.open(url,'_blank','noopener,noreferrer');return Promise.resolve(); }
export async function reveal(id:string,kind='media'){if(!window.desktop)throw new Error('请在桌面程序中定位文件');await window.desktop.showItem(id,kind);}
export async function downloadBatch(ids:string[],format='md'){
  const r=await fetch('/api/library/export',{method:'POST',headers:{'Content-Type':'application/json','X-Lulu-Client':'desktop'},body:JSON.stringify({ids,format})});
  if(!r.ok)throw new Error((await r.json()).detail||'导出失败');
  const url=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=url;a.download=format==='csv'?'Lulu所选作品.csv':'Lulu所选文稿.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
}
export async function preparePlatform(item:Item){
  if(!/https:\/\/([^/]+\.)?douyin\.com\//.test(item.source_url)||(item.metadata.direct_url&&item.status!=='error'))return;
  if(!window.desktop)throw new Error('抖音作品请在桌面版的平台窗口中完成登录与解析');
  const r=await window.desktop.platform({url:item.source_url,limit:1});
  if(!r.entries.length)throw new Error(r.error||'尚未获取到作品，请登录后重试');
  await api(`/items/${item.id}/platform`,'POST',r.entries[0]);
}
