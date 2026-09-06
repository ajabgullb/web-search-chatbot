# 6. Rendering assistant output as Markdown

Date: 2026-09-06

## Status

Accepted.

## Context

`AGENTS.md` §9 requires assistant messages to render as sanitized Markdown with
syntax-highlighted code, and states that unsanitized HTML must never be
injected. From Phase 5 that content will paraphrase pages fetched from the open
web, so it is untrusted input in the strongest sense — §6 says as much.

## Decision

Render with `react-markdown` plus `remark-gfm`, `rehype-sanitize`, and
`rehype-highlight`. Four dependencies, justified because the requirement is
explicit in the contract rather than invented here.

The safety argument is layered:

1. `react-markdown` does not render raw HTML unless `rehype-raw` is added. It is
   not added, and must not be. `<img onerror=…>` in an answer is text.
2. `rehype-sanitize` runs on the default hast schema anyway. That is redundant
   today; it is the thing that still holds if someone later adds a plugin that
   reintroduces HTML nodes.
3. Link hrefs are re-checked for `http`/`https` in application code and given
   `rel="noopener noreferrer"`. Sanitize already drops `javascript:`, but §6
   states the rule outright, so it is enforced somewhere a reader can find it.

There is no `dangerouslySetInnerHTML` in the codebase.

Highlighting colours are defined against the app's own theme tokens instead of
importing a highlight.js stylesheet, so light and dark come from one definition
and no external CSS has to be kept in step with the palette.

The renderer is a `React.lazy` chunk. The parser and highlighter together are
the largest thing the app ships (~327 kB), and they are not needed to render the
landing or sign-in pages — which is all most first-time visitors ever load. The
Suspense fallback is the same text unformatted: readable immediately, and plain
text cannot be unsafe.

## Consequences

- User messages are *not* rendered as Markdown. Someone who types `**hello**`
  meant asterisks.
- The highlighter ships `lowlight`'s common language set. Trimming it is a
  Phase 8 concern if bundle size becomes a real constraint.
- Citation rendering in Phase 7 extends this component rather than replacing it.
