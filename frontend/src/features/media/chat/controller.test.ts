import { describe, expect, it, vi } from 'vitest';
import type { ChatStreamInput, StreamedChatResponse } from '../../../lib/media-stream';
import {
  createMediaChatLifecycleController,
  type MediaChatCommand,
  type MediaChatLifecyclePorts,
  type MediaChatStreamOptions,
} from './controller';

const session = { sessionId: 'session-a', epoch: 4, authToken: 'token-a' };
const input: ChatStreamInput = {
  session_id: 'session-a', message: '做一张产品海报', current_image_url: null,
  current_image_role: null, current_image_task_id: null, resume_after_reference: false,
  model_id: 'chat-model', thinking_mode: 'low', image_model_id: 'image-model', reference_case_id: null,
};
const command: MediaChatCommand = {
  session, assistantId: 'assistant-stream-1', message: input.message, suppressUserEcho: false, input,
};
const completed: StreamedChatResponse = { reply_text: '完成', proposal_card: null, model_id: 'chat-model' };

function makePorts(overrides: Partial<MediaChatLifecyclePorts> = {}): MediaChatLifecyclePorts {
  return {
    isCurrent: () => true,
    stream: async () => completed,
    onStart: vi.fn(),
    onStatus: vi.fn(),
    onDelta: vi.fn(),
    onComplete: vi.fn(),
    onError: vi.fn(),
    onFinish: vi.fn(),
    ...overrides,
  };
}

describe('media chat lifecycle controller', () => {
  it('keeps the selected payload unchanged and reports ordered status, deltas, completion, and cleanup', async () => {
    const events: string[] = [];
    const stream = vi.fn(async (_input: ChatStreamInput, options: MediaChatStreamOptions) => {
      options.onStatus({ type: 'status', phase: 'thinking', label: '正在构思' });
      options.onDelta('你好');
      options.onDelta('，世界');
      return completed;
    });
    const ports = makePorts({
      stream,
      onStart: () => { events.push('start'); },
      onStatus: (status) => { events.push(status.label || status.phase || 'status'); },
      onDelta: (_delta, accumulated) => { events.push(accumulated); },
      onComplete: (_response, accumulated) => { events.push(`done:${accumulated}`); },
      onFinish: () => { events.push('finish'); },
    });

    expect(await createMediaChatLifecycleController(ports).send(command)).toBe(true);
    expect(stream).toHaveBeenCalledTimes(1);
    expect(stream.mock.calls[0][0]).toEqual(input);
    expect(stream.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
    expect(events).toEqual(['start', '正在构思', '你好', '你好，世界', 'done:你好，世界', 'finish']);
  });

  it('retains partial text for an explicit error result without inventing a final response', async () => {
    const onError = vi.fn();
    const onComplete = vi.fn();
    const onFinish = vi.fn();
    const stream = vi.fn(async (_input: ChatStreamInput, options: MediaChatStreamOptions) => {
      options.onDelta('已收到部分内容');
      throw new Error('stream interrupted');
    });

    expect(await createMediaChatLifecycleController(makePorts({ stream, onError, onComplete, onFinish })).send(command)).toBe(false);
    expect(stream).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith(expect.any(Error), '已收到部分内容', command);
    expect(onError.mock.calls[0][0]).toMatchObject({ message: 'stream interrupted' });
    expect(onComplete).not.toHaveBeenCalled();
    expect(onFinish).toHaveBeenCalledWith(command);
  });

  it('finishes the failed attempt and allows a later explicit send to recover', async () => {
    const events: string[] = [];
    let attempts = 0;
    const stream = vi.fn(async (_input: ChatStreamInput, options: MediaChatStreamOptions) => {
      attempts += 1;
      if (attempts === 1) {
        options.onStatus({ type: 'status', phase: 'connecting', label: '正在连接对话流…' });
        throw new Error('temporary stream failure');
      }
      options.onStatus({ type: 'status', phase: 'thinking', label: '正在生成回复…' });
      options.onDelta('已恢复');
      return completed;
    });
    const ports = makePorts({
      stream,
      onStart: () => { events.push('loading'); },
      onStatus: (status) => { events.push(`status:${status.phase}`); },
      onError: () => { events.push('error'); },
      onDelta: (_delta, accumulated) => { events.push(`delta:${accumulated}`); },
      onComplete: () => { events.push('complete'); },
      onFinish: () => { events.push('finish'); },
    });
    const controller = createMediaChatLifecycleController(ports);

    expect(await controller.send(command)).toBe(false);
    expect(await controller.send({ ...command, assistantId: 'assistant-stream-2' })).toBe(true);

    expect(stream).toHaveBeenCalledTimes(2);
    expect(events).toEqual([
      'loading', 'status:connecting', 'error', 'finish',
      'loading', 'status:thinking', 'delta:已恢复', 'complete', 'finish',
    ]);
  });

  it('cancels an active stream and suppresses its late deltas, errors, and completion', async () => {
    let options!: MediaChatStreamOptions;
    const stream = vi.fn((_input: ChatStreamInput, received: MediaChatStreamOptions) => {
      options = received;
      return new Promise<StreamedChatResponse>((_resolve, reject) => {
        received.signal.addEventListener('abort', () => reject(received.signal.reason), { once: true });
      });
    });
    const onDelta = vi.fn();
    const onError = vi.fn();
    const onComplete = vi.fn();
    const onFinish = vi.fn();
    const controller = createMediaChatLifecycleController(makePorts({ stream, onDelta, onError, onComplete, onFinish }));
    const pending = controller.send(command);

    controller.cancelAll();
    options.onDelta('迟到内容');

    expect(await pending).toBe(false);
    expect(options.signal.aborted).toBe(true);
    expect(onDelta).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
    expect(onComplete).not.toHaveBeenCalled();
    expect(onFinish).not.toHaveBeenCalled();
  });

  it('suppresses streamed updates and final callbacks when the session epoch becomes stale', async () => {
    let current = true;
    let options!: MediaChatStreamOptions;
    let finishStream!: (response: StreamedChatResponse) => void;
    const stream = vi.fn((_input: ChatStreamInput, received: MediaChatStreamOptions) => {
      options = received;
      return new Promise<StreamedChatResponse>((resolve) => { finishStream = resolve; });
    });
    const onStatus = vi.fn();
    const onDelta = vi.fn();
    const onComplete = vi.fn();
    const onFinish = vi.fn();
    const controller = createMediaChatLifecycleController(makePorts({
      isCurrent: () => current, stream, onStatus, onDelta, onComplete, onFinish,
    }));
    const pending = controller.send(command);

    current = false;
    options.onStatus({ type: 'status', phase: 'thinking', label: '迟到状态' });
    options.onDelta('迟到片段');
    finishStream(completed);

    expect(await pending).toBe(false);
    expect(onStatus).not.toHaveBeenCalled();
    expect(onDelta).not.toHaveBeenCalled();
    expect(onComplete).not.toHaveBeenCalled();
    expect(onFinish).not.toHaveBeenCalled();
  });
});
