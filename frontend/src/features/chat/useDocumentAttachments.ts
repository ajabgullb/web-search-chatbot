/**
 * Attaching PDFs to a conversation: client-side checks, upload, removal, and
 * the one error message the composer shows about them.
 */

import { useState } from 'react'

import { ApiError, NetworkError } from '@/api/errors'
import type { ChatDocument } from '@/api/types'
import { MAX_DOCUMENT_BYTES } from '@/features/chat/constants'
import { useDeleteDocument, useDocuments, useUploadDocument } from '@/features/chat/queries'

const MAX_DOCUMENT_MB = Math.round(MAX_DOCUMENT_BYTES / (1024 * 1024))

/** What the picker offers. The server decides from the file's bytes regardless. */
export const PDF_ACCEPT = 'application/pdf,.pdf'

function looksLikePdf(file: File): boolean {
  return file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')
}

function describeFailure(error: unknown): string {
  // The server's messages are written for people, so they are shown as they
  // are. Only transport failures, which have no envelope, need wording here.
  if (error instanceof ApiError) return error.message
  if (error instanceof NetworkError)
    return 'The upload failed. Check your connection and try again.'
  return 'The upload failed. Try again.'
}

export interface DocumentAttachments {
  documents: ChatDocument[]
  attach: (file: File) => void
  uploading: boolean
  /** The file being uploaded right now, shown as a pending chip. */
  uploadingName: string | null
  remove: (documentId: string) => void
  removingId: string | null
  error: string | null
  dismissError: () => void
}

export function useDocumentAttachments(conversationId: string | undefined): DocumentAttachments {
  const documents = useDocuments(conversationId)
  const upload = useUploadDocument(conversationId ?? '')
  const remove = useDeleteDocument(conversationId ?? '')
  const [error, setError] = useState<string | null>(null)

  function attach(file: File) {
    setError(null)
    // Checked here as a courtesy, so a wrong or huge file is refused before
    // it is sent. The server checks again and is the one that decides.
    if (!looksLikePdf(file)) {
      setError(`${file.name} is not a PDF. Only PDF files can be attached.`)
      return
    }
    if (file.size > MAX_DOCUMENT_BYTES) {
      setError(`${file.name} is larger than ${MAX_DOCUMENT_MB} MB.`)
      return
    }
    upload.mutate(file, {
      onError: (cause) => setError(`Could not attach ${file.name}: ${describeFailure(cause)}`),
    })
  }

  return {
    documents: documents.data?.items ?? [],
    attach,
    uploading: upload.isPending,
    uploadingName: upload.isPending ? upload.variables.name : null,
    remove: (documentId) => {
      setError(null)
      remove.mutate(documentId, {
        onError: (cause) => setError(`Could not remove the document: ${describeFailure(cause)}`),
      })
    },
    removingId: remove.isPending ? remove.variables : null,
    error,
    dismissError: () => setError(null),
  }
}
