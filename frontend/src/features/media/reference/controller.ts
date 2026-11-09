import { clearVisualInput, updateProposalCardVisualInput, uploadReferenceImage, type ProposalCard } from '../../../lib/media-client';
import { activeVisualInputFromContext, type ActiveVisualInput } from '../../../lib/media-visual-input';

export interface MediaSessionOperation { sessionId: string; epoch: number; authToken: string; }
export interface ReferenceSessionPort {
  capture(): MediaSessionOperation | null;
  isCurrent(operation: MediaSessionOperation): boolean;
  canUseCard(operation: MediaSessionOperation, cardId: string): boolean;
  commitUploadedReference(operation: MediaSessionOperation, visual: ActiveVisualInput): { resume: boolean } | null;
  commitCard(operation: MediaSessionOperation, card: ProposalCard): boolean;
  beginComposerClear(operation: MediaSessionOperation): { rollback: () => void } | null;
  reportFailure(operation: MediaSessionOperation, error: unknown | null, fallback: string): void;
}
export interface ReferenceUploadSnapshot {
  preview: { previewUrl: string; fileName: string; cardId: string | null } | null;
  mutationCardId: string | null;
}
export interface UploadedReferenceReceipt {
  operation: MediaSessionOperation;
  visualInput: ActiveVisualInput;
  resume: boolean;
  isCurrent: () => boolean;
}

class ReferenceUploadController {
  private snapshot: ReferenceUploadSnapshot = { preview: null, mutationCardId: null };
  private readonly listeners = new Set<() => void>();
  private readonly previews = new Set<string>();
  private selection: { operation: MediaSessionOperation; cardId: string | null } | null = null;
  private request: AbortController | null = null;
  private clearing: AbortController | null = null;
  private version = 0;
  private disposed = false;
  constructor(private readonly owner: ReferenceSessionPort) {}
  getSnapshot = (): ReferenceUploadSnapshot => this.snapshot;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener); return () => this.listeners.delete(listener);
  };
  private publish(next: Partial<ReferenceUploadSnapshot>): void {
    if (this.disposed) return;
    this.snapshot = { ...this.snapshot, ...next }; this.listeners.forEach(listener => listener());
  }
  private current(operation: MediaSessionOperation, request?: AbortController): boolean {
    return !this.disposed && !request?.signal.aborted && this.owner.isCurrent(operation);
  }
  private report(operation: MediaSessionOperation, request: AbortController, error: unknown, fallback: string): void {
    // API 可能已因 401 清掉令牌；会话 owner 决定错误归属，取消/卸载仍保持静默。
    if (!this.disposed && !request.signal.aborted) this.owner.reportFailure(operation, error, fallback);
  }
  private available(): boolean { return !this.disposed && !this.request; }
  private release(url: string): void { if (this.previews.delete(url)) URL.revokeObjectURL(url); }
  prepare = (cardId: string | null): boolean => {
    if (!this.available()) return false;
    const operation = this.owner.capture();
    if (!operation || !this.current(operation) || (cardId && !this.owner.canUseCard(operation, cardId))) return false;
    this.selection = { operation: { ...operation }, cardId }; return true;
  };
  upload = async (file: File): Promise<UploadedReferenceReceipt | null> => {
    const selection = this.selection;
    if (!selection || !this.available() || !this.current(selection.operation) ||
        (selection.cardId && !this.owner.canUseCard(selection.operation, selection.cardId))) return null;
    this.selection = null;
    const { operation, cardId } = selection, request = new AbortController(), version = ++this.version;
    const previewUrl = URL.createObjectURL(file); this.previews.add(previewUrl); this.request = request;
    this.publish({ preview: { previewUrl, fileName: file.name, cardId } }); this.owner.reportFailure(operation, null, '');
    try {
      const upload = await uploadReferenceImage(file, operation.sessionId, request.signal, !cardId);
      if (!this.current(operation, request)) return null;
      const visualInput = activeVisualInputFromContext(upload.visual_context)
        ?? { url: upload.ref_url, role: 'reference' as const, source: 'upload' as const, imageTaskId: null };
      let resume = false;
      if (cardId) {
        const card = await updateProposalCardVisualInput(operation.sessionId, cardId, visualInput.url, request.signal);
        if (!this.current(operation, request)) return null;
        if (card.card_id !== cardId) throw new Error('方案卡参考图响应不匹配');
        if (!this.owner.commitCard(operation, card)) return null;
      } else {
        const receipt = this.owner.commitUploadedReference(operation, visualInput);
        if (!receipt) return null;
        resume = receipt.resume;
      }
      return { operation, visualInput, resume,
        isCurrent: () => version === this.version && this.current(operation, request) };
    } catch (error: unknown) {
      this.report(operation, request, error, '上传参考图失败');
      return null;
    } finally {
      if (this.request === request) { this.request = null; this.publish({ preview: null }); }
      this.release(previewUrl);
    }
  };
  removeCard = async (cardId: string): Promise<boolean> => {
    if (!this.available()) return false;
    const operation = this.owner.capture();
    if (!operation || !this.current(operation) || !this.owner.canUseCard(operation, cardId)) return false;
    const request = new AbortController(); this.request = request;
    this.publish({ mutationCardId: cardId }); this.owner.reportFailure(operation, null, '');
    try {
      const card = await updateProposalCardVisualInput(operation.sessionId, cardId, null, request.signal);
      if (!this.current(operation, request)) return false;
      if (card.card_id !== cardId) throw new Error('方案卡参考图响应不匹配');
      return this.owner.commitCard(operation, card);
    } catch (error: unknown) {
      this.report(operation, request, error, '移除方案卡参考图失败，请重试');
      return false;
    } finally {
      if (this.request === request) { this.request = null; this.publish({ mutationCardId: null }); }
    }
  };
  clearComposer = async (): Promise<boolean> => {
    if (this.disposed || this.clearing) return false;
    const operation = this.owner.capture();
    if (!operation || !this.current(operation)) return false;
    const receipt = this.owner.beginComposerClear(operation);
    if (!receipt) return true;
    const request = new AbortController(); this.clearing = request;
    try {
      await clearVisualInput(operation.sessionId, request.signal); return this.current(operation, request);
    } catch (error: unknown) {
      if (this.current(operation, request)) receipt.rollback();
      this.report(operation, request, error, '取消参考图失败，请重试');
      return false;
    } finally { if (this.clearing === request) this.clearing = null; }
  };
  cancel = (): void => {
    this.version++; this.request?.abort(); this.request = null; this.clearing?.abort(); this.clearing = null; this.selection = null;
    this.previews.forEach(url => this.release(url)); this.publish({ preview: null, mutationCardId: null });
  };
  dispose = (): void => { this.disposed = true; this.cancel(); this.listeners.clear(); };
}

export function createReferenceUploadController(owner: ReferenceSessionPort): ReferenceUploadController {
  return new ReferenceUploadController(owner);
}
