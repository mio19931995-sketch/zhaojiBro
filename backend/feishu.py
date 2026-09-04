"""Feishu OAuth, destination presets, and resumable exports using official OpenAPI."""
import base64
import hashlib
import hmac
import json
import re
import secrets as random
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from . import store, secrets, engine

API = 'https://open.feishu.cn/open-apis'
# v2 is the documented PKCE-compatible token endpoint for the authorize endpoint.
SCOPES = ['bitable:app', 'wiki:wiki', 'drive:drive:readonly', 'drive:drive.metadata:readonly', 'offline_access']
CALLBACKS = [f'http://127.0.0.1:{p}/feishu/callback' for p in (18794,18795,18796)]
FIELDS = [('Lulu ID',1),('标题',1),('作者',1),('发布时间',1),('时长',2),('点赞',2),('收藏',2),
          ('评论',2),('分享',2),('播放',2),('原作品链接',15),('音视频直链',15),('转录文稿',1),
          ('封面',17),('音视频',17)]
auth_lock = threading.RLock()
export_lock = threading.Lock()
auth_pending = {}
auth_servers = {}
active_jobs = set()


def decode_response(response):
    try: data = response.json()
    except Exception: raise ValueError(f'飞书返回非 JSON 响应（HTTP {response.status_code}），请稍后重试')
    if response.status_code >= 400 or data.get('code',0) != 0 or data.get('error'):
        # Never echo request URLs, authorization headers, app secrets, or tokens.
        message = data.get('msg') or data.get('error_description') or data.get('error') or '请检查权限和网络'
        if not isinstance(message,str): message = '请检查应用权限和登录状态'
        raise ValueError(f'飞书错误 {data.get("code",response.status_code)}：{message[:350]}')
    return data


def credentials():
    cfg = store.settings()
    app_id = cfg['feishu_app_id']; secret = secrets.unseal(cfg.get('feishu_secret_enc',''))
    if not app_id or not secret: raise ValueError('请先填写并保存自己的飞书 App ID 和 App Secret')
    return app_id, secret


def save_token(data):
    if not data.get('access_token'): raise ValueError('授权响应缺少用户访问凭证，请重新登录')
    store.save_settings({'feishu_user_token_enc': secrets.seal(data['access_token']),
        'feishu_refresh_token_enc': secrets.seal(data.get('refresh_token','')),
        'feishu_expires_at': time.time()+data.get('expires_in',7200), 'feishu_auth_error':''})


def token(user_required=False):
    with auth_lock:
        cfg = store.settings()
        app_id, secret = credentials()
        user = secrets.unseal(cfg.get('feishu_user_token_enc',''))
        if user and float(cfg.get('feishu_expires_at',0)) > time.time()+120: return user
        refresh = secrets.unseal(cfg.get('feishu_refresh_token_enc',''))
        with httpx.Client(timeout=40) as client:
            if refresh:
                data = decode_response(client.post(API+'/authen/v2/oauth/token',json={'grant_type':'refresh_token',
                    'client_id':app_id,'client_secret':secret,'refresh_token':refresh}))
                save_token(data); return data['access_token']
            if user_required or user:
                raise ValueError('请先在飞书知识库页面点击「登录授权」')
            data = decode_response(client.post(API+'/auth/v3/tenant_access_token/internal',json={'app_id':app_id,'app_secret':secret}))
            return data['tenant_access_token']


class Client:
    def __init__(self, user_required=False):
        self.user_required = user_required
        self.http = httpx.Client(timeout=180)
    def __enter__(self): return self
    def __exit__(self,*args): self.http.close()
    def request(self,method,path,**kwargs):
        # Mutations are deliberately not retried blindly on transport errors.
        response = self.http.request(method,API+path,headers={'Authorization':'Bearer '+token(self.user_required)},**kwargs)
        data = decode_response(response)
        return data.get('data',data)
    def pages(self,path,key='items',params=None):
        result=[]; params=dict(params or {}); seen=set()
        while True:
            data=self.request('GET',path,params=params); result.extend(data.get(key,[]))
            if not data.get('has_more'): return result
            cursor=data.get('page_token') or data.get('next_page_token')
            if not cursor or cursor in seen: raise ValueError('飞书分页游标无效，请重新加载')
            seen.add(cursor); params['page_token']=cursor


def auth_status():
    cfg=store.settings()
    return {'connected':bool(cfg.get('feishu_user_token_enc')), 'name':cfg.get('feishu_user_name',''),
        'pending':bool(auth_pending), 'error':cfg.get('feishu_auth_error',''), 'callbacks':CALLBACKS,
        'scopes':SCOPES, 'scope_json':json.dumps({'scopes':{'tenant':[], 'user':SCOPES}},ensure_ascii=False,indent=2)}


def logout():
    store.save_settings({k:'' for k in ('feishu_user_token_enc','feishu_refresh_token_enc','feishu_user_name','feishu_auth_error')})
    with auth_lock:
        auth_pending.clear()
        servers=list(auth_servers.values())
    for server in servers:server.shutdown()


def complete_auth(state,code,error=''):
    with auth_lock:
        pending=auth_pending.pop(state,None)
        if not pending or pending['expires']<time.time() or not hmac.compare_digest(state,pending['state']):
            raise ValueError('授权请求已过期或校验失败，请从 Lulu 重新发起登录')
    if error: raise ValueError('你已取消飞书授权，可以回到 Lulu 重新登录')
    if not code: raise ValueError('授权回调缺少授权码')
    app_id,secret=credentials()
    if app_id != pending['app_id']: raise ValueError('应用配置已变化，请重新登录')
    with httpx.Client(timeout=40) as client:
        data=decode_response(client.post(API+'/authen/v2/oauth/token',json={'grant_type':'authorization_code',
            'client_id':app_id,'client_secret':secret,'code':code,'redirect_uri':pending['redirect'],
            'code_verifier':pending['verifier']}))
        save_token(data)
    try:
        with Client(True) as client: profile=client.request('GET','/authen/v1/user_info')
        store.save_settings({'feishu_user_name':profile.get('name','已授权用户')})
    except ValueError:
        store.save_settings({'feishu_user_name':'已授权用户'})


def start_auth():
    app_id,_=credentials()
    with auth_lock:
        if auth_pending: raise ValueError('已有授权窗口等待登录，完成或关闭后再试；十分钟后会自动过期')
        state=random.token_urlsafe(32); verifier=random.token_urlsafe(48)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                if urlparse(self.path).path != '/feishu/callback': self.send_error(404); return
                query=parse_qs(urlparse(self.path).query)
                try:
                    complete_auth(query.get('state',[''])[0],query.get('code',[''])[0],query.get('error',[''])[0])
                    message='飞书连接成功，请返回 Lulu。'; status=200
                except Exception as exc:
                    message=str(exc); status=400
                    store.save_settings({'feishu_auth_error':message})
                import html
                body=('<!doctype html><meta charset="utf-8"><title>Lulu 飞书授权</title><h2>'+html.escape(message)+'</h2>').encode()
                self.send_response(status);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
                if status==200: threading.Thread(target=self.server.shutdown,daemon=True).start()
        server=None
        for callback in CALLBACKS:
            try: server=ThreadingHTTPServer(('127.0.0.1',urlparse(callback).port),Handler); break
            except OSError: continue
        if not server: raise ValueError('授权端口被占用，请关闭其他 Lulu 实例后重试')
        auth_pending[state]={'state':state,'verifier':verifier,'redirect':callback,'expires':time.time()+600,'app_id':app_id}
        auth_servers[state]=server
        store.save_settings({'feishu_auth_error':''})
        def serve():
            server.serve_forever(); server.server_close()
            with auth_lock:
                auth_pending.pop(state,None);auth_servers.pop(state,None)
        threading.Thread(target=serve,daemon=True).start()
        def expire():
            with auth_lock: auth_pending.pop(state,None)
            server.shutdown()
        timer=threading.Timer(600,expire); timer.daemon=True; timer.start()
        return {'url':'https://accounts.feishu.cn/open-apis/authen/v1/authorize?'+urlencode({
            'client_id':app_id,'response_type':'code','redirect_uri':callback,'scope':' '.join(SCOPES),
            'state':state,'code_challenge':challenge,'code_challenge_method':'S256','prompt':'consent'})}


def identifier(value,label='标识'):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}',value or ''): raise ValueError(f'{label}格式不正确')
    return value


def document_url(client,base,table=''):
    data=client.request('POST','/drive/v1/metas/batch_query',json={'request_docs':[{'doc_token':base,'doc_type':'bitable'}],'with_url':True})
    url=next((m.get('url') for m in data.get('metas',[]) if m.get('url')),None)
    if not url:raise ValueError('无法取得作品库访问链接，请检查云文档元数据读取权限后重试')
    parsed=urlparse(url);query=parse_qs(parsed.query)
    if table:query['table']=[table]
    return parsed._replace(query=urlencode(query,doseq=True)).geturl()


def resolve_target(client,value,table=''):
    value=value.strip(); parsed=urlparse(value)
    if parsed.scheme:
        if not parsed.hostname or not (parsed.hostname.endswith('.feishu.cn') or parsed.hostname.endswith('.larksuite.com')):
            raise ValueError('请输入飞书多维表格或知识库链接')
        match=re.search(r'/(base|wiki)/([a-zA-Z0-9]+)',parsed.path)
        if not match: raise ValueError('链接中没有多维表格或知识库标识')
        base=match[2]
        table=table or parse_qs(parsed.query).get('table',[''])[0]
        if match[1]=='wiki':
            node=client.request('GET','/wiki/v2/spaces/get_node',params={'token':base})['node']
            if node['obj_type']!='bitable': raise ValueError('所选知识库节点不是多维表格')
            base=node['obj_token']
    else: base=value
    identifier(base,'Base Token')
    if not table:
        tables=client.pages(f'/bitable/v1/apps/{base}/tables')
        if len(tables)!=1: return {'base':base,'tables':tables,'table':''}
        table=tables[0]['table_id']
    return {'base':base,'table':identifier(table,'Table ID'),'url':value if parsed.scheme else document_url(client,base,table)}


def browse(kind,space='',parent=''):
    with Client(True) as client:
        if kind=='spaces': return client.pages('/wiki/v2/spaces',params={'page_size':50})
        if kind=='nodes': return client.pages(f'/wiki/v2/spaces/{identifier(space)}/nodes',params={'page_size':50,**({'parent_node_token':identifier(parent)} if parent else {})})
        if kind=='folders': return client.pages('/drive/v1/files',key='files',params={'page_size':200,**({'folder_token':identifier(parent)} if parent else {})})
    raise ValueError('无效的目录类型')


def save_preset(body):
    if body.get('mode') not in ('existing','new') or not str(body.get('name','')).strip(): raise ValueError('请填写预设名称和存储方式')
    result={'id':body.get('id') or uuid.uuid4().hex,'name':str(body['name']).strip()[:100],'mode':body['mode']}
    with Client() as client:
        if result['mode']=='existing':
            target=resolve_target(client,body.get('base',''),body.get('table',''))
            if not target.get('table'): raise ValueError('这个多维表格包含多张数据表，请选择其中一张')
            result.update(target)
        else:
            result.update({k:str(body.get(k,'')) for k in ('space','parent','folder')})
            if '://' in result['parent']:
                match=re.search(r'/wiki/([a-zA-Z0-9]+)',result['parent'])
                if not match: raise ValueError('请输入父页面的知识库链接')
                node=client.request('GET','/wiki/v2/spaces/get_node',params={'token':match[1]})['node']
                if result['space'] and result['space']!=node['space_id']: raise ValueError('父页面不在所选知识库中')
                result.update(space=node['space_id'],parent=node['node_token'])
            for key in ('space','parent','folder'):
                if result[key]: identifier(result[key])
            if result['parent'] and not result['space']: raise ValueError('请先选择知识库')
    return store.object_put('feishu-presets',result['id'],result)


def schema(client,base,table,repair):
    current={f['field_name']:f for f in client.pages(f'/bitable/v1/apps/{base}/tables/{table}/fields')}
    missing=[]
    for name,typ in FIELDS:
        if name in current and current[name]['type']!=typ: raise ValueError(f'字段「{name}」类型不匹配，请选择 Lulu 创建的作品库')
        if name not in current: missing.append((name,typ))
    if missing and not repair: raise ValueError('目标表格缺少字段：'+ '、'.join(n for n,_ in missing)+'。可勾选「补齐字段」或新建作品库')
    for name,typ in missing:
        client.request('POST',f'/bitable/v1/apps/{base}/tables/{table}/fields',json={'field_name':name,'type':typ})
    return True


def upload(client,path,base,progress=lambda n:None):
    path=Path(path); size=path.stat().st_size
    args={'file_name':path.name,'parent_type':'bitable_file','parent_node':base,'size':size}
    if size<=20*1024*1024:
        with path.open('rb') as file:
            data=client.request('POST','/drive/v1/medias/upload_all',data={k:str(v) for k,v in args.items()},files={'file':(path.name,file,'application/octet-stream')})
        progress(1); return data['file_token']
    data=client.request('POST','/drive/v1/medias/upload_prepare',json=args)
    block_size=data['block_size']; block_num=data['block_num']; upload_id=data['upload_id']
    if block_size<=0 or block_num<=0: raise ValueError('飞书没有返回有效的分片策略')
    with path.open('rb') as file:
        for seq in range(block_num):
            chunk=file.read(block_size)
            if not chunk: raise ValueError('文件长度与飞书分片策略不一致')
            client.request('POST','/drive/v1/medias/upload_part',data={'upload_id':upload_id,'seq':str(seq),'size':str(len(chunk)),
                'checksum':str(zlib.adler32(chunk)&0xffffffff)},files={'file':('part',chunk,'application/octet-stream')})
            progress((seq+1)/block_num)
            time.sleep(.21)
    return client.request('POST','/drive/v1/medias/upload_finish',json={'upload_id':upload_id,'block_num':block_num})['file_token']


def fields_for(item):
    meta=item['metadata']; date=str(meta.get('date',''))
    if re.fullmatch(r'\d{8}',date):date=f'{date[:4]}-{date[4:6]}-{date[6:]}'
    fields={'Lulu ID':item['id'],'标题':item['title'],'作者':meta.get('author',''),
        '发布时间':date,'时长':item['duration'],
        '转录文稿':'\n'.join(f'[{engine.stamp(s["start"])}] {s["text"]}' for s in item['segments']) if item['segments'] else item['transcript']}
    if len(fields['转录文稿'])>90000: raise ValueError('文稿超过单元格安全长度，请拆分后导出，正文不会截断')
    for key,name in [('likes','点赞'),('collections','收藏'),('comments','评论'),('shares','分享'),('views','播放')]:
        if isinstance(meta.get(key),(int,float)): fields[name]=meta[key]
    for url,name in [(item['source_url'],'原作品链接'),(meta.get('direct_url',''),'音视频直链')]:
        if url: fields[name]={'link':url,'text':name}
    return fields


def run_export(job_id):
    with export_lock:
        job=store.object_get('feishu-jobs',job_id)
        def save(**values):
            job.update(values); job['updated_at']=time.time();store.object_put('feishu-jobs',job_id,job)
        try:
            save(status='running',phase='正在连接飞书',error='')
            with Client(job['target']['mode']=='new') as client:
                target=job['target']
                if not job.get('base'):
                    if target['mode']=='existing':
                        resolved=resolve_target(client,target.get('base',''),target.get('table',''))
                        if not resolved.get('table'): raise ValueError('请选择目标数据表')
                        save(base=resolved['base'],table=resolved['table'],url=resolved['url'])
                    elif target.get('space'):
                        node=client.request('POST',f'/wiki/v2/spaces/{identifier(target["space"])}/nodes',json={
                            'obj_type':'bitable','node_type':'origin','title':job['name'],
                            **({'parent_node_token':identifier(target['parent'])} if target.get('parent') else {})})['node']
                        save(base=node['obj_token'],node=node['node_token'],url='')
                    else:
                        app=client.request('POST','/bitable/v1/apps',json={'name':job['name'],
                            **({'folder_token':identifier(target['folder'])} if target.get('folder') else {})})['app']
                        save(base=app['app_token'],table=app.get('default_table_id',''),url=app['url'])
                base=job['base']; table=job.get('table','')
                if not table:
                    tables=client.pages(f'/bitable/v1/apps/{base}/tables')
                    if tables: table=tables[0]['table_id']
                    else: table=client.request('POST',f'/bitable/v1/apps/{base}/tables',json={'table':{'name':'作品库','fields':[{'field_name':n,'type':t} for n,t in FIELDS]}})['table_id']
                    save(table=table)
                if not job.get('url'):save(url=document_url(client,base,table))
                save(phase='正在检查作品库字段')
                schema(client,base,table,target['mode']=='new' or job.get('repair',False))
                if target['mode']=='new' and not job.get('created_preset'):
                    preset_id=uuid.uuid4().hex
                    store.object_put('feishu-presets',preset_id,{'id':preset_id,'name':job['name'],'mode':'existing','base':base,'table':table,'url':job['url']})
                    save(created_preset=preset_id)
                results=dict(job.get('results',{}))
                for index,item_id in enumerate(job['ids']):
                    if results.get(item_id,{}).get('status')=='done': continue
                    item=store.get(item_id)
                    try:
                        if not item: raise ValueError('本地素材已移除')
                        save(phase=f'正在存入 {index+1}/{len(job["ids"])}：{item["title"]}',progress=index/len(job['ids'])*100)
                        fields=fields_for(item)
                        for include,key,name in [('cover','cover_path','封面'),('media','media_path','音视频')]:
                            path=item['media_path'] if key=='media_path' else item['metadata'].get(key,'')
                            if not job.get(include) or not path: continue
                            if not Path(path).is_file(): raise ValueError(f'{name}文件不存在，请先补下素材')
                            cache_key=f'{base}:{item_id}:{key}:{Path(path).stat().st_mtime_ns}:{Path(path).stat().st_size}'
                            cached=store.object_get('feishu-uploads',cache_key)
                            if not cached:
                                ft=upload(client,path,base,lambda p:save(phase=f'上传{name} {p:.0%} · {item["title"]}'))
                                cached=store.object_put('feishu-uploads',cache_key,{'token':ft})
                            fields[name]=[{'file_token':cached['token']}]
                        route=f'/bitable/v1/apps/{base}/tables/{table}/records'
                        # Recover writes whose response was lost by searching for a stable local id.
                        existing=client.request('POST',route+'/search',json={'filter':{'conjunction':'and','conditions':[{'field_name':'Lulu ID','operator':'is','value':[item_id]}]}}).get('items',[])
                        if existing:
                            record_id=existing[0]['record_id']
                            client.request('POST',route+'/batch_update',json={'records':[{'record_id':record_id,'fields':fields}]})
                        else:
                            answer=client.request('POST',route+'/batch_create',json={'records':[{'fields':fields}]})
                            records=answer.get('records',[])
                            if len(records)!=1 or not records[0].get('record_id'): raise ValueError('飞书没有返回已写入记录，请重试检查')
                            record_id=records[0]['record_id']
                        results[item_id]={'status':'done','record_id':record_id,'title':item['title']}
                    except Exception as exc:
                        results[item_id]={'status':'error','error':str(exc)[:500],'title':item['title'] if item else item_id}
                    save(results=results)
                count=sum(r['status']=='done' for r in results.values())
                save(status='done' if count==len(job['ids']) else 'partial',phase=f'已存入 {count}/{len(job["ids"])} 条',progress=100,count=count)
        except Exception as exc:
            save(status='error',phase='导出未完成',error=str(exc)[:700])
        finally:
            active_jobs.discard(job_id)


def start_export(body):
    ids=list(dict.fromkeys(body.get('ids',[])))
    if not ids or len(ids)>1000 or any(not store.get(i) for i in ids): raise ValueError('请选择 1–1000 条有效素材')
    target=store.object_get('feishu-presets',body['preset']) if body.get('preset') else body.get('target')
    if not target or target.get('mode') not in ('new','existing'): raise ValueError('请选择已有表格或新建位置')
    credentials()
    job={'id':uuid.uuid4().hex,'ids':ids,'target':target,'name':str(body.get('name') or 'Lulu 作品库 '+time.strftime('%Y-%m-%d'))[:100],
         'status':'queued','phase':'等待导出','progress':0,'results':{},'created_at':time.time(),
         'cover':bool(body.get('cover',True)),'media':bool(body.get('media',True)),'repair':bool(body.get('repair',False))}
    store.object_put('feishu-jobs',job['id'],job); resume_export(job['id']);return job


def resume_export(job_id):
    job=store.object_get('feishu-jobs',job_id)
    if not job: raise ValueError('找不到导出任务')
    if job_id in active_jobs:return job
    active_jobs.add(job_id)
    threading.Thread(target=run_export,args=(job_id,),daemon=True).start()
    return job
