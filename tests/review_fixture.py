"""Offline UI fixture. This module is never imported by the application."""
import os
import uvicorn
from backend import review, store, secrets, integrations
from backend.main import app

assert 'review-ui-' in os.environ.get('LULU_DATA_DIR', ''), 'Use an isolated UI test directory'
store.save_settings({'jev_api_key_enc': secrets.seal('offline-fixture'), 'llm_model': 'offline-fixture'})
store.add('Review fixture', status='done', transcript='😀开场。\n这里有一句病句。\n结尾。')


def evaluate(state, questions, config=None):
    return {'model': 'offline-fixture', 'answers': {name: {
        'type': 'choice', 'choice': 'problem' if name.startswith('1_') else 'ok', 'confidence': .95,
        'probabilities': {k: int(k == ('problem' if name.startswith('1_') else 'ok')) for k in q['criteria']}
    } for name, q in questions.items()}}


review.evaluate = evaluate
integrations.llm_post = lambda *args: '这里是一句通顺的表达。'
uvicorn.run(app, host='127.0.0.1', port=int(os.environ['LULU_PORT']), log_level='warning')
