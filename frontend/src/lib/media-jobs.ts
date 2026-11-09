import { fetchWithMediaAuth, MEDIA_API_BASE, type GenerationSettings } from './media-client';

export interface ImageJobSummary {
  id: number | string;
  task_id: string;
  status: string;
  phase?: string | null;
  poll_interval_ms: number;
  poll_timeout_ms: number;
  polling_timed_out?: boolean;
  session_id?: string;
  parent_id?: number | string | null;
  image_url?: string | null;
  result_url?: string | null;
  persistence_status?: string | null;
  is_temporary?: boolean;
  expires_at?: string | null;
  timings?: Record<string, number | null>;
  model_id?: string | null;
  requested_model_id?: string | null;
  storage_policy?: string | null;
  request_id?: string;
  proposal_card_id?: string | null;
  final_prompt?: string | null;
  aspect_ratio?: string | null;
  quality?: string | null;
  error_msg?: string | null;
  duration_ms?: number;
  created_at?: string | null;
  [key: string]: unknown;
}

export interface ImageJobInput extends GenerationSettings {
  parent_id?: number | string | null;
  proposal_card_id?: string;
}

export function imageJobCompletionNotice(job: ImageJobSummary): string | null {
  if (job.persistence_status?.toLowerCase() === 'failed') {
    return `图片已生成，但转存未完成；临时原图${job.expires_at ? `将在 ${job.expires_at} 过期` : '可能过期'}，请及时下载。`;
  }
  if (job.is_temporary) {
    return `图片已生成，当前使用本地临时保存；原图${job.expires_at ? `将在 ${job.expires_at} 过期` : '可能过期'}，请及时下载。`;
  }
  return null;
}

export type ImagePreviewSource = { kind: 'protected_result'; taskId: string } | { kind: 'direct'; url: string };

function mediaApiBaseUrl(): URL {
  return new URL(MEDIA_API_BASE, globalThis.location?.origin || 'http://localhost');
}

export function protectedImageResultTaskId(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const baseUrl = mediaApiBaseUrl();
    const candidate = new URL(value, baseUrl.origin);
    if (candidate.origin !== baseUrl.origin) return null;
    const prefixes = new Set([
      `${baseUrl.pathname.replace(/\/$/, '')}/image/result/`,
      `${baseUrl.pathname.replace(/\/media\/?$/, '')}/image/result/`,
    ]);
    for (const prefix of prefixes) {
      if (!candidate.pathname.startsWith(prefix)) continue;
      const segment = candidate.pathname.slice(prefix.length);
      if (!segment || segment.includes('/')) return null;
      const taskId = decodeURIComponent(segment);
      return /^[A-Za-z0-9._-]+$/.test(taskId) ? taskId : null;
    }
  } catch {
    return null;
  }
  return null;
}

export function resolveImagePreviewSource(imageUrl?: string | null, resultUrl?: string | null): ImagePreviewSource | null {
  const imageResultTaskId = protectedImageResultTaskId(imageUrl);
  if (imageUrl && !imageResultTaskId) return { kind: 'direct', url: imageUrl };
  const resultTaskId = protectedImageResultTaskId(resultUrl);
  if (resultTaskId) return { kind: 'protected_result', taskId: resultTaskId };
  if (imageResultTaskId) return { kind: 'protected_result', taskId: imageResultTaskId };
  if (resultUrl) return { kind: 'direct', url: resultUrl };
  return null;
}

export function isSafeImageReferenceUrl(value: string | null | undefined): boolean {
  return Boolean(value && !value.startsWith('blob:') && !value.startsWith('data:') && !protectedImageResultTaskId(value));
}

export function stableImageRequestId(
  requests: Map<string, { fingerprint: string; requestId: string }>,
  proposalCardId: string,
  fingerprint: string,
  createId: () => string,
): string {
  const previous = requests.get(proposalCardId);
  const requestId = previous?.fingerprint === fingerprint ? previous.requestId : createId();
  requests.set(proposalCardId, { fingerprint, requestId });
  return requestId;
}

async function responseError(response: Response, fallback: string): Promise<string> {
  const data = await response.json().catch(() => ({}));
  return typeof data.detail === 'string' ? data.detail : fallback;
}

function validJob(value: unknown): value is ImageJobSummary {
  return Boolean(value && typeof value === 'object'
    && (typeof (value as ImageJobSummary).id === 'number' || typeof (value as ImageJobSummary).id === 'string')
    && typeof (value as ImageJobSummary).task_id === 'string'
    && typeof (value as ImageJobSummary).status === 'string');
}

function validateJobTiming(job: ImageJobSummary): void {
  if (!Number.isFinite(job.poll_interval_ms) || job.poll_interval_ms <= 0) {
    throw new Error('图片任务缺少有效 poll_interval_ms，无法继续轮询');
  }
  if (!Number.isFinite(job.poll_timeout_ms) || job.poll_timeout_ms <= 0) {
    throw new Error('图片任务缺少有效 poll_timeout_ms，无法继续轮询');
  }
}

export async function submitImageJob(input: ImageJobInput, signal?: AbortSignal): Promise<ImageJobSummary> {
  const response = await fetchWithMediaAuth(`${MEDIA_API_BASE}/image/jobs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify({
      prompt: input.prompt,
      final_prompt: input.prompt,
      model_id: input.model_id,
      style: input.style,
      aspect_ratio: input.aspect_ratio,
      quality: 'low',
      storage_policy: input.storage_policy,
      ref_image_url: input.ref_image_url || null,
      session_id: input.session_id,
      request_id: input.request_id,
      parent_id: input.parent_id ?? null,
      proposal_card_id: input.proposal_card_id,
    }),
  });
  if (!response.ok) throw new Error(await responseError(response, `图片任务提交失败（${response.status}）`));
  const data = await response.json();
  if (!validJob(data)) throw new Error('图片任务响应格式无效');
  validateJobTiming(data);
  if (!data.proposal_card_id && input.proposal_card_id) {
    return { ...data, proposal_card_id: input.proposal_card_id };
  }
  return data;
}

export async function fetchImageJob(taskId: string, signal?: AbortSignal): Promise<ImageJobSummary> {
  const response = await fetchWithMediaAuth(`${MEDIA_API_BASE}/image/task/${encodeURIComponent(taskId)}`, { signal });
  if (!response.ok) throw new Error(await responseError(response, `加载图片任务失败（${response.status}）`));
  const data = await response.json();
  if (!validJob(data)) throw new Error('图片任务状态响应格式无效');
  validateJobTiming(data);
  return data;
}

export async function fetchActiveImageJobs(sessionId: string, signal?: AbortSignal): Promise<ImageJobSummary[]> {
  const query = new URLSearchParams({ session_id: sessionId });
  const response = await fetchWithMediaAuth(`${MEDIA_API_BASE}/image/jobs/active?${query.toString()}`, { signal });
  if (!response.ok) throw new Error(await responseError(response, `加载进行中的图片任务失败（${response.status}）`));
  const data = await response.json();
  const jobs = Array.isArray(data) ? data : data.jobs;
  if (!Array.isArray(jobs) || !jobs.every(validJob)) throw new Error('进行中的图片任务响应格式无效');
  jobs.forEach(validateJobTiming);
  return jobs;
}

export function getImageResultUrl(taskId: string): string {
  const baseUrl = mediaApiBaseUrl();
  const path = `${baseUrl.pathname.replace(/\/$/, '')}/image/result/${encodeURIComponent(taskId)}`;
  return new URL(path, baseUrl.origin).toString();
}

export async function fetchOriginalImageResult(taskId: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetchWithMediaAuth(getImageResultUrl(taskId), { signal });
  if (!response.ok) throw new Error(await responseError(response, `读取图片原图失败（${response.status}）`));
  return response.blob();
}

export function isObjectUrl(value: string | null | undefined): boolean {
  return Boolean(value?.startsWith('blob:'));
}

function isPersistenceComplete(job: ImageJobSummary): boolean {
  const status = (job.persistence_status || '').toLowerCase();
  return ['success', 'succeeded'].includes(job.status.toLowerCase())
    && ['stored', 'not_requested', 'failed'].includes(status);
}

export function isFailedImageJob(job: ImageJobSummary): boolean {
  return ['failed', 'interrupted'].includes((job.phase || '').toLowerCase())
    || ['failed', 'interrupted'].includes(job.status.toLowerCase());
}

export function upsertImageJob(jobs: ImageJobSummary[], next: ImageJobSummary): ImageJobSummary[] {
  return [...jobs.filter((entry) => entry.task_id !== next.task_id
    && (!next.proposal_card_id || entry.proposal_card_id !== next.proposal_card_id)), next];
}

function wait(milliseconds: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(signal.reason || new DOMException('Aborted', 'AbortError'));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, milliseconds);
    const abort = () => { clearTimeout(timer); reject(signal?.reason || new DOMException('Aborted', 'AbortError')); };
    signal?.addEventListener('abort', abort, { once: true });
  });
}

export async function pollImageJob(
  taskId: string,
  options: {
    signal?: AbortSignal;
    initialJob?: ImageJobSummary;
    maxPolls?: number;
    onUpdate?: (job: ImageJobSummary) => void | Promise<void>;
  } = {},
): Promise<ImageJobSummary> {
  if (options.signal?.aborted) throw options.signal.reason || new DOMException('Aborted', 'AbortError');
  const timeoutMs = options.initialJob?.poll_timeout_ms;
  if (!Number.isFinite(timeoutMs) || !timeoutMs || timeoutMs <= 0) throw new Error('图片任务未提供有效的 poll_timeout_ms，已停止自动轮询');
  const deadlineController = new AbortController();
  const abortFromCaller = () => deadlineController.abort(options.signal?.reason || new DOMException('Aborted', 'AbortError'));
  options.signal?.addEventListener('abort', abortFromCaller, { once: true });
  const timeoutReason = new DOMException('图片任务轮询已到服务端时限', 'TimeoutError');
  const timeout = setTimeout(() => deadlineController.abort(timeoutReason), timeoutMs);
  const deadline = Date.now() + timeoutMs;
  let current: ImageJobSummary | null = options.initialJob || null;
  let polls = 0;
  try {
    while (options.maxPolls === undefined || polls < Math.max(1, options.maxPolls)) {
      if (deadlineController.signal.aborted) throw deadlineController.signal.reason;
      current = await fetchImageJob(taskId, deadlineController.signal);
      polls += 1;
      await options.onUpdate?.(current);
      if (isFailedImageJob(current) || isPersistenceComplete(current)) return current;
      const remainingMs = deadline - Date.now();
      if (remainingMs <= 0) return { ...current, polling_timed_out: true };
      if (options.maxPolls !== undefined && polls >= options.maxPolls) return { ...current, polling_timed_out: true };
      const interval = current.poll_interval_ms;
      if (!Number.isFinite(interval) || interval <= 0) throw new Error('图片任务未提供有效的 poll_interval_ms，已停止自动轮询');
      await wait(Math.max(250, Math.min(interval, 30_000, remainingMs)), deadlineController.signal);
    }
    return { ...(current || options.initialJob!), polling_timed_out: true };
  } catch (error) {
    if (deadlineController.signal.reason === timeoutReason && current) return { ...current, polling_timed_out: true };
    throw error;
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener('abort', abortFromCaller);
  }
}
