import { describe, expect, it } from 'vitest';
import {
  applyStreamingDraft,
  float32ToPcm16,
  mergeVoiceSegments,
  resampleFloat32,
} from './voice-stream';

describe('voice stream helpers', () => {
  it('merges Chinese streaming segments without injecting spaces', () => {
    expect(mergeVoiceSegments('我觉得', '语音输入')).toBe('我觉得语音输入');
    expect(mergeVoiceSegments('hello', 'world')).toBe('hello world');
    expect(mergeVoiceSegments('', '开始')).toBe('开始');
  });

  it('replaces only the original selection while partial text grows', () => {
    expect(applyStreamingDraft('前面后面', '中间', 2, 2)).toEqual({
      value: '前面中间后面',
      cursor: 4,
    });
    expect(applyStreamingDraft('旧内容', '实时中文', 0, 3)).toEqual({
      value: '实时中文',
      cursor: 4,
    });
  });

  it('downsamples 48kHz audio to 16kHz', () => {
    const input = new Float32Array(4800).fill(0.25);
    const output = resampleFloat32(input, 48_000);
    expect(output.length).toBe(1600);
    expect(output[0]).toBeCloseTo(0.25);
  });

  it('converts float samples to signed 16-bit pcm', () => {
    const pcm = new Int16Array(float32ToPcm16(new Float32Array([-1, 0, 1])));
    expect(Array.from(pcm)).toEqual([-32767, 0, 32767]);
  });
});

// Synthetic AudioContext / WebSocket fixtures only; no device microphone or real ASR.
import { afterEach, beforeEach, vi } from 'vitest';
import { startVoiceStream } from './voice-stream';
const owner = vi.hoisted(() => ({ bootstrap: vi.fn(), token: vi.fn() }));
vi.mock('./media-client', () => ({ ensureMediaSession: owner.bootstrap, getStoredMediaToken: owner.token }));
let ready = true;
let sockets: FakeSocket[] = [];
let contexts: FakeContext[] = [];
let stopTrack = vi.fn();
const audioNode = () => ({ connect: vi.fn(), disconnect: vi.fn() });
class FakeContext {
  state = 'running'; sampleRate = 16000; destination = {};
  resume = vi.fn(async () => undefined);
  close = vi.fn(async () => { this.state = 'closed'; });
  createMediaStreamSource = vi.fn(audioNode);
  createScriptProcessor = vi.fn(() => ({ ...audioNode(), onaudioprocess: null }));
  createGain = vi.fn(() => ({ ...audioNode(), gain: { value: 0 } }));
  constructor() { contexts.push(this); }
}
class FakeSocket {
  static OPEN = 1;
  readyState = 1; binaryType = ''; onmessage: ((event: any) => void) | null = null;
  onclose: ((event: any) => void) | null = null; onerror: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn(() => { this.readyState = 3; this.onclose?.({ code: 1000, reason: '' }); });
  constructor(readonly url: string, readonly protocols: string[]) {
    sockets.push(this);
    if (ready) queueMicrotask(() => this.onmessage?.({ data: '{"type":"ready"}' }));
  }
}
const access = async () => ({ url: 'wss://fixture.invalid/api/voice-stream', protocols: ['feedback-bearer', 'synthetic-ticket'] });
const ownerAccess = async () => ({ url: 'wss://fixture.invalid/api/voice-stream', protocols: ['media-bearer', 'synthetic-owner'] });
const callbacks = () => ({ onPartial: vi.fn(), onFinal: vi.fn(), onError: vi.fn() });
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
beforeEach(() => {
  ready = true; sockets = []; contexts = []; stopTrack = vi.fn(); vi.clearAllMocks();
  owner.bootstrap.mockResolvedValue(undefined); owner.token.mockReturnValue('synthetic-owner-token');
  vi.stubGlobal('window', { AudioContext: FakeContext, setTimeout, clearTimeout, location: { protocol: 'https:', host: 'fixture.invalid' } });
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: stopTrack }] })) } });
  vi.stubGlobal('WebSocket', FakeSocket); vi.stubGlobal('CloseEvent', class extends Event {});
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
describe('voice stream auth injection and cancellation', () => {
  it('uses submit-only ticket and does not bootstrap/read owner credentials', async () => {
    const cb = callbacks(); const session = await startVoiceStream(cb, { getAccess: access });
    expect(sockets[0].protocols).toEqual(['feedback-bearer', 'synthetic-ticket']);
    expect(owner.bootstrap).not.toHaveBeenCalled(); expect(owner.token).not.toHaveBeenCalled();
    sockets[0].onmessage?.({ data: '{"type":"final","text":"合成转写"}' });
    expect(cb.onFinal).toHaveBeenCalledWith('合成转写');
    await session.cancel(); expect(stopTrack).toHaveBeenCalledTimes(1); expect(contexts[0].close).toHaveBeenCalled();
  });
  it('keeps the existing owner bearer path when no access provider is injected', async () => {
    const session = await startVoiceStream(callbacks());
    expect(owner.bootstrap).toHaveBeenCalledTimes(1); expect(sockets[0].protocols).toEqual(['media-bearer', 'synthetic-owner-token']);
    await session.cancel();
  });
  it('settles cancellation while waiting for WebSocket ready and stops all resources', async () => {
    ready = false; const controller = new AbortController();
    const pending = startVoiceStream(callbacks(), { getAccess: access, signal: controller.signal });
    const check = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    await tick(); expect(sockets).toHaveLength(1); controller.abort(); await check;
    expect(stopTrack).toHaveBeenCalled(); expect(contexts[0].state).toBe('closed'); expect(sockets[0].readyState).toBe(3);
  });
  it('cancels pending microphone permission and stops a late granted stream', async () => {
    let grant!: (stream: any) => void;
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn(() => new Promise(resolve => { grant = resolve; })) } });
    const controller = new AbortController(); const pending = startVoiceStream(callbacks(), { getAccess: access, signal: controller.signal });
    const check = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    await tick(); controller.abort(); grant({ getTracks: () => [{ stop: stopTrack }] }); await check;
    expect(stopTrack).toHaveBeenCalledTimes(1); expect(sockets).toHaveLength(0);
  });
  it('closes AudioContext and never falls back to owner credentials when ticket is denied', async () => {
    await expect(startVoiceStream(callbacks(), { getAccess: async () => { throw new Error('fixture ticket denied'); } })).rejects.toThrow('fixture ticket denied');
    expect(contexts[0].close).toHaveBeenCalled(); expect(owner.bootstrap).not.toHaveBeenCalled(); expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  });
});

describe('ready timeout recovery', () => {
  it('reconnects once without acquiring a second microphone or starting audio before ready', async () => {
    vi.useFakeTimers(); window.setTimeout = setTimeout; window.clearTimeout = clearTimeout; ready = false;
    const cb = callbacks(); const diagnostics = vi.fn();
    const starting = startVoiceStream({ ...cb, onDiagnostic: diagnostics }, { getAccess: ownerAccess });
    await tick();
    expect(contexts[0].createScriptProcessor).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(5000);
    expect(sockets).toHaveLength(2);
    expect(sockets[0].readyState).toBe(3);
    // A late ready from the discarded connection must not start recording.
    sockets[0].onmessage?.({ data: '{"type":"ready"}' });
    expect(contexts[0].createScriptProcessor).not.toHaveBeenCalled();
    sockets[1].onmessage?.({ data: '{"type":"ready"}' });
    const session = await starting;
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1);
    expect(contexts).toHaveLength(1);
    expect(contexts[0].createScriptProcessor).toHaveBeenCalledTimes(1);
    // Synthetic PCM reaches only the ready connection, in order, without duplication.
    const processor = contexts[0].createScriptProcessor.mock.results[0].value;
    const samples = Float32Array.from({ length: 3200 }, (_, i) => Math.sin(i / 20) * 0.25);
    (processor.onaudioprocess as any)({ inputBuffer: { getChannelData: () => samples } });
    expect(sockets[0].send).not.toHaveBeenCalled();
    expect(sockets[1].send).toHaveBeenCalledTimes(2);
    const sent = sockets[1].send.mock.calls.flatMap(([buffer]) => Array.from(new Int16Array(buffer)));
    expect(sent).toEqual(Array.from(new Int16Array(float32ToPcm16(samples))));
    sockets[1].onmessage?.({ data: '{"type":"partial","text":"合成"}' });
    sockets[1].onmessage?.({ data: '{"type":"final","text":"合成文字"}' });
    expect(cb.onPartial).toHaveBeenCalledWith('合成');
    expect(cb.onFinal).toHaveBeenCalledExactlyOnceWith('合成文字');
    await session.cancel();
    const report = diagnostics.mock.calls[diagnostics.mock.calls.length - 1][0];
    expect(report.reconnected).toBe(true);
    expect(report.events.map((e: any) => e.stage)).toEqual(['start', 'timeout', 'close', 'reconnect', 'start', 'ready', 'close']);
    expect(JSON.stringify(report)).not.toMatch(/synthetic-ticket|fixture.invalid|合成|token|protocols/);
  });
  it('reports retry after two timeouts and cleans up without recording or editing text', async () => {
    vi.useFakeTimers(); window.setTimeout = setTimeout; window.clearTimeout = clearTimeout; ready = false;
    const cb = callbacks();
    const starting = startVoiceStream(cb, { getAccess: ownerAccess });
    const rejected = expect(starting).rejects.toThrow('实时语音连接超时，已重连一次，请重试');
    await vi.advanceTimersByTimeAsync(10000); await rejected;
    expect(sockets).toHaveLength(2);
    expect(sockets.every(s => s.readyState === 3)).toBe(true);
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1);
    expect(contexts[0].createScriptProcessor).not.toHaveBeenCalled();
    expect(stopTrack).toHaveBeenCalledTimes(1);
    expect(cb.onPartial).not.toHaveBeenCalled(); expect(cb.onFinal).not.toHaveBeenCalled();
  });
  it('does not reconnect after cancellation', async () => {
    vi.useFakeTimers(); window.setTimeout = setTimeout; window.clearTimeout = clearTimeout; ready = false;
    const controller = new AbortController();
    const starting = startVoiceStream(callbacks(), { getAccess: ownerAccess, signal: controller.signal });
    const rejected = expect(starting).rejects.toMatchObject({ name: 'AbortError' });
    await tick(); controller.abort(); await rejected;
    await vi.advanceTimersByTimeAsync(10000); expect(sockets).toHaveLength(1);
  });
});

describe('diagnostic boundaries', () => {
  it('records open, ready and numeric close code without server reason', async () => {
    ready = false; const diagnostic = vi.fn();
    Object.assign(navigator, { connection: { type: 'wifi', effectiveType: '4g' } });
    const starting = startVoiceStream({ ...callbacks(), onDiagnostic: diagnostic }, { getAccess: access });
    await tick();
    (sockets[0] as any).onopen?.({});
    sockets[0].onmessage?.({ data: '{"type":"ready"}' });
    const session = await starting;
    sockets[0].onclose?.({ code: 4401, reason: 'synthetic-sensitive-reason' });
    const report = diagnostic.mock.calls[diagnostic.mock.calls.length - 1][0];
    expect(report.network).toBe('wifi/4g'); expect(report.reconnected).toBe(false);
    expect(report.events.map((e: any) => e.stage)).toEqual(['start', 'open', 'ready', 'close']);
    expect(report.events[3].closeCode).toBe(4401);
    expect(JSON.stringify(report)).not.toContain('sensitive');
    expect(report.events.every((e: any) => Number.isFinite(e.elapsedMs) && e.elapsedMs >= 0)).toBe(true);
    await session.cancel();
  });
  it('fails immediately on server close without retrying authentication failure', async () => {
    ready = false; const starting = startVoiceStream(callbacks(), { getAccess: access });
    const rejected = expect(starting).rejects.toThrow('实时语音连接失败');
    await tick(); sockets[0].onclose?.({ code: 4401, reason: '' }); await rejected;
    expect(sockets).toHaveLength(1);
  });
  it('cancels the second handshake without starting audio or further retries', async () => {
    vi.useFakeTimers(); window.setTimeout = setTimeout; window.clearTimeout = clearTimeout; ready = false;
    const controller = new AbortController();
    const starting = startVoiceStream(callbacks(), { getAccess: ownerAccess, signal: controller.signal });
    const rejected = expect(starting).rejects.toMatchObject({ name: 'AbortError' });
    await vi.advanceTimersByTimeAsync(5000); controller.abort(); await rejected;
    await vi.advanceTimersByTimeAsync(10000);
    expect(sockets).toHaveLength(2); expect(contexts[0].createScriptProcessor).not.toHaveBeenCalled();
  });
});


it('does not replay a single-use feedback ticket on ready timeout', async () => {
  vi.useFakeTimers(); window.setTimeout = setTimeout; window.clearTimeout = clearTimeout; ready = false;
  const starting = startVoiceStream(callbacks(), { getAccess: access });
  const rejected = expect(starting).rejects.toThrow('实时语音连接超时，请重试');
  await vi.advanceTimersByTimeAsync(5000); await rejected;
  expect(sockets).toHaveLength(1);
  expect(owner.bootstrap).not.toHaveBeenCalled(); expect(owner.token).not.toHaveBeenCalled();
  expect(contexts[0].createScriptProcessor).not.toHaveBeenCalled();
});
