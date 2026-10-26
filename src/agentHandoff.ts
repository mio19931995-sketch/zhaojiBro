// Short-lived UI handoff. Opening a destination only fills its form.
export type AgentHandoff = { id: string; action: 'voice' | 'transcription' | 'collection'; text?: string; source_id?: string; url?: string; limit?: number };
let pending: AgentHandoff | null = null;
export function prepareAgentHandoff(value: AgentHandoff) { pending = value; }
export function peekAgentHandoff(action: AgentHandoff['action']) { return pending?.action === action ? pending : null; }
export function clearAgentHandoff(id: string) { if (pending?.id === id) pending = null; }
