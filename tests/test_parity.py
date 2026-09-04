"""Local integration tests; Feishu transports are explicit fixtures, never called live."""
import io
import os
import tempfile
import time
import wave
import zipfile
from pathlib import Path
import httpx
import pytest

os.environ.setdefault('LULU_DATA_DIR',tempfile.mkdtemp(prefix='lulu-parity-'))
from backend import store,engine,assets,feishu
from backend.main import app
from fastapi.testclient import TestClient
client=TestClient(app,headers={'X-Lulu-Client':'desktop'})


def make_item(title='测试作品',**kwargs):return store.add(title,**kwargs)


def test_library_folder_fulltext_move_export_and_delete():
    first=make_item('普通标题',kind='document',transcript='正文独有关键词萤火虫',status='done')
    second=make_item('另一篇',kind='document',transcript='第二篇正文',status='done')
    assert first['id'] in client.get('/api/library/search',params={'q':'萤火虫'}).json()['ids']
    assert client.post('/api/folders',json={'name':'整理测试'}).status_code==200
    ids=[first['id'],second['id']]
    assert client.post('/api/items/batch',json={'ids':ids,'action':'move','folder':'整理测试'}).json()['count']==2
    assert client.patch('/api/folders',json={'old':'整理测试','name':'新名称'}).status_code==200
    assert store.get(first['id'])['folder']=='新名称'
    response=client.post('/api/library/export',json={'ids':ids,'format':'md'})
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist())==2
        assert '萤火虫' in ''.join(archive.read(n).decode('utf-8-sig') for n in archive.namelist())
    assert client.request('DELETE','/api/folders',json={'name':'新名称'}).status_code==200
    assert store.get(first['id'])['folder']=='根目录'
    response=client.post('/api/items/batch',json={'ids':ids,'action':'delete'})
    assert response.status_code==200
    assert store.get(first['id']) is None


def test_collection_capture_survives_reload_and_deduplicates():
    group={'id':'test-collection','title':'公开主页','ids':[],'status':'waiting'}
    store.object_put('collections',group['id'],group)
    entry={'url':'https://www.douyin.com/video/12345','title':'采集标题','author':'作者','likes':321,'duration':42,
           'comments':5,'collections':12,'shares':8,'direct_url':'https://example.com/test.mp4','provider':'douyin'}
    for _ in range(2):
        result=client.post('/api/collections/test-collection/capture',json={'entries':[entry],'status':'done'})
        assert result.status_code==200
    group=store.object_get('collections','test-collection')
    assert len(group['ids'])==1
    item=store.get(group['ids'][0]);assert item['metadata']['shares']==8
    assert 'direct_url' in item['metadata']


def wav(path):
    with wave.open(str(path),'wb') as out:out.setparams((1,2,16000,16000,'NONE','not compressed'));out.writeframes(b'\x00\x00'*16000)


def test_cover_only_preserves_transcript_and_does_not_download_media(monkeypatch):
    item=make_item(source_url='https://www.douyin.com/video/8',transcript='已有正文',segments=[{'start':0,'end':2,'text':'已有正文'}],
        metadata={'provider':'douyin','direct_url':'https://example.com/video.mp4','cover_url':'https://example.com/cover.jpg',
                  'action':'assets','options':{'media':False,'cover':True,'keep':True,'mode':'video'}})
    downloads=[]
    def download(url,path,*args):downloads.append(url);path.write_bytes(b'cover-bytes')
    monkeypatch.setattr(assets,'direct_download',download)
    engine.process(item['id']);result=store.get(item['id'])
    assert downloads==['https://example.com/cover.jpg']
    assert result['transcript']=='已有正文' and result['segments'][0]['end']==2
    assert Path(result['metadata']['cover_path']).read_bytes()==b'cover-bytes'
    assert result['status']=='done' and not result['media_path']


def test_subtitle_preference_skips_model_and_media(monkeypatch):
    import yt_dlp
    item=make_item(source_url='https://example.com/with-subtitle',metadata={'options':{'subtitles':True,'keep':False,'mode':'audio','cover':False}})
    calls=[]
    class YDL:
        def __init__(self,opts):self.opts=opts;calls.append(opts)
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def extract_info(self,*a,**kw):
            directory=store.DATA/'media'/item['id'];(directory/'source.zh.vtt').write_text('WEBVTT\n\n00:00.200 --> 00:02.400\n字幕全文，不应调用模型\n\n',encoding='utf-8')
            return {'title':'平台字幕作品','duration':3}
    monkeypatch.setattr(yt_dlp,'YoutubeDL',YDL);monkeypatch.setattr(engine,'validate_url',lambda u:u)
    monkeypatch.setattr(engine,'installed',lambda _:pytest.fail('字幕可用时不应加载模型'))
    engine.process(item['id']);result=store.get(item['id'])
    assert len(calls)==1 and calls[0]['skip_download']
    assert result['segments']==[{'start':.2,'end':2.4,'text':'字幕全文，不应调用模型'}]
    assert result['phase']=='已提取平台字幕' and not result['media_path']


def test_audio_video_options_retention_and_safe_cleanup(monkeypatch,tmp_path):
    item=make_item(source_url='https://www.douyin.com/video/9',metadata={'provider':'douyin','direct_url':'https://example.com/audio.wav',
        'action':'assets','options':{'media':True,'cover':False,'keep':True,'mode':'video','directory':str(tmp_path)}})
    # A real generated video exercises ffmpeg audio extraction.
    source=tmp_path/'input.mp4'
    result=engine.run_command(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=black:s=32x32:d=1','-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-c:a','aac','-shortest',str(source)],timeout=30)
    assert result.returncode==0
    monkeypatch.setattr(assets,'direct_download',lambda url,path,*a:path.write_bytes(source.read_bytes()))
    item['metadata']['options']['mode']='audio';store.update(item['id'],metadata=item['metadata'])
    engine.process(item['id']);result=store.get(item['id'])
    assert Path(result['media_path']).parent==tmp_path and Path(result['media_path']).suffix=='.m4a'
    assert .9<engine.media_info(result['media_path'])<1.2
    # Never delete user-owned files even if metadata says temporary.
    store.update(item['id'],transcript='已完成',metadata=result['metadata']|{'temporary_media':True})
    assets.finish(item['id']);assert source.exists() and Path(result['media_path']).exists()
    local=make_item(media_path=str(source),transcript='本地导入',metadata={'temporary_media':True})
    assets.finish(local['id']);assert source.exists()


class FeishuFixture:
    records={};creates=0;uploads=0;fail_attachment_once=False;schema_fields=list(feishu.FIELDS);created_nodes=[]
    def __init__(self,*a,**k):pass
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def pages(self,path,key='items',params=None):
        if path.endswith('/fields'):return [{'field_name':n,'type':t} for n,t in self.schema_fields]
        if path.endswith('/tables'):return [{'table_id':'tblTest','name':'作品库'}]
        return []
    def request(self,method,path,**kwargs):
        body=kwargs.get('json',{})
        if path.endswith('/metas/batch_query'):
            assert body['with_url'] is True
            return {'metas':[{'doc_token':'BaseTest','url':'https://fixture.feishu.cn/base/BaseTest'}]}
        if path.endswith('/nodes'):
            self.created_nodes.append(body);return {'node':{'obj_token':'BaseTest','node_token':'WikiTest'}}
        if path.endswith('/upload_all'):
            if self.fail_attachment_once:
                type(self).fail_attachment_once=False;raise ValueError('模拟附件上传失败')
            type(self).uploads+=1;return {'file_token':'fileFixture'}
        if path.endswith('/search'):
            local_id=body['filter']['conditions'][0]['value'][0]
            return {'items':[self.records[local_id]] if local_id in self.records else []}
        if path.endswith('/batch_create'):
            type(self).creates+=1;fields=body['records'][0]['fields'];record={'record_id':'rec'+fields['Lulu ID'],'fields':fields};self.records[fields['Lulu ID']]=record;return {'records':[record]}
        if path.endswith('/batch_update'):
            for r in body['records']:self.records[r['fields']['Lulu ID']]=r
            return {'records':body['records']}
        if path.endswith('/fields'):return {'field':body}
        raise AssertionError((method,path,kwargs))


def fixture_job(ids,target=None):
    job={'id':uuid_hex(),'ids':ids,'target':target or {'mode':'existing','base':'BaseTest','table':'tblTest'},'name':'接口验收',
         'status':'queued','phase':'','progress':0,'results':{},'cover':True,'media':True,'repair':False}
    store.object_put('feishu-jobs',job['id'],job);return job


def uuid_hex():
    import uuid
    return uuid.uuid4().hex


def test_feishu_partial_retry_metadata_attachments_and_idempotency(monkeypatch,tmp_path):
    FeishuFixture.records={};FeishuFixture.creates=0;FeishuFixture.uploads=0;FeishuFixture.fail_attachment_once=True
    monkeypatch.setattr(feishu,'Client',FeishuFixture)
    cover=tmp_path/'cover.jpg';cover.write_bytes(b'fixture')
    first=make_item('有附件',transcript='全文',segments=[{'start':12.4,'end':15,'text':'带时间戳的全文'}],metadata={'author':'作者','likes':8,'shares':3,'cover_path':str(cover)})
    second=make_item('尚未转录',source_url='https://example.com/video')
    job=fixture_job([first['id'],second['id']]);feishu.run_export(job['id'])
    assert store.object_get('feishu-jobs',job['id'])['status']=='partial'
    assert FeishuFixture.creates==1
    feishu.run_export(job['id']);result=store.object_get('feishu-jobs',job['id'])
    assert result['status']=='done' and result['count']==2 and FeishuFixture.creates==2
    fields=FeishuFixture.records[first['id']]['fields']
    assert fields['封面']==[{'file_token':'fileFixture'}] and fields['作者']=='作者'
    assert '[00:00:12] 带时间戳的全文'==fields['转录文稿']
    assert fields['分享']==3
    # A separately requested export updates the same local id, never duplicating it.
    again=fixture_job([first['id']]);feishu.run_export(again['id'])
    assert FeishuFixture.creates==2 and FeishuFixture.uploads==1


def test_feishu_wiki_creation_parent_preset_and_schema_mismatch(monkeypatch):
    FeishuFixture.records={};FeishuFixture.created_nodes=[]
    monkeypatch.setattr(feishu,'Client',FeishuFixture)
    item=make_item('知识库文稿',transcript='测试')
    job=fixture_job([item['id']],{'mode':'new','space':'1234567','parent':'ParentNode'})
    feishu.run_export(job['id']);result=store.object_get('feishu-jobs',job['id'])
    assert result['status']=='done' and result['base']=='BaseTest'
    assert result['url']=='https://fixture.feishu.cn/base/BaseTest?table=tblTest'
    assert FeishuFixture.created_nodes[0]['parent_node_token']=='ParentNode'
    assert store.object_get('feishu-presets',result['created_preset'])['table']=='tblTest'
    bad=FeishuFixture();bad.schema_fields=[('标题',2)]
    with pytest.raises(ValueError,match='类型不匹配'):feishu.schema(bad,'BaseTest','tblTest',True)


def test_large_feishu_attachment_splits_with_checksums(tmp_path,monkeypatch):
    import zlib
    source=tmp_path/'large.mp4';source.write_bytes(b'v'*(20*1024*1024+1));parts=[]
    class Transport:
        def request(self,method,path,**kwargs):
            if path.endswith('upload_prepare'):return {'upload_id':'u1','block_size':4*1024*1024,'block_num':6}
            if path.endswith('upload_part'):
                data=kwargs['data'];chunk=kwargs['files']['file'][1];assert int(data['checksum'])==zlib.adler32(chunk)&0xffffffff
                assert int(data['seq'])==len(parts);parts.append(chunk);return {}
            if path.endswith('upload_finish'):assert kwargs['json']['block_num']==6;return {'file_token':'largeToken'}
            raise AssertionError(path)
    monkeypatch.setattr(feishu.time,'sleep',lambda n:None)
    assert feishu.upload(Transport(),source,'BaseTest')=='largeToken'
    assert b''.join(parts)==source.read_bytes()


def test_oauth_state_expiry_and_secret_not_exposed():
    feishu.auth_pending['expected']={'state':'expected','expires':time.time()+60}
    with pytest.raises(ValueError,match='校验失败'):feishu.complete_auth('other','code')
    assert 'expected' in feishu.auth_pending
    feishu.auth_pending['expired']={'state':'expired','expires':time.time()-1}
    with pytest.raises(ValueError,match='过期'):feishu.complete_auth('expired','code')
    feishu.auth_pending.clear()
    assert client.patch('/api/settings',json={'feishu_app_id':'cli_test','feishu_secret':'test-only-secret'}).status_code==200
    public=client.get('/api/state').text
    assert 'test-only-secret' not in public and 'feishu_secret_enc' not in public


def test_subtitle_download_failure_falls_back_to_media(monkeypatch):
    import yt_dlp
    item=make_item(source_url='https://example.com/no-subtitle',metadata={'options':{'subtitles':True,'keep':True,'mode':'audio','cover':False}})
    calls=[]
    class YDL:
        def __init__(self,opts):self.opts=opts;calls.append(opts)
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def extract_info(self,*a,**kw):
            if self.opts.get('skip_download'):raise yt_dlp.utils.DownloadError('Fixture subtitle unavailable')
            wav(store.DATA/'media'/item['id']/'source.wav')
            return {'title':'仍可获取音频','duration':1}
        def prepare_filename(self,info):return str(store.DATA/'media'/item['id']/'source.wav')
    monkeypatch.setattr(yt_dlp,'YoutubeDL',YDL);monkeypatch.setattr(engine,'validate_url',lambda u:u)
    result,subtitles=assets.acquire(item)
    assert not subtitles and len(calls)==2
    assert Path(result['media_path']).is_file()
    assert result['metadata']['subtitle_warning']


def test_oauth_exchange_refresh_and_paginated_http_transport(monkeypatch):
    original=httpx.Client; requests=[]
    def handle(request):
        requests.append(request)
        if request.url.path.endswith('/oauth/token'):
            import json
            data=json.loads(request.content)
            assert data['client_id']=='cli_test' and data['client_secret']=='fixture-secret'
            if data['grant_type']=='authorization_code':
                assert data['code_verifier']=='v'*48 and data['redirect_uri']==feishu.CALLBACKS[0]
                return httpx.Response(200,json={'code':0,'access_token':'user-fixture-1','refresh_token':'refresh-fixture-1','expires_in':7200})
            assert data['grant_type']=='refresh_token' and data['refresh_token']=='refresh-fixture-1'
            return httpx.Response(200,json={'code':0,'access_token':'user-fixture-2','refresh_token':'refresh-fixture-2','expires_in':7200})
        if request.url.path.endswith('/user_info'):
            assert request.headers['authorization']=='Bearer user-fixture-1'
            return httpx.Response(200,json={'code':0,'data':{'name':'接口测试用户'}})
        if request.url.path.endswith('/spaces'):
            assert request.headers['authorization']=='Bearer user-fixture-2'
            if 'page_token' in request.url.params:return httpx.Response(200,json={'code':0,'data':{'items':[{'space_id':'2','name':'第二页'}],'has_more':False}})
            return httpx.Response(200,json={'code':0,'data':{'items':[{'space_id':'1','name':'第一页'}],'has_more':True,'page_token':'next'}})
        raise AssertionError(str(request.url))
    monkeypatch.setattr(feishu,'credentials',lambda:('cli_test','fixture-secret'))
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    feishu.auth_pending['valid']={'state':'valid','expires':time.time()+60,'app_id':'cli_test','verifier':'v'*48,'redirect':feishu.CALLBACKS[0]}
    feishu.complete_auth('valid','fixture-code')
    assert feishu.auth_status()['connected'] and feishu.auth_status()['name']=='接口测试用户'
    assert 'valid' not in feishu.auth_pending
    store.save_settings({'feishu_expires_at':time.time()-1})
    assert feishu.token(True)=='user-fixture-2'
    assert [s['space_id'] for s in feishu.browse('spaces')]==['1','2']
    assert len(requests)==5
    feishu.logout()


def test_batch_obsidian_preserves_timestamps(tmp_path):
    store.save_settings({'obsidian_path':str(tmp_path)})
    item=make_item('知识库 / 导出',transcript='内容',segments=[{'start':1,'end':2,'text':'内容'}])
    result=client.post('/api/obsidian/export',json={'ids':[item['id']]})
    assert result.status_code==200
    files=list((tmp_path/'Lulu').glob('*.md'))
    assert len(files)==1 and '[00:00:01] 内容' in files[0].read_text(encoding='utf-8')
