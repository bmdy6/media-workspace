import { describe, expect, it, vi } from 'vitest';
import type { ImageJobInput, ImageJobSummary } from '../../../lib/media-jobs';
import {
  createImageJobSubmissionController,
  type ImageJobSubmissionCommand,
  type ImageJobSubmissionPorts,
} from './submission-controller';

const session = { sessionId: 'session-a', epoch: 3, authToken: 'token-a' };
const acceptedJob: ImageJobSummary = {
  id: 41, task_id: 'task-41', status: 'queued', phase: 'queued',
  poll_interval_ms: 300, poll_timeout_ms: 240_000,
};

function command(prompt = '一张产品海报'): ImageJobSubmissionCommand {
  return {
    cardId: 'card-a',
    session,
    intent: {
      prompt,
      model_id: 'image-model',
      style: 'poster',
      aspect_ratio: '3:4',
      quality: 'standard',
      storage_policy: 'keep',
      ref_image_url: null,
      session_id: session.sessionId,
      parent_id: 17,
      proposal_card_id: 'card-a',
    },
  };
}

function makePorts(overrides: Partial<ImageJobSubmissionPorts> = {}): ImageJobSubmissionPorts {
  return {
    isCurrent: () => true,
    submit: async () => acceptedJob,
    createRequestId: () => 'request-a',
    onSubmitting: vi.fn(),
    onAccepted: vi.fn(),
    onError: vi.fn(),
    ...overrides,
  };
}

describe('image job submission controller', () => {
  it('keeps an uncertain request ID for explicit retry, rotates it for edited intent, and never retries automatically', async () => {
    const submitted: ImageJobInput[] = [];
    const submitPort = vi.fn(async (input: ImageJobInput) => {
      submitted.push(input);
      if (submitted.length === 1) throw new TypeError('connection lost');
      return acceptedJob;
    });
    const onError = vi.fn();
    const createRequestId = vi.fn().mockReturnValueOnce('request-first').mockReturnValueOnce('request-edited');
    const controller = createImageJobSubmissionController(makePorts({ submit: submitPort, onError, createRequestId }));

    expect(await controller.submit(command())).toBe(false);
    expect(submitPort).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(await controller.submit(command())).toBe(true);
    expect(await controller.submit(command('一张新的产品海报'))).toBe(true);

    expect(submitted.map((input) => input.request_id)).toEqual([
      'request-first', 'request-first', 'request-edited',
    ]);
    expect(createRequestId).toHaveBeenCalledTimes(2);
    expect(submitPort).toHaveBeenCalledTimes(3);
  });

  it('serializes concurrent confirmation calls and reports one accepted task', async () => {
    let resolveSubmit!: (job: ImageJobSummary) => void;
    const submitPort = vi.fn(() => new Promise<ImageJobSummary>((resolve) => { resolveSubmit = resolve; }));
    const onSubmitting = vi.fn();
    const onAccepted = vi.fn();
    const controller = createImageJobSubmissionController(makePorts({ submit: submitPort, onSubmitting, onAccepted }));

    const first = controller.submit(command());
    expect(await controller.submit(command())).toBe(false);
    expect(submitPort).toHaveBeenCalledTimes(1);
    resolveSubmit(acceptedJob);

    expect(await first).toBe(true);
    expect(onAccepted).toHaveBeenCalledTimes(1);
    expect(onSubmitting.mock.calls).toEqual([['card-a'], [null]]);
  });

  it('ignores a late accepted response after session invalidation without claiming to cancel server work', async () => {
    let resolveSubmit!: (job: ImageJobSummary) => void;
    const onSubmitting = vi.fn();
    const onAccepted = vi.fn();
    const submitPort = vi.fn(() => new Promise<ImageJobSummary>((resolve) => { resolveSubmit = resolve; }));
    const controller = createImageJobSubmissionController(makePorts({ submit: submitPort, onSubmitting, onAccepted }));

    const pending = controller.submit(command());
    controller.invalidate();
    resolveSubmit(acceptedJob);

    expect(await pending).toBe(false);
    expect(submitPort).toHaveBeenCalledTimes(1);
    expect(onAccepted).not.toHaveBeenCalled();
    expect(onSubmitting.mock.calls).toEqual([['card-a'], [null]]);
  });
});
