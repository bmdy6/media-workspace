export interface VoiceConnectionDiagnostic {
  startedAt: string;
  network: string;
  reconnected: boolean;
  elapsedMs: number;
  events: Array<{
    stage: 'start' | 'open' | 'ready' | 'timeout' | 'close' | 'reconnect';
    attempt: number;
    elapsedMs: number;
    closeCode?: number;
  }>;
}

class ReadyTimeoutError extends Error {}

export interface VoiceStreamCallbacks {
  onPartial: (text: string) => void;
  onFinal: (text: string) => void;
  onError?: (message: string) => void;
  onDiagnostic?: (diagnostic: VoiceConnectionDiagnostic) => void;
}

export interface VoiceStreamSession {
  pause(): void;
  resume(): void;
  stop(): Promise<void>;
  cancel(): Promise<void>;
}

export interface VoiceStreamAccess { url: string; protocols: string[] }
export interface VoiceStreamOptions {
  getAccess?: () => Promise<VoiceStreamAccess>;
  signal?: AbortSignal;
}

interface VoiceStreamMessage {
  type?: string;
  text?: string;
  detail?: string;
}

const TARGET_SAMPLE_RATE = 16_000;
const CHUNK_SAMPLES = 1_600;

export function mergeVoiceSegments(committed: string, next: string): string {
  if (!committed) return next;
  if (!next) return committed;
  if (/\s$/.test(committed) || /^\s/.test(next)) return committed + next;
  const asciiBoundary = /[A-Za-z0-9]$/.test(committed) && /^[A-Za-z0-9]/.test(next);
  return committed + (asciiBoundary ? ' ' : '') + next;
}

export function applyStreamingDraft(
  original: string,
  inserted: string,
  start: number,
  end: number,
): { value: string; cursor: number } {
  const safeStart = Math.max(0, Math.min(start, original.length));
  const safeEnd = Math.max(safeStart, Math.min(end, original.length));
  const value = original.slice(0, safeStart) + inserted + original.slice(safeEnd);
  return { value, cursor: safeStart + inserted.length };
}

export function resampleFloat32(
  input: Float32Array,
  inputRate: number,
  outputRate = TARGET_SAMPLE_RATE,
): Float32Array {
  if (inputRate === outputRate) return input.slice();
  const ratio = inputRate / outputRate;
  const length = Math.floor(input.length / ratio);
  const output = new Float32Array(length);
  for (let index = 0; index < length; index += 1) {
    const source = index * ratio;
    const left = Math.floor(source);
    const fraction = source - left;
    const a = input[left] ?? 0;
    const b = input[Math.min(left + 1, input.length - 1)] ?? 0;
    output[index] = a + (b - a) * fraction;
  }
  return output;
}

export function float32ToPcm16(input: Float32Array): ArrayBuffer {
  const pcm = new Int16Array(input.length);
  for (let index = 0; index < input.length; index += 1) {
    const value = Math.max(-1, Math.min(1, input[index]));
    pcm[index] = Math.round(value * 32767);
  }
  return pcm.buffer;
}

function webSocketUrl(): string {
  const configured = import.meta.env.VITE_VOICE_STREAM_WS?.trim();
  if (configured) return configured;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return protocol + '//' + window.location.host + '/api/voice-stream';
}

function messageError(event: Event): string {
  if (event instanceof CloseEvent && event.reason) return event.reason;
  return '实时语音连接失败，请重试';
}

export async function startVoiceStream(
  callbacks: VoiceStreamCallbacks,
  options: VoiceStreamOptions = {},
): Promise<VoiceStreamSession> {
  if (
    typeof window === 'undefined'
    || !navigator.mediaDevices?.getUserMedia
    || typeof WebSocket === 'undefined'
  ) {
    throw new Error('当前浏览器不支持实时语音输入');
  }

  const AudioContextCtor = window.AudioContext
    || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioContextCtor) throw new Error('当前浏览器不支持实时语音输入');

  // AudioContext must be created/resumed while the click still has user activation,
  // otherwise iOS Safari can leave it suspended.
  const context = new AudioContextCtor();
  const started = performance.now();
  const connection = (navigator as Navigator & { connection?: { type?: string; effectiveType?: string } }).connection;
  const network = [connection?.type, connection?.effectiveType]
    .filter((value) => typeof value === 'string' && /^(bluetooth|cellular|ethernet|none|wifi|wimax|other|unknown|slow-2g|2g|3g|4g)$/.test(value))
    .join('/') || 'unknown';
  const diagnostic: VoiceConnectionDiagnostic = {
    startedAt: new Date().toISOString(), network, reconnected: false, elapsedMs: 0, events: [],
  };
  const report = (stage: VoiceConnectionDiagnostic['events'][number]['stage'], attempt: number, closeCode?: number) => {
    diagnostic.elapsedMs = Math.max(0, Math.round(performance.now() - started));
    diagnostic.events.push({ stage, attempt, elapsedMs: diagnostic.elapsedMs, ...(closeCode === undefined ? {} : { closeCode }) });
    // Only fixed stages, times, network categories and numeric codes leave this module.
    // A diagnostic consumer must not break recording.
    try { callbacks.onDiagnostic?.({ ...diagnostic, events: diagnostic.events.map((event) => ({ ...event })) }); } catch { /* noop */ }
  };

  let stream: MediaStream | null = null;
  let socket: WebSocket | null = null;
  let source: MediaStreamAudioSourceNode | null = null;
  let processor: ScriptProcessorNode | null = null;
  let sink: GainNode | null = null;
  let paused = false;
  let stopped = false;
  let pending: Float32Array[] = [];
  let pendingCount = 0;

  const cleanupAudio = async () => {
    try { processor?.disconnect(); } catch { /* noop */ }
    try { source?.disconnect(); } catch { /* noop */ }
    try { sink?.disconnect(); } catch { /* noop */ }
    stream?.getTracks().forEach((track) => track.stop());
    processor = null;
    source = null;
    sink = null;
    stream = null;
    if (context.state !== 'closed') {
      try { await context.close(); } catch { /* noop */ }
    }
  };

  const closeSocket = () => {
    if (socket && socket.readyState <= WebSocket.OPEN) {
      try { socket.close(); } catch { /* noop */ }
    }
    socket = null;
  };

  const checkAborted = () => {
    if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
  };
  const abort = () => { stopped = true; void cleanupAudio(); closeSocket(); };
  options.signal?.addEventListener('abort', abort, { once: true });
  try {
    checkAborted();
    await context.resume();
    let access: VoiceStreamAccess;
    if (options.getAccess) {
      access = await options.getAccess();
    } else {
      // Owner credentials are loaded only by the existing owner path.
      const { ensureMediaSession, getStoredMediaToken } = await import('./media-client');
      await ensureMediaSession();
      const token = getStoredMediaToken();
      if (!token) throw new Error('Media 登录已失效，请重新登录');
      access = { url: webSocketUrl(), protocols: ['media-bearer', token] };
    }
    checkAborted();

    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    checkAborted();
    // The processor is connected only AFTER ready. No PCM has been captured or
    // sent during this handshake, so retrying it preserves the same mic/context
    // without dropping/replaying audio or duplicating any transcript.
    // Submit-only feedback tickets are single-use; never replay one after a
    // lost ready. Only the existing reusable owner bearer can reconnect.
    const maxAttempts = access.protocols[0] === 'media-bearer' ? 2 : 1;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      checkAborted();
      report('start', attempt);
      socket = new WebSocket(access.url, access.protocols);
      socket.binaryType = 'arraybuffer';
      const activeSocket = socket;
      try {
        await new Promise<void>((resolve, reject) => {
          let settled = false;
          let timeout: ReturnType<typeof window.setTimeout>;
          const complete = (error?: Error) => {
            if (settled) return;
            settled = true;
            window.clearTimeout(timeout);
            options.signal?.removeEventListener('abort', abortHandshake);
            activeSocket.onmessage = null;
            activeSocket.onerror = null;
            if (error) reject(error); else resolve();
          };
          const abortHandshake = () => complete(new DOMException('Aborted', 'AbortError'));
          timeout = window.setTimeout(() => {
            report('timeout', attempt);
            complete(new ReadyTimeoutError(maxAttempts === 2 ? '实时语音连接超时，已重连一次，请重试' : '实时语音连接超时，请重试'));
          }, 5000);
          options.signal?.addEventListener('abort', abortHandshake, { once: true });
          activeSocket.onopen = () => report('open', attempt);
          activeSocket.onclose = (event) => {
            report('close', attempt, event.code);
            complete(options.signal?.aborted ? new DOMException('Aborted', 'AbortError') : new Error(messageError(event)));
          };
          if (options.signal?.aborted) { abortHandshake(); return; }
          activeSocket.onmessage = (event) => {
            let message: VoiceStreamMessage;
            try { message = JSON.parse(String(event.data)) as VoiceStreamMessage; } catch { return; }
            if (message.type === 'ready') { report('ready', attempt); complete(); }
          };
          activeSocket.onerror = () => complete(new Error('实时语音连接失败，请重试'));
        });
        break;
      } catch (error) {
        closeSocket();
        if (!(error instanceof ReadyTimeoutError) || attempt === maxAttempts) throw error;
        checkAborted();
        diagnostic.reconnected = true;
        report('reconnect', attempt);
      }
    }
    checkAborted();

    if (!socket || socket.readyState !== WebSocket.OPEN || !stream) {
      throw new Error('实时语音连接失败，请重试');
    }

    socket.onmessage = (event) => {
      let message: VoiceStreamMessage;
      try {
        message = JSON.parse(String(event.data)) as VoiceStreamMessage;
      } catch {
        return;
      }
      if (message.type === 'partial' && typeof message.text === 'string') {
        callbacks.onPartial(message.text);
      } else if (message.type === 'final' && typeof message.text === 'string') {
        callbacks.onFinal(message.text);
      } else if (message.type === 'error') {
        callbacks.onError?.(message.detail || '实时语音识别失败');
      }
    };
    socket.onerror = () => {
      if (!stopped) callbacks.onError?.('实时语音连接异常');
    };
    const readyAttempt = diagnostic.reconnected ? 2 : 1;
    socket.onclose = (event) => {
      report('close', readyAttempt, event.code);
      if (!stopped && event.code !== 1000) callbacks.onError?.(messageError(event));
    };

    source = context.createMediaStreamSource(stream);
    processor = context.createScriptProcessor(2048, 1, 1);
    sink = context.createGain();
    sink.gain.value = 0;

    const flush = () => {
      if (pendingCount < CHUNK_SAMPLES || !socket || socket.readyState !== WebSocket.OPEN) return;
      const merged = new Float32Array(pendingCount);
      let offset = 0;
      for (const part of pending) {
        merged.set(part, offset);
        offset += part.length;
      }
      pending = [];
      pendingCount = 0;

      let position = 0;
      while (position + CHUNK_SAMPLES <= merged.length) {
        socket.send(float32ToPcm16(merged.subarray(position, position + CHUNK_SAMPLES)));
        position += CHUNK_SAMPLES;
      }
      if (position < merged.length) {
        const remainder = merged.slice(position);
        pending = [remainder];
        pendingCount = remainder.length;
      }
    };

    processor.onaudioprocess = (event) => {
      if (paused || stopped) return;
      const downsampled = resampleFloat32(
        event.inputBuffer.getChannelData(0),
        context.sampleRate,
      );
      pending.push(downsampled);
      pendingCount += downsampled.length;
      flush();
    };

    source.connect(processor);
    processor.connect(sink);
    sink.connect(context.destination);

    const finish = async (sendFinish: boolean) => {
      options.signal?.removeEventListener('abort', abort);
      if (stopped) return;
      stopped = true;
      const activeSocket = socket;
      if (sendFinish && activeSocket?.readyState === WebSocket.OPEN) {
        const done = new Promise<void>((resolve) => {
          const timeout = window.setTimeout(resolve, 1800);
          const previous = activeSocket.onmessage;
          activeSocket.onmessage = (event) => {
            previous?.call(activeSocket, event);
            try {
              const message = JSON.parse(String(event.data)) as VoiceStreamMessage;
              if (message.type === 'done') {
                window.clearTimeout(timeout);
                resolve();
              }
            } catch {
              // Keep waiting for done or timeout.
            }
          };
        });
        activeSocket.send(JSON.stringify({ type: 'finish' }));
        await done;
      }
      await cleanupAudio();
      closeSocket();
    };

    return {
      pause() { paused = true; },
      resume() { paused = false; },
      stop() { return finish(true); },
      cancel() { return finish(false); },
    };
  } catch (error) {
    options.signal?.removeEventListener('abort', abort);
    stopped = true;
    await cleanupAudio();
    closeSocket();
    throw error;
  }
}
