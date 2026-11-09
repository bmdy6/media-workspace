import { describe, expect, it } from 'vitest';
import { activeVisualInputFromContext, generationVisualInput, proposalImagePreview, restoreActiveVisualPreview, visualInputForChat, type ActiveVisualInput } from './media-visual-input';
import type { ProposalCard } from './media-client';

const baseCard: ProposalCard = {
  card_id: 'card-1',
  title: 'test',
  design_rationale: 'test',
  prompt: 'test',
  style: 'portrait',
  aspect_ratio: '3:4',
  quality: 'low',
  is_modification: false,
};

describe('media visual input', () => {
  it('keeps the canonical local reference and parent when its preview is a blob', () => {
    const card = { ...baseCard, is_modification: true, visual_input_role: 'base' as const,
      visual_input_url: '/api/image/result/owned-task' };
    const active = { url: 'blob:local-preview', sourceUrl: card.visual_input_url,
      role: 'base' as const, source: 'artwork' as const, imageTaskId: 42 };
    expect(generationVisualInput(card, active)).toEqual({ refImageUrl: card.visual_input_url, parentId: 42, valid: true });
    expect(generationVisualInput(card, { ...active, sourceUrl: '/api/image/result/different-task' }).valid).toBe(false);
  });
  it('restores a local task preview without replacing its persisted canonical identity', () => {
    const context = { url: '/api/image/result/owned-task', role: 'base' as const, image_task_id: 42, remaining_turns: 9, turn_limit: 10 };
    const artworks = [{ id:'result',role:'assistant' as const,content:'', image_task_id:42,task_id:'owned-task',image_url:'blob:original' }];
    const active = activeVisualInputFromContext(context, artworks);
    expect(active?.url).toBe('blob:original');
    expect(active?.sourceUrl).toBe(context.url);
    const card = { ...baseCard, is_modification:true, visual_input_role:'base' as const, visual_input_url:context.url };
    expect(proposalImagePreview(card,active,artworks)).toBe('blob:original');
    expect(generationVisualInput(card,active).parentId).toBe(42);
  });
  it('does not reuse an unrelated reference preview when both task IDs are null', () => {
    const previous = { url:'https://images.test/old.png',role:'reference' as const,source:'upload' as const,imageTaskId:null };
    const context = { url:'https://images.test/new.png',role:'reference' as const,image_task_id:null,remaining_turns:9,turn_limit:10 };
    expect(activeVisualInputFromContext(context,[],previous)?.url).toBe(context.url);
  });
  it('retains an expired base identity for explicit clearing without throwing away history', async () => {
    const active = { url:'/api/image/result/expired',role:'base' as const,source:'artwork' as const,imageTaskId:42 };
    const errors:unknown[]=[];
    const restored = await restoreActiveVisualPreview(active, async()=>{throw new Error('expired');}, error=>errors.push(error));
    expect(errors).toHaveLength(1);
    expect(restored).toEqual({...active,unavailable:true});
    const card = {...baseCard,is_modification:true,visual_input_role:'base' as const,visual_input_url:active.url};
    expect(generationVisualInput(card,restored).valid).toBe(false);
    expect(proposalImagePreview(card,restored,[])).toBeUndefined();
  });
  it('passes a user reference to generation even when the card is not a modification', () => {
    const active: ActiveVisualInput = {
      url: 'https://images.test/me.png',
      role: 'reference',
      source: 'upload',
      imageTaskId: null,
    };
    const card = {
      ...baseCard,
      visual_input_url: active.url,
      visual_input_role: 'reference' as const,
    };

    expect(generationVisualInput(card, active)).toEqual({
      refImageUrl: 'https://images.test/me.png',
      parentId: null,
      valid: true,
    });
  });

  it('uses an artwork base as the parent when explicitly editing it', () => {
    const active: ActiveVisualInput = {
      url: 'https://images.test/art.png',
      role: 'base',
      source: 'artwork',
      imageTaskId: 42,
    };
    const card = {
      ...baseCard,
      is_modification: true,
      visual_input_url: active.url,
      visual_input_role: 'base' as const,
    };

    expect(generationVisualInput(card, active)).toEqual({
      refImageUrl: 'https://images.test/art.png',
      parentId: 42,
      valid: true,
    });
  });
  it('keeps each card-scoped user reference valid when session context changes', () => {
    const card = {
      ...baseCard,
      visual_input_url: 'https://images.test/card-a.png',
      visual_input_role: 'reference' as const,
    };

    expect(generationVisualInput(card, null)).toEqual({
      refImageUrl: 'https://images.test/card-a.png', parentId: null, valid: true,
    });
    expect(generationVisualInput(card, {
      url: 'https://images.test/card-b.png', role: 'reference', source: 'upload', imageTaskId: null,
    })).toEqual({ refImageUrl: 'https://images.test/card-a.png', parentId: null, valid: true });
  });

  it('still invalidates a base-image edit after its active artwork context changes', () => {
    const card = {
      ...baseCard,
      is_modification: true,
      base_image_url: 'https://images.test/base.png',
      visual_input_url: 'https://images.test/base.png',
      visual_input_role: 'base' as const,
    };
    expect(generationVisualInput(card, null).valid).toBe(false);
  });


  it('maps server visual context metadata into active input state', () => {
    expect(activeVisualInputFromContext({
      url: 'https://images.test/ref.png', role: 'reference', image_task_id: null, remaining_turns: 6, turn_limit: 10,
    })).toEqual({
      url: 'https://images.test/ref.png', role: 'reference', source: 'upload', imageTaskId: null, remainingTurns: 6, turnLimit: 10,
    });
    expect(activeVisualInputFromContext(null)).toBeNull();
  });

  it('only attaches the visual explicitly before its first successful send', () => {
    const active: ActiveVisualInput = {
      url: 'https://images.test/ref.png', role: 'reference', source: 'upload', imageTaskId: null,
      remainingTurns: 9, turnLimit: 10,
    };
    expect(visualInputForChat(active, true)).toBe(active);
    expect(visualInputForChat(active, false)).toBeNull();
    expect(visualInputForChat(active, false, true)).toBe(active);
  });
});
