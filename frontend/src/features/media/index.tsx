import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, CheckCircle2, Download, History, LoaderCircle, MoreHorizontal, Plus, RefreshCw, RotateCcw, Send, Sparkles, X } from 'lucide-react';
import {
  ensureMediaSession,
  clearMediaToken,
  fetchChatHistory,
  fetchChatSessions,
  fetchMediaCapabilities,
  fetchReferences,
  fetchUserHistory,
  getStoredMediaToken,
  MediaAuthError,
  type ChatMessageItem,
  type HistoryItem,
  type MediaUser,
  type ProposalCard,
  type ReferenceCase,
} from '../../lib/media-client';
import { fetchActiveImageJobs, fetchOriginalImageResult, imageJobCompletionNotice, isFailedImageJob, isObjectUrl, isSafeImageReferenceUrl, protectedImageResultTaskId, resolveImagePreviewSource, upsertImageJob, type ImageJobSummary } from '../../lib/media-jobs';
import { renderMediaMarkdown } from '../../lib/media-markdown';
import { chatItemFromStream } from '../../lib/media-stream';
import {
  defaultMediaPreferences,
  isModelSelectable,
  shouldSubmitOnEnter,
  type MediaCapabilities,
  type MediaPreferences,
} from '../../lib/media-config';
import { chooseActiveSessionId, createChatSessionId, type ChatSession } from '../../lib/media-session';
import { activeVisualInputFromContext, generationVisualInput, proposalImagePreview, restoreActiveVisualPreview, visualInputForChat, type ActiveVisualInput } from '../../lib/media-visual-input';
import VoiceDraftActions from '../../components/voice-draft/VoiceDraftActions';
import ReferenceUploadProgress from './ReferenceUploadProgress';
import ReferenceActionButton from './ReferenceActionButton';
import { useReferenceUpload } from './reference/useReferenceUpload';
import { referenceFailureDisposition } from './session/operation-failure';
import { useMediaVisualState } from './session/useMediaVisualState';
import {
  completedImageJobDurationMs,
  createImageJobLifecycleController,
  createImageJobResultMessage,
  defaultImageJobPoller,
  formatImageJobElapsed,
  imageJobResultMessageDisposition,
  updateImageJobResultMessages,
  type ImageJobLifecycleController,
} from './image-jobs/controller';
import {
  createImageJobSubmissionController,
  defaultImageJobSubmitter,
  type ImageJobSubmissionController,
} from './image-jobs/submission-controller';
import {
  createMediaChatLifecycleController,
  defaultMediaChatStream,
  type MediaChatLifecycleController,
} from './chat/controller';

const ACTIVE_SESSION_KEY = 'media_workspace_active_session';
const INSPIRATION_TIPS = [
  { title: '做产品海报', prompt: '帮我做一张高级、有真实材质感的产品海报，你来决定构图和光影' },
  { title: '拟真摄影', prompt: '帮我做一张非常拟真的摄影作品，像真实相机拍摄，不要有明显 AI 感' },
  { title: '活动海报', prompt: '帮我做一张有氛围的活动海报，你来决定视觉方向和配色' },
  { title: '把这张图改高级', prompt: '在保持主体和核心信息不变的前提下，把当前图片做得更高级、更自然' },
];

function newRequestId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `media-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function aspectRatioLabel(ratio: string): string {
  if (ratio === '1:1') return '方图（1:1）';
  if (['9:16', '2:3', '3:4'].includes(ratio)) return `竖图（${ratio}）`;
  if (['16:9', '3:2', '4:3'].includes(ratio)) return `横图（${ratio}）`;
  return ratio;
}

function proposalPreferences(
  card: ProposalCard,
  capabilities: MediaCapabilities,
  preferences: MediaPreferences,
): MediaPreferences {
  const selected = capabilities.image_models.find((model) => model.id === preferences.imageModelId && isModelSelectable(model))
    ?? capabilities.image_models.find(isModelSelectable);
  if (!selected) return preferences;
  const quality = selected.qualities.includes(card.quality)
    ? card.quality
    : selected.qualities.includes(preferences.quality)
      ? preferences.quality
      : selected.qualities.includes(capabilities.defaults.quality) ? capabilities.defaults.quality : selected.qualities[0] || '';
  const aspectRatio = selected.aspect_ratios.includes(card.aspect_ratio)
    ? card.aspect_ratio
    : selected.aspect_ratios.includes(preferences.aspectRatio)
      ? preferences.aspectRatio
      : selected.aspect_ratios.includes(capabilities.defaults.aspect_ratio) ? capabilities.defaults.aspect_ratio : selected.aspect_ratios[0] || '';
  const storagePolicy = capabilities.storage_policies[preferences.storagePolicy]?.enabled
    ? preferences.storagePolicy
    : capabilities.storage_policies[capabilities.defaults.storage_policy]?.enabled
      ? capabilities.defaults.storage_policy
      : Object.entries(capabilities.storage_policies).find(([, policy]) => policy.enabled)?.[0] || preferences.storagePolicy;
  return { ...preferences, imageModelId: selected.id, quality, aspectRatio, storagePolicy };
}

const formatElapsed = formatImageJobElapsed;

function numericTiming(job: ImageJobSummary, key: string): number | null {
  const value = job.timings?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function jobElapsedMs(job: ImageJobSummary, nowMs: number): number {
  const createdAt = job.created_at ? Date.parse(job.created_at) : Number.NaN;
  if (Number.isFinite(createdAt)) return Math.max(0, nowMs - createdAt);
  return numericTiming(job, 'total_ms') ?? numericTiming(job, 'first_result_ms') ?? job.duration_ms ?? 0;
}

function completedDurationMs(job: ImageJobSummary): number {
  return completedImageJobDurationMs(job);
}

function jobPhase(job: ImageJobSummary): { label: string; detail: string; step: number } {
  const phase = (job.phase || job.status || '').toLowerCase();
  if (['failed', 'interrupted'].includes(phase) || ['failed', 'interrupted'].includes(job.status.toLowerCase())) return { label: '生成没有完成', detail: job.error_msg || '任务已停止，可以重新生成。', step: 0 };
  if (phase === 'completed') return { label: '作品已完成', detail: '图片已经生成并保存到作品库。', step: 5 };
  if (phase === 'persisting') return { label: '正在保存作品', detail: '图片已经返回，正在安全保存到作品库。', step: 5 };
  if (phase === 'result_ready') return { label: '正在获取结果', detail: '图片已经生成，正在把原图带回页面。', step: 4 };
  if (phase === 'generating') return { label: 'AI 正在生成图片', detail: '已经提交到图片模型，正在创作中。', step: 3 };
  if (phase === 'preparing') return { label: '正在准备任务', detail: '正在整理最终提示词和参考素材。', step: 1 };
  return { label: '任务已创建', detail: '任务已进入队列，马上提交到图片模型。', step: 1 };
}

function ImageJobProgress({ job, nowMs }: { job: ImageJobSummary; nowMs: number }) {
  const phase = jobPhase(job);
  const failed = isFailedImageJob(job);
  const elapsed = failed ? completedDurationMs(job) : jobElapsedMs(job, nowMs);
  const longWait = phase.step === 3 && elapsed >= 45_000;
  const labels = ['准备任务', '提交上游', 'AI 生成', '获取结果', '保存作品'];
  return <div className={`mt-4 overflow-hidden rounded-[24px] border p-4 ${failed ? 'border-red-100 bg-red-50/70' : 'border-blue-100 bg-blue-50/55'}`} role="status" aria-live="polite">
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-sm font-semibold text-neutral-900"><span className="relative flex h-2.5 w-2.5">{!failed && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-50" />}<span className={`relative inline-flex h-2.5 w-2.5 rounded-full ${failed ? 'bg-red-500' : 'bg-blue-600'}`} /></span>{phase.label}</div>
        <div className={`mt-1 text-[11px] leading-relaxed ${failed ? 'text-red-600' : 'text-neutral-500'}`}>{phase.detail}</div>
      </div>
      <span className={`shrink-0 rounded-full border bg-white/90 px-3 py-1.5 text-[11px] font-medium tabular-nums ${failed ? 'border-red-100 text-red-600' : 'border-blue-100 text-blue-700'}`}>{failed ? '失败于 ' : '已用时 '}{formatElapsed(elapsed)}</span>
    </div>
    <div className="mt-4 grid grid-cols-5 gap-1.5" aria-label={`当前阶段：${phase.label}`}>{labels.map((label, index) => {
      const reached = index < phase.step;
      const current = index === Math.max(0, phase.step - 1) && phase.step < 5;
      return <div key={label} className="min-w-0">
        <div className={`h-1.5 rounded-full transition-colors ${failed ? 'bg-red-100' : reached ? 'bg-blue-600' : 'bg-blue-100'}`} />
        <div className={`mt-1.5 truncate text-center text-[9px] sm:text-[10px] ${failed ? 'text-red-300' : current ? 'font-semibold text-blue-700' : reached ? 'text-neutral-600' : 'text-neutral-300'}`}>{label}</div>
      </div>;
    })}</div>
    {failed && <p className="mt-3 text-[11px] leading-relaxed text-neutral-500">这次任务已结束，可以调整后重新生成；失败状态会保留，不会再突然消失。</p>}
    {longWait && <p className="mt-3 rounded-2xl border border-blue-100 bg-white/90 px-3 py-2.5 text-[11px] leading-relaxed text-neutral-500">图片模型还在认真生成。你可以继续输入新的需求，图片完成后会自动出现在这里。</p>}
  </div>;
}

function completionTimeFromMessage(content: string): string | null {
  const match = content.match(/(?:共)?用时\s*([^·]+)/);
  return match?.[1]?.trim() || null;
}

function ArtworkCard({
  message,
  onPreview,
  onEdit,
  onRegenerate,
}: {
  message: ChatMessageItem;
  onPreview: () => void;
  onEdit: () => void;
  onRegenerate: () => void;
}) {
  if (!message.image_url) return null;
  const duration = typeof message.image_duration_ms === 'number' && message.image_duration_ms > 0
    ? formatElapsed(message.image_duration_ms)
    : completionTimeFromMessage(message.content);
  return <section className="mt-4 w-full max-w-3xl overflow-hidden rounded-[30px] bg-[#e9eaed] p-3 sm:p-4">
    <div className="flex flex-wrap items-center justify-between gap-2 px-1 pb-3">
      <div className="flex items-center gap-2 text-xs text-neutral-500"><span className="font-semibold text-neutral-800">AI 绘图</span><span className="h-1 w-1 rounded-full bg-neutral-300" /><span className="inline-flex items-center gap-1 text-emerald-600"><CheckCircle2 size={13} />已完成</span>{duration && <><span className="h-1 w-1 rounded-full bg-neutral-300" /><span>{duration}</span></>}</div>
      <span className="text-[11px] text-neutral-400">{message.card_title || '生成作品'}</span>
    </div>
    <button type="button" onClick={onPreview} className="block w-full overflow-hidden rounded-[22px] bg-neutral-100">
      <img src={message.image_url} alt={message.card_title || '生成作品'} className="mx-auto max-h-[72vh] w-auto max-w-full object-contain" />
    </button>
    <div className="flex flex-wrap items-center gap-2 px-1 pt-3">
      <button type="button" onClick={onEdit} className="inline-flex min-h-10 items-center gap-1.5 rounded-xl bg-blue-600 px-3.5 text-xs font-medium text-white transition hover:bg-blue-700"><RotateCcw size={13} />基于此图改</button>
      <a href={message.image_url} download className="btn-secondary min-h-10 rounded-xl px-3 text-xs"><Download size={13} />下载原图</a>
      <button type="button" onClick={onRegenerate} className="btn-secondary min-h-10 rounded-xl px-3 text-xs"><RefreshCw size={13} />再生成</button>

    </div>
  </section>;
}

function styleLabel(style: string): string {
  const labels: Record<string, string> = {
    real: '拟真摄影', product: '产品摄影', documentary: '纪实摄影', portrait: '人物摄影',
    tech_poster: '科技海报', campaign_poster: '活动海报', minimal: '极简视觉',
    vintage_5s: '复古摄影', ccd_snap: '自然抓拍', ui_upgrade: '视觉升级', default: 'AI 设计',
  };
  return labels[style] || 'AI 设计';
}

function ThinkingStatus({ label, visualInput }: { label: string; visualInput?: ActiveVisualInput | null }) {
  const isVision = Boolean(visualInput && label.includes('参考图'));
  return <div
    role="status"
    aria-live="polite"
    className={`inline-flex items-center gap-2.5 border bg-white/95 text-xs text-neutral-500 shadow-sm ${isVision ? 'rounded-2xl border-[#6d82f6]/20 px-2.5 py-2' : 'rounded-full border-neutral-200/80 px-3.5 py-2'}`}
  >
    {isVision && visualInput ? <div className="relative h-10 w-10 shrink-0 overflow-hidden rounded-xl bg-neutral-100">
      <img src={visualInput.url} alt="正在分析的参考图" className="h-full w-full object-cover" />
      <span className="pointer-events-none absolute inset-0 rounded-xl ring-2 ring-inset ring-[#6d82f6]/45 animate-pulse" aria-hidden="true" />
      <span className="pointer-events-none absolute inset-x-1 top-1/2 h-px bg-white/90 shadow-[0_0_8px_rgba(109,130,246,0.9)] animate-bounce" aria-hidden="true" />
    </div> : <Sparkles size={14} className="animate-pulse text-[#6d82f6]" />}
    <span className="min-w-0">
      <span className="flex items-center gap-1.5 font-medium text-neutral-600">
        {isVision && <LoaderCircle size={13} className="shrink-0 animate-spin text-[#6d82f6]" aria-hidden="true" />}
        <span>{label || '正在理解你的需求'}</span>
      </span>
      {isVision && <span className="mt-0.5 block text-[10px] text-neutral-400">AI 正在读取画面内容</span>}
    </span>
    <span className="inline-flex items-center gap-1" aria-hidden="true">
      {[0, 1, 2].map((item) => <span key={item} className="h-1 w-1 animate-bounce rounded-full bg-neutral-400" style={{ animationDelay: `${item * 120}ms` }} />)}
    </span>
  </div>;
}

function ProposalEditor({
  card,
  previewUrl,
  capabilities,
  preferences,
  activeVisualInput,
  onConfirm,
  onUploadReference,
  onRemoveReference,
  busy,
  uploading,
  uploadPreview,
  submitted,
  generating,
  job,
  nowMs,
}: {
  card: ProposalCard;
  previewUrl?: string;
  capabilities: MediaCapabilities | null;
  preferences: MediaPreferences | null;
  activeVisualInput: ActiveVisualInput | null;
  onConfirm: (card: ProposalCard, prompt: string, settings: MediaPreferences) => void;
  onUploadReference: (cardId: string) => void;
  onRemoveReference: (cardId: string) => void;
  busy: boolean;
  uploading: boolean;
  uploadPreview: { previewUrl: string; fileName: string } | null;
  generating: boolean;
  submitted: boolean;
  job: ImageJobSummary | null;
  nowMs: number;
}) {
  const settings = capabilities && preferences ? proposalPreferences(card, capabilities, preferences) : null;
  const selectedModel = capabilities?.image_models.find((model) => model.id === settings?.imageModelId);
  const prompt = card.final_prompt ?? card.prompt;
  const visualState = generationVisualInput(card, activeVisualInput);
  const canGenerate = Boolean(visualState.valid && capabilities && settings && selectedModel && isModelSelectable(selectedModel)
    && selectedModel.qualities.includes(settings.quality)
    && selectedModel.aspect_ratios.includes(settings.aspectRatio)
    && capabilities.storage_policies[settings.storagePolicy]?.enabled && prompt.trim());

  return (
    <section className="mt-3 w-full max-w-2xl rounded-[28px] border border-neutral-200/80 bg-white p-5 shadow-[0_18px_55px_rgba(15,23,42,0.07)]">
      <div className="text-[11px] font-medium text-neutral-400">{card.is_modification ? 'AI 已帮你定好修改方向' : 'AI 已帮你定好'}</div>
      <h3 className="mt-1.5 text-lg font-semibold tracking-tight text-neutral-900">{card.title}</h3>
      <p className="mt-2 text-sm leading-7 text-neutral-600">{card.design_rationale}</p>
      {!submitted && <div className="mt-4 flex flex-wrap gap-2">
        <span className="rounded-full bg-neutral-100 px-3 py-1.5 text-[11px] text-neutral-600">{styleLabel(card.style)}</span>
        <span className="rounded-full bg-neutral-100 px-3 py-1.5 text-[11px] text-neutral-600">{aspectRatioLabel(card.aspect_ratio)}</span>
        <span className="rounded-full bg-neutral-100 px-3 py-1.5 text-[11px] text-neutral-600">快速模式</span>
      </div>}
      {!submitted && <div className="mt-4 rounded-2xl border border-dashed border-neutral-200 bg-neutral-50/70 p-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-1.5">
              <div className="text-xs font-semibold text-neutral-800">参考图（可选）</div>
              {card.reference_recommended && <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700">AI 建议上传参考图</span>}
            </div>
            <div className="mt-1 text-[11px] leading-relaxed text-neutral-500">
              {card.reference_recommended
                ? (card.reference_reason || '这类需求有参考图会更容易保留真实人物、产品或原图细节，但不上传也可以继续生成。')
                : '有指定人物、产品或旧图时可以补一张；普通文生图可以直接生成。'}
            </div>
          </div>
          <ReferenceActionButton onClick={() => onUploadReference(card.card_id)} disabled={busy}>
            {card.visual_input_url ? '替换' : '上传'}
          </ReferenceActionButton>
        </div>
        {uploading && uploadPreview && <div className="mt-3"><ReferenceUploadProgress previewUrl={uploadPreview.previewUrl} fileName={uploadPreview.fileName} /></div>}
        {!uploading && card.visual_input_url && card.visual_input_role && <div className="mt-3 flex items-center gap-3 rounded-xl bg-white p-2">
          {previewUrl && <img src={previewUrl} alt={card.visual_input_role === 'reference' ? '用户参考图' : '修改底图'} className="h-12 w-12 shrink-0 rounded-lg object-cover" />}
          <div className="min-w-0 flex-1">
            <div className="text-xs font-medium text-neutral-800">{card.visual_input_role === 'reference' ? '已使用用户参考图' : '已使用修改底图'}</div>
            <div className="mt-0.5 text-[10px] text-neutral-400">生成时会把这张图传给图片模型</div>
          </div>
          <button type="button" onClick={() => onRemoveReference(card.card_id)} disabled={busy} className="text-[11px] text-neutral-400 underline disabled:opacity-40">移除</button>
        </div>}
      </div>}
      {submitted && card.visual_input_url && card.visual_input_role && <div className="mt-4 flex items-center gap-3 rounded-2xl bg-neutral-50 p-2.5">
        {previewUrl && <img src={previewUrl} alt={card.visual_input_role === 'reference' ? '用户参考图' : '修改底图'} className="h-14 w-14 shrink-0 rounded-xl object-cover" />}
        <div className="min-w-0">
          <div className="text-xs font-semibold text-neutral-800">{card.visual_input_role === 'reference' ? '参考图' : '修改底图'}</div>
          <div className="mt-0.5 text-[11px] text-neutral-400">任务已提交，参考图已锁定</div>
        </div>
      </div>}
      {card.matched_reference && <div className="mt-2 text-[11px] text-neutral-400">灵感参考：{card.matched_reference.title}</div>}
      {!visualState.valid && <p role="status" className="mt-3 text-xs text-amber-700">参考图已变化，请重新选择这张卡要使用的参考图。</p>}
      {visualState.valid && capabilities && !canGenerate && <p role="status" className="mt-3 text-xs text-amber-700">图片能力暂不可用，请稍后重试。</p>}
      <button
        type="button"
        onClick={() => settings && onConfirm(card, prompt, settings)}
        disabled={!canGenerate || busy || submitted}
        className="mt-4 inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-[18px] bg-[#6d82f6] px-4 text-sm font-semibold text-white transition hover:bg-[#5d73ee] disabled:opacity-50"
      >
        {generating ? '正在提交…' : submitted ? '任务已提交' : <><Sparkles size={15} />直接生成</>}
      </button>
      {job && <ImageJobProgress job={job} nowMs={nowMs} />}
    </section>
  );
}

export default function MediaPage() {
  const navigate = useNavigate();
  const contentScrollRef = useRef<HTMLDivElement>(null);
  const lastScrollTopRef = useRef(0);
  const [composerHidden, setComposerHidden] = useState(false);
  const [token, setToken] = useState<string | null>(() => getStoredMediaToken());
  const [user, setUser] = useState<MediaUser | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<MediaCapabilities | null>(null);
  const [capabilityError, setCapabilityError] = useState<string | null>(null);
  const [preferences, setPreferences] = useState<MediaPreferences | null>(null);
  const [sessionId, setSessionId] = useState('');
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [showSessions, setShowSessions] = useState(false);
  const [messages, setMessages] = useState<ChatMessageItem[]>([]);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const [inputText, setInputText] = useState('');
  const [loading, setLoading] = useState(false);
  const [streamStatus, setStreamStatus] = useState('');
  const { owner: visualState, snapshot: visualSnapshot } = useMediaVisualState();
  const { visualInput: activeVisualInput, attachmentPending: visualAttachmentPending } = visualSnapshot;
  const [generatingCardId, setGeneratingCardId] = useState<string | null>(null);
  const [jobSubmitting, setJobSubmitting] = useState(false);
  const [activeImageJobs, setActiveImageJobs] = useState<ImageJobSummary[]>([]);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [generatedCards, setGeneratedCards] = useState<Set<string>>(new Set());
  const [references, setReferences] = useState<ReferenceCase[]>([]);
  const [selectedReference, setSelectedReference] = useState<ReferenceCase | null>(null);
  const [showHistoryModal, setShowHistoryModal] = useState(false);
  const [historyItems, setHistoryItems] = useState<HistoryItem[]>([]);
  const [historyModalError, setHistoryModalError] = useState<string | null>(null);
  const [previewImg, setPreviewImg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const activeSessionRef = useRef('');
  const historyRequestRef = useRef(0);
  const authEpochRef = useRef(0);
  const chatLifecycleRef = useRef<MediaChatLifecycleController | null>(null);
  const imageJobLifecycleRef = useRef<ImageJobLifecycleController | null>(null);
  const imageJobSubmissionRef = useRef<ImageJobSubmissionController | null>(null);
  const objectUrlsRef = useRef(new Set<string>());
  const imageResultReservationsRef = useRef(new Map<string, string>());

  const releaseLocalObjectUrls = () => {
    objectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    objectUrlsRef.current.clear();
  };

  const abortSessionWork = () => {
    chatLifecycleRef.current?.cancelAll();
    referenceWorkflow.cancel();
    imageJobSubmissionRef.current?.invalidate();
    imageJobLifecycleRef.current?.cancelAll();
    releaseLocalObjectUrls();
    imageResultReservationsRef.current.clear();
    setLoading(false); setStreamStatus(''); setActiveImageJobs([]);
  };

  const canWriteSession = (session: string, epoch: number, authToken: string) =>
    epoch === authEpochRef.current && activeSessionRef.current === session && getStoredMediaToken() === authToken;

  const referenceWorkflow = useReferenceUpload({
    capture: () => {
      const authToken = getStoredMediaToken(), session = activeSessionRef.current;
      return authToken && session ? { sessionId: session, epoch: authEpochRef.current, authToken } : null;
    },
    isCurrent: op => canWriteSession(op.sessionId, op.epoch, op.authToken),
    canUseCard: (op, cardId) => canWriteSession(op.sessionId, op.epoch, op.authToken) && !generatedCards.has(cardId),
    commitUploadedReference: (op, visual) => canWriteSession(op.sessionId, op.epoch, op.authToken)
      ? { resume: visualState.commitUpload(visual) } : null,
    commitCard: (op, card) => {
      if (!canWriteSession(op.sessionId, op.epoch, op.authToken)) return false;
      setMessages(previous => previous.map(message => message.proposal_card?.card_id === card.card_id
        ? { ...message, proposal_card: card } : message));
      return true;
    },
    beginComposerClear: op => canWriteSession(op.sessionId, op.epoch, op.authToken) ? visualState.beginClear() : null,
    reportFailure: (op, error, fallback) => {
      const disposition = referenceFailureDisposition(op, {
        sessionId: activeSessionRef.current, epoch: authEpochRef.current,
        authToken: token, storedToken: getStoredMediaToken(),
      }, error);
      if (disposition === 'auth') handleAuthFailure(error);
      if (disposition === 'feedback') setErrorMsg(error === null ? null : (error as Error)?.message || fallback);
    },
  });
  const referenceUpload = referenceWorkflow.snapshot.preview, cardMutationId = referenceWorkflow.snapshot.mutationCardId;
  const busy = loading || referenceUpload !== null || generatingCardId !== null || cardMutationId !== null || jobSubmitting;

  const hydrateHistoryImages = async (
    historyMessages: ChatMessageItem[],
    activeInput: ActiveVisualInput | null,
    session: string,
    epoch: number,
    authToken: string,
  ): Promise<{ messages: ChatMessageItem[]; activeVisualInput: ActiveVisualInput | null } | null> => {
    const createdUrls: string[] = [];
    const replacements = new Map<string, string>();
    const resolveUrl = async (url: string | null, fallbackTaskId?: string | null): Promise<string | null> => {
      if (!url) return null;
      const source = resolveImagePreviewSource(url, fallbackTaskId);
      if (!source || source.kind === 'direct') return source?.url ?? url;
      const taskId = source.taskId || fallbackTaskId;
      if (!taskId) return null;
      const blob = await fetchOriginalImageResult(taskId);
      if (!canWriteSession(session, epoch, authToken)) return null;
      const objectUrl = URL.createObjectURL(blob);
      createdUrls.push(objectUrl); objectUrlsRef.current.add(objectUrl); replacements.set(url, objectUrl);
      return objectUrl;
    };
    try {
      const messages = await Promise.all(historyMessages.map(async (message) => {
        try {
          return { ...message,
            image_url: message.image_url ? await resolveUrl(message.image_url, message.task_id) : message.image_url,
            proposal_card: message.proposal_card ? { ...message.proposal_card,
              visual_preview_url: await resolveUrl(message.proposal_card.visual_input_url || null),
            } : message.proposal_card,
          };
        }
        catch (error) {
          handleAuthFailure(error);
          return { ...message, image_url: null, content: `${message.content}${message.content ? '\n' : ''}原图暂不可读取。` };
        }
      }));
      const restoredActive = await restoreActiveVisualPreview(activeInput,
        async (url) => replacements.get(url) || await resolveUrl(url),
        (error) => {
          handleAuthFailure(error);
          if (canWriteSession(session, epoch, authToken)) setErrorMsg('修改底图已过期或不可用，请先点击“清除”或选择另一张作品。');
        });
      if (!canWriteSession(session, epoch, authToken)) {
        createdUrls.forEach((url) => { URL.revokeObjectURL(url); objectUrlsRef.current.delete(url); });
        return null;
      }
      return { messages, activeVisualInput: restoredActive };
    } catch (error) {
      createdUrls.forEach((url) => { URL.revokeObjectURL(url); objectUrlsRef.current.delete(url); });
      if (canWriteSession(session, epoch, authToken)) handleAuthFailure(error);
      return null;
    }
  };

  const watchImageJob = (job: ImageJobSummary, session: string, epoch: number, authToken: string) =>
    imageJobLifecycleRef.current?.watch(job, { sessionId: session, epoch, authToken }) ?? Promise.resolve();

  const loadActiveImageJobs = async (session: string, epoch: number, authToken: string) => {
    try {
      const jobs = await fetchActiveImageJobs(session);
      if (!canWriteSession(session, epoch, authToken)) return;
      setActiveImageJobs(jobs);
      setGeneratedCards((current) => {
        const updated = new Set(current);
        jobs.forEach((entry) => {
          if (!entry.proposal_card_id) return;
          if (isFailedImageJob(entry)) updated.delete(entry.proposal_card_id);
          else updated.add(entry.proposal_card_id);
        });
        return updated;
      });
      jobs.filter((job) => !isFailedImageJob(job)).forEach((job) => { void watchImageJob(job, session, epoch, authToken); });
    } catch (error) {
      if (canWriteSession(session, epoch, authToken)) { setErrorMsg(`恢复图片任务失败：${(error as Error).message}`); handleAuthFailure(error); }
    }
  };

  const restoreConfirmedCards = async (session: string, epoch: number, authToken: string) => {
    try {
      const items = await fetchUserHistory();
      if (!canWriteSession(session, epoch, authToken)) return;
      const confirmed = items.filter((item) => item.proposal_card_id && (!item.session_id || item.session_id === session));
      setGeneratedCards((current) => new Set([...current, ...confirmed.map((item) => item.proposal_card_id!)]));
    } catch (error) {
      if (canWriteSession(session, epoch, authToken)) handleAuthFailure(error);
    }
  };

  const clearPrivateState = useCallback(() => {
    abortSessionWork();
    authEpochRef.current += 1;
    historyRequestRef.current += 1;
    clearMediaToken();
    setToken(null); setUser(null); setCapabilities(null); setCapabilityError(null); setPreferences(null);
    imageJobSubmissionRef.current?.clearRequestIds();
    setSessionId(''); activeSessionRef.current = ''; visualState.replace(null); visualState.setAttachmentPending(false); localStorage.removeItem(ACTIVE_SESSION_KEY);
    setSessions([]); setMessages([]); visualState.waitForReference(null); setInputText(''); setReferences([]);
    setSelectedReference(null); setGeneratedCards(new Set()); setHistoryItems([]); setShowHistoryModal(false);
    setHistoryLoading(false); setHistoryError(null); setSessionsLoading(false); setLoading(false);
    setErrorMsg(null); setLoginError('登录已失效，请重新登录。');
  }, []);

  const handleAuthFailure = useCallback((error: unknown) => {
    if (error instanceof MediaAuthError && !error.staleToken) clearPrivateState();
  }, [clearPrivateState]);

  if (!imageJobLifecycleRef.current) {
    imageJobLifecycleRef.current = createImageJobLifecycleController({
      isCurrent: (session) => canWriteSession(session.sessionId, session.epoch, session.authToken),
      poll: defaultImageJobPoller,
      resolveResult: async (job, signal, session) => {
        const source = resolveImagePreviewSource(job.image_url, job.result_url);
        if (!source) throw new Error('任务未提供可显示的图片地址');
        if (source.kind === 'direct') return source.url;
        const blob = await fetchOriginalImageResult(source.taskId, signal);
        if (!canWriteSession(session.sessionId, session.epoch, session.authToken)) return null;
        return URL.createObjectURL(blob);
      },
      releaseResult: (url) => { if (isObjectUrl(url)) URL.revokeObjectURL(url); },
      onJobUpdate: (next) => {
        setActiveImageJobs((current) => upsertImageJob(current, next));
        if (next.proposal_card_id) {
          setGeneratedCards((current) => {
            const updated = new Set(current);
            if (isFailedImageJob(next)) updated.delete(next.proposal_card_id!);
            else updated.add(next.proposal_card_id!);
            return updated;
          });
        }
        setMessages((current) => updateImageJobResultMessages(current, next));
      },
      onImageResult: (next, imageSource) => {
        const disposition = imageJobResultMessageDisposition(messagesRef.current, next, imageSource);
        if (disposition !== 'new') return disposition === 'same-image';

        const parentId = next.id ?? next.parent_id ?? null;
        const resultKeys = [`task:${next.task_id}`];
        if (parentId !== null) resultKeys.push(`parent:${String(parentId)}`);
        const reservedKey = resultKeys.find((key) => imageResultReservationsRef.current.has(key));
        if (reservedKey) return imageResultReservationsRef.current.get(reservedKey) === imageSource;

        resultKeys.forEach((key) => imageResultReservationsRef.current.set(key, imageSource));
        if (isObjectUrl(imageSource)) objectUrlsRef.current.add(imageSource);
        const resultMessage = createImageJobResultMessage(next, imageSource);
        messagesRef.current = [...messagesRef.current, resultMessage];
        setMessages((current) => imageJobResultMessageDisposition(current, next, imageSource) === 'new'
          ? [...current, resultMessage]
          : current);
        return true;
      },
      onResultError: (error, next) => {
        handleAuthFailure(error);
        if (next.status.toLowerCase() === 'failed') {
          setErrorMsg(`任务完成但原图暂不可读取：${(error as Error).message}`);
        }
      },
      onComplete: (latest) => {
        if (latest.polling_timed_out) {
          setActiveImageJobs((current) => [...current.filter((entry) => entry.task_id !== latest.task_id), latest]);
          setErrorMsg('图片仍在处理中，已达到本轮查询时限；可稍后点击“恢复状态”继续。');
        } else if (isFailedImageJob(latest)) {
          setActiveImageJobs((current) => upsertImageJob(current, latest));
          if (latest.proposal_card_id) setGeneratedCards((current) => { const updated = new Set(current); updated.delete(latest.proposal_card_id!); return updated; });
          setErrorMsg(latest.error_msg || '图片生成失败');
        } else {
          setActiveImageJobs((current) => current.filter((entry) => entry.task_id !== latest.task_id));
          const notice = imageJobCompletionNotice(latest);
          if (notice) setErrorMsg(notice);
        }
      },
      onPollError: (error) => {
        setErrorMsg(`图片任务状态读取失败：${(error as Error).message}`);
        handleAuthFailure(error);
      },
    });
  }

  if (!imageJobSubmissionRef.current) {
    imageJobSubmissionRef.current = createImageJobSubmissionController({
      isCurrent: (session) => canWriteSession(session.sessionId, session.epoch, session.authToken),
      submit: defaultImageJobSubmitter,
      createRequestId: newRequestId,
      onSubmitting: (cardId) => {
        setGeneratingCardId(cardId);
        setJobSubmitting(cardId !== null);
      },
      onAccepted: (accepted, command) => {
        const { cardId, intent, session } = command;
        setGeneratedCards((previous) => new Set(previous).add(cardId));
        setActiveImageJobs((current) => upsertImageJob(current, accepted));
        setMessages((previous) => previous.map((message) => message.proposal_card?.card_id === cardId
          ? { ...message, proposal_card: { ...message.proposal_card, prompt: intent.prompt, final_prompt: intent.prompt, aspect_ratio: intent.aspect_ratio, quality: intent.quality, image_task_id: accepted.id } }
          : message));
        setErrorMsg(null);
        void refreshSessions();
        void watchImageJob(accepted, session.sessionId, session.epoch, session.authToken);
      },
      onError: (error, command) => {
        if (!canWriteSession(command.session.sessionId, command.session.epoch, command.session.authToken)) return;
        handleAuthFailure(error);
        setErrorMsg(`图片任务提交未确认：${(error as Error).message || '连接中断'}。再次手动确认会复用同一请求编号，不会自动重试。`);
      },
    });
  }

  if (!chatLifecycleRef.current) {
    chatLifecycleRef.current = createMediaChatLifecycleController({
      isCurrent: (session) => canWriteSession(session.sessionId, session.epoch, session.authToken),
      stream: defaultMediaChatStream,
      onStart: (command) => {
        setInputText('');
        setStreamStatus('正在连接对话流…');
        setMessages((previous) => [
          ...previous,
          ...(command.suppressUserEcho ? [] : [{ id: `user-${Date.now()}`, role: 'user' as const, content: command.message }]),
          { id: command.assistantId, role: 'assistant', content: '' },
        ]);
        setLoading(true);
      },
      onStatus: (status) => setStreamStatus(status.label || status.phase || '正在生成回复…'),
      onDelta: (_delta, accumulatedText, command) => {
        setMessages((previous) => previous.map((message) => message.id === command.assistantId
          ? { ...message, content: accumulatedText }
          : message));
      },
      onComplete: (data, accumulatedText, command) => {
        setErrorMsg(null);
        const completed = chatItemFromStream(command.assistantId, data, accumulatedText);
        visualState.waitForReference(data.required_input === 'reference_image' ? command.message : null);
        if (Object.prototype.hasOwnProperty.call(data, 'visual_context')) {
          visualState.replace(activeVisualInputFromContext(data.visual_context, messagesRef.current, visualState.getSnapshot().visualInput));
        }
        visualState.setAttachmentPending(false);
        setMessages((previous) => previous.map((message) => message.id === command.assistantId ? completed : message));
      },
      onError: (error, accumulatedText, command) => {
        handleAuthFailure(error);
        setMessages((previous) => previous.map((message) => message.id === command.assistantId
          ? { ...message, content: accumulatedText ? `${accumulatedText}\n\n提示：${(error as Error).message || '对话流中断'}` : `提示：${(error as Error).message || '无法获取 AI 响应'}` }
          : message));
      },
      onFinish: () => {
        setLoading(false);
        setStreamStatus('');
        void refreshSessions();
      },
    });
  }

  const loadWorkspace = useCallback(async (authToken: string, epoch: number, currentUser: MediaUser) => {
    setUser(currentUser);
    setCapabilityError(null);
    const [catalogResult, referencesResult, sessionsResult] = await Promise.allSettled([
      fetchMediaCapabilities(), fetchReferences(), fetchChatSessions(),
    ]);
    if (epoch !== authEpochRef.current || getStoredMediaToken() !== authToken) return;
    if (catalogResult.status === 'fulfilled') {
      setCapabilities(catalogResult.value);
      setPreferences(defaultMediaPreferences(catalogResult.value));
    } else {
      setCapabilities(null); setPreferences(null);
      setCapabilityError(catalogResult.reason?.message || '运行时能力配置加载失败');
      handleAuthFailure(catalogResult.reason);
    }
    if (getStoredMediaToken() !== authToken || epoch !== authEpochRef.current) return;
    if (referencesResult.status === 'fulfilled') setReferences(referencesResult.value);
    else { setReferences([]); setErrorMsg(referencesResult.reason?.message || '案例灵感加载失败'); handleAuthFailure(referencesResult.reason); }
    if (getStoredMediaToken() !== authToken || epoch !== authEpochRef.current) return;
    if (sessionsResult.status === 'rejected') {
      setSessionsError(sessionsResult.reason?.message || '会话列表加载失败');
      handleAuthFailure(sessionsResult.reason);
      setSessionsLoading(false);
      return;
    }
    const savedSessions = sessionsResult.value;
    setSessions(savedSessions); setSessionsError(null);
    const activeId = chooseActiveSessionId(savedSessions, localStorage.getItem(ACTIVE_SESSION_KEY), createChatSessionId);
    activeSessionRef.current = activeId; setSessionId(activeId); localStorage.setItem(ACTIVE_SESSION_KEY, activeId);
    const requestId = ++historyRequestRef.current;
    setHistoryLoading(true); setHistoryError(null);
    try {
      const restored = await fetchChatHistory(activeId);
      const hydrated = await hydrateHistoryImages(restored.messages, restored.activeVisualInput, activeId, epoch, authToken);
      if (!hydrated) return;
      if (epoch === authEpochRef.current && requestId === historyRequestRef.current && activeSessionRef.current === activeId) {
        setMessages(hydrated.messages); visualState.replace(hydrated.activeVisualInput); visualState.setAttachmentPending(false);
        setGeneratedCards(new Set(hydrated.messages.flatMap((message) => message.proposal_card?.image_task_id ? [message.proposal_card.card_id] : [])));
        void restoreConfirmedCards(activeId, epoch, authToken);
      }
    } catch (error: any) {
      if (epoch === authEpochRef.current && requestId === historyRequestRef.current) {
        setHistoryError(error.message || '加载会话记录失败'); handleAuthFailure(error);
      }
    } finally {
      if (requestId === historyRequestRef.current) {
        setHistoryLoading(false);
        if (activeSessionRef.current === activeId) void loadActiveImageJobs(activeId, epoch, authToken);
      }
    }
  }, [handleAuthFailure]);

  useEffect(() => {
    let cancelled = false;
    const epoch = ++authEpochRef.current;
    setAuthLoading(true); setLoginError(null);
    void ensureMediaSession().then(async (currentUser) => {
      const authToken = getStoredMediaToken();
      if (!authToken || cancelled || epoch !== authEpochRef.current) return;
      setToken(authToken);
      await loadWorkspace(authToken, epoch, currentUser);
    }).catch((error) => {
      if (cancelled || epoch !== authEpochRef.current) return;
      setLoginError(error.message || '无法从工作台身份连接 Media，请重试');
    }).finally(() => { if (!cancelled) setAuthLoading(false); });
    return () => { cancelled = true; historyRequestRef.current += 1; authEpochRef.current += 1; };
  }, [loadWorkspace]);

  const hasLiveImageJobs = activeImageJobs.some((job) => !isFailedImageJob(job));
  useEffect(() => {
    if (!hasLiveImageJobs) return undefined;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasLiveImageJobs]);
  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' }); window.setTimeout(() => setComposerHidden(false), 360); }, [messages, loading, activeImageJobs.length]);
  useEffect(() => () => {
    chatLifecycleRef.current?.cancelAll();
    imageJobLifecycleRef.current?.cancelAll();
    imageJobSubmissionRef.current?.invalidate(false);
    objectUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    objectUrlsRef.current.clear();
  }, []);

  const retryLoginCheck = async () => {
    const epoch = ++authEpochRef.current;
    setAuthLoading(true); setLoginError(null);
    try {
      const currentUser = await ensureMediaSession();
      const authToken = getStoredMediaToken();
      if (!authToken || epoch !== authEpochRef.current) return;
      setToken(authToken); await loadWorkspace(authToken, epoch, currentUser);
    } catch (error: any) {
      if (epoch === authEpochRef.current) setLoginError(error.message || '无法从工作台身份连接 Media，请重试');
    } finally { if (epoch === authEpochRef.current) setAuthLoading(false); }
  };


  const refreshSessions = async () => {
    const epoch = authEpochRef.current;
    const authToken = getStoredMediaToken();
    if (!authToken) return;
    try {
      const nextSessions = await fetchChatSessions();
      if (epoch !== authEpochRef.current || getStoredMediaToken() !== authToken) return;
      setSessions(nextSessions); setSessionsError(null);
    } catch (error: any) {
      if (epoch !== authEpochRef.current || getStoredMediaToken() !== authToken) return;
      setSessionsError(error.message || '刷新会话列表失败'); handleAuthFailure(error);
    }
  };

  const retryHistory = async () => {
    if (!sessionId || !token) return;
    const activeId = sessionId; const requestId = ++historyRequestRef.current; const epoch = authEpochRef.current;
    const authToken = getStoredMediaToken(); if (!authToken) return;
    setHistoryLoading(true); setHistoryError(null);
    try {
      const restored = await fetchChatHistory(activeId);
      const hydrated = await hydrateHistoryImages(restored.messages, restored.activeVisualInput, activeId, epoch, authToken);
      if (!hydrated) return;
      if (epoch === authEpochRef.current && requestId === historyRequestRef.current && activeSessionRef.current === activeId) {
        setMessages(hydrated.messages); visualState.replace(hydrated.activeVisualInput); visualState.setAttachmentPending(false);
        setGeneratedCards(new Set(hydrated.messages.flatMap((message) => message.proposal_card?.image_task_id ? [message.proposal_card.card_id] : [])));
        void restoreConfirmedCards(activeId, epoch, authToken);
      }
    } catch (error: any) {
      if (requestId === historyRequestRef.current) { setHistoryError(error.message || '加载会话记录失败'); handleAuthFailure(error); }
    } finally {
      if (requestId === historyRequestRef.current) {
        setHistoryLoading(false);
        if (activeSessionRef.current === activeId) void loadActiveImageJobs(activeId, epoch, authToken);
      }
    }
  };

  const startNewSession = () => {
    if (jobSubmitting || historyLoading || sessionsLoading) return;
    abortSessionWork();
    historyRequestRef.current += 1;
    const nextId = createChatSessionId(); activeSessionRef.current = nextId;
    setSessionId(nextId); localStorage.setItem(ACTIVE_SESSION_KEY, nextId);
    setMessages([]); visualState.replace(null); visualState.setAttachmentPending(false); visualState.waitForReference(null); setSelectedReference(null); setGeneratedCards(new Set()); setErrorMsg(null); setHistoryError(null); setShowSessions(false);
    const authToken = getStoredMediaToken(); if (authToken) void loadActiveImageJobs(nextId, authEpochRef.current, authToken);
  };

  const selectSession = async (nextId: string) => {
    if (jobSubmitting || historyLoading || sessionsLoading || nextId === sessionId) return;
    abortSessionWork();
    const requestId = ++historyRequestRef.current; const epoch = authEpochRef.current;
    activeSessionRef.current = nextId; setSessionId(nextId); localStorage.setItem(ACTIVE_SESSION_KEY, nextId);
    setMessages([]); visualState.replace(null); visualState.setAttachmentPending(false); visualState.waitForReference(null); setSelectedReference(null); setGeneratedCards(new Set()); setErrorMsg(null); setHistoryError(null);
    setHistoryLoading(true); setShowSessions(false);
    const authToken = getStoredMediaToken();
    try {
      const restored = await fetchChatHistory(nextId);
      const hydrated = authToken ? await hydrateHistoryImages(restored.messages, restored.activeVisualInput, nextId, epoch, authToken) : null;
      if (!hydrated) return;
      if (epoch === authEpochRef.current && requestId === historyRequestRef.current && activeSessionRef.current === nextId) {
        setMessages(hydrated.messages); visualState.replace(hydrated.activeVisualInput); visualState.setAttachmentPending(false);
        setGeneratedCards(new Set(hydrated.messages.flatMap((message) => message.proposal_card?.image_task_id ? [message.proposal_card.card_id] : [])));
        if (authToken) void restoreConfirmedCards(nextId, epoch, authToken);
      }
    } catch (error: any) {
      if (requestId === historyRequestRef.current) { setHistoryError(error.message || '加载会话记录失败'); handleAuthFailure(error); }
    } finally {
      if (requestId === historyRequestRef.current) {
        setHistoryLoading(false);
        if (authToken && activeSessionRef.current === nextId) void loadActiveImageJobs(nextId, epoch, authToken);
      }
    }
  };

  const handleSendMessage = async (
    textOverride?: string,
    visualInputOverride: ActiveVisualInput | null = activeVisualInput,
    suppressUserEcho = false,
    resumeAfterReference = false,
  ) => {
    const text = (textOverride ?? inputText).trim();
    if (!text || (busy && !resumeAfterReference) || !sessionId || !capabilities || !preferences || !token) return;
    if (visualInputOverride?.unavailable) { setErrorMsg('请先清除已失效的修改底图，或选择另一张作品。'); return; }
    const requestSessionId = sessionId; const epoch = authEpochRef.current; const referenceId = selectedReference?.id;
    const authToken = getStoredMediaToken(); if (!authToken) return;
    const assistantId = `assistant-stream-${Date.now()}`;
    const requestVisual = visualInputForChat(visualInputOverride, visualAttachmentPending, resumeAfterReference);
    const input = {
      session_id: requestSessionId,
      message: text,
      current_image_url: requestVisual && isSafeImageReferenceUrl(requestVisual.url) ? requestVisual.url : null,
      current_image_role: requestVisual?.role ?? null,
      current_image_task_id: requestVisual?.imageTaskId ?? null,
      resume_after_reference: resumeAfterReference,
      model_id: preferences.chatModelId,
      thinking_mode: preferences.thinkingMode,
      image_model_id: preferences.imageModelId,
      reference_case_id: referenceId,
    };
    await chatLifecycleRef.current?.send({
      session: { sessionId: requestSessionId, epoch, authToken },
      assistantId,
      message: text,
      suppressUserEcho,
      input,
    });
  };

  const handleConfirmGenerate = async (card: ProposalCard, prompt: string, selected: MediaPreferences) => {
    if (busy || !sessionId || !capabilities || generatedCards.has(card.card_id)) return;
    const requestSessionId = sessionId; const epoch = authEpochRef.current; const authToken = getStoredMediaToken();
    if (!authToken) return;
    const visual = generationVisualInput(card, activeVisualInput);
    if (!visual.valid) { setErrorMsg('参考图已经变化，请先让 AI 重新整理生成方案。'); return; }
    const protectedParent = visual.parentId != null && protectedImageResultTaskId(visual.refImageUrl);
    const imageReference = protectedParent || isSafeImageReferenceUrl(visual.refImageUrl) ? visual.refImageUrl : null;
    const intent = {
      prompt, model_id: selected.imageModelId, style: card.style, aspect_ratio: selected.aspectRatio,
      quality: selected.quality, storage_policy: selected.storagePolicy, ref_image_url: imageReference,
      session_id: requestSessionId, parent_id: visual.parentId, proposal_card_id: card.card_id,
    };
    await imageJobSubmissionRef.current?.submit({
      cardId: card.card_id,
      session: { sessionId: requestSessionId, epoch, authToken },
      intent,
    });
  };

  const requestCardReferenceUpload = (cardId: string) => {
    if (busy || !sessionId || generatedCards.has(cardId)) return;
    if (referenceWorkflow.prepare(cardId)) fileInputRef.current?.click();
  };

  const requestComposerReferenceUpload = () => {
    if (busy || !sessionId) return;
    if (referenceWorkflow.prepare(null)) fileInputRef.current?.click();
  };

  const handleUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    // Reset only this selection immediately; a late finally cannot clear the next file picker.
    event.target.value = '';
    if (!file || busy || !sessionId) return;
    const receipt = await referenceWorkflow.upload(file);
    if (!receipt?.isCurrent()) return;
    void refreshSessions();
    if (receipt.resume) window.setTimeout(() => {
      if (receipt.isCurrent()) void handleSendMessage('继续刚才的需求，使用当前参考图。', receipt.visualInput, true, true);
    }, 0);
  };

  const handleRemoveCardReference = async (cardId: string) => {
    if (busy || generatedCards.has(cardId) || !sessionId) return;
    await referenceWorkflow.removeCard(cardId);
  };

  const handleClearVisualInput = (): Promise<boolean> => referenceWorkflow.clearComposer();

  const loadHistoryItems = async () => {
    const epoch = authEpochRef.current;
    const authToken = getStoredMediaToken();
    if (!authToken) return;
    setHistoryModalError(null);
    try {
      const items = await fetchUserHistory();
      if (epoch !== authEpochRef.current || getStoredMediaToken() !== authToken) return;
      const hydrated = await Promise.all(items.map(async (item) => {
        const source = resolveImagePreviewSource(item.image_url, item.task_id);
        if (!source || source.kind === 'direct') return item;
        try {
          const blob = await fetchOriginalImageResult(source.taskId);
          if (epoch !== authEpochRef.current || getStoredMediaToken() !== authToken) return item;
          const imageUrl = URL.createObjectURL(blob);
          objectUrlsRef.current.add(imageUrl);
          return { ...item, image_url: imageUrl };
        } catch (error) { handleAuthFailure(error); return item; }
      }));
      if (epoch !== authEpochRef.current || getStoredMediaToken() !== authToken) {
        hydrated.forEach((item) => { if (isObjectUrl(item.image_url)) { URL.revokeObjectURL(item.image_url); objectUrlsRef.current.delete(item.image_url); } });
        return;
      }
      setHistoryItems(hydrated);
    } catch (error: any) {
      if (epoch !== authEpochRef.current || getStoredMediaToken() !== authToken) return;
      setHistoryModalError(error.message || '加载历史作品失败'); handleAuthFailure(error);
    }
  };
  const openHistory = () => { setShowHistoryModal(true); void loadHistoryItems(); };
  const selectReference = (reference: ReferenceCase) => {
    setSelectedReference(reference);
    if (!inputText.trim() && reference.example_prompt) setInputText(reference.example_prompt);
  };

  const handleContentScroll = (event: React.UIEvent<HTMLDivElement>) => {
    const top = event.currentTarget.scrollTop;
    const delta = top - lastScrollTopRef.current;
    if (delta > 8 && top > 120) setComposerHidden(true);
    else if (delta < -6) setComposerHidden(false);
    lastScrollTopRef.current = top;
  };

  const goBack = () => {
    clearMediaToken();
    navigate('/login', { replace: true });
  };

  const visibleSessions = sessionId && !sessions.some((entry) => entry.session_id === sessionId)
    ? [{ session_id: sessionId, title: '新会话', updated_at: null, message_count: 0, last_image_url: null }, ...sessions] : sessions;

  if (authLoading) return <div className="grid h-full place-items-center bg-neutral-50 text-sm text-neutral-500" role="status">正在连接 AI 创作…</div>;
  if (!user || !token) return (
    <main className="grid h-full place-items-center bg-neutral-50 p-4">
      <section className="card w-full max-w-sm p-6 text-center">
        <div className="mx-auto grid h-11 w-11 place-items-center rounded-2xl bg-blue-600 text-white"><Sparkles size={18} /></div>
        <h1 className="mt-4 text-lg font-semibold text-neutral-900">AI 创作暂时无法连接</h1>
        <p role="alert" className="mt-2 text-sm leading-relaxed text-neutral-500">{loginError || '请登录本实例的 Media 账号。'}</p>
        <button type="button" onClick={() => void retryLoginCheck()} className="btn-primary mt-5 min-h-11 w-full rounded-2xl bg-blue-600">重新连接</button>
        <a href="/login" className="mt-3 inline-block text-xs text-neutral-500 underline">重新登录</a>
      </section>
    </main>
  );

  return (
    <div className="relative flex h-dvh min-h-0 flex-col overflow-hidden bg-[#f6f7f9]" onPointerMove={(event) => {
      if (window.innerWidth > 700 && event.clientY > window.innerHeight - 120) setComposerHidden(false);
    }}>
      <header className="absolute inset-x-0 top-0 z-30 mx-auto flex h-16 w-full max-w-[940px] items-center justify-between px-4 sm:px-6">
        <button type="button" onClick={goBack} aria-label="退出登录" className="grid h-11 w-11 place-items-center rounded-2xl bg-white text-neutral-700 shadow-[0_6px_24px_rgba(30,35,50,0.08)] transition hover:bg-neutral-50"><ArrowLeft size={19} /></button>
        <div className="text-[15px] font-semibold tracking-tight text-neutral-900">AI 生图</div>
        <div className="relative">
          <button type="button" onClick={() => setShowSessions((open) => !open)} aria-expanded={showSessions} aria-label="创作记录" className="grid h-11 w-11 place-items-center rounded-2xl text-neutral-500 transition hover:bg-white"><MoreHorizontal size={20} /></button>
          {showSessions && <div className="absolute right-0 top-full z-40 mt-2 w-[min(22rem,calc(100vw-2rem))] rounded-[22px] border border-neutral-200/80 bg-white p-2 shadow-2xl">
            <div className="flex items-center justify-between gap-2 px-2 pb-2"><span className="text-xs font-semibold text-neutral-700">创作记录</span><div className="flex gap-1"><button type="button" onClick={openHistory} className="grid h-9 w-9 place-items-center rounded-xl text-neutral-500 hover:bg-neutral-50" aria-label="作品库"><History size={15} /></button><button type="button" onClick={startNewSession} disabled={jobSubmitting || historyLoading} className="grid h-9 w-9 place-items-center rounded-xl text-neutral-500 hover:bg-neutral-50 disabled:opacity-40" aria-label="新会话"><Plus size={15} /></button></div></div>
            {sessionsError && <p role="alert" className="p-2 text-xs text-red-600">{sessionsError}<button onClick={() => void refreshSessions()} className="ml-2 underline">重试</button></p>}
            <div className="max-h-72 overflow-y-auto">{visibleSessions.map((entry) => <button key={entry.session_id} type="button" disabled={jobSubmitting || historyLoading} onClick={() => void selectSession(entry.session_id)} className="block min-h-10 w-full truncate rounded-xl px-2 text-left text-xs text-neutral-600 hover:bg-neutral-50 disabled:opacity-50">{entry.title || '新会话'}{entry.message_count ? ` · ${entry.message_count} 条` : ''}</button>)}</div>
          </div>}
        </div>
      </header>

      <div ref={contentScrollRef} onScroll={handleContentScroll} className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-40 pt-16 sm:px-6">
        <main className="mx-auto w-full max-w-[900px]">
          {capabilityError && <div role="alert" className="mx-auto mt-3 max-w-2xl rounded-2xl border border-red-200 bg-red-50 p-3 text-xs text-red-700">创作能力暂时不可用。{capabilityError}<button type="button" onClick={() => void retryLoginCheck()} className="ml-2 underline">重试</button></div>}
          {errorMsg && <div role="alert" className="mx-auto mt-3 max-w-2xl rounded-2xl border border-red-200 bg-red-50 p-3 text-xs text-red-700">{errorMsg}</div>}
          {historyLoading && <div role="status" className="py-6 text-center text-xs text-neutral-400">正在恢复创作记录…</div>}
          {historyError && !historyLoading && <div role="alert" className="mx-auto mt-3 max-w-2xl rounded-2xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">{historyError}<button type="button" onClick={() => void retryHistory()} className="ml-2 underline">重试</button></div>}
          {activeVisualInput && !visualAttachmentPending && <div className="mx-auto mt-3 flex max-w-3xl items-center justify-between gap-3 rounded-full border border-[#6d82f6]/15 bg-white/75 px-3.5 text-[11px] text-neutral-500 shadow-sm">
            <span className="min-w-0 truncate"><span className="font-medium text-neutral-700">{activeVisualInput.role === 'reference' ? '参考图上下文' : '修改底图上下文'}</span>{typeof activeVisualInput.remainingTurns === 'number' ? ` · 剩余 ${activeVisualInput.remainingTurns} 轮` : ' · 当前会话生效'}</span>
            <ReferenceActionButton variant="context" onClick={() => void handleClearVisualInput()}>清除</ReferenceActionButton>
          </div>}

          {messages.length === 0 && !historyLoading && !historyError && <section className="pb-8 pt-10 sm:pt-14">
            <h1 className="max-w-2xl text-[30px] font-bold leading-[1.08] tracking-[-0.035em] text-neutral-900 sm:text-[38px]">说一句，就开始创作。</h1>
            <p className="mt-3 max-w-lg text-sm leading-7 text-neutral-400">不用选模型、画质、风格。你只管说想要什么，其他交给 AI。</p>
            <div className="mt-7 flex gap-2.5 overflow-x-auto pb-2 [scrollbar-width:none]">{INSPIRATION_TIPS.map((tip) => <button key={tip.title} type="button" onClick={() => void handleSendMessage(tip.prompt)} disabled={busy || !capabilities} className="shrink-0 rounded-full border border-neutral-200 bg-white px-4 py-2.5 text-xs text-neutral-600 transition hover:border-neutral-300 hover:shadow-sm disabled:opacity-50">{tip.title}</button>)}</div>
            {references.length > 0 && <div className="mt-7"><div className="mb-2 text-[11px] text-neutral-400">灵感，只是参考</div><div className="flex gap-3 overflow-x-auto pb-2 [scrollbar-width:none]">{references.slice(0, 3).map((reference) => <button key={reference.id} type="button" onClick={() => selectReference(reference)} className="relative aspect-[1.1] w-[72vw] max-w-56 shrink-0 overflow-hidden rounded-[22px] bg-neutral-200 text-left sm:w-52">{reference.preview_url ? <img src={reference.preview_url} alt={reference.title} className="h-full w-full object-cover" loading="lazy" /> : <span className="flex h-full items-center px-5 text-sm leading-relaxed text-neutral-600">{reference.description || reference.title}</span>}<span className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/60 to-transparent px-3 pb-3 pt-10 text-xs font-medium text-white">{reference.title}</span></button>)}</div></div>}
          </section>}

          <div className="mx-auto max-w-3xl space-y-5 py-5">
            {messages.map((message) => <article key={message.id} className={`flex flex-col ${message.role === 'user' ? 'items-end' : 'items-start'}`}>
              {message.content && !message.image_url && <div className={`max-w-[88%] text-sm leading-7 ${message.role === 'user' ? 'whitespace-pre-wrap rounded-[22px] rounded-br-md bg-neutral-900 px-4 py-3 text-white' : 'text-neutral-700'}`}>{message.role === 'user' ? message.content : <div className="space-y-1.5">{renderMediaMarkdown(message.content)}</div>}</div>}
              {message.role === 'assistant' && message.fallback_reason && <div className="mt-1 text-[10px] text-neutral-400">已自动使用可用通道</div>}
              {message.proposal_card && <ProposalEditor card={message.proposal_card} previewUrl={proposalImagePreview(message.proposal_card, activeVisualInput, messages)} capabilities={capabilities} preferences={preferences} activeVisualInput={activeVisualInput} onConfirm={handleConfirmGenerate} onUploadReference={requestCardReferenceUpload} onRemoveReference={(cardId) => void handleRemoveCardReference(cardId)} busy={busy} uploading={referenceUpload?.cardId === message.proposal_card.card_id} uploadPreview={referenceUpload?.cardId === message.proposal_card.card_id ? referenceUpload : null} generating={generatingCardId === message.proposal_card.card_id} submitted={generatedCards.has(message.proposal_card.card_id)} job={activeImageJobs.find((job) => job.proposal_card_id === message.proposal_card?.card_id) ?? null} nowMs={nowMs} />}
              {message.image_url && <ArtworkCard message={message} onPreview={() => setPreviewImg(message.image_url!)} onEdit={() => { visualState.replace({ url: message.image_url!, sourceUrl: isObjectUrl(message.image_url) && message.task_id ? `/api/image/result/${message.task_id}` : message.image_url!, role: 'base', source: 'artwork', imageTaskId: message.image_task_id ?? null }); visualState.setAttachmentPending(true); setInputText('在此图基础上修改：'); setComposerHidden(false); }} onRegenerate={() => { void (async () => { if (await handleClearVisualInput()) void handleSendMessage('保持刚才作品的核心主题，再做一个不同构图和细节的版本。', null); })(); }} />}
            </article>)}
            {activeImageJobs.filter((job) => !job.proposal_card_id).map((job) => <div key={job.task_id} className="w-full max-w-2xl"><ImageJobProgress job={job} nowMs={nowMs} /></div>)}
            {loading && <ThinkingStatus label={streamStatus || '正在理解你的需求'} visualInput={activeVisualInput} />}
            <div ref={messagesEndRef} />
          </div>
        </main>
      </div>

      <footer className={`pointer-events-none fixed inset-x-0 bottom-[max(12px,env(safe-area-inset-bottom))] z-40 px-3 transition duration-300 ${composerHidden ? 'translate-y-[125%] opacity-0' : 'translate-y-0 opacity-100'}`}>
        <div className="pointer-events-auto mx-auto max-w-[780px]">
          <div className="mb-2 flex flex-wrap gap-2 px-1">
            {referenceUpload && referenceUpload.cardId === null && <ReferenceUploadProgress previewUrl={referenceUpload.previewUrl} fileName={referenceUpload.fileName} />}
            {selectedReference && <div className="inline-flex max-w-full items-center gap-1.5 rounded-full bg-white/95 px-3 py-1.5 text-[11px] text-neutral-600 shadow-sm"><span className="truncate">灵感：{selectedReference.title}</span><button type="button" onClick={() => setSelectedReference(null)} aria-label="移除灵感案例"><X size={12} /></button></div>}
            {activeVisualInput && visualAttachmentPending && !referenceUpload && <div className="inline-flex max-w-full items-center gap-2 rounded-2xl bg-white/95 p-1.5 pr-2.5 text-[11px] text-neutral-600 shadow-sm">
              <img src={activeVisualInput.url} alt={activeVisualInput.role === 'reference' ? '参考图缩略图' : '修改底图缩略图'} className="h-9 w-9 rounded-xl object-cover" />
              <span>{activeVisualInput.role === 'reference' ? '参考图' : '修改底图'}</span>
              <button type="button" onClick={() => void handleClearVisualInput()} aria-label={activeVisualInput.role === 'reference' ? '移除参考图' : '取消修改底图'}><X size={12} /></button>
            </div>}
          </div>
          <div className="relative flex items-end gap-1.5 rounded-[26px] border border-black/[0.06] bg-white/95 p-2 shadow-[0_18px_60px_rgba(30,35,55,0.18)] backdrop-blur-xl">
            <input type="file" ref={fileInputRef} onChange={(event) => void handleUpload(event)} accept="image/*" className="hidden" />
            <button type="button" aria-label="上传参考图" onClick={requestComposerReferenceUpload} disabled={busy || !sessionId} className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl text-neutral-400 transition hover:bg-neutral-50 disabled:opacity-30"><Plus size={18} /></button>
            <VoiceDraftActions surface="media" value={inputText} onChange={setInputText} targetRef={inputRef} disabled={busy || historyLoading || !sessionId || !capabilities} compact />
            <textarea ref={inputRef} aria-label="创作描述" rows={1} value={inputText} onFocus={() => setComposerHidden(false)} onChange={(event) => setInputText(event.target.value)} onKeyDown={(event) => {
              if (shouldSubmitOnEnter({ key: event.key, shiftKey: event.shiftKey, isComposing: event.nativeEvent.isComposing, keyCode: event.nativeEvent.keyCode })) { event.preventDefault(); void handleSendMessage(); }
            }} disabled={busy || historyLoading || !sessionId || !capabilities} placeholder={capabilityError ? '创作能力暂不可用' : '说说你想要什么…'} className="max-h-32 min-h-11 min-w-0 flex-1 resize-none border-0 bg-transparent px-2 py-3 text-sm outline-none placeholder:text-neutral-400 disabled:opacity-50" />
            <button type="button" aria-label="发送消息" onClick={() => void handleSendMessage()} disabled={!inputText.trim() || busy || historyLoading || !sessionId || !capabilities} className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-[#6d82f6] text-white transition hover:bg-[#5d73ee] disabled:pointer-events-none disabled:opacity-30"><Send size={16} /></button>
          </div>
        </div>
      </footer>
      {previewImg && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-4" onClick={() => setPreviewImg(null)}><img src={previewImg} alt="作品大图" className="max-h-full max-w-full object-contain" /><button type="button" aria-label="关闭大图" className="absolute right-4 top-4 text-white"><X size={24} /></button></div>}
      {showHistoryModal && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label="历史生成作品"><div className="card flex max-h-[80vh] w-full max-w-lg flex-col overflow-hidden p-5"><div className="flex items-center justify-between border-b pb-3"><h2 className="font-semibold">历史生成作品</h2><button type="button" onClick={() => setShowHistoryModal(false)} aria-label="关闭作品库" className="grid min-h-10 min-w-10 place-items-center"><X size={18} /></button></div><div className="flex-1 overflow-y-auto pt-3">{historyModalError ? <p role="alert" className="py-8 text-center text-xs text-red-600">{historyModalError}<button type="button" onClick={() => void loadHistoryItems()} className="ml-2 underline">重试</button></p> : historyItems.length === 0 ? <p className="py-8 text-center text-xs text-neutral-400">暂无历史作品</p> : <div className="grid grid-cols-2 gap-3">{historyItems.map((item) => <div key={item.id} className="overflow-hidden rounded-xl border"><button type="button" onClick={() => setPreviewImg(item.image_url)} className="block w-full text-left"><img src={item.image_url} alt={item.prompt} className="h-32 w-full object-cover" loading="lazy" /><span className="block truncate p-2 text-xs">{item.final_prompt || item.prompt}</span><span className="block px-2 pb-2 text-[10px] text-neutral-400">{item.aspect_ratio} · {item.style}{item.model_id ? ` · ${item.model_id}` : ''}{item.fallback_reason ? ` · 回退：${item.fallback_reason}` : ''}</span></button><button type="button" onClick={() => { visualState.replace({ url: item.image_url, sourceUrl: isObjectUrl(item.image_url) && item.task_id ? `/api/image/result/${item.task_id}` : item.image_url, role: 'base', source: 'artwork', imageTaskId: item.image_task_id ?? null }); visualState.setAttachmentPending(true); setInputText('在此图基础上修改：'); setShowHistoryModal(false); }} className="min-h-10 w-full border-t px-2 text-xs text-neutral-700">基于此图修改</button></div>)}</div>}</div></div></div>}
    </div>
  );
}
