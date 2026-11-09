import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchChatHistory, fetchChatSessions, fetchUserHistory, updateProposalCardVisualInput, uploadReferenceImage } from './media-client';
import { chooseActiveSessionId, normalizeChatHistory, type ChatSession } from './media-session';

const card = {
  card_id: 'card-1',
  title: '青瓷茶具',
  design_rationale: '拆解茶具结构',
  prompt: '一套宋代青瓷茶具',
  style: '手绘',
  aspect_ratio: '3:4',
  quality: 'low',
  is_modification: false,
};

describe('media chat sessions', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('restores server messages and proposal cards, deriving the latest reference image', () => {
    const restored = normalizeChatHistory({
      messages: [
        { id: 1, role: 'user', content: '做茶具海报', card_data: null, image_url: null },
        {
          id: 2, role: 'assistant', content: '方案如下', card_data: card, image_url: null,
          model_id: 'deepseek-v4-pro', requested_model_id: 'deepseek-v4-pro',
          fallback_from_model_id: 'deepseek-flash', fallback_reason: 'primary unavailable',
          thinking_mode: 'high',
        },
        { id: 3, role: 'system', content: '参考图已上传', card_data: null, image_url: 'https://images.test/ref.png', image_role: 'reference' },
      ],
    });

    expect(restored.messages[1].proposal_card).toEqual(card);
    expect(restored.messages[1]).toMatchObject({
      model_id: 'deepseek-v4-pro', requested_model_id: 'deepseek-v4-pro',
      fallback_from_model_id: 'deepseek-flash', fallback_reason: 'primary unavailable',
      thinking_mode: 'high',
    });
    expect(restored.messages.some((message) => message.image_role === 'reference')).toBe(false);
    expect(restored.activeVisualInput).toEqual({
      url: 'https://images.test/ref.png',
      role: 'reference',
      source: 'upload',
      imageTaskId: null,
    });
  });

  it('uses server visual context state and restores remaining turns', () => {
    const restored = normalizeChatHistory({
      visual_context: {
        url: 'https://images.test/ref.png', role: 'reference', image_task_id: null,
        remaining_turns: 7, turn_limit: 10,
      },
      messages: [
        { id: 1, role: 'system', content: '参考图已上传', image_url: 'https://images.test/ref.png', image_role: 'reference' },
        { id: 2, role: 'user', content: '第一轮' },
      ],
    });

    expect(restored.activeVisualInput).toEqual({
      url: 'https://images.test/ref.png', role: 'reference', source: 'upload', imageTaskId: null,
      remainingTurns: 7, turnLimit: 10,
    });
  });

  it('does not resurrect an expired visual context from legacy image records', () => {
    const restored = normalizeChatHistory({
      visual_context: null,
      messages: [
        { id: 1, role: 'system', content: '参考图已上传', image_url: 'https://images.test/ref.png', image_role: 'reference' },
        { id: 2, role: 'user', content: '已经聊了很多轮' },
      ],
    });

    expect(restored.activeVisualInput).toBeNull();
  });

  it('uses the stored active session only when it belongs to the server list', () => {
    const sessions: ChatSession[] = [
      { session_id: 'newest', title: '新作品', updated_at: '2026-10-05T08:00:00', message_count: 2, last_image_url: null },
      { session_id: 'older', title: '旧作品', updated_at: '2026-10-04T08:00:00', message_count: 4, last_image_url: null },
    ];

    expect(chooseActiveSessionId(sessions, 'older', () => 'fresh-id')).toBe('older');
    expect(chooseActiveSessionId(sessions, 'someone-elses-id', () => 'fresh-id')).toBe('newest');
    expect(chooseActiveSessionId([], null, () => 'fresh-id')).toBe('fresh-id');
  });

  it('requests user-scoped session and history endpoints with authorization', async () => {
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => key === 'media_workspace_token' ? 'test-token' : null,
      setItem: vi.fn(),
    });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ sessions: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ messages: [] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await fetchChatSessions();
    await fetchChatHistory('session / one');

    expect(fetchMock.mock.calls[0][0]).toBe('/api/chat/sessions');
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer test-token');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/chat/history?session_id=session+%2F+one');
    expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe('Bearer test-token');
  });

  it('reports history request failures instead of returning empty history', async () => {
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => key === 'media_workspace_token' ? 'test-token' : null,
      setItem: vi.fn(),
    });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: '会话无权访问' }), { status: 403 }),
    ));

    await expect(fetchChatHistory('private-session')).rejects.toThrow('会话无权访问');
  });

  it('reports gallery history failures instead of displaying an empty gallery', async () => {
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => key === 'media_workspace_token' ? 'test-token' : null,
      setItem: vi.fn(),
    });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: '作品库暂不可用' }), { status: 503 }),
    ));

    await expect(fetchUserHistory()).rejects.toThrow('作品库暂不可用');
  });

  it('persists card-scoped reference changes with authorization', async () => {
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => key === 'media_workspace_token' ? 'test-token' : null,
      setItem: vi.fn(),
    });
    const updatedCard = { ...card, visual_input_url: 'https://images.test/upload.png', visual_input_role: 'reference' as const };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ card: updatedCard }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ card: { ...card, visual_input_url: null, visual_input_role: null } }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(updateProposalCardVisualInput('session-42', 'card-1', updatedCard.visual_input_url)).resolves.toMatchObject(updatedCard);
    await updateProposalCardVisualInput('session-42', 'card-1', null);

    expect(fetchMock.mock.calls[0][0]).toBe('/api/chat/proposal-card/visual-input');
    expect(fetchMock.mock.calls[0][1].method).toBe('PATCH');
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer test-token');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      session_id: 'session-42', card_id: 'card-1',
      visual_input_url: 'https://images.test/upload.png', visual_input_role: 'reference',
    });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toMatchObject({ visual_input_url: null, visual_input_role: null });
  });

  it('sends the active session ID with a reference image upload', async () => {
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => key === 'media_workspace_token' ? 'test-token' : null,
      setItem: vi.fn(),
    });
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({ ref_url: 'https://images.test/upload.png' }), { status: 200 }),
    ));
    vi.stubGlobal('fetch', fetchMock);
    const file = new File(['image'], 'reference.png', { type: 'image/png' });

    const controller = new AbortController();
    await uploadReferenceImage(file, 'session-42', controller.signal);
    await uploadReferenceImage(file, 'session-42', controller.signal, false);

    const form = fetchMock.mock.calls[0][1].body as FormData;
    expect(form.get('session_id')).toBe('session-42');
    expect(form.get('activate_context')).toBeNull();
    const cardForm = fetchMock.mock.calls[1][1].body as FormData;
    expect(cardForm.get('session_id')).toBe('session-42');
    expect(cardForm.get('activate_context')).toBe('false');
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer test-token');
    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
  });
});
