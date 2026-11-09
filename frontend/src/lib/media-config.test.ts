import { describe, expect, it } from 'vitest';
import {
  defaultMediaPreferences,
  isModelSelectable,
  optionLabel,
  parseMediaCapabilities,
  reconcileImageOptions,
  shouldSubmitOnEnter,
  type MediaModel,
} from './media-config';

const payload = {
  chat_models: [
    {
      id: 'chat-a', label: 'Chat A', enabled: true, available: true, verified: false,
      availability_reason: 'configured_unverified', thinking_modes: ['off', 'low'],
    },
    {
      id: 'chat-b', label: 'Chat B', enabled: false, available: false, verified: false,
      availability_reason: 'provider_unverified', thinking_modes: [],
    },
  ],
  image_models: [
    {
      id: 'image-a', label: 'Image A', enabled: true, available: true, verified: false,
      qualities: ['low', 'standard'], aspect_ratios: ['1:1', '3:4'],
      size_by_aspect_ratio: { '1:1': '1024x1024', '3:4': '1024x1365' },
    },
    {
      id: 'image-b', label: 'Image B (unverified)', enabled: false, available: false, verified: false,
      availability_reason: 'provider_unverified', qualities: [], aspect_ratios: [],
    },
  ],
  defaults: {
    chat_model: 'chat-a', image_model: 'image-a', quality: 'low',
    aspect_ratio: '1:1', thinking_mode: 'off', storage_policy: 'cos',
  },
  storage_policies: { cos: { enabled: true }, local: { enabled: false } },
  task_poll_interval_ms: 2000,
};

describe('runtime media capabilities', () => {
  it('keeps verified and availability facts from the server without inventing support', () => {
    const catalog = parseMediaCapabilities(payload);

    expect(catalog.chat_models[0].verified).toBe(false);
    expect(isModelSelectable(catalog.chat_models[0])).toBe(true);
    expect(isModelSelectable(catalog.chat_models[1])).toBe(false);
    expect(isModelSelectable(catalog.image_models[1])).toBe(false);
    expect(defaultMediaPreferences(catalog)).toEqual({
      chatModelId: 'chat-a', thinkingMode: 'off', imageModelId: 'image-a',
      quality: 'low', aspectRatio: '1:1', storagePolicy: 'cos',
    });
  });

  it('rejects defaults that are not present in the advertised capabilities', () => {
    expect(() => parseMediaCapabilities({
      ...payload,
      defaults: { ...payload.defaults, quality: 'master' },
    })).toThrow('defaults do not match');
  });

  it('preserves valid image settings and repairs stale settings from current model capabilities', () => {
    const model: MediaModel = parseMediaCapabilities(payload).image_models[0];
    expect(reconcileImageOptions(model, { quality: 'standard', aspectRatio: '3:4' }, {
      quality: 'low', aspectRatio: '1:1',
    })).toEqual({ quality: 'standard', aspectRatio: '3:4' });
    expect(reconcileImageOptions(model, { quality: 'master', aspectRatio: '16:9' }, {
      quality: 'low', aspectRatio: '1:1',
    })).toEqual({ quality: 'low', aspectRatio: '1:1' });
  });

  it('sends Enter except during Shift+Enter or IME composition', () => {
    expect(shouldSubmitOnEnter({ key: 'Enter', shiftKey: false, isComposing: false })).toBe(true);
    expect(shouldSubmitOnEnter({ key: 'Enter', shiftKey: true, isComposing: false })).toBe(false);
    expect(shouldSubmitOnEnter({ key: 'Enter', shiftKey: false, isComposing: true })).toBe(false);
    expect(shouldSubmitOnEnter({ key: 'Enter', shiftKey: false, isComposing: false, keyCode: 229 })).toBe(false);
  });

  it('reads display labels from capabilities and falls back to the raw id', () => {
    const catalog = parseMediaCapabilities({
      ...payload,
      option_labels: { quality: { low: '快速草稿' }, storage_policy: { cos: '立即存作品库' } },
    });
    expect(optionLabel(catalog, 'quality', 'low')).toBe('快速草稿');
    expect(optionLabel(catalog, 'storage_policy', 'cos')).toBe('立即存作品库');
    expect(optionLabel(catalog, 'storage_policy', 'local')).toBe('local');
    expect(optionLabel(catalog, 'thinking_mode', 'off')).toBe('off');
  });

  it('rejects malformed display labels instead of guessing', () => {
    expect(() => parseMediaCapabilities({ ...payload, option_labels: { quality: { low: '' } } }))
      .toThrow('labels quality are invalid');
    expect(() => parseMediaCapabilities({ ...payload, option_labels: 'quick' }))
      .toThrow('option_labels is invalid');
  });
});
