import {
  stableImageRequestId,
  submitImageJob,
  type ImageJobInput,
  type ImageJobSummary,
} from '../../../lib/media-jobs';
import type { ImageJobSession } from './controller';

export type ImageJobSubmissionIntent = Omit<ImageJobInput, 'request_id'>;

export interface ImageJobSubmissionCommand {
  cardId: string;
  session: ImageJobSession;
  intent: ImageJobSubmissionIntent;
}

export interface ImageJobSubmissionPorts {
  isCurrent: (session: ImageJobSession) => boolean;
  submit: (input: ImageJobInput) => Promise<ImageJobSummary>;
  createRequestId: () => string;
  onSubmitting: (cardId: string | null) => void;
  onAccepted: (job: ImageJobSummary, command: ImageJobSubmissionCommand) => void;
  onError: (error: unknown, command: ImageJobSubmissionCommand) => void;
}

export interface ImageJobSubmissionController {
  /** A second concurrent call is ignored. Retrying after an uncertain result is always explicit. */
  submit: (command: ImageJobSubmissionCommand) => Promise<boolean>;
  /** Invalidates the UI result of an in-flight POST; it does not cancel server work. */
  invalidate: (notify?: boolean) => void;
  clearRequestIds: () => void;
}

export function createImageJobSubmissionController(
  ports: ImageJobSubmissionPorts,
): ImageJobSubmissionController {
  const requestIds = new Map<string, { fingerprint: string; requestId: string }>();
  let sequence = 0;
  let activeAttempt: number | null = null;

  const invalidate = (notify = true) => {
    if (activeAttempt === null) return;
    activeAttempt = null;
    sequence += 1;
    if (notify) ports.onSubmitting(null);
  };

  const submit = async (command: ImageJobSubmissionCommand): Promise<boolean> => {
    if (activeAttempt !== null || !ports.isCurrent(command.session)) return false;

    const attempt = ++sequence;
    activeAttempt = attempt;
    ports.onSubmitting(command.cardId);
    const requestId = stableImageRequestId(
      requestIds,
      command.cardId,
      JSON.stringify(command.intent),
      ports.createRequestId,
    );

    try {
      let accepted: ImageJobSummary;
      try {
        accepted = await ports.submit({ ...command.intent, request_id: requestId });
      } catch (error) {
        if (activeAttempt === attempt && ports.isCurrent(command.session)) ports.onError(error, command);
        return false;
      }

      if (activeAttempt !== attempt || !ports.isCurrent(command.session)) return false;
      ports.onAccepted(accepted, command);
      return true;
    } finally {
      if (activeAttempt === attempt) {
        activeAttempt = null;
        if (ports.isCurrent(command.session)) ports.onSubmitting(null);
      }
    }
  };

  return {
    submit,
    invalidate,
    clearRequestIds: () => requestIds.clear(),
  };
}

export const defaultImageJobSubmitter = submitImageJob;
