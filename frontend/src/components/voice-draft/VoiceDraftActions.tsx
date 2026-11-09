import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type RefObject,
} from 'react';
import {
  Check,
  Loader2,
  Mic,
  Pause,
  Play,
  RotateCcw,
  Wand2,
  X,
} from 'lucide-react';
import { getStoredMediaToken, getMediaSessionRevision, subscribeMediaSessionChanges } from '../../lib/media-client';
import type { PolishDepth, VoiceDraftSurface } from '../../lib/voice-draft';
import {
  applyStreamingDraft,
  mergeVoiceSegments,
  startVoiceStream,
  type VoiceStreamSession,
  type VoiceConnectionDiagnostic,
} from '../../lib/voice-stream';

type VoicePhase =
  | 'idle'
  | 'connecting'
  | 'recording'
  | 'paused'
  | 'stopping'
  | 'polishing'
  | 'error';

export interface VoiceDraftTimings {
  asrFirstTextMs?: number;
  stopToFinalMs?: number;
  polishFirstDeltaMs?: number;
  polishDeltaToDoneMs?: number;
}

interface Props {
  surface: VoiceDraftSurface;
  value: string;
  onChange: (value: string) => void;
  targetRef?: RefObject<HTMLTextAreaElement>;
  disabled?: boolean;
  allowPolish?: boolean;
  polishDepth?: PolishDepth;
  startStream?: typeof startVoiceStream;
  onBusyChange?: (busy: boolean) => void;
  compact?: boolean;
  /** Opt-in layout for the dedicated polish page; shared/compact callers stay unchanged. */
  polishPage?: boolean;
  /** Explicit owner feature for generic surfaces; public feedback leaves it unset. */
  usageFeature?: string;
  polished?: boolean;
  initialUndoText?: string | null;
  className?: string;
  onTimings?: (timings: VoiceDraftTimings) => void;
  onPolishStart?: (before: string) => void;
  onPolishApplied?: (change: { before: string; after: string; polishDepth?: PolishDepth }) => void;
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

export default function VoiceDraftActions({
  surface,
  value,
  onChange,
  targetRef,
  disabled = false,
  allowPolish = true,
  polishDepth = 'light',
  startStream = startVoiceStream,
  onBusyChange,
  compact = false,
  polishPage = false,
  usageFeature: explicitUsageFeature,
  polished = false,
  initialUndoText = null,
  className = '',
  onPolishStart,
  onPolishApplied,
  onTimings,
}: Props) {
  const usageFeature = explicitUsageFeature ?? (surface === 'generic' ? (polishPage ? 'polish' : null) : surface);
  const [phase, setPhase] = useState<VoicePhase>('idle');
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [connectionDiagnostic, setConnectionDiagnostic] = useState<VoiceConnectionDiagnostic | null>(null);
  const [diagnosticNotice, setDiagnosticNotice] = useState('');
  const [undoText, setUndoText] = useState<string | null>(initialUndoText);
  const [polishPreview, setPolishPreview] = useState('');
  const [pendingPolish, setPendingPolish] = useState<string | null>(null);

  const timingRef = useRef<{ start: number; first?: number; stopped?: number; values: VoiceDraftTimings }>({ start: 0, values: {} });
  const sessionRef = useRef<VoiceStreamSession | null>(null);
  const recordingAbortRef = useRef<AbortController | null>(null);
  const recordingGeneration = useRef(0);
  const recordingBusy = useRef(false);
  const polishAbortRef = useRef<AbortController | null>(null);
  const valueRef = useRef(value);
  const originalRef = useRef(value);
  const committedRef = useRef('');
  const partialRef = useRef('');
  const pendingPolishBaseRef = useRef<string | null>(null);
  const pendingPolishDepthRef = useRef<PolishDepth>('light');
  const selectionRef = useRef({ start: value.length, end: value.length });

  valueRef.current = value;

  const captureSelection = useCallback(() => {
    const el = targetRef?.current;
    if (!el) {
      selectionRef.current = { start: valueRef.current.length, end: valueRef.current.length };
      return;
    }
    selectionRef.current = {
      start: el.selectionStart ?? valueRef.current.length,
      end: el.selectionEnd ?? el.selectionStart ?? valueRef.current.length,
    };
  }, [targetRef]);

  const focusAt = useCallback((cursor: number) => {
    const el = targetRef?.current;
    if (!el) return;
    window.setTimeout(() => {
      el.focus();
      el.setSelectionRange(cursor, cursor);
    }, 0);
  }, [targetRef]);

  const renderStreamingText = useCallback(() => {
    const inserted = mergeVoiceSegments(committedRef.current, partialRef.current);
    const next = applyStreamingDraft(
      originalRef.current,
      inserted,
      selectionRef.current.start,
      selectionRef.current.end,
    );
    onChange(next.value);
    valueRef.current = next.value;
    focusAt(next.cursor);
  }, [focusAt, onChange]);

  const handleStreamError = useCallback((message: string) => {
    ++recordingGeneration.current;
    recordingAbortRef.current?.abort();
    recordingBusy.current = false;
    setError(message);
    setPhase('error');
    setNotice('');
    const session = sessionRef.current;
    sessionRef.current = null;
    if (session) void session.cancel();
  }, []);

  const startRecording = useCallback(async () => {
    if (disabled || phase !== 'idle' || recordingBusy.current) return;
    recordingBusy.current = true;
    const generation = ++recordingGeneration.current;
    const controller = new AbortController();
    recordingAbortRef.current = controller;

    timingRef.current = { start: performance.now(), values: {} };
    onTimings?.({});
    setError('');
    setNotice('');
    setUndoText(null);
    setPendingPolish(null);
    setPolishPreview('');
    setElapsed(0);
    setConnectionDiagnostic(null);
    setDiagnosticNotice('');
    captureSelection();
    originalRef.current = valueRef.current;
    committedRef.current = '';
    partialRef.current = '';
    setPhase('connecting');

    try {
      const session = await startStream({
        onPartial: (text) => {
          if (generation !== recordingGeneration.current) return;
          if (text && timingRef.current.first === undefined) {
            timingRef.current.first = performance.now();
            timingRef.current.values.asrFirstTextMs = timingRef.current.first - timingRef.current.start;
            onTimings?.({ ...timingRef.current.values });
          }
          partialRef.current = text;
          renderStreamingText();
        },
        onFinal: (text) => {
          if (generation !== recordingGeneration.current) return;
          const now = performance.now();
          const timing = timingRef.current;
          if (text && timing.first === undefined) { timing.first = now; timing.values.asrFirstTextMs = now - timing.start; }
          if (text && timing.stopped !== undefined && timing.values.stopToFinalMs === undefined) timing.values.stopToFinalMs = now - timing.stopped;
          if (text) onTimings?.({ ...timing.values });
          committedRef.current = mergeVoiceSegments(committedRef.current, text);
          partialRef.current = '';
          renderStreamingText();
        },
        onDiagnostic: (diagnostic) => {
          if (generation === recordingGeneration.current) setConnectionDiagnostic(diagnostic);
        },
        onError: (message) => {
          if (generation === recordingGeneration.current) handleStreamError(message);
        },
      }, { signal: controller.signal });
      if (generation !== recordingGeneration.current) { await session.cancel(); return; }
      sessionRef.current = session;
      setPhase('recording');
    } catch (err) {
      if (generation !== recordingGeneration.current) return;
      recordingBusy.current = false;
      setError(err instanceof Error ? err.message : '无法开始实时语音输入');
      setPhase('error');
    }
  }, [
    captureSelection,
    disabled,
    handleStreamError,
    phase,
    renderStreamingText,
    onTimings,
    startStream,
  ]);

  const finishRecording = useCallback(async () => {
    const session = sessionRef.current;
    if (!session) return;
    timingRef.current.stopped = performance.now();
    setPhase('stopping');
    const generation = recordingGeneration.current;
    try {
      await session.stop();
      if (generation !== recordingGeneration.current) return;
      recordingBusy.current = false;
      sessionRef.current = null;
      partialRef.current = '';
      renderStreamingText();
      setNotice('已实时转成文字');
      setPhase('idle');
    } catch (err) {
      if (generation !== recordingGeneration.current) return;
      recordingBusy.current = false;
      sessionRef.current = null;
      setError(err instanceof Error ? err.message : '结束语音输入失败');
      setPhase('error');
    }
  }, [renderStreamingText, usageFeature]);

  const cancelRecording = useCallback(async () => {
    const generation = ++recordingGeneration.current;
    recordingBusy.current = false;
    recordingAbortRef.current?.abort();
    recordingAbortRef.current = null;
    const session = sessionRef.current;
    sessionRef.current = null;
    if (session) await session.cancel();
    if (generation !== recordingGeneration.current) return;

    const original = originalRef.current;
    onChange(original);
    valueRef.current = original;
    const cursor = selectionRef.current.start;
    focusAt(cursor);

    committedRef.current = '';
    partialRef.current = '';
    setElapsed(0);
    setConnectionDiagnostic(null);
    setDiagnosticNotice('');
    setError('');
    setNotice('');
    setPhase('idle');
  }, [focusAt, onChange]);

  const togglePause = useCallback(() => {
    const session = sessionRef.current;
    if (!session) return;

    if (phase === 'recording') {
      session.pause();
      setPhase('paused');
    } else if (phase === 'paused') {
      session.resume();
      setPhase('recording');
    }
  }, [phase]);

  const polish = useCallback(async () => {
    const original = valueRef.current;
    if (!allowPolish || !original.trim() || disabled || polishAbortRef.current || recordingBusy.current) return;
    const controller = new AbortController();
    polishAbortRef.current = controller;
    const startedAt = performance.now();
    let firstDeltaAt: number | undefined;
    const bindingRevision = getMediaSessionRevision();
    const identityToken = getStoredMediaToken();
    const identity = identityToken ? { token: identityToken, revision: bindingRevision } : undefined;
    const isCurrent = () => polishAbortRef.current === controller && !controller.signal.aborted && getMediaSessionRevision() === bindingRevision;
    onTimings?.({});

    onPolishStart?.(original);
    setError('');
    setNotice('');
    setPendingPolish(null);
    pendingPolishBaseRef.current = null;
    setPolishPreview('');
    setPhase('polishing');

    try {
      const { streamRefineDraft } = await import('../../lib/voice-draft');
      if (controller.signal.aborted) return;
      const result = await streamRefineDraft(
        original,
        'polish',
        surface,
        '',
        {
          signal: controller.signal,
          polishDepth,
          mediaIdentity: identity,
          onDelta: (_delta, fullText) => {
            if (!isCurrent()) return;
            if (_delta && firstDeltaAt === undefined) {
              firstDeltaAt = performance.now();
              onTimings?.({ polishFirstDeltaMs: firstDeltaAt - startedAt });
            }
            setPolishPreview(fullText);
          },
        },
      );
      if (!isCurrent()) return;
      if (firstDeltaAt !== undefined) onTimings?.({ polishFirstDeltaMs: firstDeltaAt - startedAt, polishDeltaToDoneMs: performance.now() - firstDeltaAt });

      if (valueRef.current === original) {
        setUndoText(original);
        onChange(result.text);
        valueRef.current = result.text;
        selectionRef.current = { start: result.text.length, end: result.text.length };
        focusAt(result.text.length);
        setNotice('已润色，可撤销');
        onPolishApplied?.({ before: original, after: result.text, ...(polishDepth === 'deep' ? { polishDepth } : {}) });
      } else {
        pendingPolishBaseRef.current = original;
        pendingPolishDepthRef.current = polishDepth;
        setPendingPolish(result.text);
        setNotice('润色建议已生成，未覆盖你的修改');
      }
      setPolishPreview('');
      setPhase('idle');
    } catch (err) {
      if (!isCurrent()) return;
      if (err instanceof DOMException && err.name === 'AbortError') {
        if (polishAbortRef.current === controller) {
          setPolishPreview('');
          setPhase('idle');
        }
        return;
      }
      setPolishPreview('');
      setError(err instanceof Error ? err.message : '润色失败，请重试');
      setPhase('error');
    } finally {
      if (polishAbortRef.current === controller) polishAbortRef.current = null;
    }
  }, [allowPolish, disabled, focusAt, onChange, onPolishApplied, onPolishStart, onTimings, polishDepth, surface, usageFeature]);

  const cancelPolish = useCallback(() => {
    const controller = polishAbortRef.current;
    polishAbortRef.current = null;
    controller?.abort();
    setPolishPreview('');
    setPendingPolish(null);
    pendingPolishBaseRef.current = null;
    setError('');
    setNotice('已取消润色，当前文字已保留');
    setPhase('idle');
  }, []);

  useEffect(() => subscribeMediaSessionChanges(() => {
    // Invalidate synchronously, before React renders the next identity.
    const controller = polishAbortRef.current;
    polishAbortRef.current = null;
    controller?.abort();
    ++recordingGeneration.current;
    recordingAbortRef.current?.abort();
    recordingBusy.current = false;
    const session = sessionRef.current;
    sessionRef.current = null;
    if (session) void session.cancel();
    setPolishPreview(''); setPendingPolish(null); setUndoText(null);
    pendingPolishBaseRef.current = null;
    setError(''); setNotice(''); setPhase('idle');
    setConnectionDiagnostic(null); setDiagnosticNotice('');
  }), []);

  const applyPendingPolish = useCallback(() => {
    if (pendingPolish == null) return;
    const current = valueRef.current;
    const base = pendingPolishBaseRef.current ?? current;
    setUndoText(current);
    onChange(pendingPolish);
    valueRef.current = pendingPolish;
    const cursor = pendingPolish.length;
    selectionRef.current = { start: cursor, end: cursor };
    focusAt(cursor);
    setPendingPolish(null);
    pendingPolishBaseRef.current = null;
    onPolishApplied?.({ before: base, after: pendingPolish, ...(pendingPolishDepthRef.current === 'deep' ? { polishDepth: 'deep' as const } : {}) });
    setNotice('已采用润色，可撤销');
  }, [focusAt, onChange, onPolishApplied, pendingPolish]);

  const discardPendingPolish = useCallback(() => {
    setPendingPolish(null);
    pendingPolishBaseRef.current = null;
    setNotice('已保留当前文字');
  }, []);

  const undoPolish = useCallback(() => {
    if (undoText == null) return;
    onChange(undoText);
    valueRef.current = undoText;
    const cursor = undoText.length;
    selectionRef.current = { start: cursor, end: cursor };
    focusAt(cursor);
    setUndoText(null);
    setNotice('已撤销润色');
  }, [focusAt, onChange, undoText]);

  useEffect(() => {
    if (phase !== 'recording') return undefined;
    const timer = window.setInterval(() => {
      setElapsed((current) => current + 1);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [phase]);

  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(''), 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    onBusyChange?.(['connecting', 'recording', 'paused', 'stopping', 'polishing'].includes(phase));
  }, [onBusyChange, phase]);

  useEffect(() => () => {
    ++recordingGeneration.current;
    recordingBusy.current = false;
    recordingAbortRef.current?.abort();
    const session = sessionRef.current;
    sessionRef.current = null;
    if (session) void session.cancel();
    polishAbortRef.current?.abort();
    polishAbortRef.current = null;
  }, []);

  const pageButtonBase = 'inline-flex min-h-11 min-w-0 flex-1 items-center justify-center gap-1.5 rounded-xl border px-2 text-xs transition disabled:opacity-30';
  const buttonBase = compact
    ? 'grid h-11 w-11 shrink-0 place-items-center rounded-2xl text-neutral-400 transition hover:bg-neutral-50 disabled:opacity-30'
    : polishPage ? pageButtonBase + ' border-neutral-200 bg-white text-neutral-700'
    : 'inline-flex h-10 items-center gap-1.5 rounded-xl border border-neutral-200 bg-white px-3 text-xs text-neutral-600 transition active:bg-neutral-50 disabled:opacity-40';
  const transientClass = compact
    ? 'absolute inset-x-2 -top-14 z-20 flex min-w-0 items-center gap-1.5 rounded-2xl border border-black/[0.06] bg-white/95 p-1.5 shadow-lg backdrop-blur-xl'
    : 'flex min-w-0 items-center gap-1.5';

  if (phase === 'recording' || phase === 'paused') {
    return (
      <div className={transientClass + ' ' + className} aria-live="polite">
        <div className="inline-flex h-10 items-center gap-1.5 rounded-xl bg-red-50 px-2.5 text-xs tabular-nums text-red-600">
          <span className="h-2 w-2 rounded-full bg-red-500" />
          {formatDuration(elapsed)}
        </div>
        <button
          type="button"
          className={buttonBase}
          onClick={togglePause}
          aria-label={phase === 'paused' ? '继续录音' : '暂停录音'}
        >
          {phase === 'paused' ? <Play size={16} /> : <Pause size={16} />}
          {!compact && (phase === 'paused' ? '继续' : '暂停')}
        </button>
        <button
          type="button"
          className={buttonBase}
          onClick={() => void finishRecording()}
          aria-label="完成录音"
        >
          <Check size={16} />
          {!compact && '完成'}
        </button>
        <button
          type="button"
          className={buttonBase}
          onClick={() => void cancelRecording()}
          aria-label="取消录音"
        >
          <X size={16} />
        </button>
      </div>
    );
  }

  if (phase === 'connecting' || phase === 'stopping' || phase === 'polishing') {
    const label = phase === 'connecting'
      ? '连接语音中'
      : phase === 'stopping'
        ? '收尾中'
        : '润色中';
    return (
      <div
        className={(compact ? transientClass : 'flex min-w-0 flex-1 flex-wrap items-center gap-2') + ' ' + className}
        aria-live="polite"
      >
        <span className={compact
          ? 'grid h-11 w-11 place-items-center rounded-2xl text-neutral-400'
          : 'inline-flex h-10 shrink-0 items-center gap-1.5 rounded-xl bg-neutral-50 px-3 text-xs text-neutral-500'}
        >
          <Loader2 className="animate-spin" size={16} />
          {!compact && label}
        </span>
        {compact && <span className="truncate text-[10px] text-neutral-400">{label}</span>}
        {phase === 'connecting' && <button type="button" className={buttonBase} onClick={() => void cancelRecording()} aria-label="取消连接语音"><X size={16} />取消</button>}
        {phase === 'polishing' && <button type="button" className={buttonBase} onClick={cancelPolish} aria-label="取消润色"><X size={16} />{!compact && '取消'}</button>}
        {!compact && phase === 'polishing' && polishPreview && (
          <span className="min-w-0 max-w-md flex-1 truncate rounded-xl bg-neutral-50 px-3 py-2 text-xs text-neutral-500">
            {polishPreview}
          </span>
        )}
      </div>
    );
  }

  const diagnosticEntry = connectionDiagnostic && (
    <details className="min-w-0 max-w-full text-[11px] text-neutral-500">
      <summary className="cursor-pointer rounded px-1 py-2 focus-visible:outline focus-visible:outline-2">连接诊断</summary>
      <div className="mt-1 rounded-xl bg-neutral-50 p-2">
        <p>仅连接时序，不含音频、文字或凭据。</p>
        <pre tabIndex={0} aria-label="语音连接诊断" className="max-h-48 max-w-full overflow-auto whitespace-pre-wrap break-all text-[10px]">{JSON.stringify(connectionDiagnostic, null, 2)}</pre>
        <button type="button" className="mt-1 rounded px-2 py-1 underline focus-visible:outline focus-visible:outline-2" onClick={() => {
          void (async () => {
            try {
              await navigator.clipboard.writeText(JSON.stringify(connectionDiagnostic, null, 2));
              setDiagnosticNotice('已复制连接诊断');
            } catch { setDiagnosticNotice('复制失败，请选择上方诊断文字复制'); }
          })();
        }}>复制诊断</button>
        <span role="status">{diagnosticNotice}</span>
      </div>
    </details>
  );

  if (phase === 'error') {
    return (
      <div
        className={(compact ? transientClass : 'flex min-w-0 flex-wrap items-center gap-1.5') + ' ' + className}
        aria-live="assertive"
      >
        <span className="max-w-52 text-[11px] text-red-500" title={error}>
          {error}
        </span>
        <button
          type="button"
          className={buttonBase}
          onClick={() => {
            setError('');
            setPhase('idle');
          }}
          aria-label="关闭错误提示"
        >
          <X size={15} />
        </button>
        {diagnosticEntry}
      </div>
    );
  }

  return (
    <div className={'flex min-w-0 flex-wrap items-center gap-1.5 ' + className}>
      <button
        type="button"
        className={buttonBase}
        disabled={disabled}
        onClick={() => void startRecording()}
        aria-label="语音输入"
      >
        <Mic size={16} />
        {!compact && '语音'}
      </button>
      {allowPolish && (polishPage || value.trim()) && (
        <button
          type="button"
          className={polishPage && !polished ? pageButtonBase + ' border-neutral-900 bg-neutral-900 text-white' : buttonBase}
          disabled={disabled || !value.trim()}
          onClick={() => void polish()}
          aria-label="润色文字"
        >
          <Wand2 size={16} />
          {!compact && (polishPage && polished ? '再润色' : '润色')}
        </button>
      )}
      {pendingPolish != null && (
        <>
          <button
            type="button"
            className={buttonBase}
            disabled={disabled}
            onClick={applyPendingPolish}
            aria-label="应用润色建议"
          >
            <Check size={15} />
            {!compact && '采用建议'}
          </button>
          <button
            type="button"
            className={buttonBase}
            disabled={disabled}
            onClick={discardPendingPolish}
            aria-label="忽略润色建议"
          >
            <X size={15} />
            {!compact && '保留原文'}
          </button>
        </>
      )}
      {!compact && pendingPolish != null && <p aria-label="待采用润色建议全文" className="w-full whitespace-pre-wrap break-words rounded-xl bg-neutral-50 p-3 text-xs leading-6 [overflow-wrap:anywhere]">{pendingPolish}</p>}
      {undoText != null && (
        <button
          type="button"
          className={buttonBase}
          disabled={disabled}
          onClick={undoPolish}
          aria-label="撤销润色"
        >
          <RotateCcw size={15} />
          {!compact && '撤销'}
        </button>
      )}
      {diagnosticEntry}
      {notice && !(polishPage && ['已润色，可撤销', '已采用润色，可撤销'].includes(notice)) && (
        <span aria-live="polite" className={(polishPage ? 'w-full ' : 'max-w-44 truncate ') + 'text-[10px] text-neutral-400'}>
          {notice}
        </span>
      )}
    </div>
  );
}
