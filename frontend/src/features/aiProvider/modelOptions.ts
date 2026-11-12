/**
 * What the model picker lists, for a given search. Pure, so it is tested
 * without rendering anything.
 */

import type { LlmSettings } from '@/api/types'

export interface PickerOption {
  /** null is the app's default model. */
  provider: string | null
  model: string
  /** Whether a key is saved for the provider; choosing one without asks for it. */
  connected: boolean
  /** A model name the user typed that is not among the suggestions. */
  custom: boolean
}

export interface PickerGroup {
  id: string
  label: string
  /** "Key ending wxyz", or a prompt to add one. */
  hint: string
  options: PickerOption[]
}

// The server's pattern for model names (app/providers/llm_catalog.py).
const MODEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$/

export function isSelected(settings: LlmSettings, option: PickerOption): boolean {
  const selection = settings.selection
  if (!selection) return false
  if (option.provider === null) return selection.source === 'server'
  return (
    selection.source === 'user' &&
    selection.provider === option.provider &&
    selection.model === option.model
  )
}

export function buildGroups(settings: LlmSettings, rawQuery: string): PickerGroup[] {
  const query = rawQuery.trim()
  const needle = query.toLowerCase()
  const groups: PickerGroup[] = []

  if (settings.server_model) {
    const option: PickerOption = {
      provider: null,
      model: settings.server_model.model,
      connected: true,
      custom: false,
    }
    if (!needle || `default ${option.model}`.toLowerCase().includes(needle)) {
      groups.push({ id: 'default', label: 'App default', hint: 'No key needed', options: [option] })
    }
  }

  const providerGroups = settings.providers.map((provider) => {
    const credential = settings.credentials.find((item) => item.provider === provider.id)
    // The model last used with this key first, so it is one keystroke away.
    const models = [...new Set([credential?.model, ...provider.suggested_models])].filter(
      (model): model is string => Boolean(model),
    )
    const providerMatches = provider.label.toLowerCase().includes(needle)
    const options: PickerOption[] = models
      .filter((model) => !needle || providerMatches || model.toLowerCase().includes(needle))
      .map((model) => ({
        provider: provider.id,
        model,
        connected: credential !== undefined,
        custom: false,
      }))
    return {
      id: provider.id,
      label: provider.label,
      hint: credential ? `Key ending ${credential.key_hint}` : 'Add a key to use',
      options,
    }
  })

  // Any model the account can use, not only the suggestions: when the search
  // matches nothing listed, offer it as a model name under each provider
  // with a key. Offered only then, so searching never clutters the list.
  const matchedSomething = [...groups, ...providerGroups].some((group) => group.options.length > 0)
  if (query && MODEL_PATTERN.test(query) && !matchedSomething) {
    for (const group of providerGroups) {
      if (settings.credentials.some((item) => item.provider === group.id)) {
        group.options.push({ provider: group.id, model: query, connected: true, custom: true })
      }
    }
  }

  groups.push(...providerGroups.filter((group) => group.options.length > 0))
  return groups
}
