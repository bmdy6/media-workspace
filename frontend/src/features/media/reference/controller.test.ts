import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ActiveVisualInput } from '../../../lib/media-visual-input';
import type { ProposalCard } from '../../../lib/media-client';

const api = vi.hoisted(() => ({ upload: vi.fn(), patch: vi.fn(), clear: vi.fn() }));
vi.mock('../../../lib/media-client', () => ({ uploadReferenceImage: api.upload, updateProposalCardVisualInput: api.patch, clearVisualInput: api.clear }));
const visual: ActiveVisualInput = { url: '/fixture/reference.png', role: 'reference', source: 'upload', imageTaskId: null };
const card: ProposalCard = { card_id: 'card-a', title: 'Fixture', prompt: 'Fixture', design_rationale: '', style: '', aspect_ratio: '1:1', quality: 'standard', is_modification: false };
const createUrl = vi.fn(), revokeUrl = vi.fn();
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; }
async function make() {
  const env = { sessionId: 'session-a', epoch: 1, authToken: 'fixture-token-a', submitted: false };
  const owner = (await import('../session/visual-state')).createMediaVisualState();
  const errors = vi.fn(), cards = vi.fn();
  const port = {
    capture: () => ({ sessionId: env.sessionId, epoch: env.epoch, authToken: env.authToken }),
    isCurrent: (op: { sessionId: string; epoch: number; authToken: string }) => op.sessionId === env.sessionId && op.epoch === env.epoch && op.authToken === env.authToken,
    canUseCard: () => !env.submitted,
    commitUploadedReference: (_op: unknown, value: ActiveVisualInput) => ({ resume: owner.commitUpload(value) }),
    commitCard: (_op: unknown, value: ProposalCard) => { cards(value); return true; },
    beginComposerClear: () => owner.beginClear(), reportFailure: errors,
  };
  const controller = (await import('./controller')).createReferenceUploadController(port);
  return { env, owner, errors, cards, controller };
}
const file = () => new File(['fixture'], 'reference.png', { type: 'image/png' });
beforeEach(() => {
  api.upload.mockReset().mockResolvedValue({ ref_url: visual.url, visual_context: null }); api.patch.mockReset().mockResolvedValue({ ...card, visual_input_url: visual.url, visual_input_role: 'reference' }); api.clear.mockReset().mockResolvedValue(undefined);
  createUrl.mockReset().mockReturnValue('blob:fixture-1'); revokeUrl.mockReset(); vi.spyOn(URL, 'createObjectURL').mockImplementation(createUrl); vi.spyOn(URL, 'revokeObjectURL').mockImplementation(revokeUrl);
});
afterEach(() => vi.restoreAllMocks());

describe('reference upload lifecycle', () => {
  it('uploads the composer image and commits through its sole owner', async () => {
    const { controller, owner } = await make(); controller.prepare(null); const receipt = await controller.upload(file());
    expect(api.upload).toHaveBeenCalledWith(expect.any(File), 'session-a', expect.any(AbortSignal), true);
    expect(owner.getSnapshot()).toMatchObject({ visualInput: visual, attachmentPending: true }); expect(receipt).toMatchObject({ visualInput: visual, resume: false }); expect(receipt?.isCurrent()).toBe(true); expect(revokeUrl).toHaveBeenCalledOnce(); expect(controller.getSnapshot().preview).toBeNull(); controller.dispose();
  });
  it('shows the real pending preview and deduplicates another submission', async () => {
    const wait = deferred<{ ref_url: string; visual_context: null }>(); api.upload.mockReturnValueOnce(wait.promise); const { controller } = await make(); controller.prepare(null); const uploading = controller.upload(file());
    expect(controller.getSnapshot().preview).toMatchObject({ previewUrl: 'blob:fixture-1', fileName: 'reference.png', cardId: null }); expect(await controller.upload(file())).toBeNull(); expect(api.upload).toHaveBeenCalledOnce(); wait.resolve({ ref_url: visual.url, visual_context: null }); await uploading; controller.dispose();
  });
  it('attaches card uploads without activating a second composer context', async () => {
    const { controller, owner, cards } = await make(); controller.prepare('card-a'); await controller.upload(file());
    expect(api.upload).toHaveBeenCalledWith(expect.any(File), 'session-a', expect.any(AbortSignal), false); expect(api.patch).toHaveBeenCalledWith('session-a', 'card-a', visual.url, expect.any(AbortSignal)); expect(cards).toHaveBeenCalledWith(expect.objectContaining({ card_id: 'card-a', visual_input_url: visual.url })); expect(owner.getSnapshot().visualInput).toBeNull(); controller.dispose();
  });
  it('preserves uploaded visual context role and turn information', async () => {
    api.upload.mockResolvedValueOnce({ ref_url: '/fallback.png', visual_context: { url: '/context.png', role: 'reference', remaining_turns: 3, turn_limit: 4 } }); const { controller, owner } = await make(); controller.prepare(null); await controller.upload(file()); expect(owner.getSnapshot().visualInput).toMatchObject({ url: '/context.png', remainingTurns: 3, turnLimit: 4 }); controller.dispose();
  });
  it('returns a guarded continuation receipt and consumes the pending prompt once', async () => {
    const { controller, owner } = await make(); owner.waitForReference('fixture prompt'); controller.prepare(null); const receipt = await controller.upload(file()); expect(receipt?.resume).toBe(true); expect(owner.getSnapshot().pendingPrompt).toBeNull(); controller.cancel(); expect(receipt?.isCurrent()).toBe(false); controller.dispose();
  });
  it('reports upload failure, releases preview and allows explicit retry', async () => {
    api.upload.mockRejectedValueOnce(new Error('fixture network')); const { controller, errors, owner } = await make(); controller.prepare(null); expect(await controller.upload(file())).toBeNull(); expect(owner.getSnapshot().visualInput).toBeNull(); expect(errors).toHaveBeenCalledWith(expect.anything(), expect.any(Error), '上传参考图失败'); expect(revokeUrl).toHaveBeenCalledOnce(); controller.prepare(null); expect(await controller.upload(file())).not.toBeNull(); controller.dispose();
  });
  it('does not commit a card until its persistence succeeds', async () => {
    api.patch.mockRejectedValueOnce(new Error('fixture patch')); const { controller, cards } = await make(); controller.prepare('card-a'); expect(await controller.upload(file())).toBeNull(); expect(cards).not.toHaveBeenCalled(); expect(revokeUrl).toHaveBeenCalledOnce(); controller.dispose();
  });
  it.each(['sessionId', 'epoch', 'authToken'] as const)('ignores a late upload when %s changes', async key => {
    const wait = deferred<{ ref_url: string; visual_context: null }>(); api.upload.mockReturnValueOnce(wait.promise); const { controller, env, cards, owner } = await make(); controller.prepare('card-a'); const uploading = controller.upload(file());
    if (key === 'epoch') env.epoch++; else env[key] += '-new'; wait.resolve({ ref_url: visual.url, visual_context: null }); expect(await uploading).toBeNull(); expect(api.patch).not.toHaveBeenCalled(); expect(cards).not.toHaveBeenCalled(); expect(owner.getSnapshot().visualInput).toBeNull(); controller.dispose();
  });
  it('does not upload to another session after a file picker was opened', async () => {
    const { controller, env } = await make(); controller.prepare('card-a'); env.sessionId = 'session-b'; expect(await controller.upload(file())).toBeNull(); expect(api.upload).not.toHaveBeenCalled(); controller.dispose();
  });
  it('blocks a card that was submitted while its file picker was open', async () => {
    const { controller, env } = await make(); controller.prepare('card-a'); env.submitted = true; expect(await controller.upload(file())).toBeNull(); expect(api.upload).not.toHaveBeenCalled(); controller.dispose();
  });
  it('aborts and releases previews on cancel with no late error feedback', async () => {
    const wait = deferred<{ ref_url: string; visual_context: null }>(); api.upload.mockReturnValueOnce(wait.promise); const { controller, errors, owner } = await make(); controller.prepare(null); const uploading = controller.upload(file()); const signal = api.upload.mock.calls[0][2]; controller.cancel(); expect(signal.aborted).toBe(true); expect(controller.getSnapshot().preview).toBeNull(); expect(revokeUrl).toHaveBeenCalledOnce(); wait.resolve({ ref_url: visual.url, visual_context: null }); expect(await uploading).toBeNull(); expect(owner.getSnapshot().visualInput).toBeNull(); expect(errors.mock.calls.filter(call => call[1])).toHaveLength(0); expect(revokeUrl).toHaveBeenCalledOnce(); controller.dispose();
  });
  it('does not let the old finally clear a newer upload preview', async () => {
    const first = deferred<{ ref_url: string; visual_context: null }>(), second = deferred<{ ref_url: string; visual_context: null }>(); api.upload.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise); createUrl.mockReturnValueOnce('blob:first').mockReturnValueOnce('blob:second');
    const { controller } = await make(); controller.prepare(null); const old = controller.upload(file()); controller.cancel(); controller.prepare(null); const current = controller.upload(file()); first.resolve({ ref_url: visual.url, visual_context: null }); await old; expect(controller.getSnapshot().preview?.previewUrl).toBe('blob:second'); second.resolve({ ref_url: visual.url, visual_context: null }); await current; expect(revokeUrl.mock.calls.map(call => call[0])).toEqual(['blob:first', 'blob:second']); controller.dispose();
  });
  it('disposes the transfer and prevents a late patch commit', async () => {
    const wait = deferred<ProposalCard>(); api.patch.mockReturnValueOnce(wait.promise); const { controller, cards } = await make(); controller.prepare('card-a'); const uploading = controller.upload(file()); await vi.waitFor(() => expect(api.patch).toHaveBeenCalledOnce()); const signal = api.patch.mock.calls[0][3]; controller.dispose(); expect(signal.aborted).toBe(true); wait.resolve(card); expect(await uploading).toBeNull(); expect(cards).not.toHaveBeenCalled(); expect(revokeUrl).toHaveBeenCalledOnce();
  });
  it('removes a card reference through the original endpoint', async () => {
    api.patch.mockResolvedValueOnce({ ...card, visual_input_url: null }); const { controller, cards } = await make(); expect(await controller.removeCard('card-a')).toBe(true); expect(api.patch).toHaveBeenCalledWith('session-a', 'card-a', null, expect.any(AbortSignal)); expect(cards).toHaveBeenCalledWith(expect.objectContaining({ visual_input_url: null })); expect(controller.getSnapshot().mutationCardId).toBeNull(); controller.dispose();
  });
  it('keeps the card unchanged on removal failure and rejects submitted cards', async () => {
    api.patch.mockRejectedValueOnce(new Error('fixture patch')); const { controller, cards, env, errors } = await make(); expect(await controller.removeCard('card-a')).toBe(false); expect(cards).not.toHaveBeenCalled(); expect(errors).toHaveBeenCalledWith(expect.anything(), expect.any(Error), '移除方案卡参考图失败，请重试'); env.submitted = true; expect(await controller.removeCard('card-a')).toBe(false); expect(api.patch).toHaveBeenCalledOnce(); controller.dispose();
  });
  it('clears the composer optimistically and restores the same image on failure', async () => {
    api.clear.mockRejectedValueOnce(new Error('fixture clear')); const { controller, owner } = await make(); owner.commitUpload(visual); expect(await controller.clearComposer()).toBe(false); expect(owner.getSnapshot()).toMatchObject({ visualInput: visual, attachmentPending: true }); controller.dispose();
  });
  it('does not roll a failed clear back over a newer image', async () => {
    let reject!: (reason: Error) => void; api.clear.mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; })); const { controller, owner } = await make(); owner.commitUpload(visual); const clearing = controller.clearComposer(); expect(owner.getSnapshot().visualInput).toBeNull(); owner.commitUpload({ ...visual, url: '/new.png' }); reject(new Error('fixture clear')); await clearing; expect(owner.getSnapshot().visualInput?.url).toBe('/new.png'); controller.dispose();
  });
  it('does not restore the old image after a clear fails in another session', async () => {
    let reject!: (reason: Error) => void; api.clear.mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; })); const { controller, owner, env } = await make(); owner.commitUpload(visual); const clearing = controller.clearComposer(); env.sessionId = 'session-b'; owner.replace({ ...visual, url: '/other.png' }); reject(new Error('fixture clear')); await clearing; expect(owner.getSnapshot().visualInput?.url).toBe('/other.png'); controller.dispose();
  });
  it('deduplicates clears and does not request when there is no current image', async () => {
    const { controller, owner } = await make(); expect(await controller.clearComposer()).toBe(true); expect(api.clear).not.toHaveBeenCalled(); owner.commitUpload(visual); const wait = deferred<void>(); api.clear.mockReturnValueOnce(wait.promise); const first = controller.clearComposer(); expect(await controller.clearComposer()).toBe(false); wait.resolve(); expect(await first).toBe(true); expect(api.clear).toHaveBeenCalledOnce(); controller.dispose();
  });
});
