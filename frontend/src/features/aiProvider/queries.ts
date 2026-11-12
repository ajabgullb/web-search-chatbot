import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError } from '@/api/errors'
import type { LlmCredentialWrite, LlmSettings } from '@/api/types'
import * as providerApi from '@/features/aiProvider/api'

export const llmSettingsKeys = {
  all: ['llm-settings'] as const,
}

export const AI_PROVIDER_PATH = '/settings/ai-provider'

/** Run errors the user fixes by changing their key or model. */
export const LLM_KEY_ERROR_CODES: ReadonlySet<string> = new Set([
  'llm_key_required',
  'llm_key_rejected',
  'llm_key_unreadable',
  'llm_model_unavailable',
])

export function useLlmSettings() {
  return useQuery({ queryKey: llmSettingsKeys.all, queryFn: providerApi.getLlmSettings })
}

/** The server answers changes with the new settings; use them as they are. */
function useStoreSettings() {
  const queryClient = useQueryClient()
  return (settings: LlmSettings) => queryClient.setQueryData(llmSettingsKeys.all, settings)
}

export function useSaveLlmKey() {
  const store = useStoreSettings()
  return useMutation({
    mutationFn: ({ provider, ...body }: LlmCredentialWrite & { provider: string }) =>
      providerApi.saveLlmKey(provider, body),
    onSuccess: store,
  })
}

export function useSelectLlm() {
  const store = useStoreSettings()
  return useMutation({ mutationFn: providerApi.selectLlm, onSuccess: store })
}

export function useDeleteLlmKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: providerApi.deleteLlmKey,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: llmSettingsKeys.all }),
  })
}

export function describeKeyError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.is('rate_limited')) {
      return 'Too many keys checked recently. Wait a while, then try again.'
    }
    return error.message
  }
  return 'Something went wrong. Try again.'
}

export function providerLabel(settings: LlmSettings, id: string | null | undefined): string {
  return settings.providers.find((provider) => provider.id === id)?.label ?? id ?? ''
}
