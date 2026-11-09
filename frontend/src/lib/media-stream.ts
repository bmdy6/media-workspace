import { fetchWithMediaAuth, MEDIA_API_BASE, type ChatMessageItem, type ProposalCard, type VisualContextState } from './media-client';

export interface StreamedChatResponse {
  reply_text: string;
  proposal_card: ProposalCard | null;
  model_id?: string | null;
  requested_model_id?: string | null;
  fallback_from_model_id?: string | null;
  fallback_reason?: string | null;
  thinking_mode?: string | null;
  needs_clarification?: boolean;
  required_input?: 'reference_image' | null;
  visual_context?: VisualContextState | null;
  [key: string]: unknown;
}

export type MediaChatStreamEvent =
  | { type: 'status'; phase?: string; label?: string }
  | { type: 'delta'; text: string }
  | { type: 'done'; response: StreamedChatResponse }
  | { type: 'error'; message: string };

export interface ChatStreamInput {
  session_id: string;
  message: string;
  current_image_url: string | null;
  current_image_role?: 'reference' | 'base' | null;
  current_image_task_id?: number | string | null;
  resume_after_reference?: boolean;
  model_id: string;
  thinking_mode: string;
  image_model_id?: string;
  reference_case_id?: string | null;
}

const MAX_SSE_EVENT_SIZE = 1024 * 1024;
const abortError = (signal: AbortSignal) => signal.reason || new DOMException('Aborted', 'AbortError');

function normalizeEvent(name: string, raw: unknown): MediaChatStreamEvent | null {
  if (!raw || typeof raw !== 'object') return null;
  const data = raw as Record<string, unknown>;
  const type = (typeof data.type === 'string' ? data.type : name).toLowerCase();
  if (type === 'status') {
    if (typeof data.phase !== 'string' || typeof data.label !== 'string') throw new Error('对话流 status 事件格式无效');
    return { type, phase: data.phase, label: data.label };
  }
  if (type === 'delta') {
    if (typeof data.text !== 'string') throw new Error('对话流 delta 事件格式无效');
    return { type, text: data.text };
  }
  if (type === 'error') {
    if (typeof data.message !== 'string' || !data.message) throw new Error('对话流 error 事件格式无效');
    return { type, message: data.message };
  }
  if (type === 'done') {
    const response = data.response && typeof data.response === 'object' ? data.response : data;
    const result = response as Record<string, unknown>;
    if (typeof result.reply_text !== 'string'
      || !Object.prototype.hasOwnProperty.call(result, 'proposal_card')
      || (result.proposal_card !== null && (typeof result.proposal_card !== 'object' || Array.isArray(result.proposal_card)))) {
      throw new Error('对话流 done 事件格式无效');
    }
    return { type, response: response as StreamedChatResponse };
  }
  throw new Error(`无法识别对话流事件：${type || '空类型'}`);
}

function parseSseBlock(block: string): MediaChatStreamEvent | null {
  let eventName = '';
  const dataLines: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'event') eventName = value;
    else if (field === 'data') dataLines.push(value);
  }
  if (!dataLines.length) return null;
  const text = dataLines.join('\n');
  if (text === '[DONE]') return null;
  if (text.length > MAX_SSE_EVENT_SIZE) throw new Error('对话流事件超过大小限制');
  let parsed: unknown;
  try { parsed = JSON.parse(text); }
  catch { throw new Error('对话流事件 JSON 格式无效'); }
  return normalizeEvent(eventName, parsed);
}

export async function consumeMediaEventStream(
  stream: ReadableStream<Uint8Array>,
  options: {
    signal?: AbortSignal;
    onStatus?: (status: Extract<MediaChatStreamEvent, { type: 'status' }>) => void;
    onDelta?: (text: string) => void;
  } = {},
): Promise<StreamedChatResponse> {
  if (options.signal?.aborted) throw abortError(options.signal);
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let completed: StreamedChatResponse | null = null;
  const abort = () => { void reader.cancel(options.signal?.reason).catch(() => undefined); };
  options.signal?.addEventListener('abort', abort, { once: true });

  const processBlock = (block: string) => {
    if (block.length > MAX_SSE_EVENT_SIZE) throw new Error('对话流事件超过大小限制');
    const event = parseSseBlock(block);
    if (!event || completed) return;
    if (event.type === 'status') options.onStatus?.(event);
    else if (event.type === 'delta') options.onDelta?.(event.text);
    else if (event.type === 'error') throw new Error(event.message);
    else if (event.type === 'done') completed = event.response;
  };

  try {
    while (true) {
      if (options.signal?.aborted) throw options.signal.reason || new DOMException('Aborted', 'AbortError');
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > MAX_SSE_EVENT_SIZE && !/\r?\n\r?\n/.test(buffer)) throw new Error('对话流事件超过大小限制');
      let boundary = buffer.match(/\r?\n\r?\n/);
      while (boundary?.index !== undefined) {
        const block = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        processBlock(block);
        if (completed) {
          await reader.cancel();
          return completed;
        }
        boundary = buffer.match(/\r?\n\r?\n/);
      }
      if (buffer.length > MAX_SSE_EVENT_SIZE && !/\r?\n\r?\n/.test(buffer)) throw new Error('对话流事件超过大小限制');
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      processBlock(buffer);
      if (completed) return completed;
    }
    if (options.signal?.aborted) throw abortError(options.signal);
    if (!completed) throw new Error('对话流意外结束，未收到完成事件');
    return completed;
  } catch (error) {
    await reader.cancel(error).catch(() => undefined);
    throw error;
  } finally {
    options.signal?.removeEventListener('abort', abort);
    reader.releaseLock();
  }
}

export async function streamMediaChat(
  input: ChatStreamInput,
  options: {
    signal?: AbortSignal;
    onStatus?: (status: Extract<MediaChatStreamEvent, { type: 'status' }>) => void;
    onDelta?: (text: string) => void;
  } = {},
): Promise<StreamedChatResponse> {
  const response = await fetchWithMediaAuth(`${MEDIA_API_BASE}/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(input),
    signal: options.signal,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === 'string' ? body.detail : `对话流请求失败（${response.status}）`);
  }
  if (!response.body) throw new Error('浏览器未提供对话流读取器');
  return consumeMediaEventStream(response.body, options);
}

export function chatItemFromStream(
  id: string | number,
  response: StreamedChatResponse,
  streamedText: string,
): ChatMessageItem {
  return {
    id,
    role: 'assistant',
    content: response.reply_text || streamedText,
    proposal_card: response.proposal_card || null,
    model_id: response.model_id,
    requested_model_id: response.requested_model_id,
    fallback_from_model_id: response.fallback_from_model_id,
    fallback_reason: response.fallback_reason,
    thinking_mode: response.thinking_mode,
    required_input: response.required_input,
  };
}
