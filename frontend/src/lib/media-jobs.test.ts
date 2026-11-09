import { afterEach, describe, expect, it, vi } from 'vitest';
import { clearMediaToken } from './media-client';
import { fetchOriginalImageResult, getImageResultUrl, imageJobCompletionNotice, isFailedImageJob, pollImageJob, stableImageRequestId, submitImageJob, upsertImageJob } from './media-jobs';

function setupAuth() {
  const values = new Map([['media_workspace_token', 'job-token']]);
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
}

afterEach(() => { clearMediaToken(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('media image jobs', () => {
  it('describes local retention without falsely reporting a failed COS upload', () => {
    const job = { id:1,task_id:'owned',status:'success',poll_interval_ms:1,poll_timeout_ms:1,is_temporary:true,persistence_status:'not_requested',storage_policy:'direct' };
    expect(imageJobCompletionNotice(job)).toContain('本地临时保存');
    expect(imageJobCompletionNotice(job)).not.toContain('转存未完成');
    expect(imageJobCompletionNotice({...job,persistence_status:'failed'})).toContain('转存未完成');
  });
  it('keeps an intent ID for unchanged explicit retries and rotates it when the payload changes', () => {
    const requests = new Map<string, { fingerprint: string; requestId: string }>();
    const create = vi.fn().mockReturnValueOnce('request-a').mockReturnValueOnce('request-b');
    expect(stableImageRequestId(requests, 'card-1', 'prompt/3:4/low', create)).toBe('request-a');
    expect(stableImageRequestId(requests, 'card-1', 'prompt/3:4/low', create)).toBe('request-a');
    expect(stableImageRequestId(requests, 'card-1', 'edited/3:4/low', create)).toBe('request-b');
    expect(create).toHaveBeenCalledTimes(2);
  });

  it('keeps failed jobs visible and replaces an older failed attempt when the same proposal retries', () => {
    const failed = { id: 1, task_id: 'failed-1', status: 'failed', phase: 'failed', proposal_card_id: 'card-1', poll_interval_ms: 300, poll_timeout_ms: 240_000 };
    const running = { id: 2, task_id: 'running-2', status: 'generating', phase: 'generating', proposal_card_id: 'card-1', poll_interval_ms: 300, poll_timeout_ms: 240_000 };

    expect(isFailedImageJob(failed)).toBe(true);
    expect(isFailedImageJob(running)).toBe(false);
    expect(upsertImageJob([failed], running)).toEqual([running]);
  });

  it('submits one exact-prompt task with request ID and owned parent ID', async () => {
    setupAuth();
    const result = { id: 91, task_id: 'task-91', status: 'queued', phase: 'queued', poll_interval_ms: 300, poll_timeout_ms: 240_000 };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(result), { status: 202 }));
    vi.stubGlobal('fetch', fetchMock);
    const input = {
      prompt: '改过的最终提示词\n保留换行', model_id: 'image-a', style: 'poster',
      aspect_ratio: '3:4', quality: 'standard', storage_policy: 'cos', session_id: 'session-a',
      request_id: 'request-stable', parent_id: 21,
    };

    const submitted = await submitImageJob(input);

    expect(submitted).toEqual(result);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/image/jobs');
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer job-token');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({
      prompt: input.prompt, final_prompt: input.prompt, request_id: 'request-stable', parent_id: 21, quality: 'low',
    });
  });

  it('keeps the proposal association on the first accepted job even if an older backend omits it', async () => {
    setupAuth();
    const result = { id: 92, task_id: 'task-92', status: 'pending', phase: 'queued', poll_interval_ms: 300, poll_timeout_ms: 240_000 };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(result), { status: 202 })));

    const submitted = await submitImageJob({
      prompt: '立即显示进度', model_id: 'image-a', style: 'poster', aspect_ratio: '1:1', quality: 'low',
      storage_policy: 'background_cos', session_id: 'session-progress', request_id: 'request-progress', proposal_card_id: 'card-progress',
    });

    expect(submitted.proposal_card_id).toBe('card-progress');
  });

  it('does not automatically retry a failed paid submission', async () => {
    setupAuth();
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('connection lost'));
    vi.stubGlobal('fetch', fetchMock);

    await expect(submitImageJob({
      prompt: '一张海报', model_id: 'image-a', style: 'poster', aspect_ratio: '1:1', quality: 'low',
      storage_policy: 'cos', session_id: 'session-a', request_id: 'request-once',
    })).rejects.toThrow('connection lost');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('emits the first temporary result before persistence finishes and bounds polling', async () => {
    setupAuth();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: 7, task_id: 'task-7', status: 'succeeded', phase: 'persisting',
        image_url: 'data:image/png;base64,AA==', is_temporary: true, persistence_status: 'pending', poll_interval_ms: 300, poll_timeout_ms: 240_000,
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: 7, task_id: 'task-7', status: 'succeeded', phase: 'persisting',
        image_url: 'data:image/png;base64,AA==', is_temporary: true, persistence_status: 'pending', poll_interval_ms: 300, poll_timeout_ms: 240_000,
      }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();
    const updates: string[] = [];
    const pending = pollImageJob('task-7', {
      signal: controller.signal,
      initialJob: { id: 7, task_id: 'task-7', status: 'queued', phase: 'queued', poll_interval_ms: 300, poll_timeout_ms: 240_000 },
      maxPolls: 5,
      onUpdate: (job) => { updates.push(job.image_url || ''); if (updates.length === 1) controller.abort(); },
    });

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(updates).toEqual(['data:image/png;base64,AA==']);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('ends on backend terminal persistence states, including temporary originals after transfer failure', async () => {
    setupAuth();
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: 7, task_id: 'task-7', status: 'success', phase: 'result_ready',
      image_url: 'https://cdn.invalid/temp.png', is_temporary: true, persistence_status: 'failed',
      poll_interval_ms: 300, poll_timeout_ms: 240_000,
    }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const result = await pollImageJob('task-7', {
      initialJob: { id: 7, task_id: 'task-7', status: 'queued', phase: 'queued', poll_interval_ms: 300, poll_timeout_ms: 240_000 },
    });
    expect(result.persistence_status).toBe('failed');
    expect(result.is_temporary).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('requires server polling timing and aborts a hung status fetch at the server deadline', async () => {
    setupAuth();
    const fetchMock = vi.fn((_url, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
    }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(pollImageJob('task-no-config')).rejects.toThrow('poll_timeout_ms');
    const initialJob = { id: 8, task_id: 'task-8', status: 'queued', phase: 'queued', poll_interval_ms: 300, poll_timeout_ms: 20 };
    const result = await pollImageJob('task-8', { initialJob });
    expect(result.polling_timed_out).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('maps original-result requests to the configured API and authenticates only that route', async () => {
    setupAuth();
    expect(getImageResultUrl('task/with space')).toContain('/api/image/result/task%2Fwith%20space');
    const expectedBlob = new Blob(['original-png-bytes'], { type: 'image/png' });
    const fetchMock = vi.fn().mockResolvedValue(new Response(expectedBlob, { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const blob = await fetchOriginalImageResult('task-1');

    expect(await blob.text()).toBe('original-png-bytes');
    expect(fetchMock.mock.calls[0][0]).toBe('http://localhost/api/image/result/task-1');
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer job-token');
  });
});
