"""Behavioral boundaries discovered while repeating the supplied video workflow."""
import os
import tempfile
from pathlib import Path
import pytest

os.environ.setdefault('LULU_DATA_DIR', tempfile.mkdtemp(prefix='lulu-acceptance-'))
from backend import store, engine, assets, feishu
from backend.main import app
from fastapi.testclient import TestClient
client = TestClient(app, headers={'X-Lulu-Client': 'desktop'})


def test_resume_while_pause_is_finishing_explains_retry():
    item = store.add('暂停中', status='paused')
    engine.pending.add(item['id']); engine.cancelled.add(item['id'])
    try:
        response = client.post('/api/items/'+item['id']+'/start')
        assert response.status_code == 400
        assert '等待' in response.json()['detail']
    finally:
        engine.pending.discard(item['id']); engine.cancelled.discard(item['id'])


@pytest.mark.parametrize('suffix,content', [
    ('.srt', '1\n00:00:01,200 --> 00:00:03,400\n第一句 &amp; 第二句\n'),
    ('.vtt', 'WEBVTT\n\n00:01.200 --> 00:03.400 align:start\n<b>第一句 &amp; 第二句</b>\n'),
    ('.json3', '{"events":[{"tStartMs":1200,"dDurationMs":2200,"segs":[{"utf8":"第一句 & 第二句"}]}]}'),
])
def test_all_subtitle_formats_preserve_time_and_text(tmp_path, suffix, content):
    source=tmp_path/('subtitle'+suffix); source.write_text(content,encoding='utf8')
    assert assets.read_subtitle(source)==[{'start':1.2,'end':3.4,'text':'第一句 & 第二句'}]


def test_corrupt_subtitle_falls_back_instead_of_breaking_download(monkeypatch):
    import yt_dlp
    from tests.test_parity import wav
    item=store.add('损坏字幕样例',source_url='https://example.com/video',metadata={'options':{'subtitles':True,'keep':True}})
    class YDL:
        def __init__(self,opts): self.opts=opts
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def extract_info(self,*args,**kwargs):
            directory=store.DATA/'media'/item['id']
            if self.opts.get('skip_download'):
                (directory/'source.zh.json3').write_text('{bad json',encoding='utf8')
            else: wav(directory/'source.wav')
            return {'title':'仍可下载音频','duration':1}
        def prepare_filename(self,info): return str(store.DATA/'media'/item['id']/'source.wav')
    monkeypatch.setattr(yt_dlp,'YoutubeDL',YDL)
    monkeypatch.setattr(engine,'validate_url',lambda u:u)
    result,subtitles=assets.acquire(item)
    assert not subtitles and Path(result['media_path']).is_file()
    assert '无法读取' in result['metadata']['subtitle_warning']


def test_only_owned_temporary_files_are_removed_after_success(tmp_path):
    item=store.add('临时音频',source_url='https://example.com/video',transcript='成功文稿',metadata={'temporary_media':True})
    owned=store.DATA/'media'/item['id']; owned.mkdir()
    for name in ['source.mp4','audio.m4a','cover.jpg']:(owned/name).write_bytes(b'fixture')
    outside=tmp_path/'original.wav'; outside.write_bytes(b'original')
    store.update(item['id'],media_path=str(owned/'audio.m4a'))
    assets.finish(item['id'])
    assert not (owned/'audio.m4a').exists() and not (owned/'source.mp4').exists()
    assert (owned/'cover.jpg').exists() and outside.exists()
    assert store.get(item['id'])['media_path']==''


def test_no_asset_selected_and_missing_directory_explain_correction():
    item=store.add('素材')
    r=client.post('/api/items/batch',json={'ids':[item['id']],'action':'assets','options':{'media':False,'cover':False}})
    assert r.status_code==400 and '至少选择' in r.json()['detail']
    r=client.post('/api/items/batch',json={'ids':[item['id']],'action':'assets','options':{'directory':str(store.DATA/'missing-folder')}})
    assert r.status_code==400 and '文件夹不存在' in r.json()['detail']


def test_transient_network_failure_retries_without_changing_verification(monkeypatch,tmp_path):
    import httpx
    item=store.add('网络重试'); calls=[]
    def transfer(url,path,item_id,label):
        calls.append(url)
        if len(calls)<3: raise httpx.ConnectError('TLS unexpected EOF')
        path.write_bytes(b'downloaded')
    monkeypatch.setattr(assets,'_direct_download_once',transfer)
    monkeypatch.setattr(assets.time,'sleep',lambda seconds:None)
    target=tmp_path/'cover.jpg'
    assets.direct_download('https://example.com/cover.jpg',target,item['id'],'下载封面')
    assert len(calls)==3 and target.read_bytes()==b'downloaded'


def test_subtitle_language_variants_are_selected_by_real_extractor():
    import yt_dlp
    with yt_dlp.YoutubeDL({'quiet':True,'writesubtitles':True,'writeautomaticsub':True,'subtitleslangs':['zh.*','ai-zh.*','en.*']}) as downloader:
        chosen=downloader.process_subtitles('sample',{'zh-Hant':[{'ext':'srt'}]}, {'ai-zh':[{'ext':'srt'}],'en-orig':[{'ext':'vtt'}]})
    assert set(chosen)=={'zh-Hant','ai-zh','en-orig'}


def test_feishu_multiple_tables_parent_link_and_invalid_schema(monkeypatch):
    class Fixture:
        def __init__(self,*args): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def pages(self,path,*args,**kwargs): return [{'table_id':'tblFirst','name':'第一张表'},{'table_id':'tblSecond','name':'第二张表'}]
        def request(self,method,path,**kwargs): return {'node':{'space_id':'SpaceB','node_token':'NodeB','obj_type':'bitable','obj_token':'BaseB'}}
    monkeypatch.setattr(feishu,'Client',Fixture)
    result=client.post('/api/feishu/resolve',json={'base':'https://fixture.feishu.cn/base/BaseB'})
    assert result.status_code==200 and len(result.json()['tables'])==2 and not result.json()['table']
    result=client.post('/api/feishu/presets',json={'name':'已有作品库','mode':'existing','base':'https://fixture.feishu.cn/base/BaseB','table':'tblSecond'})
    assert result.status_code==200 and result.json()['table']=='tblSecond'
    result=client.post('/api/feishu/presets',json={'name':'父页面','mode':'new','space':'SpaceB','parent':'https://fixture.feishu.cn/wiki/NodeB'})
    assert result.status_code==200 and result.json()['parent']=='NodeB'
    result=client.post('/api/feishu/presets',json={'name':'错误父页面','mode':'new','space':'SpaceA','parent':'https://fixture.feishu.cn/wiki/NodeB'})
    assert result.status_code==400 and '不在所选知识库' in result.json()['detail']


def test_feishu_real_loopback_callback_and_cancellation_with_fixture_tokens(monkeypatch):
    import httpx
    from urllib.parse import urlparse,parse_qs
    original=httpx.Client
    def handle(request):
        if request.url.path.endswith('/oauth/token'):
            return httpx.Response(200,json={'code':0,'access_token':'fixture-access','refresh_token':'fixture-refresh','expires_in':7200})
        if request.url.path.endswith('/user_info'):
            return httpx.Response(200,json={'code':0,'data':{'name':'仅用于本地回调测试'}})
        raise AssertionError(request.url.path)
    monkeypatch.setattr(feishu,'credentials',lambda:('cli_fixture','fixture-secret'))
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    try:
        first=client.post('/api/feishu/login'); assert first.status_code==200
        query=parse_qs(urlparse(first.json()['url']).query)
        assert query['code_challenge_method']==['S256']
        callback=query['redirect_uri'][0]
        assert callback in feishu.CALLBACKS
        # This request actually traverses the loopback HTTP callback server.
        with original(timeout=8,trust_env=False) as browser:
            invalid=browser.get(callback,params={'state':'not-the-state','code':'fixture-code'})
            assert invalid.status_code==400 and feishu.auth_pending
            success=browser.get(callback,params={'state':query['state'][0],'code':'fixture-code'})
            assert success.status_code==200 and '连接成功' in success.text
        assert client.get('/api/feishu/status').json()['connected']
        assert 'fixture-access' not in client.get('/api/state').text
        feishu.logout()
        assert not client.get('/api/feishu/status').json()['connected']
        again=client.post('/api/feishu/login'); assert again.status_code==200
        assert client.delete('/api/feishu/login').status_code==200
        assert not client.get('/api/feishu/status').json()['pending']
    finally: feishu.logout()


def test_feishu_new_cloud_folder_creates_schema_and_reuses_export_target(monkeypatch):
    from tests.test_parity import FeishuFixture,fixture_job
    class CloudFixture(FeishuFixture):
        schema_fields=[];created_fields=[];base_creates=0
        def request(self,method,path,**kwargs):
            body=kwargs.get('json',{})
            if path=='/bitable/v1/apps':
                assert body['folder_token']=='ChosenFolder'
                type(self).base_creates+=1
                return {'app':{'app_token':'BaseTest','default_table_id':'tblTest','url':'https://fixture.feishu.cn/base/BaseTest'}}
            if path.endswith('/fields'):self.created_fields.append(body);return {'field':body}
            return super().request(method,path,**kwargs)
    FeishuFixture.records={}
    monkeypatch.setattr(feishu,'Client',CloudFixture)
    item=store.add('云盘导出',transcript='真实导出正文的测试样例')
    job=fixture_job([item['id']],{'mode':'new','folder':'ChosenFolder'})
    feishu.run_export(job['id'])
    result=store.object_get('feishu-jobs',job['id'])
    assert result['status']=='done',result
    assert len(CloudFixture.created_fields)==len(feishu.FIELDS)
    assert {f['field_name']:f['type'] for f in CloudFixture.created_fields}==dict(feishu.FIELDS)
    CloudFixture.schema_fields=feishu.FIELDS
    feishu.run_export(job['id'])
    assert CloudFixture.base_creates==1
    assert store.object_get('feishu-presets',result['created_preset'])['table']=='tblTest'
