import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('LULU_DATA_DIR', tempfile.mkdtemp(prefix='lulu-asr-tests-'))
import pytest
from backend import asr_windows as asr, engine, store


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path / 'library.sqlite3')
    for directory in ('models', 'documents', 'media', 'outputs'):
        (tmp_path / directory).mkdir()
    store.init()


def fixture_weight(monkeypatch, tmp_path, name='sensevoice-q8'):
    file = tmp_path / 'external' / asr.NATIVE_MODELS[name]['file']
    file.parent.mkdir(exist_ok=True)
    file.write_bytes(b'GGUF' + b'local model fixture' * 8)
    spec = dict(asr.NATIVE_MODELS[name], bytes=file.stat().st_size,
                sha256=hashlib.sha256(file.read_bytes()).hexdigest())
    monkeypatch.setitem(asr.NATIVE_MODELS, name, spec)
    return file


def test_import_checks_integrity_reuses_original_and_removal_preserves_weights(monkeypatch, tmp_path):
    file = fixture_weight(monkeypatch, tmp_path)
    asr.bind_model('sensevoice-q8', str(file))
    assert asr.external_path('sensevoice-q8') == file
    assert not asr.model_status('sensevoice-q8')['installed']  # Missing runtime, not falsely ready.
    asr.model_unbind_route('sensevoice-q8')
    assert file.read_bytes().startswith(b'GGUF')
    assert asr.external_path('sensevoice-q8') is None


def test_bad_import_preserves_valid_binding_and_changed_file_loses_readiness(monkeypatch, tmp_path):
    file = fixture_weight(monkeypatch, tmp_path)
    asr.bind_model('sensevoice-q8', str(file))
    bad = tmp_path / 'bad.gguf'
    bad.write_bytes(b'GGUF' + b'x' * (file.stat().st_size - 4))
    with pytest.raises(ValueError, match='SHA-256'):
        asr.bind_model('sensevoice-q8', str(bad))
    assert asr.external_path('sensevoice-q8') == file
    file.write_bytes(b'changed')
    assert asr.external_path('sensevoice-q8') is None


def test_readonly_discovery_does_not_bind_or_download(monkeypatch, tmp_path):
    file = fixture_weight(monkeypatch, tmp_path)
    target = tmp_path / 'models' / file.name
    target.write_bytes(file.read_bytes())
    candidates = asr.discover_models()
    assert any(row['path'] == str(target) and row['verified'] is False for row in candidates)
    assert not asr.binding_file('sensevoice-q8').exists()
    assert not asr.runtime_root().exists()


def test_mac_and_wrong_format_models_are_rejected(tmp_path):
    path = tmp_path / 'model.safetensors'
    path.write_bytes(b'not GGUF')
    with pytest.raises(ValueError, match='完整'):
        asr.bind_model('whisper-turbo-q4', str(path))


def moss_row(**changes):
    row = {'file': 'audio.wav', 'text': '第一句话。第二句话。',
           'raw_text': '[0.00][S1]第一句话。[1.00][1.20][S2]第二句话。[2.00]',
           'segments': [{'t0_ms': 0, 't1_ms': 1000, 'speaker_id': 1, 'text': '第一句话。'},
                        {'t0_ms': 1200, 't1_ms': 2000, 'speaker_id': 2, 'text': '第二句话。'}]}
    return row | changes


def test_moss_keeps_real_speaker_ids_and_timestamps():
    text, segments = asr.parse_rows(json.dumps(moss_row()), [Path('audio.wav')], 'moss-q5', 3)
    assert text == '第一句话。第二句话。'
    assert [segment['speaker_id'] for segment in segments] == [1, 2]
    assert segments[1]['start'] == 1.2


@pytest.mark.parametrize('changes', [
    {'raw_text': '[0.00][S1]第一句话。[1.00][1.20][S2]第二句话。'},
    {'raw_text': '[0.00][S1]第一句话。[1.00][1.20][S3]第二句话。[2.00]'},
    {'text': '被截断的文本'},
    {'segments': [{'t0_ms': 0, 't1_ms': 50000, 'speaker_id': 1, 'text': '第一句话。第二句话。'}]},
])
def test_moss_incomplete_turns_are_not_marked_complete(changes):
    with pytest.raises(asr.IncompleteResult):
        asr.parse_rows(json.dumps(moss_row(**changes)), [Path('audio.wav')], 'moss-q5', 3)


def test_sensevoice_never_fabricates_a_subtitle_timeline():
    rows = '\n'.join(json.dumps({'file': str(i) + '.wav', 'text': '<|zh|>' + text})
                     for i, text in enumerate(['第一句', '第二句']))
    text, segments = asr.parse_rows(rows, [Path('0.wav'), Path('1.wav')], 'sensevoice-q8', 40)
    assert text == '第一句\n第二句' and segments == []


def test_wrong_file_or_missing_audio_chunk_rejected():
    with pytest.raises(ValueError, match='全部音频块'):
        asr.parse_rows('', [Path('audio.wav')], 'sensevoice-q8', 3)
    with pytest.raises(ValueError, match='源文件'):
        asr.parse_rows(json.dumps(moss_row(file='other.wav')), [Path('audio.wav')], 'moss-q5', 3)


def test_native_pipeline_preserves_speaker_metadata_simplifies_and_finishes(monkeypatch):
    from backend import assets
    item = store.add('native', media_path='fixture.wav', metadata={'model': 'moss-q5', 'keep': 'original'})
    monkeypatch.setattr(engine, 'installed', lambda _: True)
    monkeypatch.setattr(asr, 'transcribe', lambda *args: {'text': '說話內容',
        'segments': [{'start': 0, 'end': 1, 'text': '說話內容', 'speaker_id': 2}], 'duration': 1,
        'asr': {'timestamp_kind': 'segment', 'diarization': True, 'timing_estimated': False}})
    finished = []
    monkeypatch.setattr(assets, 'finish', finished.append)
    engine.process(item['id'])
    result = store.get(item['id'])
    assert result['status'] == 'done' and result['transcript'] == '说话内容'
    assert result['segments'][0]['speaker_id'] == 2
    assert result['metadata']['keep'] == 'original' and finished == [item['id']]
    assert '发言人 2' in engine.export_content(result, 'md')


def test_incomplete_native_result_retains_recovery_text_without_overwriting_document(monkeypatch):
    item = store.add('native', media_path='fixture.wav', metadata={'model': 'moss-q5'}, transcript='原稿')
    monkeypatch.setattr(engine, 'installed', lambda _: True)
    def incomplete(*args):
        raise asr.IncompleteResult('未完成的說話內容')
    monkeypatch.setattr(asr, 'transcribe', incomplete)
    with pytest.raises(asr.IncompleteResult):
        engine.process(item['id'])
    result = store.get(item['id'])
    assert result['status'] != 'done' and result['transcript'] == '原稿'
    assert result['metadata']['asr_partial_text'] == '未完成的说话内容'


def test_cancel_stops_owned_subprocess():
    started = time.monotonic()
    def cancel():
        if time.monotonic() - started > .15:
            raise InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        asr.run_process([sys.executable, '-c', 'import time; time.sleep(30)'], cancel)
    assert time.monotonic() - started < 5


def test_shutdown_reaps_real_native_process_tree_within_budget(tmp_path):
    """Exercise the real Windows launcher/grandchild chain, without model weights."""
    child_pid_file = tmp_path / 'native-child.pid'
    child_code = 'import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)'
    parent_code = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]);time.sleep(30)'
    errors = []
    def run():
        try:
            asr.run_process([sys.executable, '-c', parent_code, child_code, str(child_pid_file)])
        except Exception as exc:
            errors.append(exc)
    item = store.add('shutdown fixture')
    engine.pending.add(item['id'])
    worker = threading.Thread(target=run)
    worker.start()
    child_handle = None
    try:
        deadline = time.monotonic() + 8
        while not child_pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(.02)
        assert child_pid_file.is_file(), 'synthetic native child did not start'
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            child_handle = kernel.OpenProcess(0x00100000, False, int(child_pid_file.read_text()))
            assert child_handle
            assert kernel.WaitForSingleObject(child_handle, 0) == 258  # Running before shutdown.
        started = time.monotonic()
        result = engine.shutdown(timeout=8)
        assert time.monotonic() - started < 8
        assert result['remaining_processes'] == 0
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
        assert item['id'] in engine.cancelled
        if child_handle:
            assert kernel.WaitForSingleObject(child_handle, 0) == 0  # Actual grandchild exited.
        with pytest.raises(InterruptedError, match='应用正在退出'):
            asr.run_process([sys.executable, '-c', 'raise AssertionError("must not start")'])
        with pytest.raises(ValueError, match='应用正在退出'):
            engine.enqueue(item['id'])
    finally:
        asr.shutdown(timeout=2)
        worker.join(timeout=2)
        if child_handle:
            kernel.CloseHandle(child_handle)
        engine.pending.discard(item['id'])
        engine.cancelled.discard(item['id'])
        engine._stopping.clear()
        asr.reset_shutdown()


def test_worker_uses_cpu_and_copies_real_api_output_without_downloading(monkeypatch, tmp_path, capsys):
    # Real PCM decoding; fake model only. This verifies our API call contract, not ASR accuracy.
    from backend import asr_worker
    audio = tmp_path / 'audio.wav'
    with wave.open(str(audio), 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes(b'\0\0' * 1600)
    batch = tmp_path / 'input.txt'; batch.write_text(str(audio) + '\n', encoding='utf-8')
    observed = {}
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def run(self, pcm, **kwargs):
            observed['pcm'] = len(pcm); observed.update(kwargs)
            return SimpleNamespace(text='hello', raw_text='hello', language='en', timestamp_kind='segment',
                segments=[SimpleNamespace(text='hello', t0_ms=0, t1_ms=100, speaker_id=0)])
    class Model(Session):
        def __init__(self, path, backend): observed['backend'] = backend
        def session(self, **kwargs): observed.update(kwargs); return Session()
    monkeypatch.setitem(sys.modules, 'transcribe_cpp', SimpleNamespace(Model=Model))
    monkeypatch.setattr(sys, 'argv', ['worker', '--library', str(tmp_path / 'transcribe.dll'), '--model', 'model.gguf', '--batch', str(batch), '--timestamps'])
    monkeypatch.setenv('TRANSCRIBE_LIBRARY', '')
    asr_worker.main()
    assert observed['backend'] == 'cpu' and observed['n_threads'] == 4
    assert observed['pcm'] == 1600 and observed['timestamps'] == 'segment' and observed['language'] is None
    row = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert row['text'] == 'hello' and row['segments'][0]['t1_ms'] == 100
