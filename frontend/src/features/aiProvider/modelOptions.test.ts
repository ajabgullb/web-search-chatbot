import { describe, expect, it } from 'vitest'

import type { LlmSettings } from '@/api/types'
import { buildGroups, isSelected } from '@/features/aiProvider/modelOptions'
import { defaultLlmSettings } from '@/test/utils'

const openAiKey = {
  provider: 'openai',
  model: 'gpt-4o',
  key_hint: 'wxyz',
  is_active: true,
  verified_at: '2026-11-10T10:00:00Z',
  updated_at: '2026-11-10T10:00:00Z',
}

function settings(overrides: Partial<LlmSettings> = {}): LlmSettings {
  return { ...(defaultLlmSettings as LlmSettings), ...overrides }
}

const withOpenAi = settings({
  credentials: [openAiKey],
  selection: { source: 'user', provider: 'openai', model: 'gpt-4o' },
})

describe('buildGroups', () => {
  it('lists the default first, then every provider with a hint about its key', () => {
    const groups = buildGroups(withOpenAi, '')
    expect(groups.map((group) => [group.label, group.hint])).toEqual([
      ['App default', 'No key needed'],
      ['OpenAI', 'Key ending wxyz'],
      ['Anthropic', 'Add a key to use'],
    ])
  })

  it('puts the model last used with a key first, without listing it twice', () => {
    const openAi = buildGroups(withOpenAi, '').find((group) => group.id === 'openai')
    expect(openAi?.options.map((option) => option.model)).toEqual(['gpt-4o', 'gpt-4.1-mini'])
  })

  it('has no default group when the server has no model', () => {
    const groups = buildGroups(settings({ server_model: null, selection: null }), '')
    expect(groups.map((group) => group.id)).toEqual(['openai', 'anthropic'])
  })

  it('searches model names and provider names', () => {
    expect(buildGroups(withOpenAi, 'haiku').flatMap((g) => g.options.map((o) => o.model))).toEqual([
      'claude-haiku-5-5',
    ])
    const anthropic = buildGroups(withOpenAi, 'anthro')
    expect(anthropic.map((group) => group.id)).toEqual(['anthropic'])
    expect(anthropic[0]?.options).toHaveLength(2)
  })

  it('offers a typed model name, when nothing listed matches, under providers with a key', () => {
    const groups = buildGroups(withOpenAi, 'o3-mini')
    const custom = groups.flatMap((group) => group.options).filter((option) => option.custom)
    expect(custom).toEqual([
      { provider: 'openai', model: 'o3-mini', connected: true, custom: true },
    ])
  })

  it('offers no typed model that matches a listed one or is not a model name', () => {
    const typed = (query: string) =>
      buildGroups(withOpenAi, query)
        .flatMap((group) => group.options)
        .filter((option) => option.custom)
    expect(typed('gpt-4o')).toEqual([])
    expect(typed('haiku')).toEqual([]) // matches a listed Claude model
    expect(typed('not a model')).toEqual([])
  })
})

describe('isSelected', () => {
  it('marks the default or the exact provider and model in use', () => {
    const defaultOption = { provider: null, model: 'gpt-4o-mini', connected: true, custom: false }
    const gpt4o = { provider: 'openai', model: 'gpt-4o', connected: true, custom: false }
    expect(isSelected(settings(), defaultOption)).toBe(true)
    expect(isSelected(settings(), gpt4o)).toBe(false)
    expect(isSelected(withOpenAi, gpt4o)).toBe(true)
    expect(isSelected(withOpenAi, { ...gpt4o, model: 'gpt-4.1-mini' })).toBe(false)
  })
})
