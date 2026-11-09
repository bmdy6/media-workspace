export interface MediaModel {
  id: string;
  label: string;
  enabled: boolean;
  available: boolean;
  verified: boolean;
  availability_reason: string;
  qualities: string[];
  aspect_ratios: string[];
  size_by_aspect_ratio: Record<string, string>;
  thinking_modes: string[];
  supports_vision?: boolean;
}

export interface MediaCapabilities {
  chat_models: MediaModel[];
  image_models: MediaModel[];
  defaults: {
    chat_model: string;
    image_model: string;
    quality: string;
    aspect_ratio: string;
    thinking_mode: string;
    storage_policy: string;
  };
  storage_policies: Record<string, { enabled: boolean }>;
  option_labels: Record<string, Record<string, string>>;
  task_poll_interval_ms: number;
}

export interface MediaPreferences {
  chatModelId: string;
  thinkingMode: string;
  imageModelId: string;
  quality: string;
  aspectRatio: string;
  storagePolicy: string;
}

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringArray(value: unknown, field: string): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || !value.every((item) => typeof item === 'string')) {
    throw new Error(`Media capability field ${field} is invalid`);
  }
  return value;
}

function parseModel(value: unknown, kind: 'chat' | 'image'): MediaModel {
  if (!isRecord(value)
    || typeof value.id !== 'string'
    || typeof value.label !== 'string'
    || typeof value.enabled !== 'boolean'
    || typeof value.available !== 'boolean'
    || typeof value.verified !== 'boolean') {
    throw new Error(`Media ${kind} model entry is invalid`);
  }

  const ratios = stringArray(value.aspect_ratios, 'aspect_ratios');
  const sizes = value.size_by_aspect_ratio;
  if (sizes !== undefined && (!isRecord(sizes) || !Object.values(sizes).every((size) => typeof size === 'string'))) {
    throw new Error('Media capability field size_by_aspect_ratio is invalid');
  }
  if (typeof value.supports_vision !== 'undefined' && typeof value.supports_vision !== 'boolean') {
    throw new Error('Media capability field supports_vision is invalid');
  }

  return {
    id: value.id,
    label: value.label,
    enabled: value.enabled,
    available: value.available,
    verified: value.verified,
    availability_reason: typeof value.availability_reason === 'string' ? value.availability_reason : '',
    qualities: stringArray(value.qualities, 'qualities'),
    aspect_ratios: ratios,
    size_by_aspect_ratio: (sizes || {}) as Record<string, string>,
    thinking_modes: stringArray(value.thinking_modes, 'thinking_modes'),
    ...(typeof value.supports_vision === 'boolean' ? { supports_vision: value.supports_vision } : {}),
  };
}

export function parseMediaCapabilities(value: unknown): MediaCapabilities {
  if (!isRecord(value)
    || !Array.isArray(value.chat_models)
    || !Array.isArray(value.image_models)
    || !isRecord(value.defaults)
    || !isRecord(value.storage_policies)
    || typeof value.task_poll_interval_ms !== 'number'
    || !Number.isFinite(value.task_poll_interval_ms)
    || value.task_poll_interval_ms <= 0) {
    throw new Error('Media capability catalog response is invalid');
  }

  const chatModels = value.chat_models.map((model) => parseModel(model, 'chat'));
  const imageModels = value.image_models.map((model) => parseModel(model, 'image'));
  const defaults = value.defaults;
  const requiredDefaults = ['chat_model', 'image_model', 'quality', 'aspect_ratio', 'thinking_mode', 'storage_policy'];
  if (!requiredDefaults.every((key) => typeof defaults[key] === 'string')) {
    throw new Error('Media capability defaults are invalid');
  }

  const policies = Object.fromEntries(Object.entries(value.storage_policies).map(([key, policy]) => {
    if (!isRecord(policy) || typeof policy.enabled !== 'boolean') {
      throw new Error(`Media storage policy ${key} is invalid`);
    }
    return [key, { enabled: policy.enabled }];
  }));

  const rawLabels = value.option_labels ?? {};
  if (!isRecord(rawLabels)) {
    throw new Error('Media capability field option_labels is invalid');
  }
  const optionLabels: Record<string, Record<string, string>> = {};
  for (const [group, labels] of Object.entries(rawLabels)) {
    if (!isRecord(labels) || !Object.values(labels).every((label) => typeof label === 'string' && label)) {
      throw new Error(`Media capability labels ${group} are invalid`);
    }
    optionLabels[group] = labels as Record<string, string>;
  }

  const result: MediaCapabilities = {
    chat_models: chatModels,
    image_models: imageModels,
    defaults: defaults as MediaCapabilities['defaults'],
    storage_policies: policies,
    option_labels: optionLabels,
    task_poll_interval_ms: value.task_poll_interval_ms,
  };
  const defaultChat = chatModels.find((model) => model.id === result.defaults.chat_model);
  const defaultImage = imageModels.find((model) => model.id === result.defaults.image_model);
  if (!defaultChat || !isModelSelectable(defaultChat)
    || !defaultImage || !isModelSelectable(defaultImage)
    || !defaultChat.thinking_modes.includes(result.defaults.thinking_mode)
    || !defaultImage.qualities.includes(result.defaults.quality)
    || !defaultImage.aspect_ratios.includes(result.defaults.aspect_ratio)
    || !result.storage_policies[result.defaults.storage_policy]?.enabled) {
    throw new Error('Media capability defaults do not match available capabilities');
  }
  return result;
}

export function isModelSelectable(model: MediaModel): boolean {
  return model.enabled && model.available;
}

export function optionLabel(capabilities: MediaCapabilities, group: string, id: string): string {
  return capabilities.option_labels[group]?.[id] ?? id;
}

export function defaultMediaPreferences(capabilities: MediaCapabilities): MediaPreferences {
  return {
    chatModelId: capabilities.defaults.chat_model,
    thinkingMode: capabilities.defaults.thinking_mode,
    imageModelId: capabilities.defaults.image_model,
    quality: capabilities.defaults.quality,
    aspectRatio: capabilities.defaults.aspect_ratio,
    storagePolicy: capabilities.defaults.storage_policy,
  };
}

export function reconcileImageOptions(
  model: MediaModel,
  current: Pick<MediaPreferences, 'quality' | 'aspectRatio'>,
  defaults: Pick<MediaPreferences, 'quality' | 'aspectRatio'>,
): Pick<MediaPreferences, 'quality' | 'aspectRatio'> {
  return {
    quality: model.qualities.includes(current.quality)
      ? current.quality
      : model.qualities.includes(defaults.quality) ? defaults.quality : model.qualities[0] || '',
    aspectRatio: model.aspect_ratios.includes(current.aspectRatio)
      ? current.aspectRatio
      : model.aspect_ratios.includes(defaults.aspectRatio) ? defaults.aspectRatio : model.aspect_ratios[0] || '',
  };
}

export function shouldSubmitOnEnter(input: {
  key: string;
  shiftKey: boolean;
  isComposing: boolean;
  keyCode?: number;
}): boolean {
  return input.key === 'Enter' && !input.shiftKey && !input.isComposing && input.keyCode !== 229;
}
