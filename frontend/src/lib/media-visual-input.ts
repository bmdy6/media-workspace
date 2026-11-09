import type { ChatMessageItem, ProposalCard, VisualContextState, VisualInputRole } from './media-client';

export type VisualInputSource = 'upload' | 'artwork';

export interface ActiveVisualInput {
  url: string;
  sourceUrl?: string;
  unavailable?: boolean;
  role: VisualInputRole;
  source: VisualInputSource;
  imageTaskId: number | string | null;
  remainingTurns?: number;
  turnLimit?: number;
}

export interface GenerationVisualInput {
  refImageUrl: string | null;
  parentId: number | string | null;
  valid: boolean;
}

export function generationVisualInput(
  card: ProposalCard,
  active: ActiveVisualInput | null,
): GenerationVisualInput {
  const role = card.visual_input_role
    ?? (card.is_modification && card.base_image_url ? 'base' : null);
  const url = card.visual_input_url
    ?? (role === 'base' ? card.base_image_url ?? null : null);

  if (!url || !role) return { refImageUrl: null, parentId: null, valid: true };
  // A persisted user reference belongs to the proposal card itself. It remains
  // valid even when the session's latest visual context changes to another card.
  if (role === 'reference') return { refImageUrl: url, parentId: null, valid: true };
  // Artwork/base edits still need the active task so we can preserve parent_id.
  if (!active || active.unavailable || (active.sourceUrl ?? active.url) !== url || active.role !== role) {
    return { refImageUrl: null, parentId: null, valid: false };
  }

  return { refImageUrl: url, parentId: active.imageTaskId, valid: true };
}

export function activeVisualInputFromContext(context: VisualContextState | null | undefined,
  artworks: ChatMessageItem[] = [], previous: ActiveVisualInput | null = null): ActiveVisualInput | null {
  if (!context) return null;
  const artwork = context.role !== 'base' || context.image_task_id == null ? null : artworks.find(message => message.image_task_id === context.image_task_id && message.image_url);
  const sameRole = previous?.role === context.role;
  const sameTask = context.image_task_id != null && previous?.imageTaskId === context.image_task_id;
  const sameSource = previous && (previous.sourceUrl ?? previous.url) === context.url;
  const preview = artwork?.image_url ?? (sameRole && (sameTask || sameSource) ? previous?.url : null);
  return {
    url: preview || context.url,
    ...(preview && preview !== context.url ? { sourceUrl: context.url } : {}),
    role: context.role,
    source: context.role === 'reference' ? 'upload' : 'artwork',
    imageTaskId: context.image_task_id ?? null,
    remainingTurns: context.remaining_turns,
    turnLimit: context.turn_limit,
  };
}

export function proposalImagePreview(card: ProposalCard, active: ActiveVisualInput | null, artworks: ChatMessageItem[]): string | undefined {
  if (card.visual_preview_url) return card.visual_preview_url;
  const url = card.visual_input_url || card.base_image_url;
  if (!url) return undefined;
  if (active && (active.sourceUrl ?? active.url) === url) return active.unavailable ? undefined : active.url;
  return artworks.find(message => message.task_id && `/api/image/result/${message.task_id}` === url)?.image_url || (url.startsWith('/api/image/result/') ? undefined : url);
}

export async function restoreActiveVisualPreview(active: ActiveVisualInput | null,
  resolve: (url: string) => Promise<string | null>, onError: (error: unknown) => void): Promise<ActiveVisualInput | null> {
  if (!active) return null;
  try {
    const preview = await resolve(active.url);
    if (!preview) return { ...active, unavailable: true };
    return preview === active.url ? active : { ...active, sourceUrl: active.url, url: preview };
  } catch (error) {
    onError(error);
    // Keep identity so the page can expose its existing explicit clear action.
    return { ...active, unavailable: true };
  }
}

export function visualInputForChat(
  active: ActiveVisualInput | null,
  attachmentPending: boolean,
  resumeAfterReference = false,
): ActiveVisualInput | null {
  return attachmentPending || resumeAfterReference ? active : null;
}
