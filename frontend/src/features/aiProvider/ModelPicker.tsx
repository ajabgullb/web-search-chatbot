/**
 * The model picker in the chat box (ADR-0020).
 *
 * A button showing the model that will answer; it opens a searchable list of
 * the app's default and every provider's models. Choosing a model whose
 * provider has a key switches at once. Choosing one without a key asks for
 * the key right here, then switches.
 *
 * Keyboard: the search box is an ARIA combobox. Arrow keys move through the
 * list, Enter chooses, Escape closes (or steps back from the key form), and
 * focus returns to the button. Arrow keys on the closed button open it.
 */

import { Check, ChevronDown, KeyRound, Lock, Search, Settings2 } from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Link } from 'react-router'

import type { LlmProvider, LlmSettings } from '@/api/types'
import { Alert } from '@/components/ui/Alert'
import { ConnectKeyForm } from '@/features/aiProvider/ConnectKeyForm'
import { buildGroups, isSelected } from '@/features/aiProvider/modelOptions'
import type { PickerOption } from '@/features/aiProvider/modelOptions'
import {
  AI_PROVIDER_PATH,
  describeKeyError,
  providerLabel,
  useLlmSettings,
  useSelectLlm,
} from '@/features/aiProvider/queries'
import { cn } from '@/lib/utils'

interface ModelPickerProps {
  /** Which way the list opens: up from a docked box, down from the hero box. */
  placement?: 'up' | 'down'
}

function triggerText(settings: LlmSettings): { model: string; detail: string } {
  const selection = settings.selection
  if (!selection) return { model: 'Choose a model', detail: 'No model yet' }
  if (selection.source === 'server') return { model: selection.model, detail: 'App default' }
  return { model: selection.model, detail: providerLabel(settings, selection.provider) }
}

export function ModelPicker({ placement = 'up' }: ModelPickerProps) {
  const settings = useLlmSettings()
  const select = useSelectLlm()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const [connecting, setConnecting] = useState<{ provider: LlmProvider; model: string } | null>(
    null,
  )
  const [announcement, setAnnouncement] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const baseId = useId()
  const listId = `${baseId}-list`
  const optionId = (index: number) => `${baseId}-option-${index}`

  const data = settings.data
  const groups = useMemo(() => (data ? buildGroups(data, query) : []), [data, query])
  const options = groups.flatMap((group) => group.options)
  const active = options.length === 0 ? -1 : Math.min(activeIndex, options.length - 1)

  // A click anywhere else closes it, as any menu does.
  useEffect(() => {
    if (!open) return
    function onPointerDown(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false)
        setConnecting(null)
      }
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open])

  if (settings.isPending || settings.isError || !data) {
    return (
      <span className="px-2 text-xs text-muted-foreground">
        {settings.isError ? 'Models unavailable' : 'Loading models…'}
      </span>
    )
  }
  const current = data

  function openPicker() {
    // Start on the model in use, so Enter alone keeps it.
    const all = buildGroups(current, '').flatMap((group) => group.options)
    const selected = all.findIndex((option) => isSelected(current, option))
    setQuery('')
    setActiveIndex(Math.max(selected, 0))
    setConnecting(null)
    select.reset()
    setOpen(true)
  }

  function close(restoreFocus: boolean) {
    setOpen(false)
    setConnecting(null)
    if (restoreFocus) triggerRef.current?.focus()
  }

  function move(index: number) {
    setActiveIndex(index)
    document.getElementById(optionId(index))?.scrollIntoView?.({ block: 'nearest' })
  }

  function choose(option: PickerOption) {
    if (!option.connected) {
      const provider = current.providers.find((candidate) => candidate.id === option.provider)
      if (provider) setConnecting({ provider, model: option.model })
      return
    }
    const label = option.provider === null ? `${option.model}, the app default` : option.model
    select.mutate(
      { provider: option.provider, model: option.provider === null ? null : option.model },
      {
        onSuccess: () => {
          setAnnouncement(`Answers will now use ${label}.`)
          close(true)
        },
      },
    )
  }

  function onSearchKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (options.length === 0) return
      const step = event.key === 'ArrowDown' ? 1 : -1
      move((active + step + options.length) % options.length)
    } else if (event.key === 'Enter') {
      // Never the composer's submit: this Enter belongs to the list.
      event.preventDefault()
      const option = options[active]
      if (option) choose(option)
    } else if (event.key === 'Tab') {
      close(false)
    }
  }

  function onPanelKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'Escape') return
    event.preventDefault()
    event.stopPropagation()
    if (connecting) setConnecting(null)
    else close(true)
  }

  function onTriggerKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (!open && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault()
      openPicker()
    }
  }

  const text = triggerText(current)
  const noModel = current.selection === null
  // Each group's first index in the flat list the keyboard moves through.
  const starts = groups.map((_, index) =>
    groups.slice(0, index).reduce((count, group) => count + group.options.length, 0),
  )

  return (
    <div ref={rootRef} className="relative min-w-0">
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Model: ${text.model} (${text.detail}). Change model`}
        title={`${text.detail} · ${text.model}`}
        onClick={() => (open ? close(false) : openPicker())}
        onKeyDown={onTriggerKeyDown}
        className={cn(
          'flex h-8 max-w-56 min-w-0 items-center gap-1.5 rounded-lg px-2 text-xs font-medium',
          'transition-colors duration-150 motion-reduce:transition-none',
          'focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
          noModel
            ? 'bg-primary/10 text-primary hover:bg-primary/15'
            : 'text-muted-foreground hover:bg-accent hover:text-foreground',
          open && !noModel && 'bg-accent text-foreground',
        )}
      >
        {noModel ? <KeyRound className="size-3.5 shrink-0" aria-hidden /> : null}
        <span className="truncate">{text.model}</span>
        {current.selection?.source === 'server' ? (
          <span className="hidden shrink-0 rounded bg-muted px-1 py-px text-[10px] font-normal sm:inline">
            Default
          </span>
        ) : null}
        <ChevronDown
          className={cn('size-3.5 shrink-0 transition-transform', open && 'rotate-180')}
          aria-hidden
        />
      </button>

      {open ? (
        <div
          onKeyDown={onPanelKeyDown}
          className={cn(
            'animate-fade absolute left-0 z-30 w-[min(22rem,calc(100vw-2.5rem))] overflow-hidden',
            'rounded-2xl border border-border bg-card shadow-xl',
            placement === 'up' ? 'bottom-full mb-2' : 'top-full mt-2',
          )}
        >
          {connecting ? (
            <div className="p-4">
              <ConnectKeyForm
                provider={connecting.provider}
                model={connecting.model}
                onSaved={() => {
                  setAnnouncement(`Answers will now use ${connecting.model}.`)
                  close(true)
                }}
                onCancel={() => setConnecting(null)}
                cancelLabel="Back"
              />
            </div>
          ) : (
            <>
              <div className="flex items-center gap-2 border-b border-border px-3">
                <Search className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                <input
                  role="combobox"
                  aria-expanded
                  aria-controls={listId}
                  aria-autocomplete="list"
                  aria-activedescendant={active >= 0 ? optionId(active) : undefined}
                  aria-label="Search models"
                  placeholder="Search models or type a model name…"
                  // Opening the picker is asking to choose; start in the search.
                  autoFocus
                  autoComplete="off"
                  spellCheck={false}
                  value={query}
                  onChange={(event) => {
                    setQuery(event.target.value)
                    setActiveIndex(0)
                  }}
                  onKeyDown={onSearchKeyDown}
                  className="h-11 min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground/80"
                />
              </div>

              <ul
                id={listId}
                role="listbox"
                aria-label="Models"
                className="max-h-72 overflow-y-auto overscroll-contain p-1.5"
              >
                {groups.map((group, groupIndex) => {
                  const start = starts[groupIndex] ?? 0
                  const headingId = `${baseId}-group-${group.id}`
                  return (
                    <li key={group.id} role="presentation" className="pb-1">
                      <div
                        id={headingId}
                        className="flex items-baseline justify-between gap-2 px-2 pt-2 pb-1"
                      >
                        <span className="text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">
                          {group.label}
                        </span>
                        <span className="text-[11px] text-muted-foreground/80">{group.hint}</span>
                      </div>
                      <ul role="group" aria-labelledby={headingId}>
                        {group.options.map((option, position) => {
                          const index = start + position
                          const selected = isSelected(current, option)
                          return (
                            <li
                              key={`${option.provider ?? 'default'}:${option.model}:${option.custom}`}
                              id={optionId(index)}
                              role="option"
                              aria-selected={selected}
                              aria-disabled={select.isPending || undefined}
                              // Keep focus in the search box while clicking.
                              onMouseDown={(event) => event.preventDefault()}
                              onMouseMove={() => index !== active && setActiveIndex(index)}
                              onClick={() => choose(option)}
                              className={cn(
                                'flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm',
                                index === active && 'bg-accent text-accent-foreground',
                                !option.connected && 'text-muted-foreground',
                              )}
                            >
                              <span className="min-w-0 flex-1 truncate">
                                {option.custom ? `Use “${option.model}”` : option.model}
                              </span>
                              {selected ? (
                                <Check className="size-4 shrink-0 text-primary" aria-hidden />
                              ) : null}
                              {!option.connected ? (
                                <span className="flex shrink-0 items-center gap-1 text-[11px]">
                                  <Lock className="size-3" aria-hidden />
                                  Add key
                                </span>
                              ) : null}
                            </li>
                          )
                        })}
                      </ul>
                    </li>
                  )
                })}
              </ul>
              {options.length === 0 ? (
                <p className="px-4 pb-4 text-sm text-muted-foreground">
                  No models match “{query.trim()}”.
                </p>
              ) : null}

              {select.isError ? (
                <div className="px-3 pb-2">
                  <Alert>{describeKeyError(select.error)}</Alert>
                </div>
              ) : null}

              <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-2 text-xs text-muted-foreground">
                <span>↑↓ to move · Enter to choose · Esc to close</span>
                <Link
                  to={AI_PROVIDER_PATH}
                  onClick={() => close(false)}
                  className="flex items-center gap-1 rounded font-medium hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                >
                  <Settings2 className="size-3.5" aria-hidden />
                  Manage keys
                </Link>
              </div>
            </>
          )}
        </div>
      ) : null}

      {/* Always mounted, so the change of model is announced when it happens.
          A live region, not role="status": the page's status region belongs
          to the answer's progress. */}
      <span aria-live="polite" className="sr-only">
        {announcement}
      </span>
    </div>
  )
}
