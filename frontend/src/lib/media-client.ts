import { normalizeChatHistory, type ChatSession, type RestoredChatHistory } from './media-session';
import { parseMediaCapabilities, type MediaCapabilities } from './media-config';

export interface MediaUser {
  id: number;
  username: string;
  is_admin: boolean;
}

export interface ReferenceCase {
  id: string;
  title: string;
  style: string;
  tags: string[];
  description: string;
  recommended_ratio: string;
  preview_url: string;
  example_prompt?: string;
}

export type VisualInputRole = 'reference' | 'base';

export interface VisualContextState {
  url: string;
  role: VisualInputRole;
  image_task_id?: number | string | null;
  remaining_turns: number;
  turn_limit: number;
}

export interface ProposalCard {
  card_id: string;
  title: string;
  design_rationale: string;
  prompt: string;
  final_prompt?: string | null;
  style: string;
  aspect_ratio: string;
  quality: string;
  is_modification: boolean;
  base_image_url?: string | null;
  visual_input_url?: string | null;
  visual_preview_url?: string | null;
  visual_input_role?: VisualInputRole | null;
  reference_recommended?: boolean;
  reference_reason?: string | null;
  matched_reference?: ReferenceCase | null;
  image_task_id?: number | string | null;
}

export interface ChatMessageItem {
  id: string | number;
  role: 'user' | 'assistant';
  content: string;
  proposal_card?: ProposalCard | null;
  image_url?: string | null;
  image_role?: 'reference' | 'base' | 'artwork' | 'visual_input_cleared' | null;
  card_title?: string;
  model_id?: string | null;
  requested_model_id?: string | null;
  fallback_from_model_id?: string | null;
  fallback_reason?: string | null;
  thinking_mode?: string | null;
  required_input?: 'reference_image' | null;
  image_task_id?: number | string | null;
  task_id?: string | null;
  parent_id?: number | string | null;
  image_duration_ms?: number | null;
  image_model_id?: string | null;
  image_aspect_ratio?: string | null;
  image_quality?: string | null;
  persistence_status?: string | null;
}

export interface HistoryItem {
  id: number;
  task_id: string;
  prompt: string;
  style: string;
  aspect_ratio: string;
  image_url: string;
  model_id?: string | null;
  requested_model_id?: string | null;
  fallback_from_model_id?: string | null;
  fallback_reason?: string | null;
  storage_policy?: string | null;
  image_task_id?: number | string | null;
  parent_id?: number | string | null;
  session_id?: string;
  proposal_card_id?: string | null;
  final_prompt?: string | null;
  created_at?: string;
}

export interface ModelMetadata {
  model_id?: string | null;
  requested_model_id?: string | null;
  fallback_from_model_id?: string | null;
  fallback_reason?: string | null;
}

export interface GenerationSettings {
  prompt: string;
  model_id: string;
  style: string;
  aspect_ratio: string;
  quality: string;
  storage_policy: string;
  ref_image_url?: string | null;
  session_id: string;
  request_id: string;
}

export class MediaAuthError extends Error {
  constructor(message: string, readonly staleToken = false) {
    super(message);
    this.name = 'MediaAuthError';
  }
}

const TOKEN_KEY = 'media_workspace_token';
const configuredApiBase = import.meta.env.VITE_MEDIA_API_BASE?.trim().replace(/\/+$/, '');
export const MEDIA_API_BASE = configuredApiBase || '/api';

let cachedToken: string | null = null;
let sessionRevision = 0;
const sessionListeners = new Set<() => void>();
function sessionChanged(): void { sessionRevision++; sessionListeners.forEach((listener) => listener()); }
export function getMediaSessionRevision(): number { getStoredMediaToken(); return sessionRevision; }
export function subscribeMediaSessionChanges(listener: () => void): () => void {
  sessionListeners.add(listener);
  const onStorage = () => { getStoredMediaToken(); };
  if (typeof window !== 'undefined') window.addEventListener?.('storage', onStorage);
  return () => { sessionListeners.delete(listener); if (typeof window !== 'undefined') window.removeEventListener?.('storage', onStorage); };
}

export function getStoredMediaToken(): string | null {
  const storedToken = typeof localStorage === 'undefined' ? null : localStorage.getItem(TOKEN_KEY);
  if (cachedToken !== storedToken) {
    cachedToken = storedToken;
    sessionChanged();
  }
  return cachedToken;
}

export function storeMediaToken(token: string): void {
  cachedToken = token;
  localStorage.setItem(TOKEN_KEY, token);
  sessionChanged();
}

export function clearMediaToken(expectedToken?: string): boolean {
  const currentToken = getStoredMediaToken();
  if (expectedToken && currentToken !== expectedToken) return false;
  cachedToken = null;
  if (typeof localStorage !== 'undefined') localStorage.removeItem(TOKEN_KEY);
  sessionChanged();
  return true;
}

async function responseError(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => ({}));
  return typeof body.detail === 'string' ? body.detail : fallback;
}

export async function fetchWithMediaAuth(url: string, init: RequestInit = {}, token = getStoredMediaToken()): Promise<Response> {
  if (!token) throw new MediaAuthError('请先登录 Media 账号');
  const res = await fetch(url, {
    ...init,
    headers: { ...(init.headers || {}), Authorization: `Bearer ${token}` },
  });
  if (res.status === 401) {
    const staleToken = getStoredMediaToken() !== token;
    if (!staleToken) clearMediaToken(token);
    throw new MediaAuthError('Media 登录已失效，请重新登录', staleToken);
  }
  return res;
}


export async function ensureMediaSession(_force = false, signal?: AbortSignal): Promise<MediaUser> {
  if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
  const token = getStoredMediaToken();
  if (!token) throw new MediaAuthError('请先登录 Media 账号');
  const user = await fetchCurrentUser(token);
  if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
  if (getStoredMediaToken() !== token) throw new MediaAuthError('账号已更改，请重试', true);
  return user;
}

export async function loginMediaUser(username: string, password: string): Promise<MediaUser> {
  const res = await fetch(`${MEDIA_API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) throw new Error(await responseError(res, '登录失败，请检查用户名和密码'));

  const data = await res.json();
  if (typeof data.access_token !== 'string' || !data.access_token || !isMediaUser(data.user)) {
    throw new Error('登录响应格式无效，请重试');
  }
  storeMediaToken(data.access_token);
  return data.user;
}

function isMediaUser(value: unknown): value is MediaUser {
  if (!value || typeof value !== 'object') return false;
  const user = value as Record<string, unknown>;
  return typeof user.id === 'number'
    && typeof user.username === 'string'
    && typeof user.is_admin === 'boolean';
}

export async function fetchCurrentUser(token = getStoredMediaToken()): Promise<MediaUser> {
  const data = await authenticatedJson(`${MEDIA_API_BASE}/auth/me`, { method: 'GET' }, token);
  if (!isMediaUser(data)) throw new Error('当前用户响应格式无效');
  return data;
}

async function authenticatedJson(
  url: string,
  init: RequestInit = {},
  token = getStoredMediaToken(),
): Promise<any> {
  if (!token) throw new MediaAuthError('请先登录 Media 账号');
  const res = await fetchWithMediaAuth(url, init, token);
  if (!res.ok) throw new Error(await responseError(res, 'Media 请求失败'));
  return res.json();
}

async function authenticatedRequest<T>(url: string, init: RequestInit = {}): Promise<T> {
  if (!getStoredMediaToken()) throw new MediaAuthError('请先登录 Media 账号');
  return authenticatedJson(url, init) as Promise<T>;
}

export async function fetchMediaCapabilities(): Promise<MediaCapabilities> {
  const res = await fetch(`${MEDIA_API_BASE}/capabilities`);
  if (!res.ok) throw new Error(await responseError(res, '加载 Media 能力配置失败'));
  return parseMediaCapabilities(await res.json());
}

export async function sendChatMessage(
  sessionId: string,
  message: string,
  currentImageUrl: string | null | undefined,
  options: {
    model_id: string;
    thinking_mode: string;
    image_model_id?: string;
    reference_case_id?: string | null;
  },
): Promise<{
  reply_text: string;
  proposal_card: ProposalCard | null;
  model_id?: string | null;
  requested_model_id?: string | null;
  fallback_from_model_id?: string | null;
  fallback_reason?: string | null;
  thinking_mode?: string | null;
}> {
  return authenticatedRequest(`${MEDIA_API_BASE}/chat/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      message,
      current_image_url: currentImageUrl || null,
      model_id: options.model_id,
      thinking_mode: options.thinking_mode,
      image_model_id: options.image_model_id,
      reference_case_id: options.reference_case_id || null,
    }),
  });
}

export async function confirmGenerateImage(settings: GenerationSettings): Promise<{
  task_id: string;
  request_id: string;
  status: string;
  image_url: string;
  duration_ms: number;
  error_msg?: string;
  model_id?: string | null;
  requested_model_id?: string | null;
  fallback_from_model_id?: string | null;
  fallback_reason?: string | null;
  storage_policy?: string | null;
}> {
  return authenticatedRequest(`${MEDIA_API_BASE}/image/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt: settings.prompt,
      final_prompt: settings.prompt,
      model_id: settings.model_id,
      style: settings.style,
      aspect_ratio: settings.aspect_ratio,
      quality: settings.quality,
      storage_policy: settings.storage_policy,
      ref_image_url: settings.ref_image_url || null,
      session_id: settings.session_id,
      request_id: settings.request_id,
    }),
  });
}

export async function uploadReferenceImage(
  file: File,
  sessionId?: string,
  signal?: AbortSignal,
  activateContext = true,
): Promise<{ ref_url: string; visual_context: VisualContextState | null }> {
  const formData = new FormData();
  formData.append('file', file);
  if (sessionId) formData.append('session_id', sessionId);
  if (!activateContext) formData.append('activate_context', 'false');

  return authenticatedRequest<{ ref_url: string; visual_context: VisualContextState | null }>(`${MEDIA_API_BASE}/image/upload_ref`, {
    method: 'POST',
    body: formData,
    signal,
  });
}

export async function updateProposalCardVisualInput(
  sessionId: string,
  cardId: string,
  refUrl: string | null,
  signal?: AbortSignal,
): Promise<ProposalCard> {
  const data = await authenticatedRequest<{ card: ProposalCard }>(`${MEDIA_API_BASE}/chat/proposal-card/visual-input`, {
    method: 'PATCH',
    signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      card_id: cardId,
      visual_input_url: refUrl,
      visual_input_role: refUrl ? 'reference' : null,
    }),
  });
  if (!data.card || typeof data.card.card_id !== 'string') throw new Error('方案卡参考图响应格式无效');
  return data.card;
}

export async function clearVisualInput(sessionId: string, signal?: AbortSignal): Promise<void> {
  const formData = new FormData();
  formData.append('session_id', sessionId);
  await authenticatedRequest(`${MEDIA_API_BASE}/image/visual-input/clear`, {
    method: 'POST',
    body: formData,
    signal,
  });
}

export async function fetchChatSessions(): Promise<ChatSession[]> {
  const data = await authenticatedRequest<{ sessions: ChatSession[] }>(`${MEDIA_API_BASE}/chat/sessions`);
  if (!Array.isArray(data.sessions)) throw new Error('会话列表响应格式无效');
  return data.sessions.filter((session) => typeof session.session_id === 'string');
}

export async function fetchChatHistory(sessionId: string): Promise<RestoredChatHistory> {
  const query = new URLSearchParams({ session_id: sessionId });
  const data = await authenticatedRequest<{ messages: unknown[]; visual_context?: VisualContextState | null }>(`${MEDIA_API_BASE}/chat/history?${query.toString()}`);
  if (!Array.isArray(data.messages)) throw new Error('会话记录响应格式无效');
  return normalizeChatHistory(data as Parameters<typeof normalizeChatHistory>[0]);
}

export async function fetchReferences(): Promise<ReferenceCase[]> {
  const res = await fetch(`${MEDIA_API_BASE}/image/references`);
  if (!res.ok) throw new Error(await responseError(res, '加载参考案例失败'));
  const data = await res.json();
  if (!Array.isArray(data.references)) throw new Error('参考案例响应格式无效');
  return data.references;
}

export async function fetchUserHistory(): Promise<HistoryItem[]> {
  const data = await authenticatedRequest<{ images: HistoryItem[] }>(`${MEDIA_API_BASE}/image/history`);
  if (!Array.isArray(data.images)) throw new Error('历史作品响应格式无效');
  return data.images;
}
