# Load testing

How many people can chat at once before answers slow down, measured rather
than guessed.

```bash
make up-db        # PostgreSQL (and Redis, from `make dev`) must be running
make loadtest     # about 6 minutes; options: uv run python -m tests.load.run --help
```

## What is measured

`tests/load/run.py` starts `tests/load/server.py` as **one uvicorn process**,
the same as one production API container. The server is the real application:
registration and sign-in with Argon2, the run lock and rate limits in Redis,
runs and messages in PostgreSQL, the LangGraph agent, SSE streaming. Only the
model and search engine are replaced, by stand-ins that **wait like the real
ones** but cost nothing: 1.0 s per search, 0.8 s to the first token, then about
600 characters over 2.5 s. A load test against the real providers would
measure their rate limits and your bill, not this server.

Each virtual user repeatedly asks a question (alternately one that searches
and one that does not), reads the whole answer stream, waits 2 s, and asks
again. That is far more often than a person types, which is the point: it
loads the server with answers.

Reported per level:

- **first-token overhead**: time to the first answer text *minus* the scripted
  provider delays (1.8 s with a search, 0.8 s without). What remains is this
  server's own queueing and work.
- **probe**: latency of `/health/live`, called four times a second. It does no
  work, so it measures how long any request waits for the event loop.
- server CPU (100% = one core) and resident memory, read from `/proc`.

## Results (2026-10-11)

Machine: a 4-core, 7 GB laptop also running the load generator and the
development stack (Vite, a second API, PostgreSQL, Redis, Mailpit). The
generator competes with the server for CPU, so a dedicated server does at
least this well.

| concurrent users | answers | failed | answers/min | first-token overhead p50 | p95 | probe p95 | probe max | server CPU | memory |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 80 | 0 | 117 | 0.13 s | 0.55 s | 14 ms | 42 ms | 22% | 139 MB |
| 25 | 200 | 0 | 283 | 0.26 s | 0.66 s | 19 ms | 67 ms | 44% | 142 MB |
| 50 | 385 | 0 | 521 | 0.27 s | 1.23 s | 33 ms | 183 ms | 66% | 148 MB |
| 100 | 537 | 0 | 721 | 1.80 s | 3.00 s | 97 ms | 759 ms | 86% | 159 MB |
| 200 | 590 | 0 | 696 | 5.41 s | 8.85 s | 152 ms | 4849 ms | 87% | 180 MB |

The test stops after the first level past the breaking point, so 400 was not
run.

**Reading it.**

- **No answer failed at any level.** Under overload the server slows down; it
  does not drop requests or corrupt state.
- **The limit is one CPU core.** Throughput rises with users until about 100,
  then stays flat near **700 answers a minute** while waiting time grows. CPU
  sits near 87%: a single Python process is using all of its one core. Memory
  (under 200 MB) and the database pool are nowhere near their limits.
- **Comfortable capacity per process: about 50 simultaneous answer streams,
  roughly 500 answers a minute**, with first tokens arriving within ~1 s of
  when the model produces them. At 100 streams it still works, with a
  noticeable 2-3 s extra wait.

**In people.** A person reading answers asks perhaps one question a minute
while active. 500 answers a minute is then roughly **500 people actively
chatting at the same moment** per API process. If one registered user in ten
is active at the busiest time, that is a few thousand registered users. Two
caveats keep this conservative: real models stream in smaller pieces than the
stand-in (more events per answer, more CPU), and your model and search plans
have their own rate limits, which are likely to be reached first.

## Sign-ins under load: the off-loop hashing fix, measured

50 users chatting, then the same 50 with 5 sign-ins per second alongside
(each one an Argon2 verification). The second column re-runs it with
`LOAD_HASH_ON_EVENT_LOOP=1`, a test-only switch that puts hashing back on the
event loop, as it was before [ADR-0013](adr/0013-abuse-controls.md).

| 50 users + 5 sign-ins/s | hashing in a thread (now) | hashing on the event loop (before) |
| --- | ---: | ---: |
| answers/min (no sign-ins: ~550) | 524 | **146** |
| first-token overhead p95 | 1.78 s | **25.6 s** |
| probe p95 | 37 ms | **946 ms** |
| sign-in p95 | 0.32 s | 4.30 s |
| server CPU | 184% | 140% |

Before the fix, five sign-ins a second cut answer throughput by 72% and made
the slowest answers wait 25 seconds. With it, sign-ins cost the chat almost
nothing. CPU rises above 100% because the hashing now runs on other cores
while the event loop keeps serving.

Registration ran at about 10-11 accounts a second, bounded by the two-thread
hashing pool (each hash already uses four cores' worth of lanes).

## PDF uploads while answering (2026-10-20)

25 users chatting, then the same 25 while unique 30-page PDFs (51 KB, about
42,000 characters each) are uploaded at **2 per second**, each parsed for real
and embedded by a stand-in that waits 0.3 s per request. That is far beyond
real use: each account may upload 10 documents in 10 minutes.

```bash
uv run python -m tests.load.run --levels 25 --duration 30 --storm-users 25 \
    --login-rate 0 --upload-rate 2 --login-users 40
```

The first run found work still on the event loop. Parsing was not it (it
runs in a child process); measured at the largest document allowed, the event
loop was paying for turning vectors into SQL text (110 ms per batch of 100),
hashing the upload (33 ms for 10 MiB), and chunking (19 ms). Vectors are now
formatted in a thread and cast in SQL, hashing runs in a thread (hashlib
releases the GIL), and chunking moved into the parsing child.

| 25 users | no uploads | + 2 uploads/s, before | + 2 uploads/s, after |
| --- | ---: | ---: | ---: |
| answers/min | 280 | 264 | 275 |
| first-token overhead p50 | 0.16-0.23 s | 0.37 s | 0.26 s |
| first-token overhead p95 | 0.79-1.29 s | 0.89 s | 0.85 s |
| probe p95 | 21-22 ms | 64 ms | 48 ms |
| probe max | 118-174 ms | 164 ms | 97 ms |
| upload p95 (request accepted) | | 0.58 s | 0.28 s |
| failed answers, refused uploads | 0 | 0 | 0 |

Two uploads a second now add about 30 ms to the typical time to first token,
and nothing to the slowest. Part of the remaining probe rise is the stand-in
embedder computing its vectors inside the API process; the real provider
does that work on its own servers. The server CPU column covers the uvicorn
process only, so it does not include the parsing children. The p95 overhead
varies by several hundred milliseconds between identical phases on this
shared laptop, which is why each comparison is a pair run back to back.

## Scaling past one process

Every piece of coordination (run locks, rate limits, cancellation, the daily
quota) goes through Redis or PostgreSQL, so more API replicas behind nginx add
capacity roughly linearly until the database becomes the limit. Run them as
separate containers rather than `uvicorn --workers`: Prometheus metrics are
kept per process, and multi-worker containers would need the client library's
multiprocess mode to report them correctly. Rerun this test after any change
to the streaming path, and on the production machine before launch.
