/**
 * Paste a provider's API key, have the server check it, and switch to it.
 *
 * Used inside the composer's model picker and on the settings page. It is a
 * group, not a `<form>`: in the picker it sits inside the composer's form,
 * and nested forms are invalid HTML (Enter would submit the message). Enter
 * in the key field is handled here instead.
 */

import { ArrowLeft, KeyRound } from 'lucide-react'
import { useId, useState } from 'react'
import type { KeyboardEvent } from 'react'

import type { LlmProvider } from '@/api/types'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Field } from '@/components/ui/Field'
import { describeKeyError, useSaveLlmKey } from '@/features/aiProvider/queries'

interface ConnectKeyFormProps {
  provider: LlmProvider
  /** The model to check the key with, and switch to once it is saved. */
  model: string
  /** Show an editable model field; otherwise the model is fixed and shown. */
  editableModel?: boolean
  /** Whether a key is already saved for this provider (wording only). */
  replacing?: boolean
  onSaved: () => void
  onCancel: () => void
  cancelLabel?: string
}

export function ConnectKeyForm({
  provider,
  model: initialModel,
  editableModel = false,
  replacing = false,
  onSaved,
  onCancel,
  cancelLabel = 'Cancel',
}: ConnectKeyFormProps) {
  const save = useSaveLlmKey()
  const [model, setModel] = useState(initialModel)
  const [apiKey, setApiKey] = useState('')
  const [keyError, setKeyError] = useState<string>()
  const headingId = useId()
  const suggestionsId = useId()

  function submit() {
    const key = apiKey.trim()
    if (!key) {
      setKeyError(`Paste your ${provider.label} API key.`)
      return
    }
    setKeyError(undefined)
    save.mutate(
      { provider: provider.id, model: model.trim(), api_key: key },
      {
        // The key leaves the page's state as soon as the server has it.
        onSuccess: () => {
          setApiKey('')
          onSaved()
        },
      },
    )
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <div role="group" aria-labelledby={headingId} className="space-y-4">
      <div className="space-y-1">
        <p id={headingId} className="text-sm font-semibold">
          {replacing ? `Replace your ${provider.label} key` : `Connect ${provider.label}`}
        </p>
        {editableModel ? null : (
          <p className="text-xs text-muted-foreground">
            Answers will use <span className="font-medium text-foreground">{model}</span>.
          </p>
        )}
      </div>

      {editableModel ? (
        <>
          <Field
            label="Model"
            value={model}
            maxLength={128}
            list={suggestionsId}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => setModel(event.target.value)}
            onKeyDown={onKeyDown}
            hint="Pick a suggestion or type any model your account can use."
          />
          <datalist id={suggestionsId}>
            {provider.suggested_models.map((suggestion) => (
              <option key={suggestion} value={suggestion} />
            ))}
          </datalist>
        </>
      ) : null}

      <Field
        label="API key"
        type="password"
        value={apiKey}
        maxLength={512}
        // Not a site password: keep password managers from offering to save it.
        autoComplete="off"
        spellCheck={false}
        // Focused on arrival: pasting the key is the one thing this step is for.
        autoFocus
        onChange={(event) => setApiKey(event.target.value)}
        onKeyDown={onKeyDown}
        error={keyError}
        hint={
          <>
            Create one at{' '}
            <a
              href={provider.key_url}
              target="_blank"
              rel="noopener noreferrer"
              className="underline underline-offset-2 hover:text-foreground"
            >
              {provider.label}
            </a>
            . It is stored encrypted and never shown again.
          </>
        }
      />

      {save.isError ? <Alert>{describeKeyError(save.error)}</Alert> : null}

      <div className="flex items-center gap-2">
        <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
          <ArrowLeft className="size-3.5" aria-hidden />
          {cancelLabel}
        </Button>
        <Button
          type="button"
          variant="ink"
          size="sm"
          className="ml-auto"
          disabled={!model.trim() || save.isPending}
          onClick={submit}
        >
          <KeyRound className="size-3.5" aria-hidden />
          {save.isPending ? 'Checking key…' : 'Check and save'}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Saving sends one short test message to {provider.label} to check the key.
      </p>
    </div>
  )
}
