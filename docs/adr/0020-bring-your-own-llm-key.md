# 20. Users bring their own LLM API key

Date: 2026-11-11

## Status

Accepted.

## Context

Every answer was paid for by one operator key, `LLM_API_KEY`, and came from
one model. The product owner asked that a user be able to paste the API key of
an LLM provider they already pay for and use the app with it.

This touches security (the server must hold a secret it can read back),
cost (who pays for a run), and architecture (the model is no longer one
object built at startup). AGENTS.md §16 asks for a decision on each. The
owner chose four answers:

1. **Storage: encrypted in the database.** A key that lived only in the
   browser would have to be pasted again after every reload. MCP clients
   (ADR-0019), which never see the browser, could not use it at all.
2. **Providers: a fixed list, not a free-form URL.** The server sends the key
   to the provider's URL. If the user chose the URL, the API could be made to
   call any address, including internal services and cloud metadata
   endpoints: server-side request forgery. A blocklist of private ranges would
   also have to handle DNS rebinding and redirects, and still be a list of
   ways to get it wrong.
3. **The server key becomes a fallback.** A user with no key of their own
   uses `LLM_API_KEY` if the operator set one. If it is empty, there is no
   server model, and production no longer refuses to start without it.
4. **Chat model only.** Web search (Tavily) and PDF embeddings stay on the
   operator's keys. Embeddings in particular cannot follow a user's choice:
   stored vectors only compare with vectors from the same model.

## Decision

- **One key per provider per user**, in a new `llm_credentials` table: the
  preset id, the model last chosen for it, the encrypted key, its last four
  characters (`key_hint`), `is_active`, and when it was checked. Rows are
  deleted with the user (cascade).
  - At most one row per user is active: the model their answers come from.
    A partial unique index (`WHERE is_active`) enforces this in the
    database, however two requests interleave.
  - The first design kept a single key per user. It changed before release,
    once the model picker made switching providers a one-click action:
    re-pasting a key on every switch defeats the picker.
- **AES-256-GCM, under a new `CREDENTIAL_ENCRYPTION_KEY`** (`app/core/crypto.py`).
  - The 256-bit key is derived with HKDF, so the setting is any long random
    string.
  - The secret is deliberately separate from `JWT_SECRET`, so rotating the
    signing key (which signs everyone out) does not destroy saved keys.
  - Each ciphertext is bound to its owner's user id as associated data. A
    ciphertext copied onto another user's row fails to decrypt instead of
    handing that user someone else's key.
  - A version byte leads each ciphertext, leaving room to rotate the
    encryption key later.
  - Production refuses the development default, values shorter than 32
    characters, and a value equal to `JWT_SECRET`.
- **The catalogue** (`app/providers/llm_catalog.py`): OpenAI, Anthropic,
  Google Gemini, Groq, OpenRouter, Mistral, DeepSeek, and xAI, each with a
  fixed HTTPS base URL that never leaves the server.
  - Anthropic uses `langchain-anthropic`. Anthropic documents its
    OpenAI-compatibility layer as intended for testing, not production.
  - The rest use the existing OpenAI-compatible client.
  - The routing step's structured output uses tool calling everywhere
    except OpenAI, which keeps strict `json_schema`. Many compatible APIs do
    not support `json_schema`, or support it only for some models.
  - The model is free text, checked against a conservative pattern. The
    suggestions per preset are hints only: model names change faster than
    this file.
- **Saving checks the key.** `PUT /users/me/llm-credentials/{provider}` streams one
  16-token reply ("Reply with the single word: OK") from the chosen provider
  and model.
  - Outcomes: a 401/403 is `llm_key_invalid`; another refusal (usually a
    wrong model name) is `llm_model_rejected`; a timeout, 5xx, or 429 is
    `llm_key_check_unavailable`. Nothing is saved unless the call succeeds.
  - The check runs outside any database transaction.
  - It is limited to 10 per user per hour (`RATE_LIMIT_LLM_KEY_CHECKS`), so
    the endpoint cannot be used to test stolen keys in bulk.
  - A saved key becomes the active one: adding a key is how a user says
    "answer with this".
  - A key is only ever sent to the provider it was saved for.
- **Switching models is instant.** `PUT /users/me/llm-selection` makes one
  saved key active with a model, or (both fields null) picks the server's
  model. It makes no provider call: the key was checked when saved. A model
  name the provider turns out not to have fails the next run with
  `llm_model_unavailable`, which tells the user to pick another.
- **Each run's model is chosen when the run is accepted**, by one rule
  (`choose` in `app/services/llm_credentials.py`). The settings response
  uses the same rule, so the picker always shows the model that will answer:
  1. the user's active key, with the model they picked for it;
  2. otherwise the server's model;
  3. otherwise the user's most recently checked key, so that removing the
     active key never strands a user who has another;
  4. otherwise the run is refused with `409 llm_key_required` before
     anything is stored.

  The REST route and the MCP `ask` tool resolve it the same way. The run
  records the preset id and model in `runs.provider` and `runs.model`.
- **The client is built per run** from the decrypted key. That costs no
  connection pool: both LangChain packages cache one HTTP client per base
  URL. The key is never placed in graph state, checkpoints, or traces.
- **No temperature is sent for a user's model.** `LLM_TEMPERATURE` was tuned
  for the server's model, and some models a user might choose accept only
  their default.
- **Failures the user can fix are worded for the user.** A user's key that
  the provider rejects mid-run raises `LLMKeyRejected`. The run fails with
  `llm_key_rejected`, "Your AI provider rejected your API key. Update it in
  AI provider settings.", instead of the operator-facing `llm_auth_failed`.
  A key that no longer decrypts (`CREDENTIAL_ENCRYPTION_KEY` changed) is
  `409 llm_key_unreadable`. It is not silently replaced by the server's
  model: the user chose their own and should learn that it stopped working.
- **Secrets stay out of logs and metrics.**
  - The key never appears in a response, a log line, or an exception chain:
    provider errors are re-raised `from None`.
  - Token metrics label a user's model as `user:<preset>`, because a free-text
    model name would be an unbounded Prometheus label.
  - `test_llm_credentials.py` checks the logs for the key.
- **Frontend: a model picker in the chat box.** The composer gains a toolbar
  under the text: attach, the model picker, and send. The picker:
  - shows the model that will answer (with a "Default" tag for the
    server's), or "Choose a model" in the accent colour when there is none;
  - opens a searchable list: the app default, then each provider's models,
    with the model last used for a saved key first;
  - offers a typed model name ("Use “o3-mini”") under providers with a key
    when the search matches nothing listed, so any model the account can
    use is reachable, not only the suggestions;
  - for a provider with no key, asks for the key inside the list, checks
    it, and switches to it;
  - is an ARIA combobox: arrow keys move, Enter chooses, Escape steps back
    from the key form and then closes, and focus returns to the button.
    Enter in the picker never sends the message;
  - announces a change of model through a live region.

  `/settings/ai-provider` (linked from the sidebar and the picker's "Manage
  keys") lists every provider to connect, replace, remove, or use. Key fields
  are password inputs with autocomplete off. Key-related run errors link to
  that page.

## Consequences

- An operator can run the app without paying for any answers. Search and
  embeddings still cost the operator, so the daily run allowance
  (ADR-0013) still applies to users with their own keys.
- The database now holds secrets that can be decrypted. Anyone who has a
  database dump **and** `CREDENTIAL_ENCRYPTION_KEY` has every saved key. Keep
  the two apart: the key belongs in the deployment's secret store, not in the
  backups. If the key is lost, every user must enter their key again; there
  is no recovery.
- Rotating `CREDENTIAL_ENCRYPTION_KEY` is not supported yet: changing it
  makes saved keys unreadable, and their users are asked for them again. The
  version byte leaves room for a rotation that tries the old key and
  re-encrypts.
- A key revoked after it was saved is found on the next run, not in advance.
  Nothing marks the stored credential as broken; the user sees
  `llm_key_rejected` until they replace or remove it.
- Reasoning models spend part of `LLM_MAX_OUTPUT_TOKENS` on hidden reasoning,
  so a user who picks one may get short or empty answers. The suggested
  models avoid them; the limit is still the server's to bound cost and
  latency.
- Adding a provider is a code change to the catalogue plus a test run. The
  catalogue is not stored in the database, so removing a preset leaves rows
  that no longer resolve. Those users fall back to the server's model, and a
  warning is logged.
