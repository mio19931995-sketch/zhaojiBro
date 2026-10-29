// Executed in the normal, sandboxed platform page. No private APIs or hidden state.
function snapshot(provider) {
  const selectors = {
    zhihu: ['article.Post-Main', '.Post-Main', '.AnswerItem', '.RichContent'],
    toutiao: ['.article-content', '.article-detail', 'article'],
    xiaohongshu: ['#noteContainer', '.note-container', '.note-detail'],
  };
  const root = selectors[provider]?.map(s => document.querySelector(s)).find(Boolean);
  const visible = el => el && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0;
  if (!root || !visible(root)) return { error: '未找到文章正文，请确认链接有效，并在此窗口完成必要的登录或验证后，点击菜单“保存当前文章”。' };
  const expand = root.querySelector('.ContentItem-expandButton');
  if (provider === 'zhihu' && visible(expand)) { expand.click(); return { error: '正在展开正文；若平台要求登录，请完成后保存。' }; }
  const text = root.innerText.trim();
  if (/付费阅读全文|购买后阅读|会员专享全文|开通会员查看/.test(text)) return { error: '当前仅显示付费预览，请先按平台要求取得完整阅读权限。' };
  if (provider === 'xiaohongshu' && root.querySelector('video')) return { media: true, url: location.href };
  const first = (...values) => values.find(v => typeof v === 'string' && v.trim())?.trim() || '';
  const title = first(root.querySelector('h1,#detail-title,.title')?.textContent, document.querySelector('h1.QuestionHeader-title')?.textContent,
    document.querySelector('meta[property="og:title"]')?.content, document.title.replace(/\s*[-–]\s*(知乎|今日头条|小红书.*)$/, ''));
  if (!title || text.length < 2 && !root.querySelector('img,.note-slider-img')) return { error: '正文尚未加载完整，请稍后保存。' };
  const author = first(root.querySelector('.AuthorInfo-name,.article-meta a,.username,.nickname')?.textContent);
  const date = first(root.querySelector('time,.article-meta span,.date,.ContentItem-time')?.textContent);
  const clone = root.cloneNode(true);
  const sourceNodes = [root, ...root.querySelectorAll('*')], clones = [clone, ...clone.querySelectorAll('*')];
  const properties = ['font-family','font-size','font-weight','font-style','line-height','letter-spacing','word-spacing','color','background-color',
    'text-align','text-indent','text-decoration','white-space','padding','margin','border','border-radius','list-style-type','vertical-align'];
  for (let i = 0; i < sourceNodes.length; i++) {
    const source = sourceNodes[i], node = clones[i], style = getComputedStyle(source);
    node.removeAttribute('style');
    for (const property of properties) node.style.setProperty(property, style.getPropertyValue(property));
    // Preserve invisible descendants as invisible (except lazy images).
    if (style.display === 'none' && source.tagName !== 'IMG') node.style.display = 'none';
    if (source.tagName === 'IMG') {
      node.src = source.getAttribute('data-original') || source.getAttribute('data-actualsrc') || source.getAttribute('data-src') || source.currentSrc || source.src;
      node.removeAttribute('data-src'); node.removeAttribute('srcset');
      node.style.maxWidth = '100%'; node.style.height = 'auto'; node.style.objectFit = 'contain';
      if (parseFloat(style.width) > 0) node.style.width = style.width;
      node.style.display = style.display;
    }
  }
  clone.querySelectorAll('script,noscript,iframe,video,audio,button,form,.comments-container,.Comments-container,.ContentItem-actions,.Post-Sub,.engage-bar,.follow-btn,.follow-button').forEach(n => n.remove());
  clone.style.cssText += ';position:static;transform:none;overflow:visible;height:auto;width:auto;max-width:100%;margin:0;';
  if (provider === 'xiaohongshu') {
    const images = [...root.querySelectorAll('.swiper-slide img,.note-slider-img')].map(img => {
      const src = img.currentSrc || img.src || getComputedStyle(img).backgroundImage.match(/url\(["']?(.*?)["']?\)/)?.[1];
      return { src, order: Number(img.closest('[data-swiper-slide-index]')?.getAttribute('data-swiper-slide-index') || 0) };
    }).filter(x => /^(https?:|data:image\/)/.test(x.src || '')).sort((a,b) => a.order-b.order);
    const urls = [...new Set(images.map(x => x.src))];
    if (!urls.length) return { error: '图文图片尚未加载，请在平台中查看图片后重试保存。' };
    const gallery = document.createElement('div'); gallery.setAttribute('data-lulu-gallery', 'true');
    gallery.style.cssText='flex:1 1 0;min-width:0;max-width:100%';
    for (const [i, url] of urls.entries()) { const img = document.createElement('img'); img.src=url; img.alt=`原文图片 ${i+1}`; img.style.cssText=`max-width:100%;width:100%;height:auto;object-fit:contain;display:${i?'none':'block'}`;gallery.append(img); }
    const media = clone.querySelector('.media-container,.note-slider,.swiper,.swiper-container');
    if (media) media.replaceWith(gallery); else clone.prepend(gallery);
    clone.querySelectorAll('.author-container,.note-content,.note-scroller,.content,.interaction-container').forEach(n => { n.style.height='auto'; n.style.overflow='visible'; n.style.maxHeight='none'; n.style.maxWidth='100%'; });
    clone.querySelectorAll('.interaction-container').forEach(n=>{n.style.flex='1 1 0';n.style.minWidth='0';});
  }
  const body = document.createElement('div'); body.id='lulu-article-root';
  body.style.cssText=`max-width:${Math.min(1100, Math.max(640, Math.ceil(root.getBoundingClientRect().width)))}px;margin:24px auto;padding:0 20px;box-sizing:border-box;overflow-wrap:anywhere;`;
  // Zhihu places the article's title image immediately before Post-Main.
  // Keep that visible image without including the recommendation/sidebar area.
  if (provider === 'zhihu' && root.matches('.Post-Main')) {
    const previous = root.previousElementSibling;
    const cover = previous?.matches('img') ? previous : previous?.querySelector('picture img,.Post-TitleImage');
    if (visible(cover)) {
      const image = document.createElement('img');
      image.src = cover.currentSrc || cover.src;
      image.alt = cover.alt || '原文题图'; image.setAttribute('data-lulu-cover', 'true');
      image.style.cssText = 'display:block;width:100%;max-width:100%;height:auto;margin:0 0 20px';
      body.append(image);
    }
  }
  body.append(clone);
  const head = document.createElement('head');
  for(const [name,content] of Object.entries({'lulu:title':title,'lulu:author':author,'lulu:date':date})) {const m=document.createElement('meta');m.name=name;m.content=content;head.append(m);}
  // Font-face declarations remain available; all external URLs are rewritten by the archive.
  for(const sheet of document.querySelectorAll('link[rel="stylesheet"],style')) head.append(sheet.cloneNode(true));
  return {url:location.href,html:`<!doctype html><html>${head.outerHTML}<body style="margin:0;background:white">${body.outerHTML}</body></html>`};
}
module.exports = { snapshot };
