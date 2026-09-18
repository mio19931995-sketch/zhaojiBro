"""Provider routing and key isolation; temporary SQLite plus HTTP mocks only."""
import asyncio
import json
import os
import tempfile

os.environ['LULU_DATA_DIR'] = tempfile.mkdtemp(prefix='lulu-llm-provider-tests-')

import httpx
import pytest
from fastapi.testclient import TestClient
from backend import integrations, secrets, store
from backend.main import app

client = TestClient(app, headers={'X-Lulu-Client': 'desktop'})
RealHTTPClient = httpx.Client
RealAsyncHTTPClient = httpx.AsyncClient
AGNES = 'https://apihub.agnes-ai.com/v1'
DEEPSEEK = 'https://api.deepseek.com'


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch, tmp_path):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'library.sqlite3')
    store.init()


def mock_http(monkeypatch, handler):
    calls = []
    configurations = []
    def transport(request):
        calls.append(request)
        return handler(request)
    def factory(**kwargs):
        configurations.append(kwargs)
        return RealHTTPClient(transport=httpx.MockTransport(transport), **kwargs)
    def async_factory(**kwargs):
        configurations.append(kwargs)
        return RealAsyncHTTPClient(transport=httpx.MockTransport(transport), **kwargs)
    monkeypatch.setattr(integrations.httpx, 'Client', factory)
    monkeypatch.setattr(integrations.httpx, 'AsyncClient', async_factory)
    return calls, configurations


def answer(content='OK'):
    return httpx.Response(200, json={'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})


def legacy_config():
    store.save_settings({'llm_url': DEEPSEEK, 'llm_model': 'deepseek-flash',
                         'llm_api_key_enc': secrets.seal('deepseek-test-key')})


@pytest.mark.parametrize('url,model,base,expected_model', [
    ('https://platform.agnes-ai.com/settings/apiKeys', '', AGNES, 'agnes-3.0-flash'),
    ('https://platform.agnes-ai.com/settings/apiKeys/', 'Agens', AGNES, 'agnes-3.0-flash'),
    ('https://apihub.agnes-ai.com', 'AGNES', AGNES, 'agnes-3.0-flash'),
    ('https://apihub.agnes-ai.cn/', '', 'https://apihub.agnes-ai.cn/v1', 'agnes-3.0-flash'),
    ('https://api.agnes-ai.cn', 'agnes-2.5-flash', 'https://api.agnes-ai.cn/v1', 'agnes-2.5-flash'),
    (AGNES + '/chat/completions', 'agnes-custom', AGNES + '/chat/completions', 'agnes-custom'),
    ('https://APIHUB.AGNES-AI.COM:443/v1/', 'custom-model', AGNES, 'custom-model'),
    ('https://apihub.agnes-ai.com:9443', 'agnes', 'https://apihub.agnes-ai.com:9443/v1', 'agnes-3.0-flash'),
    ('http://127.0.0.1:11434/v1', 'agnes', 'http://127.0.0.1:11434/v1', 'agnes'),
    ('https://other.test/v1', '', 'https://other.test/v1', ''),
    ('https://platform.deepseek.com/api_keys', 'deepseek', DEEPSEEK, 'deepseek-flash'),
])
def test_normalization(url, model, base, expected_model):
    assert integrations.normalize_llm_config(url, model) == (base, expected_model)
    assert integrations.llm_chat_endpoint(base).count('/chat/completions') == 1


def test_canonical_origins_preserve_scheme_host_and_port_boundaries():
    assert integrations.llm_origin('https://APIHUB.AGNES-AI.COM:443/v1') == 'https://apihub.agnes-ai.com'
    assert integrations.llm_origin('http://apihub.agnes-ai.com/v1') != integrations.llm_origin(AGNES)
    assert integrations.llm_origin('https://apihub.agnes-ai.com:444/v1') != integrations.llm_origin(AGNES)
    assert integrations.llm_origin('https://apihub.agnes-ai.cn/v1') != integrations.llm_origin(AGNES)


@pytest.mark.parametrize('url', ['https://other.test:bad', 'https://other.test:0',
                              'https://user:password@other.test/v1', 'https://other.test/v1?key=secret'])
def test_bad_urls_are_rejected_without_echoing_input(url):
    with pytest.raises(ValueError) as error:
        integrations.normalize_llm_config(url, '')
    assert 'secret' not in str(error.value) and 'password' not in str(error.value)


def test_switching_providers_retains_each_encrypted_key_and_hides_internal_settings():
    legacy_config()
    result = client.patch('/api/settings', json={'llm_url': AGNES, 'llm_model': 'agnes'})
    assert result.status_code == 200
    assert result.json()['has_llm_key'] is False and result.json()['has_deepseek_key'] is True
    assert result.json()['has_agnes_key'] is False
    assert store.settings()['llm_api_key_enc'] == ''
    assert secrets.unseal(store.settings()['llm_keys_enc'][DEEPSEEK]) == 'deepseek-test-key'
    result = client.patch('/api/settings', json={'llm_api_key': 'agnes-test-key'})
    assert result.json()['has_llm_key'] is True and result.json()['has_agnes_key'] is True
    assert 'agnes-test-key' not in json.dumps(store.settings())
    assert not any(key.endswith('_enc') for key in result.json())
    result = client.patch('/api/settings', json={'llm_url': DEEPSEEK, 'llm_model': 'deepseek-flash', 'llm_api_key': ''})
    assert result.json()['has_llm_key'] is True and result.json()['has_agnes_key'] is True
    assert secrets.unseal(store.settings()['llm_api_key_enc']) == 'deepseek-test-key'
    assert secrets.unseal(store.settings()['llm_keys_enc']['https://apihub.agnes-ai.com']) == 'agnes-test-key'
    assert client.patch('/api/settings', json={'llm_keys_enc': {'https://evil.test': 'forged'},
                                              'llm_api_key_enc': 'forged'}).status_code == 200
    assert 'https://evil.test' not in store.settings()['llm_keys_enc']
    assert secrets.unseal(store.settings()['llm_api_key_enc']) == 'deepseek-test-key'


def test_preset_key_flags_do_not_claim_cn_keys_work_on_com():
    result = client.patch('/api/settings', json={'llm_url': 'https://apihub.agnes-ai.cn',
        'llm_model': 'agnes', 'llm_api_key': 'cn-only-key'})
    assert result.json()['has_llm_key'] is True
    assert result.json()['has_agnes_key'] is False
    result = client.patch('/api/settings', json={'llm_url': AGNES})
    assert result.json()['has_llm_key'] is False


def test_connection_uses_unsaved_key_without_changing_settings_or_creating_content(monkeypatch):
    legacy_config()
    before = store.settings()
    calls, options = mock_http(monkeypatch, lambda request: answer())
    result = client.post('/api/llm/test', json={'llm_url': 'https://platform.agnes-ai.com/settings/apiKeys',
        'llm_model': 'Agnes', 'llm_api_key': 'temporary-agnes-key'})
    assert result.status_code == 200, result.text
    assert result.json() == {'ok': True, 'model': 'agnes-3.0-flash', 'base_url': AGNES,
                              'message': '连接成功，模型已返回有效响应'}
    assert len(calls) == 1 and str(calls[0].url) == AGNES + '/chat/completions'
    assert calls[0].headers['Authorization'] == 'Bearer temporary-agnes-key'
    payload = json.loads(calls[0].content)
    assert payload['messages'] == [{'role': 'user', 'content': '只回复 OK'}]
    assert payload['stream'] is False and payload['max_tokens'] == 1024
    assert options[0]['timeout'] <= 60 and options[0]['follow_redirects'] is False
    assert store.settings() == before and store.items() == []


def test_agnes_without_its_own_key_is_rejected_locally(monkeypatch):
    legacy_config()
    calls, _ = mock_http(monkeypatch, lambda request: answer())
    result = client.post('/api/llm/test', json={'llm_url': AGNES, 'llm_model': ''})
    assert result.status_code == 400 and '当前 Agnes' in result.json()['detail']
    assert calls == []


@pytest.mark.parametrize('key', ['sensitive-long-key-' * 600, {'value': 'sensitive-wrong-type-key'}])
def test_invalid_test_parameters_never_echo_keys(key):
    result = client.post('/api/llm/test', json={'llm_url': AGNES, 'llm_model': 'agnes', 'llm_api_key': key})
    assert result.status_code == 422
    assert result.json() == {'detail': '模型测试参数格式不正确，请检查服务地址、模型名称和 API Key'}
    assert 'sensitive-' not in result.text and 'input' not in result.text


@pytest.mark.parametrize('url', ['https://apihub.agnes-ai.cn/v1', 'http://apihub.agnes-ai.com/v1',
                              'https://apihub.agnes-ai.com:444/v1', 'https://api.agnes-ai.cn/v1'])
def test_saved_agnes_key_is_never_reused_on_a_different_origin(monkeypatch, url):
    client.patch('/api/settings', json={'llm_url': AGNES, 'llm_model': 'agnes', 'llm_api_key': 'com-only-key'})
    calls, _ = mock_http(monkeypatch, lambda request: answer())
    result = client.post('/api/llm/test', json={'llm_url': url, 'llm_model': 'agnes'})
    assert result.status_code == 400 and calls == []


def test_unknown_host_never_receives_current_service_key(monkeypatch):
    legacy_config()
    calls, _ = mock_http(monkeypatch, lambda request: answer())
    result = client.post('/api/llm/test', json={'llm_url': 'https://other.test/v1', 'llm_model': 'custom-model'})
    assert result.status_code == 200
    assert len(calls) == 1 and 'authorization' not in calls[0].headers


@pytest.mark.parametrize('status,word', [(401, '认证'), (403, '权限'), (402, '额度'),
                                     (404, '模型名称'), (405, '接口地址'), (429, '频繁'), (503, '暂时不可用')])
def test_http_errors_are_localized_without_exposing_upstream_details(monkeypatch, status, word):
    mock_http(monkeypatch, lambda request: httpx.Response(status, json={'error': {'message': 'private-key-upstream-details'}}))
    result = client.post('/api/llm/test', json={'llm_url': AGNES, 'llm_model': 'agnes', 'llm_api_key': 'private-key'})
    assert result.status_code == 400 and word in result.json()['detail']
    assert 'private-key' not in result.text and 'upstream-details' not in result.text


@pytest.mark.parametrize('exception,word', [(httpx.ReadTimeout, '超时'), (httpx.ConnectError, '网络连接')])
def test_network_errors_are_safe_and_localized(monkeypatch, exception, word):
    def fail(request):
        raise exception('private-key-upstream-details', request=request)
    mock_http(monkeypatch, fail)
    result = client.post('/api/llm/test', json={'llm_url': AGNES, 'llm_model': 'agnes', 'llm_api_key': 'private-key'})
    assert result.status_code == 400 and word in result.json()['detail']
    assert 'private-key' not in result.text


@pytest.mark.parametrize('payload', [{}, [], {'choices': []}, {'error': {'message': 'sensitive-error'}},
                                 {'choices': [{'message': {'content': ''}}]},
                                 {'choices': [{'message': {'content': '  '}}]},
                                 {'choices': [{'message': {'content': None}}]}])
def test_fake_success_payloads_do_not_pass_connection_test(monkeypatch, payload):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    result = client.post('/api/llm/test', json={'llm_url': AGNES, 'llm_model': 'agnes', 'llm_api_key': 'test-key'})
    assert result.status_code == 400 and 'sensitive-error' not in result.text


def test_html_and_redirect_are_not_accepted_or_followed(monkeypatch):
    calls, _ = mock_http(monkeypatch, lambda request: httpx.Response(200, text='<html>private-key</html>'))
    body = {'llm_url': AGNES, 'llm_model': 'agnes', 'llm_api_key': 'private-key'}
    response = client.post('/api/llm/test', json=body)
    assert response.status_code == 400 and 'JSON' in response.json()['detail']
    assert 'private-key' not in response.text
    calls, _ = mock_http(monkeypatch, lambda request: httpx.Response(307, headers={'Location': 'https://elsewhere.test/'}))
    response = client.post('/api/llm/test', json=body)
    assert response.status_code == 400 and '重定向' in response.json()['detail']
    assert len(calls) == 1


def test_length_limited_response_is_not_a_successful_connection(monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={'choices': [
        {'message': {'content': 'partial'}, 'finish_reason': 'length'}]}))
    result = client.post('/api/llm/test', json={'llm_url': AGNES, 'llm_model': 'agnes', 'llm_api_key': 'test-key'})
    assert result.status_code == 400
    assert '模型已响应' in result.json()['detail'] and '长度限制' in result.json()['detail']
    assert '原文已保留' not in result.text


def test_connection_has_a_total_deadline_and_cancels_request(monkeypatch):
    cancelled = []
    async def delayed(request):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return answer()
    mock_http(monkeypatch, delayed)
    monkeypatch.setattr(integrations, 'LLM_TEST_TIMEOUT', .01)
    result = client.post('/api/llm/test', json={'llm_url': AGNES, 'llm_model': 'agnes', 'llm_api_key': 'test-key'})
    assert result.status_code == 400 and '超时' in result.json()['detail']
    assert cancelled == [True]


def test_saved_key_is_reused_only_for_same_origin_and_process_text_remains_compatible(monkeypatch):
    client.patch('/api/settings', json={'llm_url': AGNES, 'llm_model': 'agnes-2.5-flash', 'llm_api_key': 'saved-agnes-key'})
    calls, _ = mock_http(monkeypatch, lambda request: answer('這是處理後的文稿'))
    result = client.post('/api/llm/test', json={'llm_url': 'https://APIHUB.AGNES-AI.COM:443/v1/chat/completions',
                                            'llm_model': 'agnes-2.5-flash'})
    assert result.status_code == 200
    result = client.post('/api/process-text', json={'text': '原始文稿', 'action': 'rewrite'})
    assert result.status_code == 200 and result.json()['transcript'] == '这是处理后的文稿'
    assert all(request.headers['Authorization'] == 'Bearer saved-agnes-key' for request in calls)
    assert all(json.loads(request.content)['model'] == 'agnes-2.5-flash' for request in calls)
    assert len(store.items()) == 1


def test_legacy_process_text_and_local_without_key_still_work(monkeypatch):
    legacy_config()
    calls, _ = mock_http(monkeypatch, lambda request: answer('正文'))
    assert integrations.process_text('原文', 'correct') == '正文'
    assert calls[0].headers['Authorization'] == 'Bearer deepseek-test-key'
    client.patch('/api/settings', json={'llm_url': 'http://127.0.0.1:11434/v1', 'llm_model': 'local-model'})
    assert integrations.process_text('原文', 'correct') == '正文'
    assert 'authorization' not in calls[-1].headers
