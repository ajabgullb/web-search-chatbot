# 1. Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-09-06

## Context

`AGENTS.md` fixes the major technology choices but explicitly allows small,
documented deviations. Without a record, the reasoning behind a deviation is
lost and a later contributor re-litigates or silently reverts it.

## Decision

Record every architecturally significant decision as a numbered Markdown file
in `docs/adr/`, using this template: Context, Decision, Consequences. A decision
is significant if it is hard to reverse, deviates from `AGENTS.md`, or affects
security, data compatibility, cost, or the public API.

## Consequences

- Deviations are reviewable and reversible with their reasoning intact.
- ADRs are immutable once accepted: a later decision supersedes an earlier one
  by adding a new file and marking the old one `Superseded by ADR-N`.
