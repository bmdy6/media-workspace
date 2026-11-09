import {
  streamMediaChat,
  type ChatStreamInput,
  type MediaChatStreamEvent,
  type StreamedChatResponse,
} from '../../../lib/media-stream';

export interface MediaChatSession {
  sessionId: string;
  epoch: number;
  authToken: string;
}

export interface MediaChatCommand {
  session: MediaChatSession;
  assistantId: string;
  message: string;
  suppressUserEcho: boolean;
  input: ChatStreamInput;
}

export interface MediaChatStreamOptions {
  signal: AbortSignal;
  onStatus: (status: Extract<MediaChatStreamEvent, { type: 'status' }>) => void;
  onDelta: (text: string) => void;
}

export interface MediaChatLifecyclePorts {
  isCurrent: (session: MediaChatSession) => boolean;
  stream: (input: ChatStreamInput, options: MediaChatStreamOptions) => Promise<StreamedChatResponse>;
  onStart: (command: MediaChatCommand) => void;
  onStatus: (status: Extract<MediaChatStreamEvent, { type: 'status' }>, command: MediaChatCommand) => void;
  onDelta: (delta: string, accumulatedText: string, command: MediaChatCommand) => void;
  onComplete: (response: StreamedChatResponse, accumulatedText: string, command: MediaChatCommand) => void;
  onError: (error: unknown, accumulatedText: string, command: MediaChatCommand) => void;
  onFinish: (command: MediaChatCommand) => void;
}

export interface MediaChatLifecycleController {
  send: (command: MediaChatCommand) => Promise<boolean>;
  cancelAll: () => void;
}

export function createMediaChatLifecycleController(
  ports: MediaChatLifecyclePorts,
): MediaChatLifecycleController {
  const active = new Map<number, AbortController>();
  let nextOperationId = 0;

  const ownsCurrentSession = (operationId: number, controller: AbortController, command: MediaChatCommand) =>
    !controller.signal.aborted
    && active.get(operationId) === controller
    && ports.isCurrent(command.session);

  const cancelAll = () => {
    active.forEach((controller) => controller.abort());
    active.clear();
  };

  const send = async (command: MediaChatCommand): Promise<boolean> => {
    if (!ports.isCurrent(command.session)) return false;
    const operationId = ++nextOperationId;
    const controller = new AbortController();
    active.set(operationId, controller);
    let accumulatedText = '';

    try {
      ports.onStart(command);
      const response = await ports.stream(command.input, {
        signal: controller.signal,
        onStatus: (status) => {
          if (ownsCurrentSession(operationId, controller, command)) ports.onStatus(status, command);
        },
        onDelta: (delta) => {
          if (!ownsCurrentSession(operationId, controller, command)) return;
          accumulatedText += delta;
          ports.onDelta(delta, accumulatedText, command);
        },
      });
      if (!ownsCurrentSession(operationId, controller, command)) return false;
      ports.onComplete(response, accumulatedText, command);
      return true;
    } catch (error) {
      if (ownsCurrentSession(operationId, controller, command)) {
        ports.onError(error, accumulatedText, command);
      }
      return false;
    } finally {
      const shouldFinish = ownsCurrentSession(operationId, controller, command);
      if (active.get(operationId) === controller) active.delete(operationId);
      if (shouldFinish) ports.onFinish(command);
    }
  };

  return { send, cancelAll };
}

export const defaultMediaChatStream = streamMediaChat;
