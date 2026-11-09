interface ReferenceUploadProgressProps {
  previewUrl: string;
  fileName: string;
}

/**
 * Transient reference-image upload feedback.
 *
 * This deliberately shows an indeterminate bar: fetch does not expose upload
 * byte progress, so displaying a numeric percentage here would be fake.
 */
export default function ReferenceUploadProgress({ previewUrl, fileName }: ReferenceUploadProgressProps) {
  return <div role="status" aria-live="polite" className="inline-flex max-w-full items-center gap-2.5 rounded-2xl border border-[#6d82f6]/15 bg-white/95 p-1.5 pr-3 text-[11px] text-neutral-600 shadow-sm">
    <img src={previewUrl} alt="正在上传的参考图" className="h-9 w-9 shrink-0 rounded-xl object-cover" />
    <div className="min-w-0 flex-1">
      <div className="flex items-center justify-between gap-3">
        <span className="font-medium text-neutral-700">正在上传参考图</span>
        <span className="max-w-28 truncate text-[10px] text-neutral-400">{fileName}</span>
      </div>
      <div aria-label="参考图上传处理中" className="mt-1.5 h-1 overflow-hidden rounded-full bg-neutral-100">
        <div className="h-full w-full animate-pulse rounded-full bg-[#6d82f6]/55" />
      </div>
    </div>
  </div>;
}
