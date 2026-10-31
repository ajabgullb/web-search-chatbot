/**
 * The chips that show a conversation's PDFs inside the composer. The upload
 * logic is in `useDocumentAttachments.ts`.
 *
 * A document belongs to the conversation, not to one message (ADR-0015), so
 * attaching one does not send anything. It is uploaded at once, stays in the
 * box for as long as the conversation has it, and every later answer can use
 * it. The server prepares it in the background; the chip shows where that is.
 */

import { CircleAlert, FileText, LoaderCircle, X } from 'lucide-react'

import type { ChatDocument } from '@/api/types'
import type { DocumentAttachments } from '@/features/chat/useDocumentAttachments'
import { cn } from '@/lib/utils'

/** Why a document's preparation failed, in words. */
function describeDocumentError(code: string | null | undefined): string {
  return code === 'embedding_unavailable'
    ? 'It could not be prepared. Remove it and try again later.'
    : 'It could not be prepared.'
}

function StatusIcon({ status }: { status: ChatDocument['status'] | 'uploading' }) {
  if (status === 'processing' || status === 'uploading') {
    return (
      <LoaderCircle
        className="size-3.5 shrink-0 animate-spin text-muted-foreground motion-reduce:animate-none"
        aria-hidden
      />
    )
  }
  if (status === 'failed') {
    return <CircleAlert className="size-3.5 shrink-0 text-destructive" aria-hidden />
  }
  return <FileText className="size-3.5 shrink-0 text-primary" aria-hidden />
}

const STATUS_TEXT: Record<ChatDocument['status'] | 'uploading', string> = {
  uploading: 'uploading',
  processing: 'preparing',
  ready: 'ready',
  failed: 'failed',
}

interface DocumentChipsProps {
  attachments: DocumentAttachments
  /** True while an answer is being written; the server refuses removal then. */
  running: boolean
}

export function DocumentChips({ attachments, running }: DocumentChipsProps) {
  const { documents, uploadingName, removingId, error } = attachments
  if (documents.length === 0 && uploadingName === null && error === null) return null

  return (
    <div className="flex flex-col gap-1.5">
      {documents.length > 0 || uploadingName !== null ? (
        // Polite live region: a screen reader hears "report.pdf, ready" when
        // preparation finishes, without the chip stealing focus.
        <ul aria-label="Attached documents" aria-live="polite" className="flex flex-wrap gap-1.5">
          {documents.map((document) => (
            <li
              key={document.id}
              className={cn(
                'animate-rise flex max-w-full items-center gap-1.5 rounded-xl border border-border bg-muted/70 py-1 pr-1 pl-2 text-xs font-medium',
                document.status === 'ready' && 'border-primary/25 bg-primary/8',
                document.status === 'failed' && 'border-destructive/50 bg-destructive/6',
              )}
              title={
                document.status === 'failed'
                  ? describeDocumentError(document.error_code)
                  : `${document.page_count} pages`
              }
            >
              <StatusIcon status={document.status} />
              <span className="max-w-48 truncate">{document.filename}</span>
              <span className="sr-only">, {STATUS_TEXT[document.status]}</span>
              <button
                type="button"
                onClick={() => attachments.remove(document.id)}
                disabled={running || removingId === document.id}
                aria-label={`Remove ${document.filename}`}
                title={running ? 'Wait for the answer to finish' : `Remove ${document.filename}`}
                className="rounded-md p-0.5 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50"
              >
                <X className="size-3.5" aria-hidden />
              </button>
            </li>
          ))}
          {uploadingName !== null ? (
            <li className="animate-rise flex max-w-full items-center gap-1.5 rounded-xl border border-dashed border-border px-2 py-1 text-xs text-muted-foreground">
              <StatusIcon status="uploading" />
              <span className="max-w-48 truncate">{uploadingName}</span>
              <span className="sr-only">, {STATUS_TEXT.uploading}</span>
            </li>
          ) : null}
        </ul>
      ) : null}

      {error !== null ? (
        <div role="alert" className="flex items-start gap-1.5 text-xs text-destructive">
          <CircleAlert className="mt-px size-3.5 shrink-0" aria-hidden />
          <p className="flex-1">{error}</p>
          <button
            type="button"
            onClick={attachments.dismissError}
            aria-label="Dismiss"
            className="rounded p-0.5 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      ) : null}
    </div>
  )
}
