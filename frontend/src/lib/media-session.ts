import type { ChatMessageItem, ProposalCard, VisualContextState } from './media-client';
import { activeVisualInputFromContext, type ActiveVisualInput } from './media-visual-input';

export interface ChatSession {
  session_id: string;
  title: string;
  updated_at: string | null;
  message_count: number;
  last_image_url: string | null;
}

export interface RestoredChatHistory {
  messages: ChatMessageItem[];
  activeVisualInput: ActiveVisualInput | null;
}

interface StoredChatMessage {
  id: string | number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  card_data?: ProposalCard | null;
  image_url?: string | null;
  image_role?: 'reference' | 'base' | 'artwork' | 'visual_input_cleared' | null;
  created_at?: string | null;
  model_id?: string | null;
  requested_model_id?: string | null;
  fallback_from_model_id?: string | null;
  fallback_reason?: string | null;
  thinking_mode?: string | null;
  image_task_id?: number | string | null;
  task_id?: string | null;
  parent_id?: number | string | null;
  image_duration_ms?: number | null;
  image_model_id?: string | null;
  image_aspect_ratio?: string | null;
  image_quality?: string | null;
  persistence_status?: string | null;
}

export function chooseActiveSessionId(
  sessions: ChatSession[],
  storedSessionId: string | null,
  createSessionId: () => string,
): string {
  if (storedSessionId && sessions.some((session) => session.session_id === storedSessionId)) {
    return storedSessionId;
  }
  return sessions[0]?.session_id || createSessionId();
}

export function createChatSessionId(): string {
  return `sess_${crypto.randomUUID()}`;
}

export function normalizeChatHistory(data: {
  messages?: StoredChatMessage[];
  visual_context?: VisualContextState | null;
}): RestoredChatHistory {
  const stored = data.messages || [];
  let activeVisualInput: ActiveVisualInput | null = null;

  if (Object.prototype.hasOwnProperty.call(data, 'visual_context')) {
    activeVisualInput = activeVisualInputFromContext(data.visual_context);
  } else for (const message of stored) {
    if (message.image_role === 'visual_input_cleared') {
      activeVisualInput = null;
    } else if ((message.image_role === 'reference' || message.image_role === 'base') && message.image_url) {
      activeVisualInput = {
        url: message.image_url,
        role: message.image_role,
        source: message.image_role === 'reference' ? 'upload' : 'artwork',
        imageTaskId: message.image_task_id ?? message.parent_id ?? null,
      };
    }
  }

  const messages: ChatMessageItem[] = stored
    .filter((message) => !(message.role === 'system'
      && ['reference', 'visual_input_cleared'].includes(message.image_role || '')))
    .filter((message): message is StoredChatMessage & { role: 'user' | 'assistant' } => message.role !== 'system')
    .map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      proposal_card: message.card_data || null,
      image_url: message.image_role === 'reference' || message.image_role === 'base'
        ? null
        : message.image_url || null,
      image_role: message.image_role || null,
      card_title: message.card_data?.title,
      model_id: message.model_id,
      requested_model_id: message.requested_model_id,
      fallback_from_model_id: message.fallback_from_model_id,
      fallback_reason: message.fallback_reason,
      thinking_mode: message.thinking_mode,
      image_task_id: message.image_task_id,
      task_id: message.task_id,
      parent_id: message.parent_id,
      image_duration_ms: message.image_duration_ms,
      image_model_id: message.image_model_id,
      image_aspect_ratio: message.image_aspect_ratio,
      image_quality: message.image_quality,
      persistence_status: message.persistence_status,
    }));

  const cardTitlesByTask = new Map(
    messages.flatMap((message) => message.proposal_card?.image_task_id != null
      ? [[String(message.proposal_card.image_task_id), message.proposal_card.title] as const]
      : []),
  );
  const titledMessages = messages.map((message) => message.image_url && message.image_task_id != null
    ? { ...message, card_title: cardTitlesByTask.get(String(message.image_task_id)) || message.card_title }
    : message);

  return { messages: titledMessages, activeVisualInput };
}
