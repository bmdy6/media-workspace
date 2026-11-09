import type { ChatMessageItem } from '../../../lib/media-client';
import { pollImageJob, type ImageJobSummary } from '../../../lib/media-jobs';

export interface ImageJobSession {
  sessionId: string;
  epoch: number;
  authToken: string;
}

export interface ImageJobPollOptions {
  signal: AbortSignal;
  initialJob: ImageJobSummary;
  onUpdate: (job: ImageJobSummary) => void | Promise<void>;
}

export interface ImageJobLifecyclePorts {
  isCurrent: (session: ImageJobSession) => boolean;
  poll: (taskId: string, options: ImageJobPollOptions) => Promise<ImageJobSummary>;
  resolveResult: (job: ImageJobSummary, signal: AbortSignal, session: ImageJobSession) => Promise<string | null>;
  releaseResult: (url: string) => void;
  onJobUpdate: (job: ImageJobSummary, session: ImageJobSession) => void;
  /** Return true when the caller has accepted or already owns the image URL. */
  onImageResult: (job: ImageJobSummary, imageUrl: string, session: ImageJobSession) => boolean;
  onResultError: (error: unknown, job: ImageJobSummary, session: ImageJobSession) => void;
  onComplete: (job: ImageJobSummary, session: ImageJobSession) => void;
  onPollError: (error: unknown, job: ImageJobSummary, session: ImageJobSession) => void;
}

export interface ImageJobLifecycleController {
  watch: (job: ImageJobSummary, session: ImageJobSession) => Promise<void>;
  cancelAll: () => void;
}

export function formatImageJobElapsed(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`;
}

export function completedImageJobDurationMs(job: ImageJobSummary): number {
  const total = job.timings?.total_ms;
  const firstResult = job.timings?.first_result_ms;
  return (typeof total === 'number' && Number.isFinite(total) ? total : null)
    ?? (typeof firstResult === 'number' && Number.isFinite(firstResult) ? firstResult : null)
    ?? job.duration_ms ?? 0;
}

export function imageJobPersistenceMessage(job: ImageJobSummary): string {
  const status = (job.persistence_status || '').toLowerCase();
  if (status === 'stored') return '已保存';
  if (status === 'not_requested') return '未转存，原图可下载';
  if (status === 'failed') return `转存失败，临时原图仍可下载${job.expires_at ? `（${job.expires_at} 过期）` : ''}`;
  return '正在转存';
}

export function createImageJobResultMessage(job: ImageJobSummary, imageUrl: string): ChatMessageItem {
  const parentId = job.id ?? job.parent_id ?? null;
  const duration = completedImageJobDurationMs(job);
  const metadata = job.model_id ? ` · 实际模型：${job.model_id}` : '';
  return {
    id: `image-task-${job.task_id}`,
    role: 'assistant',
    content: `图片已就绪 · 用时 ${formatImageJobElapsed(duration)} · ${imageJobPersistenceMessage(job)}${metadata}`,
    image_url: imageUrl,
    card_title: '生成作品',
    model_id: job.model_id,
    requested_model_id: job.requested_model_id,
    image_task_id: parentId,
    task_id: job.task_id,
    image_duration_ms: duration,
    image_model_id: job.model_id,
    image_aspect_ratio: job.aspect_ratio,
    image_quality: job.quality,
    persistence_status: job.persistence_status,
  };
}

export type ImageJobResultMessageDisposition = 'new' | 'duplicate' | 'same-image';

export function imageJobResultMessageDisposition(
  messages: ChatMessageItem[],
  job: ImageJobSummary,
  imageUrl: string,
): ImageJobResultMessageDisposition {
  const parentId = job.id ?? job.parent_id ?? null;
  const existing = messages.find((message) => message.task_id === job.task_id
    || (parentId !== null && String(message.image_task_id) === String(parentId)));
  if (!existing) return 'new';
  return existing.image_url === imageUrl ? 'same-image' : 'duplicate';
}

export function updateImageJobResultMessages(messages: ChatMessageItem[], job: ImageJobSummary): ChatMessageItem[] {
  if (!job.persistence_status || !['success', 'succeeded'].includes(job.status.toLowerCase())) return messages;
  const status = job.persistence_status.toLowerCase();
  const pending = ['pending', 'queued', 'running', 'saving', 'persisting'].includes(status);
  const persistenceCopy = pending ? '正在转存' : imageJobPersistenceMessage(job);
  const messageId = String(job.id);
  return messages.map((message) => {
    const matchesJob = message.task_id === job.task_id
      || (job.id !== undefined && job.id !== null && String(message.image_task_id) === messageId);
    if (!matchesJob) return message;
    const actualModel = message.model_id ? ` · 实际模型：${message.model_id}` : '';
    return {
      ...message,
      content: `图片已完成 · 共用时 ${formatImageJobElapsed(completedImageJobDurationMs(job))} · ${persistenceCopy}${actualModel}`,
      persistence_status: job.persistence_status,
    };
  });
}

export function createImageJobLifecycleController(ports: ImageJobLifecyclePorts): ImageJobLifecycleController {
  const active = new Map<string, AbortController>();
  const pendingResults = new Map<string, AbortController>();
  const handledResults = new Set<string>();

  const isActive = (session: ImageJobSession, signal: AbortSignal) => !signal.aborted && ports.isCurrent(session);

  const cancelAll = () => {
    active.forEach((controller) => controller.abort());
    active.clear();
    pendingResults.clear();
    handledResults.clear();
  };

  const watch = async (job: ImageJobSummary, session: ImageJobSession): Promise<void> => {
    if (!ports.isCurrent(session) || active.has(job.task_id)) return;
    const controller = new AbortController();
    active.set(job.task_id, controller);
    try {
      const latest = await ports.poll(job.task_id, {
        signal: controller.signal,
        initialJob: job,
        onUpdate: async (next) => {
          if (!isActive(session, controller.signal)) return;
          ports.onJobUpdate(next, session);
          if (!(next.image_url || next.result_url)
            || handledResults.has(next.task_id)
            || pendingResults.has(next.task_id)) return;

          pendingResults.set(next.task_id, controller);
          let imageUrl: string | null = null;
          let delivered = false;
          try {
            imageUrl = await ports.resolveResult(next, controller.signal, session);
            if (!imageUrl) return;
            if (!isActive(session, controller.signal)) return;
            const accepted = ports.onImageResult(next, imageUrl, session);
            handledResults.add(next.task_id);
            delivered = accepted;
          } catch (error) {
            if (isActive(session, controller.signal)) ports.onResultError(error, next, session);
          } finally {
            if (imageUrl && !delivered) ports.releaseResult(imageUrl);
            if (pendingResults.get(next.task_id) === controller) pendingResults.delete(next.task_id);
          }
        },
      });
      if (isActive(session, controller.signal)) ports.onComplete(latest, session);
    } catch (error) {
      if (isActive(session, controller.signal)) ports.onPollError(error, job, session);
    } finally {
      if (active.get(job.task_id) === controller) active.delete(job.task_id);
    }
  };

  return {
    watch,
    cancelAll,
  };
}

export const defaultImageJobPoller = pollImageJob;
