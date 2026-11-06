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
            className="h-9 w-full rounded-lg border border-ring bg-card px-2.5 text-sm ring-4 ring-ring/15 focus-visible:outline-none"
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
      <li className="animate-fade rounded-xl border border-destructive/25 bg-destructive/6 px-2.5 py-2.5 text-sm">
        <p className="mb-2 text-xs">
          Delete <span className="font-medium">{conversation.title}</span>?
        </p>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => setMode('idle')}>
            Cancel
          </Button>
          <Button
            size="sm"
            className="bg-destructive text-destructive-foreground shadow-none hover:bg-destructive/90"
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
          'relative block rounded-lg py-2 pr-2 pl-3 text-[13.5px] transition-colors motion-reduce:transition-none',
          'focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
          // The bar marks the open chat by shape as well as by shade.
          'before:absolute before:inset-y-2 before:left-0 before:w-[3px] before:rounded-full before:bg-foreground before:transition-opacity',
          // Opaque fills, so the actions' gradient below can match them.
          isActive
            ? 'bg-accent font-medium text-foreground before:opacity-100'
            : 'text-foreground/75 before:opacity-0 group-focus-within/item:bg-muted group-focus-within/item:text-foreground group-hover/item:bg-muted group-hover/item:text-foreground',
        )}
      >
        {/* Full width, fading at the edge; the actions cover the end only
            while they are shown. */}
        <span className="text-fade block">{conversation.title}</span>
      </NavLink>

      {/* Revealed on hover, but always reachable by keyboard via focus-within.
          The gradient takes the row's own fill, so the title fades under the
          buttons instead of being cut by them. */}
      <div
        className={cn(
          'absolute inset-y-0 right-0 flex items-center rounded-r-lg bg-linear-to-l pr-1 pl-6',
          isActive ? 'from-accent from-70%' : 'from-muted from-70%',
          'opacity-0 transition-opacity group-focus-within/item:opacity-100 group-hover/item:opacity-100 motion-reduce:transition-none',
        )}
      >
        <Button
          size="icon"
          variant="ghost"
          className="size-7 rounded-lg text-muted-foreground hover:bg-background hover:text-foreground"
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
          className="size-7 rounded-lg text-muted-foreground hover:bg-background hover:text-destructive"
          aria-label={`Delete ${conversation.title}`}
          onClick={() => setMode('confirming-delete')}
        >
          <Trash2 className="size-3.5" aria-hidden />
        </Button>
      </div>
    </li>
  )
}
