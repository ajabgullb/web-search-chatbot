import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import * as tokensApi from '@/features/aiClients/api'

export const accessTokenKeys = {
  all: ['access-tokens'] as const,
}

export function useAccessTokens() {
  return useQuery({ queryKey: accessTokenKeys.all, queryFn: tokensApi.listAccessTokens })
}

export function useCreateAccessToken() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: tokensApi.createAccessToken,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: accessTokenKeys.all }),
  })
}

export function useRevokeAccessToken() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: tokensApi.revokeAccessToken,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: accessTokenKeys.all }),
  })
}
