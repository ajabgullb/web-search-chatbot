/**
 * Mirrors `Settings.max_user_message_chars` (MAX_USER_MESSAGE_CHARS) on the
 * backend.
 *
 * The limit is enforced server-side; this copy exists so the composer can show
 * a live counter instead of letting someone type 20,000 characters and only
 * then discover the message was rejected. A backend test asserts the two stay
 * equal, because a silently drifting client-side limit is worse than none.
 */
export const MAX_MESSAGE_CHARACTERS = 16_000

/**
 * Mirrors `Settings.max_document_bytes` (MAX_DOCUMENT_BYTES): 10 MiB.
 *
 * Same reasoning as the character limit: checked here so a 40 MB scan is
 * refused before it is uploaded, not after. A backend test pins the two.
 */
export const MAX_DOCUMENT_BYTES = 10_485_760

/** Show the counter only when it starts to matter. */
export const COUNTER_VISIBLE_FROM = MAX_MESSAGE_CHARACTERS - 2_000
