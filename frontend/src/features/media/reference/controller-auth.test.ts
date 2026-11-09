import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { clearMediaToken, getStoredMediaToken, MediaAuthError, storeMediaToken } from '../../../lib/media-client';
import { createReferenceUploadController, type MediaSessionOperation } from './controller';
import { createMediaVisualState } from '../session/visual-state';
import { referenceFailureDisposition } from '../session/operation-failure';

beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal('localStorage', { getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key) });
  storeMediaToken('fixture-token');
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:fixture');
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
});
afterEach(() => { clearMediaToken(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function setup() {
  const current = { sessionId: 'fixture-session', epoch: 1, authToken: 'fixture-token' };
  const reports = vi.fn(), visual = createMediaVisualState();
  const controller = createReferenceUploadController({
    capture: () => ({ ...current }),
    isCurrent: (op: MediaSessionOperation) => op.sessionId === current.sessionId && op.epoch === current.epoch && op.authToken === getStoredMediaToken(),
    canUseCard: () => true,
    commitUploadedReference: (_op, image) => ({ resume: visual.commitUpload(image) }),
    commitCard: () => true, beginComposerClear: () => visual.beginClear(),
    reportFailure: (op, error, fallback) => {
      const disposition = referenceFailureDisposition(op, { ...current, storedToken: getStoredMediaToken() }, error);
      if (disposition !== 'ignore') reports(op, error, fallback);
    },
  });
  const run = (action: string) => {
    if (action === 'clear') { visual.commitUpload({ url: '/fixture.png', role: 'reference', source: 'upload', imageTaskId: null }); return controller.clearComposer(); }
    if (action === 'remove') return controller.removeCard('fixture-card');
    controller.prepare(null); return controller.upload(new File(['fixture'], 'fixture.png', { type: 'image/png' }));
  };
  return { current, reports, controller, visual, run };
}

describe('reference requests with the real authentication client', () => {
  it.each(['upload', 'remove', 'clear'])('reports the current %s 401 after the client clears the stored token', async action => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })));
    const { controller, reports, visual, run } = setup(); await run(action);
    expect(getStoredMediaToken()).toBeNull();
    expect(reports).toHaveBeenCalledWith(expect.anything(), expect.any(MediaAuthError), expect.any(String));
    if (action === 'clear') expect(visual.getSnapshot().visualInput).toBeNull();
    controller.dispose();
  });
  it.each(['upload', 'remove', 'clear'])('isolates a late %s 401 from a replacement token', async action => {
    let resolve!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise<Response>(done => { resolve = done; })));
    const { controller, reports, current, run } = setup(); const pending = run(action);
    storeMediaToken('new-token'); current.authToken = 'new-token';
    resolve(new Response('{}', { status: 401 })); await pending;
    expect(getStoredMediaToken()).toBe('new-token');
    expect(reports.mock.calls.filter(call => call[1] instanceof MediaAuthError)).toHaveLength(0); controller.dispose();
  });
  it.each(['upload', 'remove', 'clear'])('does not report an old %s failure after leaving its session', async action => {
    let resolve!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise<Response>(done => { resolve = done; })));
    const { controller, reports, current, run } = setup(); const pending = run(action);
    current.sessionId = 'new-session'; resolve(new Response('{"detail":"old failure"}', { status: 503 })); await pending;
    expect(reports.mock.calls.filter(call => call[1])).toHaveLength(0); controller.dispose();
  });
  it.each(['cancel', 'dispose'] as const)('keeps a completed 401 silent after %s', async action => {
    let resolve!: (response: Response) => void;
    const fetchMock = vi.fn().mockReturnValue(new Promise<Response>(done => { resolve = done; })); vi.stubGlobal('fetch', fetchMock);
    const { controller, reports, run } = setup(); const pending = run('upload');
    controller[action](); expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    resolve(new Response('{}', { status: 401 })); await pending;
    expect(reports.mock.calls.filter(call => call[1])).toHaveLength(0); controller.dispose();
  });
});
