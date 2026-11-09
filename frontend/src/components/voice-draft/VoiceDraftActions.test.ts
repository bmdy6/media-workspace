const usageMock = vi.hoisted(() => vi.fn());

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const hooks = vi.hoisted(() => {
  const h = {
    slots: [] as any[], cursor: 0, queued: [] as (() => void)[], dirty: false,
    state(initial: any) {
      const index = h.cursor++;
      if (!(index in h.slots)) h.slots[index] = typeof initial === 'function' ? initial() : initial;
      return [h.slots[index], (value: any) => { const next = typeof value === 'function' ? value(h.slots[index]) : value; if (!Object.is(next, h.slots[index])) { h.slots[index] = next; h.dirty = true; } }];
    },
    ref(initial: any) { return h.state({ current: initial })[0]; },
    memo(value: any, deps: any[]) {
      const index = h.cursor++; const previous = h.slots[index];
      if (!previous || deps.some((dep, i) => !Object.is(dep, previous.deps[i]))) h.slots[index] = { value, deps };
      return h.slots[index].value;
    },
    effect(callback: () => any, deps: any[] = []) {
      const index = h.cursor++; const previous = h.slots[index];
      if (!previous || deps.some((dep, i) => !Object.is(dep, previous.deps[i]))) {
        h.slots[index] = { deps, cleanup: previous?.cleanup };
        h.queued.push(() => { previous?.cleanup?.(); h.slots[index].cleanup = callback(); });
      }
    },
    reset() { for (const slot of h.slots) slot?.cleanup?.(); h.slots = []; h.cursor = 0; h.queued = []; h.dirty = false; },
    render(component: () => any) {
      let tree: any;
      for (let count = 0; count < 20; count++) {
        h.cursor = 0; h.dirty = false; tree = component();
        const queued = h.queued.splice(0); queued.forEach(effect => effect());
        if (!h.dirty) return tree;
      }
      throw new Error('Hook fixture render did not settle');
    },
  };
  return h;
});
vi.mock('react', async original => ({ ...await original<typeof import('react')>(), useState: hooks.state, useRef: hooks.ref, useCallback: hooks.memo, useEffect: hooks.effect }));
function nodes(tree: any): any[] { return !tree || typeof tree !== 'object' ? [] : [tree, ...[tree.props?.children].flat(Infinity).flatMap(nodes)]; }
function text(tree: any): string { if (tree == null || typeof tree === 'boolean') return ''; return typeof tree === 'string' || typeof tree === 'number' ? String(tree) : [tree?.props?.children].flat(Infinity).map(text).join(''); }
function button(tree: any, label: string) { const result = nodes(tree).find(node => node.type === 'button' && text(node).includes(label)); if (!result) throw new Error('Missing button: ' + label); return result; }
async function flush() { await vi.dynamicImportSettled(); for (let i = 0; i < 8; i++) await Promise.resolve(); await new Promise(resolve => setTimeout(resolve, 0)); }

const refineMock = vi.hoisted(() => vi.fn());
vi.mock('../../lib/voice-draft', () => ({ streamRefineDraft: refineMock }));
import VoiceDraftActions from './VoiceDraftActions';
import { clearMediaToken } from '../../lib/media-client';
const value = '合成文字';
let change = vi.fn();
let busy = vi.fn();
let stream = vi.fn();
const props = () => ({ surface: 'generic' as const, value, onChange: change, allowPolish: false, startStream: stream, onBusyChange: busy });
const render = () => hooks.render(() => VoiceDraftActions(props()));
beforeEach(() => {
  hooks.reset(); usageMock.mockClear(); change = vi.fn(); busy = vi.fn(); stream = vi.fn();
  const values = new Map<string, string>();
  vi.stubGlobal('localStorage', { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key,value), removeItem: (key: string) => values.delete(key) });
  vi.stubGlobal('window', { setTimeout, clearTimeout, setInterval, clearInterval });
  clearMediaToken();
});
afterEach(() => { hooks.reset(); vi.unstubAllGlobals(); });
describe('shared voice controls preserve owner capabilities and isolate feedback', () => {
  it('hides polish only for opt-out callers, preserving the existing owner default', () => {
    let tree = render(); expect(nodes(tree).some(node => node.props?.['aria-label'] === '润色文字')).toBe(false);
    hooks.reset(); tree = hooks.render(() => VoiceDraftActions({ ...props(), allowPolish: true }));
    expect(nodes(tree).some(node => node.props?.['aria-label'] === '润色文字')).toBe(true);
  });
  it('cancels connection explicitly and rejects late callback text/session after cancel', async () => {
    let resolve!: (value: any) => void;
    stream.mockImplementation(() => new Promise(done => { resolve = done; }));
    let tree = render(); button(tree, '语音').props.onClick(); tree = render();
    const callback = stream.mock.calls[0][0]; const signal = stream.mock.calls[0][1].signal;
    button(tree, '取消').props.onClick(); await flush(); tree = render();
    expect(signal.aborted).toBe(true); callback.onFinal('迟到的转写');
    const session = { cancel: vi.fn(async () => undefined), stop: vi.fn(), pause: vi.fn(), resume: vi.fn() };
    resolve(session); await flush(); render();
    expect(session.cancel).toHaveBeenCalled(); expect(change).not.toHaveBeenCalledWith('迟到的转写');
    expect(busy).toHaveBeenLastCalledWith(false);
  });
  it('prevents two simultaneous starts from repeated clicks', async () => {
    stream.mockImplementation(() => new Promise(() => undefined));
    const tree = render(); const click = button(tree, '语音').props.onClick; click(); click(); await flush();
    expect(stream).toHaveBeenCalledTimes(1);
  });
  it('aborts in-flight startup when the public page is unmounted', () => {
    stream.mockImplementation(() => new Promise(() => undefined));
    const tree = render(); button(tree, '语音').props.onClick(); const signal = stream.mock.calls[0][1].signal;
    hooks.reset(); expect(signal.aborted).toBe(true);
  });
  it.each(['binding change', 'unmount'])('does not restore old text when recording cancellation resolves after %s', async (event) => {
    let finishCancel!: () => void;
    const session = { cancel: vi.fn(() => new Promise<void>(resolve => { finishCancel = resolve; })), stop: vi.fn(), pause: vi.fn(), resume: vi.fn() };
    stream.mockResolvedValue(session);
    button(render(), '语音').props.onClick(); await flush();
    const cancel = nodes(render()).find(node => node.props?.['aria-label'] === '取消录音');
    cancel.props.onClick();
    if (event === 'unmount') hooks.reset();
    else { clearMediaToken(); render(); }
    change.mockClear(); finishCancel(); await flush();
    expect(change).not.toHaveBeenCalled();
  });
});


describe('polish cancellation and generation safety', () => {
  let value = '合成待润色原文';
  let polishDepth: 'light' | 'deep' = 'light';
  let applied = vi.fn();
  let requests: any[];
  const renderPolish = () => hooks.render(() => VoiceDraftActions({ surface: 'generic', value, polishDepth, onChange: (next) => { value = next; }, onPolishApplied: applied }));
  const byLabel = (tree: any, label: string) => {
    const node = nodes(tree).find(node => node.props?.['aria-label'] === label);
    if (!node) throw new Error('Missing: ' + label);
    return node;
  };
  beforeEach(() => {
    value = '合成待润色原文'; polishDepth = 'light'; applied = vi.fn(); requests = [];
    refineMock.mockReset().mockImplementation((_text, _mode, _surface, _nearby, options) => new Promise((resolve, reject) => requests.push({ resolve, reject, options })));
  });
  it('latches repeated clicks synchronously and allows cancelling without losing manual text', async () => {
    const click = byLabel(renderPolish(), '润色文字').props.onClick;
    click(); click(); await flush();
    expect(requests).toHaveLength(1);
    value = '生成时手改'; renderPolish();
    byLabel(renderPolish(), '取消润色').props.onClick();
    expect(requests[0].options.signal.aborted).toBe(true);
    expect(value).toBe('生成时手改');
    expect(text(renderPolish())).toContain('已取消润色');
  });
  it.each(['resolve', 'reject'])('ignores late delta and %s after cancel and restart', async (outcome) => {
    byLabel(renderPolish(), '润色文字').props.onClick(); await flush();
    byLabel(renderPolish(), '取消润色').props.onClick(); renderPolish();
    byLabel(renderPolish(), '润色文字').props.onClick(); await flush();
    requests[0].options.onDelta('旧输出', '旧输出');
    if (outcome === 'resolve') requests[0].resolve({ text: '旧结果' }); else requests[0].reject(new Error('旧错误'));
    await flush();
    expect(text(renderPolish())).toContain('润色中');
    expect(text(renderPolish())).not.toMatch(/旧输出|旧错误/);
    requests[1].resolve({ text: '新结果全文' }); await flush();
    expect(value).toBe('新结果全文'); expect(applied).toHaveBeenCalledTimes(1);
  });
  it('keeps edited text until explicit adoption and undo restores that edit', async () => {
    byLabel(renderPolish(), '润色文字').props.onClick(); await flush();
    value = '手改应保留'; renderPolish();
    requests[0].resolve({ text: '完整建议正文' }); await flush();
    expect(value).toBe('手改应保留');
    expect(text(renderPolish())).toContain('完整建议正文');
    byLabel(renderPolish(), '应用润色建议').props.onClick(); renderPolish();
    expect(value).toBe('完整建议正文');
    byLabel(renderPolish(), '撤销润色').props.onClick();
    expect(value).toBe('手改应保留');
  });
  it('ignores callbacks after unmount, including non-Abort errors', async () => {
    byLabel(renderPolish(), '润色文字').props.onClick(); await flush();
    hooks.reset(); requests[0].options.onDelta('迟到', '迟到'); requests[0].reject(new Error('迟到错误')); await flush();
    expect(applied).not.toHaveBeenCalled(); expect(value).toBe('合成待润色原文');
    expect(hooks.dirty).toBe(false);
  });
  it('captures deep at click time even when the selection changes before the request completes', async () => {
    polishDepth = 'deep';
    byLabel(renderPolish(), '润色文字').props.onClick();
    polishDepth = 'light'; renderPolish(); await flush();
    expect(requests[0].options.polishDepth).toBe('deep');
    requests[0].resolve({ text: '深度合成结果' }); await flush();
    expect(applied).toHaveBeenCalledWith({ before: '合成待润色原文', after: '深度合成结果', polishDepth: 'deep' });
  });
  it('keeps the original result depth when explicitly adopting a pending suggestion later', async () => {
    polishDepth = 'deep'; byLabel(renderPolish(), '润色文字').props.onClick(); await flush();
    value = '手改应保留'; polishDepth = 'light'; renderPolish();
    requests[0].resolve({ text: '深度合成建议' }); await flush();
    expect(value).toBe('手改应保留'); expect(applied).not.toHaveBeenCalled();
    byLabel(renderPolish(), '应用润色建议').props.onClick(); renderPolish();
    expect(applied).toHaveBeenCalledWith({ before: '合成待润色原文', after: '深度合成建议', polishDepth: 'deep' });
    byLabel(renderPolish(), '撤销润色').props.onClick(); expect(value).toBe('手改应保留');
  });
  it('keeps text and no adoptable suggestion after a deep acknowledgement failure', async () => {
    polishDepth = 'deep'; byLabel(renderPolish(), '润色文字').props.onClick(); await flush();
    requests[0].options.onDelta('未确认的预览', '未确认的预览'); renderPolish();
    requests[0].reject(new Error('服务端尚未确认深度润色')); await flush();
    expect(value).toBe('合成待润色原文'); expect(applied).not.toHaveBeenCalled();
    expect(text(renderPolish())).toContain('服务端尚未确认深度润色');
    expect(text(renderPolish())).not.toContain('未确认的预览');
    expect(nodes(renderPolish()).some(node => node.props?.['aria-label'] === '应用润色建议')).toBe(false);
  });
});


describe('client event timings remain separate from provider execution', () => {
  it('measures first ASR text and stop-to-final at their callbacks, not at stop resolution', async () => {
    let now = 100; vi.spyOn(performance, 'now').mockImplementation(() => now);
    const timing = vi.fn(); let callback: any; let finish!: () => void;
    const session = { cancel: vi.fn(), pause: vi.fn(), resume: vi.fn(), stop: vi.fn(() => new Promise<void>(resolve => { finish = resolve; })) };
    stream.mockImplementation(async callbacks => { callback = callbacks; return session; });
    const run = () => hooks.render(() => VoiceDraftActions({ ...props(), onTimings: timing }));
    button(run(), '语音').props.onClick(); await flush(); run();
    now = 350; callback.onPartial('首字'); expect(timing).toHaveBeenLastCalledWith({ asrFirstTextMs: 250 });
    now = 1000; button(run(), '完成').props.onClick();
    now = 1120; callback.onFinal('完整识别'); expect(timing).toHaveBeenLastCalledWith({ asrFirstTextMs: 250, stopToFinalMs: 120 });
    now = 1500; finish(); await flush(); run();
    expect(timing).toHaveBeenLastCalledWith({ asrFirstTextMs: 250, stopToFinalMs: 120 });
    vi.restoreAllMocks();
  });
  it('reports click-to-first-delta and first-delta-to-done separately', async () => {
    let now = 100; vi.spyOn(performance, 'now').mockImplementation(() => now);
    const timing = vi.fn(); let options: any; let finish!: (value: any) => void;
    refineMock.mockImplementation((_text, _mode, _surface, _nearby, opts) => { options = opts; return new Promise(resolve => { finish = resolve; }); });
    const run = () => hooks.render(() => VoiceDraftActions({ ...props(), allowPolish: true, onTimings: timing }));
    button(run(), '润色').props.onClick(); await flush(); run();
    now = 450; options.onDelta('首段', '首段'); expect(timing).toHaveBeenLastCalledWith({ polishFirstDeltaMs: 350 });
    now = 700; finish({ text: '完整结果' }); await flush(); run();
    expect(timing).toHaveBeenLastCalledWith({ polishFirstDeltaMs: 350, polishDeltaToDoneMs: 250 });
    vi.restoreAllMocks();
  });
});

describe('connection diagnostics and existing text', () => {
  const diagnostic = { startedAt: '2026-10-31T00:00:00.000Z', network: '4g', reconnected: true, elapsedMs: 10000, events: [{ stage: 'timeout', attempt: 2, elapsedMs: 10000 }] };
  it('keeps existing text on two failed connections and exposes a selectable, copyable report next to the error', async () => {
    stream.mockImplementation(async (cb) => { cb.onDiagnostic(diagnostic); throw new Error('实时语音连接超时，已重连一次，请重试'); });
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    button(render(), '语音').props.onClick(); await flush(); const tree = render();
    expect(change).not.toHaveBeenCalled();
    expect(text(tree)).toContain('实时语音连接超时，已重连一次，请重试');
    expect(text(tree)).toContain('连接诊断');
    expect(nodes(tree).find(n => n.type === 'pre').props.children).toBe(JSON.stringify(diagnostic, null, 2));
    button(tree, '复制诊断').props.onClick(); await flush();
    expect(writeText).toHaveBeenCalledWith(JSON.stringify(diagnostic, null, 2));
    expect(text(render())).toContain('已复制连接诊断');
    nodes(render()).find(n => n.props?.['aria-label'] === '关闭错误提示').props.onClick(); render();
    expect(change).not.toHaveBeenCalled();
  });
  it('preserves original text while connecting, then appends final transcript once', async () => {
    stream.mockImplementation(async (cb) => { cb.onDiagnostic(diagnostic); return { cancel: vi.fn(), stop: vi.fn(), pause: vi.fn(), resume: vi.fn() }; });
    button(render(), '语音').props.onClick(); await flush(); render();
    expect(change).not.toHaveBeenCalled();
    stream.mock.calls[0][0].onPartial('合成'); render();
    stream.mock.calls[0][0].onFinal('合成转写'); render();
    expect(change).toHaveBeenLastCalledWith(value + '合成转写');
    expect(change.mock.calls.filter(([text]) => text === value + '合成转写')).toHaveLength(1);
  });
});

describe('dedicated polish page opt-in controls', () => {
  it('keeps the primary polish action visible but disabled when empty, and secondary after applying', () => {
    const pageProps = { ...props(), allowPolish: true, polishPage: true, value: '' };
    let tree = hooks.render(() => VoiceDraftActions(pageProps));
    expect(button(tree, '润色').props.disabled).toBe(true);
    pageProps.value = '待润色'; tree = hooks.render(() => VoiceDraftActions(pageProps));
    expect(button(tree, '润色').props.disabled).toBe(false);
    expect(button(tree, '润色').props.className).toContain('bg-neutral-900');
    tree = hooks.render(() => VoiceDraftActions({ ...pageProps, polished: true }));
    expect(button(tree, '再润色').props.className).not.toContain('bg-neutral-900');
  });
  it('a page reset remount discards old undo and pending suggestion, and explicit restore is one-shot', async () => {
    const pageProps = { ...props(), allowPolish: true, polishPage: true, initialUndoText: '合成原文' };
    let tree = hooks.render(() => VoiceDraftActions(pageProps));
    button(tree, '撤销').props.onClick(); tree = hooks.render(() => VoiceDraftActions(pageProps));
    expect(change).toHaveBeenCalledWith('合成原文');
    expect(nodes(tree).some(node => node.props?.['aria-label'] === '撤销润色')).toBe(false);
    hooks.reset(); change.mockClear();
    refineMock.mockResolvedValue({ text: '合成结果' });
    let current = '开始原文';
    const pendingProps = () => ({ ...pageProps, value: current, initialUndoText: null });
    tree = hooks.render(() => VoiceDraftActions(pendingProps()));
    button(tree, '润色').props.onClick(); current = '执行期间编辑';
    hooks.render(() => VoiceDraftActions(pendingProps())); await flush();
    tree = hooks.render(() => VoiceDraftActions(pendingProps()));
    expect(nodes(tree).some(node => node.props?.['aria-label'] === '应用润色建议')).toBe(true);
    hooks.reset();
    tree = hooks.render(() => VoiceDraftActions({ ...pendingProps(), value: '' }));
    for (const label of ['应用润色建议', '撤销润色']) expect(nodes(tree).some(node => node.props?.['aria-label'] === label)).toBe(false);
  });
});
