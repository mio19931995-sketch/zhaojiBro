"""Isolated native-ASR worker; deliberately does not import the application store."""
import argparse
import json
import os
import sys
import wave
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--library', required=True)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--model')
    parser.add_argument('--batch')
    parser.add_argument('--language', default='auto')
    parser.add_argument('--timestamps', action='store_true')
    parser.add_argument('--diarize', action='store_true')
    args = parser.parse_args()
    os.environ['TRANSCRIBE_LIBRARY'] = str(Path(args.library).resolve())
    import transcribe_cpp as native
    if args.probe:
        print(json.dumps({'version': native.native_version(), 'commit': native.native_commit(),
                          'cpu': native.backend_available('cpu')}, ensure_ascii=False), flush=True)
        return
    import numpy as np
    with native.Model(args.model, backend='cpu') as model, model.session(n_threads=4) as session:
        print(json.dumps({'type': 'batch_header'}), flush=True)
        for file in Path(args.batch).read_text(encoding='utf-8').splitlines():
            with wave.open(file, 'rb') as audio:
                if audio.getnchannels() != 1 or audio.getframerate() != 16000 or audio.getsampwidth() != 2:
                    raise ValueError('Expected 16 kHz mono PCM16 audio')
                pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').astype(np.float32) / 32768
            result = session.run(pcm, language=None if args.language == 'auto' else args.language,
                                 timestamps='segment' if args.timestamps else 'none',
                                 diarize='on' if args.diarize else 'default')
            segments = [{'text': segment.text, 't0_ms': segment.t0_ms, 't1_ms': segment.t1_ms,
                         **({'speaker_id': segment.speaker_id} if segment.speaker_id > 0 else {})}
                        for segment in result.segments]
            print(json.dumps({'file': file, 'text': result.text, 'raw_text': result.raw_text,
                              'language': result.language, 'timestamp_kind': result.timestamp_kind,
                              'segments': segments}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
