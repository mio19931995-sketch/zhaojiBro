import { useEffect, useRef, useState } from 'react';
import { ArrowsInSimple, ArrowsOutSimple, ArrowSquareOut } from '@phosphor-icons/react';
import { api, openExternal, captureArticle, isWechatArticle } from './api';
import type { Item } from './api';
import { Button } from './components';

export function ArticleReader({ item, active, refresh, notify }: {
  item: Item; active: boolean; refresh: () => Promise<void>; notify: (text: string, error?: boolean) => void;
}) {
  const root = useRef<HTMLElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const ownedFullscreen = useRef(false);
  const removeFrameListener = useRef<() => void>(() => {});
  const [fullscreen, setFullscreen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const view = item.metadata.article_view;
  const version = view?.version;

  function escapeArticle(event: KeyboardEvent) {
    if (event.key !== 'Escape' || !ownedFullscreen.current) return;
    event.preventDefault();
    const exit = window.desktop?.setFullscreen ? window.desktop.setFullscreen(false) : document.exitFullscreen();
    void exit.then(() => { ownedFullscreen.current = false; setFullscreen(false); }).catch(() => notify('退出全屏失败，请点击退出全屏重试', true));
  }
  useEffect(() => { setFailed(false); }, [version]);
  useEffect(() => {
    const changed = () => { const value = document.fullscreenElement === root.current; ownedFullscreen.current = value; setFullscreen(value); };
    const unsubscribe = window.desktop?.onFullscreenChange?.(value => {
      if (ownedFullscreen.current && !value) { ownedFullscreen.current = false; setFullscreen(false); }
    });
    document.addEventListener('fullscreenchange', changed);
    document.addEventListener('keydown', escapeArticle);
    return () => {
      unsubscribe?.(); document.removeEventListener('fullscreenchange', changed);
      document.removeEventListener('keydown', escapeArticle); removeFrameListener.current();
      if (ownedFullscreen.current) {
        if (window.desktop?.setFullscreen) void window.desktop.setFullscreen(false).catch(() => {});
        else if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
      }
    };
  }, []);

  async function toggleFullscreen() {
    if (!root.current) return;
    try {
      if (window.desktop?.setFullscreen) {
        const next = !ownedFullscreen.current;
        ownedFullscreen.current = next;
        await window.desktop.setFullscreen(next);
        setFullscreen(next);
      } else if (document.fullscreenElement) await document.exitFullscreen();
      else await root.current.requestFullscreen();
    } catch {
      ownedFullscreen.current = false; setFullscreen(false);
      notify('全屏切换失败，请重试', true);
    }
  }
  async function loadOriginal() {
    setBusy(true);
    try { if (!isWechatArticle(item)) await captureArticle(item, true); await api(`/items/${item.id}/article-refresh`, 'POST'); await refresh(); notify('正在保存原文图文，已编辑的文稿会保留'); }
    catch (e) { notify((e as Error).message, true); }
    finally { setBusy(false); }
  }
  return <section ref={root} className={`article-reader ${fullscreen ? 'article-reader-fullscreen' : ''}`} aria-label="原文阅读器">
    <div className="article-reader-toolbar">
      <Button primary onClick={toggleFullscreen}>{fullscreen ? <ArrowsInSimple size={16}/> : <ArrowsOutSimple size={16}/>}{fullscreen ? '退出全屏' : '全屏浏览'}</Button>
      <Button disabled={active || busy} onClick={loadOriginal}>{active || busy ? '正在保存图文…' : version ? '刷新图文' : '加载原文'}</Button>
      <Button onClick={() => openExternal(item.source_url).catch(e => notify(e.message, true))}><ArrowSquareOut size={15}/>在线原页</Button>
      {fullscreen && <span>Esc 返回</span>}
    </div>
    {(view?.missing_images > 0 || view?.missing_resources > 0 || view?.missing_styles > 0) && <p className="article-reader-notice" role="status">已保存 {view.saved_images}/{view.image_count} 张原图；部分图片或样式未获取，可刷新图文或查看在线原页。</p>}
    {item.status === 'error' && <p className="article-reader-notice" role="alert">{item.error}</p>}
    {version && !failed ? <iframe ref={frame} title="文章原文图文" sandbox="allow-same-origin" referrerPolicy="no-referrer"
      src={`/api/items/${item.id}/article-view/${version}`} onLoad={() => {
        // A missing archive returns JSON; show a recovery action instead of an empty reader.
        try {
          removeFrameListener.current();
          const doc = frame.current?.contentDocument;
          if (doc?.contentType !== 'text/html') { setFailed(true); return; }
          // The trusted parent listens directly: iframe keys do not bubble out.
          // This does not enable or execute any script from the captured article.
          doc.addEventListener('keydown', escapeArticle);
          removeFrameListener.current = () => doc.removeEventListener('keydown', escapeArticle);
          for (const gallery of doc.querySelectorAll<HTMLElement>('[data-lulu-gallery]')) {
            const pictures = [...gallery.querySelectorAll<HTMLImageElement>('img')];
            if (pictures.length < 2) continue;
            let index = 0;
            const controls = doc.createElement('div'), previous = doc.createElement('button'), next = doc.createElement('button'), count = doc.createElement('span');
            controls.style.cssText='display:flex;justify-content:center;gap:16px;align-items:center;padding:12px';
            previous.textContent='上一张'; next.textContent='下一张';
            const show = () => { pictures.forEach((picture,i)=>picture.style.display=i===index?'block':'none'); count.textContent=`${index+1} / ${pictures.length}`; previous.disabled=index===0; next.disabled=index===pictures.length-1; };
            previous.onclick=()=>{index=Math.max(0,index-1);show();}; next.onclick=()=>{index=Math.min(pictures.length-1,index+1);show();};
            controls.append(previous,count,next);gallery.append(controls);show();
          }
        } catch { setFailed(true); }
      }} onError={() => setFailed(true)}/> : <div className="article-reader-empty"><strong>{failed ? '原文图文文件暂不可用' : active ? '正在保存原文图文' : '这篇文章尚未保存原文图文'}</strong><p>点击「{version ? '刷新图文' : '加载原文'}」保留原文图片、字体和段落排版，不会覆盖已编辑的文稿。</p><p>视频、互动组件及需要登录的内容，请使用「在线原页」查看。</p></div>}
  </section>;
}
