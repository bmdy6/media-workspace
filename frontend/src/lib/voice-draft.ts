import {
  MEDIA_API_BASE,
  MediaAuthError,
  ensureMediaSession,
  fetchWithMediaAuth,
  getStoredMediaToken,
} from './media-client';
import { getMediaSessionRevision } from './media-client';

export type VoiceDraftSurface = 'capture' | 'media' | 'planner' | 'prompts' | 'generic';
export type DraftRefineMode = 'faithful' | 'polish';
export type PolishDepth = 'light' | 'deep';

export interface VoiceDraftResult {
  traceId: string;
  rawTranscript: string;
  text: string;
  language?: string;
  warnings: string[];
  timings: {
    asrMs: number;
    refineMs: number;
    totalMs: number;
  };
}

export const VOICE_DRAFT_MAX_BYTES = 20 * 1024 * 1024;
export const VOICE_DRAFT_MAX_SECONDS = 10 * 60;

const RECORDER_MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
  'audio/ogg;codecs=opus',
  'audio/ogg',
] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

async function responseError(response: Response, fallback: string): Promise<string> {
  const body = await response.json().catch(() => ({})) as { detail?: unknown };
  return typeof body.detail === 'string' && body.detail.trim() ? body.detail : fallback;
}

export function pickRecorderMimeType(
  isSupported: (mime: string) => boolean,
): string | undefined {
  return RECORDER_MIME_CANDIDATES.find((mime) => isSupported(mime));
}

export function voiceDraftFilename(mimeType: string): string {
  if (mimeType.includes('mp4')) return 'voice-draft.m4a';
  if (mimeType.includes('ogg')) return 'voice-draft.ogg';
  if (mimeType.includes('webm')) return 'voice-draft.webm';
  return 'voice-draft.audio';
}

export function insertDraftAtSelection(
  current: string,
  inserted: string,
  start = current.length,
  end = start,
): { value: string; cursor: number } {
  const safeStart = Math.max(0, Math.min(start, current.length));
  const safeEnd = Math.max(safeStart, Math.min(end, current.length));
  const next = current.slice(0, safeStart) + inserted + current.slice(safeEnd);
  return { value: next, cursor: safeStart + inserted.length };
}

export function parseVoiceDraftResponse(value: unknown): VoiceDraftResult {
  if (!isRecord(value)) throw new Error('语音转写响应格式无效');
  const raw = value.raw_transcript;
  const text = value.text;
  const traceId = value.trace_id;
  const warnings = value.warnings;
  const timings = value.timings;
  if (
    typeof raw !== 'string'
    || typeof text !== 'string'
    || typeof traceId !== 'string'
    || !Array.isArray(warnings)
    || warnings.some((item) => typeof item !== 'string')
    || !isRecord(timings)
    || typeof timings.asr_ms !== 'number'
    || typeof timings.refine_ms !== 'number'
    || typeof timings.total_ms !== 'number'
  ) {
    throw new Error('语音转写响应格式无效');
  }
  return {
    traceId,
    rawTranscript: raw,
    text,
    language: typeof value.language === 'string' ? value.language : undefined,
    warnings: warnings as string[],
    timings: {
      asrMs: timings.asr_ms,
      refineMs: timings.refine_ms,
      totalMs: timings.total_ms,
    },
  };
}

export async function createVoiceDraft(
  audio: Blob,
  surface: VoiceDraftSurface,
  existingText = '',
  language: 'auto' | 'zh' | 'en' = 'auto',
): Promise<VoiceDraftResult> {
  if (audio.size > VOICE_DRAFT_MAX_BYTES) throw new Error('录音超过 20MB，请缩短后重试');
  if (!audio.size) throw new Error('录音内容为空');

  await ensureMediaSession();
  const form = new FormData();
  form.append('audio', audio, voiceDraftFilename(audio.type));
  form.append('surface', surface);
  form.append('existing_text', existingText.slice(0, 4000));
  form.append('language', language);

  const response = await fetchWithMediaAuth(MEDIA_API_BASE + '/voice/voice-draft', {
    method: 'POST',
    body: form,
  });
  if (!response.ok) throw new Error(await responseError(response, '语音转写失败，请重试'));
  return parseVoiceDraftResponse(await response.json());
}

export async function refineDraft(
  text: string,
  mode: DraftRefineMode,
  surface: VoiceDraftSurface,
  nearbyText = '',
): Promise<string> {
  const trimmed = text.trim();
  if (!trimmed) return text;

  await ensureMediaSession();
  const response = await fetchWithMediaAuth(MEDIA_API_BASE + '/voice/draft-refine', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      text: trimmed,
      mode,
      surface,
      nearby_text: nearbyText.slice(0, 4000),
    }),
  });
  if (!response.ok) throw new Error(await responseError(response, '文字整理失败，请稍后重试'));
  const body = await response.json().catch(() => ({}));
  if (!isRecord(body) || typeof body.text !== 'string' || !body.text.trim()) {
    throw new Error('文字整理响应格式无效');
  }
  return body.text;
}

export interface DraftRefineStreamResult {
  text: string;
  mode: DraftRefineMode;
  modelId: string;
  thinkingMode: string;
}

export interface DraftRefineStreamOptions {
  polishDepth?: PolishDepth;
  signal?: AbortSignal;
  onDelta?: (delta: string, fullText: string) => void;
  mediaIdentity?: { token: string; revision: number };
}

function assertCurrentMediaIdentity(identity: { token: string; revision: number }): void {
  if (getMediaSessionRevision() !== identity.revision || getStoredMediaToken() !== identity.token) {
    throw new MediaAuthError('账号已更改，请确认当前账号后重试。', true);
  }
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw signal.reason ?? new DOMException('Aborted', 'AbortError');
}

const REFINE_MAX_SSE_EVENT_SIZE = 256 * 1024;

function parseRefineSseBlock(block: string): {
  event: string;
  data: Record<string, unknown>;
} | null {
  let event = '';
  const dataLines: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    else if (field === 'data') dataLines.push(value);
  }
  if (!dataLines.length) return null;
  const raw = dataLines.join('\n');
  if (raw.length > REFINE_MAX_SSE_EVENT_SIZE) throw new Error('润色流事件超过大小限制');
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error('润色流事件格式无效');
  }
  if (!isRecord(parsed)) throw new Error('润色流事件格式无效');
  return { event, data: parsed };
}

export async function consumeDraftRefineStream(
  stream: ReadableStream<Uint8Array>,
  options: DraftRefineStreamOptions = {},
): Promise<DraftRefineStreamResult> {
  if (options.signal?.aborted) {
    throw options.signal.reason || new DOMException('Aborted', 'AbortError');
  }

  const reader = stream.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let buffer = '';
  let fullText = '';
  let result: DraftRefineStreamResult | null = null;
  const abort = () => {
    void reader.cancel(options.signal?.reason).catch(() => undefined);
  };
  options.signal?.addEventListener('abort', abort, { once: true });

  const processBlock = (block: string) => {
    if (!block.trim() || result) return;
    if (block.length > REFINE_MAX_SSE_EVENT_SIZE) throw new Error('润色流事件超过大小限制');
    const parsed = parseRefineSseBlock(block);
    if (!parsed) return;
    const { event, data } = parsed;
    if (event === 'delta') {
      if (typeof data.text !== 'string') throw new Error('润色流 delta 格式无效');
      fullText += data.text;
      options.onDelta?.(data.text, fullText);
      return;
    }
    if (event === 'error') {
      throw new Error(
        typeof data.message === 'string' && data.message
          ? data.message
          : '文字润色暂时不可用，请稍后重试',
      );
    }
    if (event === 'done') {
      if (
        typeof data.text !== 'string' || !data.text.trim()
        || (data.mode !== 'polish' && data.mode !== 'faithful')
        || typeof data.model_id !== 'string'
        || typeof data.thinking_mode !== 'string'
      ) {
        throw new Error('润色流 done 格式无效');
      }
      if (options.polishDepth === 'deep' && (data.polish_depth !== 'deep' || data.mode !== 'polish')) {
        throw new Error('服务端尚未确认深度润色，请选择轻改或更新服务；原文未被替换。');
      }
      result = {
        text: data.text,
        mode: data.mode as DraftRefineMode,
        modelId: data.model_id,
        thinkingMode: data.thinking_mode,
      };
      return;
    }
    throw new Error('无法识别润色流事件');
  };

  try {
    while (true) {
      if (options.signal?.aborted) {
        throw options.signal.reason || new DOMException('Aborted', 'AbortError');
      }
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.match(/\r?\n\r?\n/);
      while (boundary?.index !== undefined) {
        const block = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        processBlock(block);
        if (result) {
          await reader.cancel();
          return result;
        }
        boundary = buffer.match(/\r?\n\r?\n/);
      }
      if (buffer.length > REFINE_MAX_SSE_EVENT_SIZE) throw new Error('润色流事件超过大小限制');
    }

    buffer += decoder.decode();
    // SSE dispatches only blank-line-terminated events. EOF is not a completion.
    // Never apply an unterminated/truncated done frame.
    if (!result) throw new Error('润色流意外结束，未收到完成事件');
    return result;
  } catch (error) {
    await reader.cancel(error).catch(() => undefined);
    throw error;
  } finally {
    options.signal?.removeEventListener('abort', abort);
    reader.releaseLock();
  }
}

export async function streamRefineDraft(
  text: string,
  mode: DraftRefineMode,
  surface: VoiceDraftSurface,
  nearbyText = '',
  options: DraftRefineStreamOptions = {},
): Promise<DraftRefineStreamResult> {
  if (options.polishDepth === 'deep' && mode !== 'polish') {
    throw new Error('深度润色仅适用于整段润色，不适用于忠实整理。');
  }
  const trimmed = text.trim();
  if (!trimmed) {
    return {
      text,
      mode,
      modelId: '',
      thinkingMode: 'off',
    };
  }

  throwIfAborted(options.signal);
  const identity = options.mediaIdentity;
  let identityToken: string | null = null;
  if (identity) {
    // Keep the account identity captured at submission; never redirect text to a different login.
    assertCurrentMediaIdentity(identity);
    await ensureMediaSession(true, options.signal);
    throwIfAborted(options.signal);
    assertCurrentMediaIdentity(identity);
    identityToken = getStoredMediaToken();
    if (!identityToken) throw new MediaAuthError('Media 临时身份不可用，请重试。');
  } else {
    await ensureMediaSession(false, options.signal);
  }
  throwIfAborted(options.signal);
  const request = {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify({
      text: trimmed,
      mode,
      surface,
      nearby_text: nearbyText.slice(0, 4000),
      ...(options.polishDepth === 'deep' ? { polish_depth: 'deep' } : {}),
    }),
    signal: options.signal,
  };
  const response = identityToken
    ? await fetchWithMediaAuth(MEDIA_API_BASE + '/voice/draft-refine/stream', request, identityToken)
    : await fetchWithMediaAuth(MEDIA_API_BASE + '/voice/draft-refine/stream', request);
  if (!response.ok) throw new Error(await responseError(response, '文字润色失败，请稍后重试'));
  if (!response.body) throw new Error('浏览器未提供润色流读取器');
  return consumeDraftRefineStream(response.body, options);
}
