import { describe, expect, it, vi } from 'vitest';
import {
  createImageJobLifecycleController,
  createImageJobResultMessage,
  imageJobResultMessageDisposition,
  updateImageJobResultMessages,
  type ImageJobLifecyclePorts,
} from './controller';
import type { ImageJobSummary } from '../../../lib/media-jobs';

const session = { sessionId: 'session-a', epoch: 3, authToken: 'token-a' };
const queuedJob: ImageJobSummary = {
  id: 11, task_id: 'task-a', status: 'queued', phase: 'queued',
  poll_interval_ms: 300, poll_timeout_ms: 10_000,
};

function makePorts(overrides: Partial<ImageJobLifecyclePorts> = {}): ImageJobLifecyclePorts {
  return {
    isCurrent: () => true,
    poll: async () => queuedJob,
    resolveResult: async () => 'blob:result-a',
    releaseResult: vi.fn(),
    onJobUpdate: vi.fn(),
    onImageResult: () => true,
    onResultError: vi.fn(),
    onComplete: vi.fn(),
    onPollError: vi.fn(),
    ...overrides,
  };
}

describe('image job lifecycle controller', () => {
  it('reports preparation through background persistence and emits the first image only once', async () => {
    const preparing = { ...queuedJob, status: 'generating', phase: 'preparing' };
    const generating = { ...queuedJob, status: 'generating', phase: 'generating' };
    const resultReady = {
      ...queuedJob, status: 'success', phase: 'result_ready', image_url: 'https://cdn.invalid/result.png', persistence_status: 'pending',
      requested_model_id: 'requested-model', model_id: 'actual-model',
    };
    const persisting = { ...resultReady, phase: 'persisting' };
    const stored = { ...resultReady, phase: 'completed', persistence_status: 'stored' };
    const onImageResult = vi.fn(() => true);
    const updates: ImageJobSummary[] = [];
    const ports = makePorts({
      poll: async (_taskId, options) => {
        await options.onUpdate(preparing);
        await options.onUpdate(generating);
        await options.onUpdate(resultReady);
        await options.onUpdate(persisting);
        await options.onUpdate(resultReady);
        await options.onUpdate(stored);
        return stored;
      },
      onJobUpdate: (job) => { updates.push(job); },
      onImageResult,
    });
    const controller = createImageJobLifecycleController(ports);

    await controller.watch(queuedJob, session);

    expect(onImageResult).toHaveBeenCalledTimes(1);
    expect(onImageResult).toHaveBeenCalledWith(resultReady, 'blob:result-a', session);
    expect(updates.map((job) => job.phase)).toEqual(['preparing', 'generating', 'result_ready', 'persisting', 'result_ready', 'completed']);
    expect(ports.onComplete).toHaveBeenCalledWith(stored, session);
    const readyMessage = createImageJobResultMessage(resultReady, 'blob:result-a');
    expect(readyMessage.content).toContain('实际模型：actual-model');
    expect(readyMessage.requested_model_id).toBe('requested-model');
    expect(updateImageJobResultMessages([readyMessage], stored)[0].image_url).toBe('blob:result-a');
  });

  it('keeps the ready image visible as persistence becomes stored or failed', () => {
    const resultReady = { ...queuedJob, status: 'succeeded', phase: 'result_ready', image_url: 'blob:ready-image', persistence_status: 'pending' };
    const readyMessage = createImageJobResultMessage(resultReady, resultReady.image_url!);
    const stored = { ...resultReady, phase: 'completed', persistence_status: 'stored' };
    const failed = { ...resultReady, phase: 'completed', persistence_status: 'failed', is_temporary: true, expires_at: '2026-10-12' };

    const storedMessage = updateImageJobResultMessages([readyMessage], stored)[0];
    const failedMessage = updateImageJobResultMessages([readyMessage], failed)[0];

    expect(storedMessage.image_url).toBe('blob:ready-image');
    expect(storedMessage.content).toContain('已保存');
    expect(storedMessage.persistence_status).toBe('stored');
    expect(failedMessage.image_url).toBe('blob:ready-image');
    expect(failedMessage.content).toContain('转存失败，临时原图仍可下载');
    expect(failedMessage.content).toContain('2026-10-12');
    expect(failedMessage.persistence_status).toBe('failed');
  });

  it('recognizes duplicate result task IDs and retains an already-owned identical image URL', () => {
    const existing = createImageJobResultMessage({ ...queuedJob, id: 11 }, 'blob:owned-image');
    const duplicateTask = { ...queuedJob, id: 12 };

    expect(imageJobResultMessageDisposition([existing], duplicateTask, 'blob:new-image')).toBe('duplicate');
    expect(imageJobResultMessageDisposition([existing], { ...queuedJob, id: 12 }, 'blob:owned-image')).toBe('same-image');
  });

  it('recognizes original parent ID duplicates even when the result task IDs differ', () => {
    const originalParentResult = createImageJobResultMessage({ ...queuedJob, task_id: 'task-original', id: 11 }, 'blob:original-image');
    const resumedTask = { ...queuedJob, task_id: 'task-resumed', id: 11 };

    expect(imageJobResultMessageDisposition([originalParentResult], resumedTask, 'blob:resumed-image')).toBe('duplicate');
    expect(imageJobResultMessageDisposition([originalParentResult], resumedTask, 'blob:original-image')).toBe('same-image');
  });

  it('releases an image URL when the result callback rejects an already-present duplicate', async () => {
    const releaseResult = vi.fn();
    const onImageResult = vi.fn(() => false);
    const resultReady = { ...queuedJob, status: 'succeeded', phase: 'result_ready', result_url: '/api/image/result/task-a' };
    const ports = makePorts({
      poll: async (_taskId, options) => {
        await options.onUpdate(resultReady);
        return resultReady;
      },
      resolveResult: async () => 'blob:duplicate-result',
      releaseResult,
      onImageResult,
    });

    await createImageJobLifecycleController(ports).watch(queuedJob, session);

    expect(onImageResult).toHaveBeenCalledWith(resultReady, 'blob:duplicate-result', session);
    expect(releaseResult).toHaveBeenCalledTimes(1);
    expect(releaseResult).toHaveBeenCalledWith('blob:duplicate-result');
  });

  it('does not release an identical image URL that the result callback already owns', async () => {
    const releaseResult = vi.fn();
    const resultReady = { ...queuedJob, status: 'succeeded', phase: 'result_ready', result_url: '/api/image/result/task-a' };
    const ports = makePorts({
      poll: async (_taskId, options) => {
        await options.onUpdate(resultReady);
        return resultReady;
      },
      resolveResult: async () => 'blob:owned-result',
      releaseResult,
      onImageResult: () => true,
    });

    await createImageJobLifecycleController(ports).watch(queuedJob, session);

    expect(releaseResult).not.toHaveBeenCalled();
  });

  it('releases an asynchronously resolved image when its session is stale', async () => {
    let current = true;
    let resolveResult!: (url: string) => void;
    const onImageResult = vi.fn();
    const releaseResult = vi.fn();
    const ports = makePorts({
      isCurrent: () => current,
      poll: async (_taskId, options) => {
        await options.onUpdate({ ...queuedJob, status: 'succeeded', phase: 'result_ready', result_url: '/api/image/result/task-a' });
        return queuedJob;
      },
      resolveResult: () => new Promise((resolve) => { resolveResult = resolve; }),
      releaseResult,
      onImageResult,
    });
    const controller = createImageJobLifecycleController(ports);
    const watching = controller.watch(queuedJob, session);

    await Promise.resolve();
    await Promise.resolve();
    current = false;
    resolveResult('blob:late-result');
    await watching;

    expect(onImageResult).not.toHaveBeenCalled();
    expect(releaseResult).toHaveBeenCalledWith('blob:late-result');
    expect(ports.onComplete).not.toHaveBeenCalled();
  });

  it('cancels active polling and suppresses completion callbacks', async () => {
    let pollSignal: AbortSignal | undefined;
    const onComplete = vi.fn();
    const onPollError = vi.fn();
    const ports = makePorts({
      poll: (_taskId, options) => new Promise((_resolve, reject) => {
        pollSignal = options.signal;
        options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true });
      }),
      onComplete,
      onPollError,
    });
    const controller = createImageJobLifecycleController(ports);
    const watching = controller.watch(queuedJob, session);

    await Promise.resolve();
    controller.cancelAll();
    await watching;

    expect(pollSignal?.aborted).toBe(true);
    expect(onComplete).not.toHaveBeenCalled();
    expect(onPollError).not.toHaveBeenCalled();
  });
});
