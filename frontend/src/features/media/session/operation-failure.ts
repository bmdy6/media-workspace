import { MediaAuthError } from '../../../lib/media-client';
import type { MediaSessionOperation } from '../reference/controller';

// 401 会先清持久令牌；仍须让发起它的当前会话退出登录，旧会话/新账号则不能受影响。
export function referenceFailureDisposition(
  operation: MediaSessionOperation,
  current: { sessionId: string; epoch: number; authToken: string | null; storedToken: string | null },
  error: unknown,
): 'ignore' | 'auth' | 'feedback' {
  if (operation.sessionId !== current.sessionId || operation.epoch !== current.epoch || operation.authToken !== current.authToken) return 'ignore';
  if (error instanceof MediaAuthError) {
    return !error.staleToken && current.storedToken === null ? 'auth' : 'ignore';
  }
  return operation.authToken === current.storedToken ? 'feedback' : 'ignore';
}
