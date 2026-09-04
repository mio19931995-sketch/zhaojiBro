import { api } from './api';
// Immediate cache survives navigation; ordered SQLite writes survive restarts.
const cache = new Map<string, any>();
let writes: Promise<unknown> = Promise.resolve();
export async function readDraft<T>(key: string): Promise<T | null> {
  if (cache.has(key)) return cache.get(key);
  await writes;
  const draft = await api<T | null>(`/drafts/${key}`);
  if (!cache.has(key)) cache.set(key, draft);
  return cache.get(key);
}
export function writeDraft(key: string, draft: unknown): Promise<unknown> {
  cache.set(key, draft);
  writes = writes.catch(() => {}).then(() => api(`/drafts/${key}`, 'PUT', draft));
  return writes;
}
export async function clearDraft(key: string) {
  cache.set(key, null);
  writes = writes.catch(() => {}).then(() => api(`/drafts/${key}`, 'DELETE'));
  return writes;
}
