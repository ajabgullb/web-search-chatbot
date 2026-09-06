/**
 * One row in the sidebar, with inline rename and delete.
 *
 * Both destructive-ish actions stay inside the row rather than opening a modal
 * or calling `window.confirm`: the confirmation is real DOM, so it is
 * keyboard-navigable, announced, and testable.
 */

import { Check, Pencil, Trash2 } from 'lucide-react'
import { useState } from 'react'
import type { KeyboardEvent } from 'react'
import { NavLink } from 'react-router'

import type { Conversation } from '@/api/types'
import { Button } from '@/components/ui/Button'
import { cn } from '@/lib/utils'

interface ConversationItemProps {
  conversation: Conversation
  isActive: boolean
  onRename: (title: string) => void
  onDelete: () => void
  /** Called when the row is followed, so a mobile panel can close itself. */
  onNavigate: () => void
}

type Mode = 'idle' | 'renaming' | 'confirming-delete'

export function ConversationItem({
  conversation,
  isActive,
  onRename,
  onDelete,
  onNavigate,
}: ConversationItemProps) {
  const [mode, setMode] = useState<Mode>('idle')
  const [draft, setDraft] = useState(conversation.title)

  function commitRename() {
    const title = draft.trim()
    // An empty title is a no-op, not a request to blank the conversation.
    if (title && title !== conversation.title) onRename(title)
    setMode('idle')
  }

  function handleRenameKeys(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter') {
      event.preventDefault()
      commitRename()
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      setDraft(conversation.title)
      setMode('idle')
    }
  }

  if (mode === 'renaming') {
    return (
      <li className="px-2 py-1">
        <label htmlFor={`rename-${conversation.id}`} className="sr-only">
          Rename conversation
        </label>
        <div className="flex items-center gap-1">
          <input
            id={`rename-${conversation.id}`}
            autoFocus
            value={draft}
            maxLength={200}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={handleRenameKeys}
            onBlur={commitRename}
            className="h-8 w-full rounded-md border border-input bg-background px-2 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          />
          <Button size="icon" variant="ghost" aria-label="Save name" onMouseDown={commitRename}>
            <Check className="size-4" aria-hidden />
          </Button>
        </div>
      </li>
    )
  }

  if (mode === 'confirming-delete') {
    return (
      <li className="rounded-md bg-accent px-2 py-2 text-sm">
        <p className="mb-2 text-xs">
          Delete <span className="font-medium">{conversation.title}</span>?
        </p>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => setMode('idle')}>
            Cancel
          </Button>
          <Button
            size="sm"
            aria-label={`Confirm delete ${conversation.title}`}
            onClick={() => {
              setMode('idle')
              onDelete()
            }}
          >
            Delete
          </Button>
        </div>
      </li>
    )
  }

  return (
    <li className="group/item relative">
      <NavLink
        to={`/chat/${conversation.id}`}
        onClick={onNavigate}
        aria-current={isActive ? 'page' : undefined}
        className={cn(
          'block truncate rounded-md py-2 pr-16 pl-2 text-sm transition-colors motion-reduce:transition-none',
          'focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
          isActive ? 'bg-accent font-medium' : 'hover:bg-accent/60',
        )}
      >
        {conversation.title}
      </NavLink>

      {/* Revealed on hover, but always reachable by keyboard via focus-within. */}
      <div className="absolute top-1/2 right-1 flex -translate-y-1/2 opacity-0 group-focus-within/item:opacity-100 group-hover/item:opacity-100">
        <Button
          size="icon"
          variant="ghost"
          aria-label={`Rename ${conversation.title}`}
          onClick={() => {
            setDraft(conversation.title)
            setMode('renaming')
          }}
        >
          <Pencil className="size-3.5" aria-hidden />
        </Button>
        <Button
          size="icon"
          variant="ghost"
          aria-label={`Delete ${conversation.title}`}
          onClick={() => setMode('confirming-delete')}
        >
          <Trash2 className="size-3.5" aria-hidden />
        </Button>
      </div>
    </li>
  )
}
