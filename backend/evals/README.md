# Agent evaluations

A small, versioned dataset (`dataset.json`) of questions that exercise the
agent's decisions, citations, and failure handling. It covers every category
in AGENTS.md §13: no-search, freshness, multi-source, conflicting sources,
prompt injection in retrieved content, search outage, empty results, malformed
citations, and conversation follow-ups.

Every case runs through the **real compiled graph**. Only the providers
change between the two subsets.

## `ci`: deterministic, free, gated

```bash
cd backend
uv run python -m evals.run --subset ci      # or: make eval
```

- The LLM and search are scripted per case (`script` in the dataset), so the
  run is reproducible and calls no paid API.
- It checks the parts of the pipeline that do not depend on model judgement:
  - heuristic search routing (and that the heuristics made the decision);
  - that every citation names a retrieved source, and invalid markers are
    dropped;
  - degraded flags and notices on outage and empty results;
  - search queries unaffected by injected instructions, with retrieved text
    inside the untrusted-content fence;
  - conversation context reaching the prompt;
  - no system-prompt text in answers.
- Gates: every check passes, search-decision accuracy 1.0, and citation
  validity 1.0. It exits non-zero otherwise. CI runs it, and so does
  `make test` (`tests/unit/test_evals.py`).

## `live`: real model plus an LLM judge

```bash
cd backend
uv run python -m evals.run --subset live
```

- Needs `LLM_API_KEY` and `SEARCH_API_KEY`, and **costs money**: roughly two
  model calls per case plus one judge call, and real searches for cases
  marked `"search": "real"`. Cases whose point is a scripted condition
  (conflicting sources, injection, outage, empty results) keep scripted
  search with the real model. `malformed-citation` is ci-only, because a real
  model cannot be made to cite `[S7]`.
- The judge (`judge.py`) scores each answer against the case's `rubric`
  (1–5) and counts unsupported claims. It is a model grading a model: compare
  runs of the same dataset version (before and after a prompt change) rather
  than reading the numbers as absolute.
- Gates are looser: decision accuracy ≥ 0.8, citation validity ≥ 0.95, task
  score ≥ 0.7, and unsupported-claim rate ≤ 0.2.
- Never run in CI.

## Metrics

| Metric | ci | live |
| --- | --- | --- |
| Search-decision accuracy | heuristic-decided cases (model decisions are scripted) | all cases |
| Citation validity | citations that name a retrieved source, over all citations | same |
| Citation precision | emitted source labels that survived validation | same |
| Task score | share of cases passing every check | mean judge score / 5 |
| Unsupported-claim rate | proxy: uncited sentences in sourced answers | judge's unsupported / total claims |
| Latency | p50 / p95 per case | same |
| Approximate cost | fake token counts | provider tokens × `--price-prompt` / `--price-completion` (USD per 1M tokens) |

Each run writes a JSON report to `evals/reports/` (git-ignored) with scores
and failed checks. It holds no answer text.

## Changing the dataset

Bump `version` whenever a case changes, so reports from different datasets
are never compared. A new case needs `expect`, a `script` for the ci subset,
and `live` (`null` to skip it live) with a rubric.
