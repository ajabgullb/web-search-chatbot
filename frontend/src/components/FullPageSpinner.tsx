import { LogoMark } from '@/components/Logo'

/** Centred busy state, announced to assistive technology. */
export function FullPageSpinner({ label }: { label: string }) {
  return (
    <div
      className="flex min-h-dvh flex-1 items-center justify-center"
      role="status"
      aria-live="polite"
    >
      <LogoMark className="size-10 animate-breathe motion-reduce:animate-none" />
      <span className="sr-only">{label}</span>
    </div>
  )
}
