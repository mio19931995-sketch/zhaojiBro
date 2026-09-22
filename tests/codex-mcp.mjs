// Real MCP SDK client -> stdio server -> SQLite and FFmpeg, no external model.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const root = process.cwd();
await fs.mkdir(path.join(root, '.test-data'), { recursive: true });
const data = await fs.mkdtemp(path.join(root, '.test-data', 'codex-mcp-'));
const media = path.join(data, '中文 视频.mp4');
const video = spawnSync('ffmpeg', ['-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=teal:s=320x180:r=24:d=3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', media], { windowsHide: true });
assert.equal(video.status, 0);
const env = { ...process.env, LULU_DATA_DIR: data, PYTHONIOENCODING: 'utf-8', FIXTURE_VIDEO: media };
const fixture = spawnSync(path.join(root, '.venv/Scripts/python.exe'), ['-c', `
import os,json,time
from backend import store
item=store.add('中文测试视频', status='done', media_path=os.environ['FIXTURE_VIDEO'], duration=3, transcript='中文😀' * 7000, segments=[{'start':0,'end':3,'text':'字幕'}],metadata={'direct_url':'DO_NOT_EXPOSE'})
job=store.add('处理中',status='processing')
store.object_put('codex_handoff','current',{'item_id':item['id'],'selected_at':time.time()})
print(json.dumps({'id':item['id'],'pending_id':job['id']}))
`], { cwd: root, windowsHide: true, env, encoding: 'utf8' });
assert.equal(fixture.status, 0, fixture.stderr);
const ids = JSON.parse(fixture.stdout);
const transport = new StdioClientTransport({ command: process.execPath, args: [path.join(root, 'mcp/lulu.cjs')], env, stderr: 'pipe' });
const client = new Client({ name: 'lulu-verification', version: '1.0' });
try {
  await client.connect(transport);
  const tools = (await client.listTools()).tools;
  assert.equal(tools.length, 5);
  assert(tools.every(t => t.annotations.readOnlyHint));
  async function call(name, args = {}) {
    const result = await client.callTool({ name, arguments: args });
    assert(!result.isError, JSON.stringify(result));
    return result;
  }
  const listing = (await call('lulu_list_assets')).structuredContent;
  assert.equal(listing.total, 1);
  const asset = (await call('lulu_get_asset', { item_id: ids.id })).structuredContent;
  assert.equal(asset.media_path, media);
  assert(!JSON.stringify(asset).includes('DO_NOT_EXPOSE'));
  const first = (await call('lulu_read_transcript', { item_id: ids.id, text_limit: 20000 })).structuredContent;
  const tail = (await call('lulu_read_transcript', { item_id: ids.id, text_offset: first.next_text_offset })).structuredContent;
  assert.equal(first.text + tail.text, '中文😀'.repeat(7000));
  const current = (await call('lulu_current_asset')).structuredContent;
  assert.equal(current.asset.id, ids.id);
  const frame = await call('lulu_video_frame', { item_id: ids.id, seconds: 1 });
  const image = frame.content.find(c => c.type === 'image');
  assert(image && image.mimeType === 'image/jpeg');
  const bytes = Buffer.from(image.data, 'base64');
  assert.equal(bytes.readUInt16BE(0), 0xffd8);
  await fs.writeFile(path.join(data, 'frame.jpg'), bytes);
  const pending = (await call('lulu_get_asset', { item_id: ids.pending_id })).structuredContent;
  assert.equal(pending.status, 'processing');
  const invalid = await client.callTool({ name: 'lulu_get_asset', arguments: { item_id: '../private' } });
  assert(invalid.isError);
  console.log(JSON.stringify({ ok: true, tools: tools.map(t => t.name), read_only: true, media_reused: true, unicode_pagination: true, frame: path.join(data, 'frame.jpg') }));
} finally { await client.close(); }
