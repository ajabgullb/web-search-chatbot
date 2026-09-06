/**
 * Convenience aliases over the generated OpenAPI schema.
 *
 * Application code imports from here rather than reaching into
 * `generated/schema.d.ts` directly, so a regeneration that renames an internal
 * operation id breaks one file instead of twenty.
 */

import type { components } from '@/api/generated/schema'

type Schemas = components['schemas']

export type User = Schemas['UserRead']
export type TokenResponse = Schemas['TokenResponse']
export type RegisterRequest = Schemas['RegisterRequest']
export type LoginRequest = Schemas['LoginRequest']
export type ForgotPasswordRequest = Schemas['ForgotPasswordRequest']
export type ResetPasswordRequest = Schemas['ResetPasswordRequest']
export type MessageResponse = Schemas['MessageResponse']
export type ErrorBody = Schemas['ErrorBody']

export type Conversation = Schemas['ConversationRead']
export type ConversationCreate = Schemas['ConversationCreate']
export type ConversationUpdate = Schemas['ConversationUpdate']
export type ChatMessage = Schemas['MessageRead']
export type ConversationPage = Schemas['Page_ConversationRead_']
export type MessagePage = Schemas['Page_MessageRead_']
