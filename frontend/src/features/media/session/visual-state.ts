import type { ActiveVisualInput } from '../../../lib/media-visual-input';

export interface MediaVisualSnapshot {
  visualInput: ActiveVisualInput | null;
  attachmentPending: boolean;
  pendingPrompt: string | null;
}

// 会话内图片的唯一记录；上传只提交命令，不持有第二份可修改的当前图片。
class MediaVisualState {
  private snapshot: MediaVisualSnapshot = { visualInput: null, attachmentPending: false, pendingPrompt: null };
  private revision = 0;
  private readonly listeners = new Set<() => void>();
  getSnapshot = (): MediaVisualSnapshot => this.snapshot;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener); return () => this.listeners.delete(listener);
  };
  private publish(next: Partial<MediaVisualSnapshot>): void {
    this.revision++; this.snapshot = { ...this.snapshot, ...next }; this.listeners.forEach(listener => listener());
  }
  replace = (visualInput: ActiveVisualInput | null): void => { this.publish({ visualInput }); };
  setAttachmentPending = (attachmentPending: boolean): void => { this.publish({ attachmentPending }); };
  waitForReference = (pendingPrompt: string | null): void => { this.publish({ pendingPrompt }); };
  commitUpload = (visualInput: ActiveVisualInput): boolean => {
    const resume = Boolean(this.snapshot.pendingPrompt);
    this.publish({ visualInput, attachmentPending: true, pendingPrompt: null }); return resume;
  };
  beginClear = (): { rollback: () => void } | null => {
    if (!this.snapshot.visualInput) return null;
    const previous = this.snapshot;
    this.publish({ visualInput: null, attachmentPending: false });
    const revision = this.revision;
    return { rollback: () => {
      // A failed old clear must not replace a newer upload, history restore or chat image.
      if (revision === this.revision) this.publish({ visualInput: previous.visualInput, attachmentPending: previous.attachmentPending });
    } };
  };
}

export function createMediaVisualState(): MediaVisualState { return new MediaVisualState(); }
