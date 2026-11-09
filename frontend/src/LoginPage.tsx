import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { loginMediaUser, MEDIA_API_BASE } from './lib/media-client';

export default function LoginPage() {
  const navigate = useNavigate();
  const [register, setRegister] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [invite, setInvite] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError('');
    try {
      if (register) {
        const response = await fetch(`${MEDIA_API_BASE}/auth/register`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password, invite_code: invite }),
        });
        if (!response.ok) {
          const result = await response.json().catch(() => ({}));
          throw new Error(typeof result.detail === 'string' ? result.detail : '注册失败，请检查邀请码');
        }
      }
      await loginMediaUser(username, password);
      setPassword(''); navigate('/', { replace: true });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '连接失败，请重试');
    } finally { setBusy(false); }
  }

  return <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center px-5 py-10">
    <h1 className="text-2xl font-semibold">Media Workspace</h1>
    <p className="mt-2 text-sm text-neutral-500">先讨论方案，再确认生成，接着上一张图继续修改。</p>
    <form onSubmit={submit} className="mt-8 space-y-4">
      <div><label htmlFor="username" className="mb-1 block text-sm">账号</label><input id="username" autoComplete="username" required minLength={2} value={username} onChange={(e) => setUsername(e.target.value)} className="input w-full" disabled={busy} /></div>
      <div><label htmlFor="password" className="mb-1 block text-sm">密码</label><input id="password" type="password" autoComplete={register ? 'new-password' : 'current-password'} required minLength={register ? 6 : 1} value={password} onChange={(e) => setPassword(e.target.value)} className="input w-full" disabled={busy} /></div>
      {register && <div><label htmlFor="invite" className="mb-1 block text-sm">邀请码</label><input id="invite" required value={invite} onChange={(e) => setInvite(e.target.value)} className="input w-full" disabled={busy} /></div>}
      {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
      <button type="submit" className="btn-primary w-full" disabled={busy}>{busy ? '正在连接…' : register ? '注册并登录' : '登录'}</button>
      <button type="button" disabled={busy} className="w-full text-sm text-neutral-600 underline" onClick={() => { setRegister(!register); setError(''); }}>{register ? '已有账号，去登录' : '有邀请码，创建账号'}</button>
    </form>
  </main>;
}
