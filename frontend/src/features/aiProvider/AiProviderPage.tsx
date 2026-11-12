/**
 * `/settings/ai-provider`: manage the API keys behind the model picker (ADR-0020).
 *
 * Day to day, a user switches models from the picker in the chat box. This
 * page is where keys are added, replaced, and removed: one per provider.
 * A key is never shown again; only its last four characters are.
 */

import { Sparkles } from 'lucide-react'
import { useId, useState } from 'react'

import type { LlmCredential, LlmProvider, LlmSettings } from '@/api/types'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { ConnectKeyForm } from '@/features/aiProvider/ConnectKeyForm'
import {
  describeKeyError,
  providerLabel,
  useDeleteLlmKey,
  useLlmSettings,
  useSelectLlm,
} from '@/features/aiProvider/queries'
import { cn } from '@/lib/utils'

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' })

export function AiProviderPage() {
  const settings = useLlmSettings()

  return (
    <main className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-2xl space-y-8 px-4 py-8 sm:px-6 sm:py-12">
        <header className="space-y-2">
          <h1 className="flex items-center gap-2.5 text-2xl font-semibold tracking-tight">
            <Sparkles className="size-5 text-muted-foreground" aria-hidden />
            AI provider
          </h1>
          <p className="text-sm text-muted-foreground">
            Answer with your own API keys from OpenAI, Anthropic, Google, and others. Usage is
            billed to your account with each provider. Switch models from the picker in the chat
            box; add and remove keys here. Keys are stored encrypted and never shown again.
          </p>
        </header>

        {settings.isPending ? (
          <p className="text-sm text-muted-foreground">Loading your settings…</p>
        ) : null}
        {settings.isError ? <Alert>Could not load your AI provider settings.</Alert> : null}
        {settings.data ? (
          <>
            <CurrentModel settings={settings.data} />
            <KeyList settings={settings.data} />
          </>
        ) : null}
      </div>
    </main>
  )
}

function CurrentModel({ settings }: { settings: LlmSettings }) {
  const selection = settings.selection
  return (
    <section aria-label="Answering with">
      {selection === null ? (
        <Alert variant="info" title="No model yet">
          This app has no default model. Connect a provider below to start chatting.
        </Alert>
      ) : (
        <p className="rounded-xl border border-border bg-card px-4 py-3 text-sm">
          Answering with <span className="font-medium">{selection.model}</span>
          {selection.source === 'server'
            ? ', the app’s default model.'
            : ` from ${providerLabel(settings, selection.provider)}.`}
        </p>
      )}
    </section>
  )
}

function KeyList({ settings }: { settings: LlmSettings }) {
  const headingId = useId()
  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <h2 id={headingId} className="text-base font-semibold">
        Providers
      </h2>
      <ul className="space-y-2">
        {settings.providers.map((provider) => (
          <ProviderRow
            key={provider.id}
            settings={settings}
            provider={provider}
            credential={settings.credentials.find((item) => item.provider === provider.id)}
          />
        ))}
      </ul>
    </section>
  )
}

function ProviderRow({
  settings,
  provider,
  credential,
}: {
  settings: LlmSettings
  provider: LlmProvider
  credential: LlmCredential | undefined
}) {
  const remove = useDeleteLlmKey()
  const use = useSelectLlm()
  const [mode, setMode] = useState<'view' | 'connect' | 'confirm-remove'>('view')
  const active =
    credential !== undefined &&
    settings.selection?.source === 'user' &&
    settings.selection.provider === provider.id

  return (
    <li
      className={cn(
        'rounded-xl border px-4 py-3 text-sm',
        mode === 'confirm-remove'
          ? 'border-destructive/25 bg-destructive/6'
          : 'border-border bg-card',
      )}
    >
      {mode === 'connect' ? (
        <ConnectKeyForm
          provider={provider}
          model={credential?.model ?? provider.default_model}
          editableModel
          replacing={credential !== undefined}
          onSaved={() => setMode('view')}
          onCancel={() => setMode('view')}
        />
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <p className="flex items-center gap-2 font-medium">
              {provider.label}
              {active ? (
                <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
                  In use
                </span>
              ) : null}
            </p>
            <p className="text-xs text-muted-foreground">
              {credential ? (
                <>
                  {credential.model} · Key ending{' '}
                  <code className="rounded bg-muted px-1.5 py-0.5">{credential.key_hint}</code> ·
                  Checked {dateFormat.format(new Date(credential.verified_at))}
                </>
              ) : (
                'Not connected'
              )}
            </p>
          </div>

          {mode === 'confirm-remove' ? (
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => setMode('view')}>
                Cancel
              </Button>
              <Button
                size="sm"
                className="bg-destructive text-destructive-foreground shadow-none hover:bg-destructive/90"
                aria-label={`Confirm remove ${provider.label} key`}
                disabled={remove.isPending}
                onClick={() => remove.mutate(provider.id, { onSuccess: () => setMode('view') })}
              >
                Remove
              </Button>
            </div>
          ) : credential ? (
            <div className="flex flex-wrap gap-2">
              {active ? null : (
                <Button
                  size="sm"
                  variant="outline"
                  aria-label={`Use ${provider.label}`}
                  disabled={use.isPending}
                  onClick={() => use.mutate({ provider: provider.id, model: credential.model })}
                >
                  Use
                </Button>
              )}
              <Button
                size="sm"
                variant="outline"
                aria-label={`Replace ${provider.label} key`}
                onClick={() => setMode('connect')}
              >
                Replace key
              </Button>
              <Button
                size="sm"
                variant="outline"
                aria-label={`Remove ${provider.label} key`}
                onClick={() => setMode('confirm-remove')}
              >
                Remove
              </Button>
            </div>
          ) : (
            <Button
              size="sm"
              variant="outline"
              aria-label={`Connect ${provider.label}`}
              onClick={() => setMode('connect')}
            >
              Connect
            </Button>
          )}
        </div>
      )}
      {mode === 'confirm-remove' ? (
        <p className="mt-2 text-xs">
          {active
            ? settings.server_model
              ? `New answers will use the app's default model, ${settings.server_model.model}.`
              : 'New answers will use another of your keys, if you have one.'
            : 'The key is deleted from this app. It stays valid with the provider.'}
        </p>
      ) : null}
      {remove.isError ? <Alert className="mt-2">{describeKeyError(remove.error)}</Alert> : null}
      {use.isError ? <Alert className="mt-2">{describeKeyError(use.error)}</Alert> : null}
    </li>
  )
}
