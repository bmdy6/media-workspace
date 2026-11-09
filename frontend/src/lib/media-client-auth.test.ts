import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { clearMediaToken, ensureMediaSession, fetchWithMediaAuth, getMediaSessionRevision, getStoredMediaToken, loginMediaUser, storeMediaToken, subscribeMediaSessionChanges } from './media-client';

beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal('localStorage', { getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key) });
});
afterEach(() => { clearMediaToken(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('standalone Media authentication', () => {
  it('does not invalidate in-flight work when reading an unchanged token', () => {
    storeMediaToken('same-token');
    const revision = getMediaSessionRevision();
    const changed = vi.fn();
    const unsubscribe = subscribeMediaSessionChanges(changed);
    try {
      expect(getStoredMediaToken()).toBe('same-token');
      expect(getMediaSessionRevision()).toBe(revision);
      expect(changed).not.toHaveBeenCalled();
      localStorage.setItem('media_workspace_token', 'replacement-token');
      expect(getStoredMediaToken()).toBe('replacement-token');
      expect(changed).toHaveBeenCalledTimes(1);
    } finally { unsubscribe(); }
  });
  it('requires explicit login without exchanging host or WebDAV credentials', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    await expect(ensureMediaSession()).rejects.toThrow('请先登录');
    expect(fetch).not.toHaveBeenCalled();
  });
  it('logs in through the existing password endpoint and validates the returned identity', async () => {
    const user = { id: 1, username: 'fixture', is_admin: true };
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ access_token: 'fixture-token', user })));
    vi.stubGlobal('fetch', fetch);
    expect(await loginMediaUser('fixture', 'synthetic-password')).toEqual(user);
    expect(fetch.mock.calls[0][0]).toBe('/api/auth/login');
    expect(getStoredMediaToken()).toBe('fixture-token');
  });
  it('keeps a newer login when a previous account request returns 401', async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise<Response>(resolve => { finish = resolve; })));
    storeMediaToken('old-token'); const revision = getMediaSessionRevision();
    const pending = fetchWithMediaAuth('/api/auth/me');
    storeMediaToken('new-token'); finish(new Response('{}', { status: 401 }));
    await expect(pending).rejects.toThrow();
    expect(getStoredMediaToken()).toBe('new-token');
    expect(getMediaSessionRevision()).toBeGreaterThan(revision);
  });
  it('rejects a completed session validation if the login changed during the request', async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise<Response>(resolve => { finish = resolve; })));
    storeMediaToken('old-token'); const pending = ensureMediaSession();
    storeMediaToken('new-token'); finish(new Response(JSON.stringify({ id: 1, username: 'old', is_admin: false })));
    await expect(pending).rejects.toThrow('账号已更改');
    expect(getStoredMediaToken()).toBe('new-token');
  });
});
