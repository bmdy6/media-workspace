import { describe, expect, it } from 'vitest';
import {
  consumeDraftRefineStream,
  insertDraftAtSelection,
  parseVoiceDraftResponse,
  pickRecorderMimeType,
  voiceDraftFilename,
} from './voice-draft';

describe('voice draft helpers', () => {
  it('prefers opus webm when supported and falls back to mp4 for Safari-like support', () => {
    expect(pickRecorderMimeType((mime) => mime === 'audio/webm;codecs=opus')).toBe('audio/webm;codecs=opus');
    expect(pickRecorderMimeType((mime) => mime === 'audio/mp4')).toBe('audio/mp4');
    expect(pickRecorderMimeType(() => false)).toBeUndefined();
  });

  it('maps recorder mime types to upload filenames', () => {
    expect(voiceDraftFilename('audio/mp4')).toBe('voice-draft.m4a');
    expect(voiceDraftFilename('audio/webm;codecs=opus')).toBe('voice-draft.webm');
    expect(voiceDraftFilename('audio/ogg')).toBe('voice-draft.ogg');
  });

  it('inserts voice text at the current selection instead of replacing the whole draft', () => {
    expect(insertDraftAtSelection('前面后面', '中间', 2, 2)).toEqual({
      value: '前面中间后面',
      cursor: 4,
    });
    expect(insertDraftAtSelection('旧内容', '新内容', 0, 3)).toEqual({
      value: '新内容',
      cursor: 3,
    });
  });

  it('parses the authenticated backend response including refine fallback warnings', () => {
    expect(parseVoiceDraftResponse({
      trace_id: 'vd_123',
      raw_transcript: '嗯我想先存进去',
      text: '我想先存进去。',
      language: 'zh',
      warnings: ['refine_failed'],
      timings: { asr_ms: 120, refine_ms: 30, total_ms: 150 },
    })).toEqual({
      traceId: 'vd_123',
      rawTranscript: '嗯我想先存进去',
      text: '我想先存进去。',
      language: 'zh',
      warnings: ['refine_failed'],
      timings: { asrMs: 120, refineMs: 30, totalMs: 150 },
    });
  });

  it('rejects malformed responses instead of silently inserting bad data', () => {
    expect(() => parseVoiceDraftResponse({ text: 'x' })).toThrow('语音转写响应格式无效');
  });

  it('consumes streamed polish deltas without requiring textarea mutation', async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('event: delta\ndata: {"text":"我觉得"}\n\n'));
        controller.enqueue(encoder.encode('event: delta\ndata: {"text":"可以做。"}\n\n'));
        controller.enqueue(encoder.encode('event: done\ndata: {"text":"我觉得可以做。","mode":"polish","model_id":"deepseek/deepseek-v4.1-flash","thinking_mode":"off"}\n\n'));
        controller.close();
      },
    });
    const seen: string[] = [];

    const result = await consumeDraftRefineStream(stream, {
      onDelta: (_delta, fullText) => seen.push(fullText),
    });

    expect(seen).toEqual(['我觉得', '我觉得可以做。']);
    expect(result).toEqual({
      text: '我觉得可以做。',
      mode: 'polish',
      modelId: 'deepseek/deepseek-v4.1-flash',
      thinkingMode: 'off',
    });
  });

  it('surfaces safe errors from the polish stream', async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('event: error\ndata: {"message":"文字润色暂时不可用，请稍后重试"}\n\n'));
        controller.close();
      },
    });

    await expect(consumeDraftRefineStream(stream)).rejects.toThrow('文字润色暂时不可用');
  });
});


describe('refine stream rejects incomplete or unusable results', () => {
  const encode = (text: string) => new TextEncoder().encode(text);
  const body = (chunks: Uint8Array[]) => new ReadableStream<Uint8Array>({ start(controller) { chunks.forEach(chunk => controller.enqueue(chunk)); controller.close(); } });
  const done = (text = '合成全文', mode = 'polish') => `event: done\ndata: ${JSON.stringify({ text, mode, model_id: 'synthetic', thinking_mode: 'off' })}\n\n`;

  it.each(['', '   '])('rejects empty done text %j rather than erasing the draft', async (text) => {
    await expect(consumeDraftRefineStream(body([encode(done(text))]))).rejects.toThrow('done');
  });
  it('rejects unknown modes while preserving faithful mode', async () => {
    await expect(consumeDraftRefineStream(body([encode(done('全文', 'untrusted'))]))).rejects.toThrow('done');
    expect((await consumeDraftRefineStream(body([encode(done('全文', 'faithful'))]))).mode).toBe('faithful');
  });
  it('requires the final blank-line delimiter, including at EOF', async () => {
    await expect(consumeDraftRefineStream(body([encode(done().trimEnd())]))).rejects.toThrow('完成事件');
  });
  it('rejects malformed UTF-8 instead of silently inserting replacement characters', async () => {
    await expect(consumeDraftRefineStream(body([encode('event: delta\ndata: {"text":"'), new Uint8Array([0xff]), encode('"}\n\n' + done())]))).rejects.toThrow();
  });
  it('keeps valid split multibyte text and CRLF framing', async () => {
    const bytes = encode(done().replace(/\n/g, '\r\n'));
    const result = await consumeDraftRefineStream(body(Array.from(bytes, byte => new Uint8Array([byte]))));
    expect(result.text).toBe('合成全文');
  });
});
