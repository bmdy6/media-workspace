import { afterEach, describe, expect, it, vi } from 'vitest';
import { clearMediaToken } from './media-client';
import { consumeMediaEventStream, streamMediaChat } from './media-stream';

function streamFrom(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  });
}

function authStorage(token: string) {
  const values = new Map([['media_workspace_token', token]]);
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
}

afterEach(() => { clearMediaToken(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('media chat event stream', () => {
  it('parses split UTF-8 and event delimiters, returning one final response', async () => {
    const encoded = new TextEncoder().encode(
      'event: status\ndata: {"phase":"thinking","label":"正在构思"}\n\n'
      + 'event: delta\ndata: {"text":"你好，世界 🌱"}\n\n'
      + 'event: done\ndata: {"reply_text":"你好，世界 🌱","proposal_card":null,"model_id":"chat-a"}\n\n'
      + 'event: delta\ndata: {"text":"should be ignored"}\n\n',
    );
    const labelIndex = encoded.indexOf(new TextEncoder().encode('界')[0]);
    const deltas: string[] = [];
    const statuses: string[] = [];
    const response = await consumeMediaEventStream(new ReadableStream({
      start(controller) {
        controller.enqueue(encoded.slice(0, labelIndex + 1));
        controller.enqueue(encoded.slice(labelIndex + 1, labelIndex + 2));
        controller.enqueue(encoded.slice(labelIndex + 2));
        controller.close();
      },
    }), { onDelta: (text) => deltas.push(text), onStatus: (status) => statuses.push(status.label || '') });

    expect(statuses).toEqual(['正在构思']);
    expect(deltas).toEqual(['你好，世界 🌱']);
    expect(response).toMatchObject({ reply_text: '你好，世界 🌱', model_id: 'chat-a' });
  });

  it('parses a final event without a trailing blank line and rejects EOF without done', async () => {
    await expect(consumeMediaEventStream(streamFrom(['event: done\ndata: {"reply_text":"完成","proposal_card":null}'])))
      .resolves.toMatchObject({ reply_text: '完成' });
    await expect(consumeMediaEventStream(streamFrom(['event: delta\ndata: {"text":"部分"}'])))
      .rejects.toThrow('未收到完成事件');
  });

  it('surfaces server error events and aborts an unfinished reader', async () => {
    await expect(consumeMediaEventStream(streamFrom(['event: error\ndata: {"message":"服务拒绝"}\n\n'])))
      .rejects.toThrow('服务拒绝');
    const controller = new AbortController();
    const openStream = new ReadableStream<Uint8Array>({ start() {} });
    const pending = consumeMediaEventStream(openStream, { signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('fails malformed events and cancels upstream as soon as a valid done arrives', async () => {
    await expect(consumeMediaEventStream(streamFrom(['event: delta\ndata: {"text":7}\n\n'])))
      .rejects.toThrow('delta 事件格式无效');
    await expect(consumeMediaEventStream(streamFrom(['event: done\ndata: {"reply_text":"缺card字段"}\n\n'])))
      .rejects.toThrow('done 事件格式无效');
    const controller = new AbortController(); controller.abort();
    await expect(consumeMediaEventStream(streamFrom([]), { signal: controller.signal }))
      .rejects.toMatchObject({ name: 'AbortError' });

    let cancelled = false;
    const openAfterDone = new ReadableStream<Uint8Array>({
      start(streamController) {
        streamController.enqueue(new TextEncoder().encode('event: done\ndata: {"reply_text":"完成","proposal_card":null}\n\n'));
      },
      cancel() { cancelled = true; },
    });
    await expect(consumeMediaEventStream(openAfterDone)).resolves.toMatchObject({ reply_text: '完成' });
    expect(cancelled).toBe(true);
  });

  it('rejects an unterminated oversized event instead of buffering without limit', async () => {
    await expect(consumeMediaEventStream(streamFrom(['x'.repeat(1024 * 1024 + 1)])))
      .rejects.toThrow('超过大小限制');
  });

  it('sends the authenticated stream request once with the selected chat payload', async () => {
    authStorage('stream-token');
    const responseStream = streamFrom(['event: done\ndata: {"reply_text":"好了","proposal_card":null}\n\n']);
    const fetchMock = vi.fn().mockResolvedValue(new Response(responseStream, { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const payload = {
      session_id: 'session-3', message: '画一幅海报', current_image_url: null,
      model_id: 'chat-a', thinking_mode: 'low', image_model_id: 'image-b', reference_case_id: 'case-7',
    };

    await streamMediaChat(payload);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/chat/stream');
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer stream-token');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(payload);
  });
});
