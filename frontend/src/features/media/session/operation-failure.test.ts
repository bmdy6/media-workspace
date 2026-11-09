import { describe, expect, it } from 'vitest';
import { MediaAuthError } from '../../../lib/media-client';
import { referenceFailureDisposition } from './operation-failure';

const operation = { sessionId: 'session-a', epoch: 1, authToken: 'token-a' };
const current = { ...operation, storedToken: 'token-a' as string | null };
describe('reference failure ownership', () => {
  it('handles a current 401 after the API clears storage', () => {
    expect(referenceFailureDisposition(operation, { ...current, storedToken: null }, new MediaAuthError('expired'))).toBe('auth');
  });
  it.each([
    { sessionId: 'session-b' }, { epoch: 2 }, { authToken: 'token-b' }, { storedToken: 'token-b' },
  ])('does not log out another session or login: %j', change => {
    expect(referenceFailureDisposition(operation, { ...current, storedToken: null, ...change }, new MediaAuthError('expired'))).toBe('ignore');
  });
  it('ignores an explicitly stale 401 even when storage is empty', () => {
    expect(referenceFailureDisposition(operation, { ...current, storedToken: null }, new MediaAuthError('expired', true))).toBe('ignore');
  });
  it('shows current network failures and clears their old feedback on retry', () => {
    expect(referenceFailureDisposition(operation, current, new Error('network'))).toBe('feedback');
    expect(referenceFailureDisposition(operation, current, null)).toBe('feedback');
  });
  it('ignores normal errors after the stored token changes', () => {
    expect(referenceFailureDisposition(operation, { ...current, storedToken: null }, new Error('network'))).toBe('ignore');
  });
});
