import csv
import io
import json
import time
import threading
import uuid
import zipfile
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, FileResponse
from . import store,engine,assets,feishu

router=APIRouter(prefix='/api')
collection_cancel=set()


def require_ids(ids):
    if not isinstance(ids,list) or not ids or len(ids)>1000: raise ValueError('请选择 1–1000 条素材')
    items=[store.get(i) for i in dict.fromkeys(ids)]
    if any(i is None for i in items): raise ValueError('所选素材已移除，请刷新')
    return items


@router.get('/folders')
def folders(): return store.folders()


@router.post('/folders')
def create_folder(body:dict):
    name=str(body.get('name','')).strip()
    if not name or len(name)>200: raise ValueError('请输入不超过 200 字的文件夹名称')
    with store.connection() as con: con.execute('INSERT OR IGNORE INTO folders VALUES (?)',(name,))
    return {'name':name}


@router.patch('/folders')
def rename_folder(body:dict):
    old=body.get('old');new=str(body.get('name','')).strip()
    if not old or old=='根目录': raise ValueError('根目录不能改名')
    if not new or len(new)>200: raise ValueError('请输入有效名称')
    if new!=old and new in store.folders(): raise ValueError('已有同名文件夹')
    if any(i['folder']==old and i['id'] in engine.pending for i in store.items()): raise ValueError('请等待文件夹内的任务结束')
    with store.connection() as con:
        con.execute('UPDATE items SET folder=?,updated_at=? WHERE folder=?',(new,time.time(),old))
        con.execute('DELETE FROM folders WHERE name=?',(old,));con.execute('INSERT OR IGNORE INTO folders VALUES (?)',(new,))
    return {'ok':True}


@router.delete('/folders')
def delete_folder(body:dict):
    name=body.get('name')
    if not name or name=='根目录':raise ValueError('根目录不能移除')
    if any(i['folder']==name and i['id'] in engine.pending for i in store.items()):raise ValueError('请等待文件夹内的任务结束')
    with store.connection() as con:
        con.execute("UPDATE items SET folder='根目录',updated_at=? WHERE folder=?",(time.time(),name))
        con.execute('DELETE FROM folders WHERE name=?',(name,))
    return {'ok':True}


@router.get('/library/search')
def search(q:str=''):
    return {'ids':[i['id'] for i in store.items() if q.casefold() in (i['title']+'\n'+i['transcript']+'\n'+i['metadata'].get('author','')).casefold()]}


@router.post('/items/batch')
def batch(body:dict):
    items=require_ids(body.get('ids'));action=body.get('action')
    if action not in ('move','delete','assets','transcribe','pause'): raise ValueError('不支持的批量操作')
    if action!='pause' and any(i['id'] in engine.pending for i in items): raise ValueError('请等待所选任务结束或暂停后再操作')
    opts=assets.validate_options(body.get('options',{}))
    if action=='assets' and not opts.get('media',True) and not opts.get('cover'):raise ValueError('请至少选择音视频或封面')
    if action=='move':create_folder({'name':body.get('folder')})
    for item in items:
        if action=='move':store.update(item['id'],folder=body['folder'])
        elif action=='delete':store.update(item['id'],deleted=1)
        elif action=='pause':engine.pause(item['id'])
        else:
            metadata=item['metadata'] | {'download_only':action=='assets','action':action,'options':opts}
            store.update(item['id'],metadata=metadata)
            engine.enqueue(item['id'])
    return {'count':len(items)}


@router.get('/items/{item_id}/cover')
def cover(item_id:str):
    item=store.get(item_id)
    path=item['metadata'].get('cover_path') if item else None
    if not path or not Path(path).is_file():raise HTTPException(404,'还没有本地封面')
    return FileResponse(path)


@router.get('/items/{item_id}/file-location')
def file_location(item_id:str,kind:str='media'):
    item=store.get(item_id)
    if not item:raise ValueError('找不到素材')
    path=item['metadata'].get('cover_path','') if kind=='cover' else item['media_path'] if kind=='media' else str(store.DATA/'documents'/f'{item_id}.md')
    if kind=='document':Path(path).write_text(engine.export_content(item,'md'),encoding='utf-8')
    if not path or not Path(path).is_file():raise ValueError('本地文件不存在，请先下载对应素材')
    return {'path':path}


@router.post('/library/export')
def export_batch(body:dict):
    items=require_ids(body.get('ids'));fmt=body.get('format','md')
    if fmt not in ('md','txt','srt','csv'):raise ValueError('不支持的导出格式')
    if fmt=='csv':
        stream=io.StringIO(newline='');writer=csv.writer(stream)
        writer.writerow(['标题','作者','发布时间','时长','点赞','收藏','评论','分享','原作品链接','文稿'])
        for i in items:
            m=i['metadata'];row=[i['title'],m.get('author',''),m.get('date',''),i['duration'],m.get('likes',''),m.get('collections',''),m.get('comments',''),m.get('shares',''),i['source_url'],i['transcript']]
            writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in row])
        return Response(stream.getvalue().encode('utf-8-sig'),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="lulu-selected.csv"'})
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as archive:
        for i in items:archive.writestr(f'{assets.safe_name(i["title"])}-{i["id"][:8]}.{fmt}',engine.export_content(i,fmt).encode('utf-8-sig'))
    return Response(buf.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="lulu-selected.zip"'})


def add_entries(collection,entries):
    existing={i['source_url']:i for i in store.items()}
    item_ids=list(collection.get('ids',[]))
    for entry in entries:
        url=entry.get('url','')
        if not url.startswith(('http://','https://')):continue
        metadata={k:entry.get(k) for k in ('author','date','likes','views','comments','shares','collections','direct_url','cover_url','provider') if entry.get(k) is not None}
        metadata['collection_id']=collection['id']
        if url in existing:
            item=existing[url]
            store.update(item['id'],metadata=item['metadata'] | metadata,title=entry.get('title') or item['title'],duration=entry.get('duration') or item['duration'])
        else:
            item=store.add(str(entry.get('title') or '未命名作品')[:500],source_url=url,folder=collection['title'],duration=entry.get('duration') or 0,metadata=metadata)
            existing[url]=item
        if item['id'] not in item_ids:item_ids.append(item['id'])
    collection['ids']=item_ids;collection['updated_at']=time.time()
    store.object_put('collections',collection['id'],collection)
    return collection


@router.get('/collections')
def collections():return store.object_list('collections')


@router.post('/collections')
def create_collection(body:dict):
    url=body.get('url','');engine.validate_url(url)
    limit=int(body.get('limit',100))
    if limit<1 or limit>10000:raise ValueError('采集上限应在 1–10000 条之间')
    collection={'id':uuid.uuid4().hex,'url':url,'title':body.get('title') or '主页采集 '+time.strftime('%m-%d %H:%M'),
                'ids':[],'status':'waiting' if body.get('browser') else 'running','error':'','created_at':time.time(),'limit':limit}
    store.object_put('collections',collection['id'],collection)
    if not body.get('browser'):
        def run():
            try:
                import yt_dlp
                opts=engine.yt_options() | {'extract_flat':'in_playlist','noplaylist':False,'playlistend':limit,'lazy_playlist':True,'skip_download':True}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info=ydl.extract_info(url,download=False)
                    collection['title']=info.get('title') or collection['title']
                    for entry in info.get('entries') or [info]:
                        if collection['id'] in collection_cancel:break
                        if not entry:continue
                        source=entry.get('webpage_url') or entry.get('url') or url
                        if not source.startswith('http') and entry.get('ie_key')=='Youtube':source='https://www.youtube.com/watch?v='+entry['id']
                        add_entries(collection,[{'url':source,'title':entry.get('title'),'author':entry.get('uploader') or info.get('uploader',''),
                            'duration':entry.get('duration',0),'date':entry.get('upload_date',''),'likes':entry.get('like_count'),
                            'views':entry.get('view_count'),'comments':entry.get('comment_count'),'cover_url':entry.get('thumbnail','')}])
                collection['status']='paused' if collection['id'] in collection_cancel else 'done'
            except Exception as exc:collection.update(status='error',error=str(exc)[:700])
            store.object_put('collections',collection['id'],collection)
        threading.Thread(target=run,daemon=True).start()
    return collection


@router.post('/collections/{collection_id}/capture')
def capture(collection_id:str,body:dict):
    collection=store.object_get('collections',collection_id)
    if not collection:raise ValueError('采集任务不存在')
    entries=body.get('entries',[])
    if not isinstance(entries,list) or len(entries)>1000:raise ValueError('采集结果格式错误')
    if body.get('title'):collection['title']=str(body['title'])[:200]
    collection['status']=body.get('status','running');collection['error']=str(body.get('error',''))[:500]
    return add_entries(collection,entries)


@router.post('/collections/{collection_id}/pause')
def pause_collection(collection_id:str):
    collection_cancel.add(collection_id)
    return {'ok':True}


@router.delete('/collections/{collection_id}')
def remove_collection(collection_id:str):
    collection=store.object_get('collections',collection_id)
    if collection and collection['status']=='running':raise ValueError('请先停止采集再移除任务')
    store.object_delete('collections',collection_id)
    return {'ok':True}


@router.post('/items/{item_id}/platform')
def capture_single(item_id:str,body:dict):
    item=store.get(item_id)
    if not item or item_id in engine.pending:raise ValueError('素材不存在或正在处理')
    metadata=item['metadata'] | {k:body[k] for k in ('author','date','likes','views','comments','shares','collections','direct_url','cover_url','provider') if k in body}
    store.update(item_id,metadata=metadata,title=body.get('title') or item['title'],duration=body.get('duration') or item['duration'])
    return store.get(item_id)


@router.get('/feishu/status')
def feishu_status():return feishu.auth_status()
@router.post('/feishu/login')
def feishu_login():return feishu.start_auth()
@router.delete('/feishu/login')
def feishu_logout():feishu.logout();return {'ok':True}
@router.get('/feishu/browse')
def feishu_browse(kind:str='spaces',space:str='',parent:str=''):return feishu.browse(kind,space,parent)
@router.post('/feishu/resolve')
def feishu_resolve(body:dict):
    with feishu.Client() as client:return feishu.resolve_target(client,body.get('base',''),body.get('table',''))
@router.get('/feishu/presets')
def presets():return store.object_list('feishu-presets')
@router.post('/feishu/presets')
def save_preset(body:dict):return feishu.save_preset(body)
@router.delete('/feishu/presets/{preset_id}')
def delete_preset(preset_id:str):store.object_delete('feishu-presets',preset_id);return {'ok':True}
@router.post('/feishu/jobs')
def start_export(body:dict):return feishu.start_export(body)
@router.get('/feishu/jobs')
def export_jobs():
    jobs=store.object_list('feishu-jobs')
    for job in jobs:
        if job['status'] in ('running','queued') and job['id'] not in feishu.active_jobs:
            job.update(status='paused',phase='上次导出中断，可继续');store.object_put('feishu-jobs',job['id'],job)
    return jobs
@router.post('/feishu/jobs/{job_id}/retry')
def retry_export(job_id:str):return feishu.resume_export(job_id)
