export type TextRange = { start: number; end: number; text: string; offset_unit: 'utf8' };
export type TargetRange = TextRange & { revision: number };
export type Reference = TextRange & { id: string; kind: 'draft' | 'document'; source_id: string; title: string; source_hash: string; stale?: boolean; stale_reason?: string };
export type Message = { id: string; role: 'user' | 'assistant'; text: string; run_id: string; scope?: 'document' | 'selection'; references?: Reference[] };
export type Run = { id: string; status: string; phase: string; instruction: string; preset: string; current: number; total: number; result: string; error: string; model: string; saved_output_id: string; applied: boolean; proposal_used?: boolean; scope?: 'document' | 'selection'; selection?: TextRange | null; references?: Reference[]; draft_revision: number };
export type Workspace = { id: string; source_id: string; source_hash: string; title: string; text: string; instruction?: string; preset?: string; revision: number; messages: Message[]; references?: Reference[]; run_id: string; run?: Run | null };
export type Proposal = { id: string; start: number; end: number; before: string; after: string; state: string; can_apply?: boolean; blocked_reason?: string };
export type ProposalList = { proposals: Proposal[]; can_apply: boolean; reason: string; can_undo: boolean; revision: number; scope: string };

// Browser selections use UTF-16; the API's anchors follow upstream UTF-8 bytes.
export function selectedRange(element: HTMLTextAreaElement): TextRange | null {
  const { value, selectionStart: start, selectionEnd: end } = element;
  if (end <= start) return null;
  const encoder = new TextEncoder();
  return { start: encoder.encode(value.slice(0, start)).length, end: encoder.encode(value.slice(0, end)).length, text: value.slice(start, end), offset_unit: 'utf8' };
}
export function rangeText(value: string, range: TextRange): string {
  const bytes = new TextEncoder().encode(value);
  if (!Number.isInteger(range.start) || !Number.isInteger(range.end) || range.start < 0 || range.end > bytes.length || range.end < range.start) throw Error('片段范围已变化，请重新选择');
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes.slice(range.start, range.end));
}
export function locateRange(element: HTMLTextAreaElement, range: TextRange) {
  if (rangeText(element.value, range) !== range.text) throw Error('片段内容已变化，请重新选择');
  const bytes = new TextEncoder().encode(element.value), decoder = new TextDecoder('utf-8', { fatal: true });
  element.focus();
  element.setSelectionRange(decoder.decode(bytes.slice(0, range.start)).length, decoder.decode(bytes.slice(0, range.end)).length);
}
