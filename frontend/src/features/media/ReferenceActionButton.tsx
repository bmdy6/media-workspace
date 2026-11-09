import type { ReactNode } from 'react';

type Props = {
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  variant?: 'context' | 'card';
};

/** Local reference-image actions share a keyboard-visible, 44px touch target. */
export default function ReferenceActionButton({ children, onClick, disabled = false, variant = 'card' }: Props) {
  const appearance = variant === 'context'
    ? 'rounded-full px-2 text-neutral-400 underline decoration-neutral-300 underline-offset-2'
    : 'rounded-xl border border-neutral-200 bg-white px-3 py-2 text-[11px] font-medium text-neutral-700';
  return <button
    type="button"
    onClick={onClick}
    disabled={disabled}
    className={`inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-neutral-700 disabled:opacity-40 ${appearance}`}
  >{children}</button>;
}
