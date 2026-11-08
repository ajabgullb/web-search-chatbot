/**
 * `/settings/ai-clients`: connect MCP clients to this account (ADR-0019).
 *
 * One personal access token per client, so each can be revoked on its own.
 * A new token is shown exactly once, next to the configuration it goes in;
 * after that the list shows only its first characters.
 */

import { KeyRound, Plug } from 'lucide-react'
import { useId, useState } from 'react'
import type { FormEvent } from 'react'

import { ApiError } from '@/api/errors'
import type { AccessToken, AccessTokenCreated, TokenScope } from '@/api/types'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Field } from '@/components/ui/Field'
import { CopyButton } from '@/features/chat/CopyButton'
import {
  useAccessTokens,
  useCreateAccessToken,
  useRevokeAccessToken,
} from '@/features/aiClients/queries'
import {
  LIFETIMES,
  SCOPES,
  claudeCodeCommand,
  jsonConfig,
  mcpUrl,
} from '@/features/aiClients/connection'
import { cn } from '@/lib/utils'

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' })

function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return 'Something went wrong. Try again.'
}

export function AiClientsPage() {
  const [created, setCreated] = useState<AccessTokenCreated | null>(null)

  return (
    <main className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-2xl space-y-10 px-4 py-8 sm:px-6 sm:py-12">
        <header className="space-y-2">
          <h1 className="flex items-center gap-2.5 text-2xl font-semibold tracking-tight">
            <Plug className="size-5 text-muted-foreground" aria-hidden />
            AI clients
          </h1>
          <p className="text-sm text-muted-foreground">
            Use this assistant from Claude Code, Cursor, or any MCP client that can connect to a
            remote server. Give each client its own access token, so you can revoke one without
            touching the others. Tokens work only for MCP, never for signing in here.
          </p>
        </header>

        {created ? (
          <NewToken token={created} onDone={() => setCreated(null)} />
        ) : (
          <CreateTokenForm onCreated={setCreated} />
        )}

        <TokenList />
      </div>
    </main>
  )
}

function CreateTokenForm({ onCreated }: { onCreated: (token: AccessTokenCreated) => void }) {
  const create = useCreateAccessToken()
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<TokenScope[]>(['conversations:read', 'runs:create'])
  const [days, setDays] = useState<number>(30)
  const [nameError, setNameError] = useState<string>()
  const headingId = useId()
  const lifetimeId = useId()

  function toggle(scope: TokenScope, on: boolean) {
    setScopes((current) => (on ? [...current, scope] : current.filter((s) => s !== scope)))
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!name.trim()) {
      setNameError('Give the token a name, such as the client it is for.')
      return
    }
    setNameError(undefined)
    create.mutate(
      { name: name.trim(), scopes, expires_in_days: days },
      {
        onSuccess: (token) => {
          setName('')
          onCreated(token)
        },
      },
    )
  }

  return (
    <section aria-labelledby={headingId} className="rounded-2xl border border-border bg-card p-5">
      <h2 id={headingId} className="mb-4 text-base font-semibold">
        Create a token
      </h2>
      <form className="space-y-5" onSubmit={submit} noValidate>
        <Field
          label="Name"
          placeholder="Claude Code on my laptop"
          maxLength={100}
          value={name}
          onChange={(event) => setName(event.target.value)}
          error={nameError}
        />

        <fieldset className="space-y-3">
          <legend className="mb-2 text-sm font-medium">Allow it to</legend>
          {SCOPES.map((scope) => (
            <label key={scope.value} className="flex items-start gap-3 text-sm">
              <input
                type="checkbox"
                className="mt-0.5 size-4 accent-foreground"
                checked={scopes.includes(scope.value)}
                onChange={(event) => toggle(scope.value, event.target.checked)}
              />
              <span>
                <span className="block font-medium">{scope.label}</span>
                <span className="block text-xs text-muted-foreground">{scope.hint}</span>
              </span>
            </label>
          ))}
        </fieldset>

        <div className="space-y-1.5">
          <label htmlFor={lifetimeId} className="block text-sm font-medium">
            Expires after
          </label>
          <select
            id={lifetimeId}
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
            className="h-11 rounded-xl border border-input bg-card px-3 text-sm shadow-xs focus-visible:border-ring focus-visible:ring-4 focus-visible:ring-ring/20 focus-visible:outline-none"
          >
            {LIFETIMES.map((value) => (
              <option key={value} value={value}>
                {value} days
              </option>
            ))}
          </select>
        </div>

        {create.isError ? <Alert>{describeError(create.error)}</Alert> : null}

        <Button type="submit" variant="ink" disabled={scopes.length === 0 || create.isPending}>
          <KeyRound className="size-4" aria-hidden />
          {create.isPending ? 'Creating…' : 'Create token'}
        </Button>
        {scopes.length === 0 ? (
          <p className="text-xs text-muted-foreground">Choose at least one permission.</p>
        ) : null}
      </form>
    </section>
  )
}

function Snippet({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium">{label}</p>
        <CopyButton value={value} label={`Copy ${label.toLowerCase()}`} />
      </div>
      <pre className="overflow-x-auto rounded-xl border border-border bg-muted/60 p-3 text-xs leading-relaxed">
        <code>{value}</code>
      </pre>
    </div>
  )
}

function NewToken({ token, onDone }: { token: AccessTokenCreated; onDone: () => void }) {
  const url = mcpUrl()
  const headingId = useId()

  return (
    <section
      aria-labelledby={headingId}
      className="space-y-5 rounded-2xl border border-border bg-card p-5"
    >
      <h2 id={headingId} className="text-base font-semibold">
        Connect {token.name}
      </h2>
      <Alert variant="success" title="Token created">
        Copy it now. It will not be shown again, and anyone who has it can use your account through
        MCP until it expires or you revoke it.
      </Alert>

      <Snippet label="Token" value={token.token} />
      <Snippet label="Server URL" value={url} />
      <Snippet label="Claude Code command" value={claudeCodeCommand(url, token.token)} />
      <Snippet label="JSON configuration" value={jsonConfig(url, token.token)} />

      <Button variant="outline" onClick={onDone}>
        Done
      </Button>
    </section>
  )
}

function TokenList() {
  const tokens = useAccessTokens()
  const headingId = useId()
  // Read once: "expired" only needs to be right as of when the page opened.
  const [now] = useState(() => Date.now())
  const items = tokens.data?.items ?? []

  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <h2 id={headingId} className="text-base font-semibold">
        Your tokens
      </h2>
      {tokens.isPending ? <p className="text-sm text-muted-foreground">Loading tokens…</p> : null}
      {tokens.isError ? <Alert>Could not load your tokens.</Alert> : null}
      {tokens.isSuccess && items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No tokens yet.</p>
      ) : null}
      <ul className="space-y-2">
        {items.map((token) => (
          <TokenRow key={token.id} token={token} now={now} />
        ))}
      </ul>
    </section>
  )
}

function TokenRow({ token, now }: { token: AccessToken; now: number }) {
  const revoke = useRevokeAccessToken()
  const [confirming, setConfirming] = useState(false)
  const expired = new Date(token.expires_at).getTime() <= now
  const scopeLabels = token.scopes.map(
    (scope) => SCOPES.find((known) => known.value === scope)?.label ?? scope,
  )

  return (
    <li
      className={cn(
        'rounded-xl border px-4 py-3 text-sm',
        confirming ? 'border-destructive/25 bg-destructive/6' : 'border-border bg-card',
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <p className="font-medium">
            {token.name}{' '}
            <code className="ml-1 rounded bg-muted px-1.5 py-0.5 text-xs font-normal">
              {token.token_prefix}…
            </code>
          </p>
          <p className="text-xs text-muted-foreground">{scopeLabels.join(' · ')}</p>
          <p className="text-xs text-muted-foreground">
            {expired ? (
              <span className="font-medium text-destructive">Expired</span>
            ) : (
              <>Expires {dateFormat.format(new Date(token.expires_at))}</>
            )}
            {' · '}
            {token.last_used_at
              ? `Last used ${dateFormat.format(new Date(token.last_used_at))}`
              : 'Never used'}
          </p>
        </div>
        {confirming ? (
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              size="sm"
              className="bg-destructive text-destructive-foreground shadow-none hover:bg-destructive/90"
              aria-label={`Confirm revoke ${token.name}`}
              disabled={revoke.isPending}
              onClick={() => revoke.mutate(token.id)}
            >
              Revoke
            </Button>
          </div>
        ) : (
          <Button
            size="sm"
            variant="outline"
            aria-label={`Revoke ${token.name}`}
            onClick={() => setConfirming(true)}
          >
            Revoke
          </Button>
        )}
      </div>
      {confirming ? (
        <p className="mt-2 text-xs">Clients using this token stop working at once.</p>
      ) : null}
      {revoke.isError ? <Alert className="mt-2">{describeError(revoke.error)}</Alert> : null}
    </li>
  )
}
