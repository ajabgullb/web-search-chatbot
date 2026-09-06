/** Centred busy state, announced to assistive technology. */
export function FullPageSpinner({ label }: { label: string }) {
  return (
    <div className="flex min-h-dvh items-center justify-center" role="status" aria-live="polite">
      <span
        aria-hidden="true"
        className="size-6 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent motion-reduce:animate-none"
      />
      <span className="sr-only">{label}</span>
    </div>
  )
}
