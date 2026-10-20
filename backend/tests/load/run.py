"""Load test: how many people can chat at once before answers slow down?

    make loadtest                                   # defaults below
    uv run python -m tests.load.run --levels 10,50,100 --duration 30

Starts ``tests/load/server.py`` as one uvicorn process (what one production
API container runs), registers a pool of users, then steps up concurrency.
At each level, every virtual user repeatedly asks a question, reads the whole
answer stream, and pauses ``--think`` seconds. Meanwhile a probe calls
``/health/live`` four times a second: it does no work, so its latency is how
long a request waits for the event loop, and a stalled loop shows up there
first.

Per level it reports throughput, failures, and two latencies with the scripted
provider delays *subtracted*, so what remains is this server's own overhead:

- **first-token overhead**: time to the first answer text, minus the scripted
  search and first-token delays (1.8 s with a search, 0.8 s without);
- **probe latency**: how responsive the process stayed.

Two final phase pairs repeat one level while users sign in, then while users
upload PDFs, at a steady rate. Each pair is measured with and without the
extra load, to show whether password hashing or PDF parsing disturbs the
streams. Parsing runs in child processes, so the server CPU column (the
uvicorn process only) does not include it; that is the point of them.

The driver and the server share the machine, so the driver's own CPU use is
part of the result: on a dedicated server the API has more headroom than this
measures, not less.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from tests.load.server import FIRST_TOKEN_SECONDS, SEARCH_SECONDS
from tests.pdfs import make_pdf

BACKEND_DIR = Path(__file__).resolve().parents[2]
PASSWORD = "correct-horse-battery-staple"

# One question that the routing heuristics send to search, one they do not.
SEARCH_QUESTION = "What is the latest central bank rate decision?"
DIRECT_QUESTION = "Explain how photosynthesis turns light into chemical energy."
BASELINE = {
    SEARCH_QUESTION: SEARCH_SECONDS + FIRST_TOKEN_SECONDS,
    DIRECT_QUESTION: FIRST_TOKEN_SECONDS,
}


@dataclass
class RunSample:
    ok: bool
    error: str | None
    first_token_overhead: float | None
    duration: float


@dataclass
class LevelResult:
    name: str
    users: int
    seconds: float
    runs: list[RunSample] = field(default_factory=list)
    probes: list[float] = field(default_factory=list)
    probe_failures: int = 0
    cpu_percent: list[float] = field(default_factory=list)
    rss_mb: list[float] = field(default_factory=list)
    logins: list[float] = field(default_factory=list)
    uploads: list[float] = field(default_factory=list)
    upload_errors: dict[str, int] = field(default_factory=dict)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


# --- server process -------------------------------------------------------------


def start_server(port: int) -> subprocess.Popen[bytes]:
    # sys.executable, not `uv run`: the PID must be uvicorn's own, so the CPU
    # and memory sampled below are the server's.
    return subprocess.Popen(  # noqa: S603 - fixed argv, no user input
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.load.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            "--log-level",
            "warning",
        ],
        cwd=BACKEND_DIR,
    )


async def wait_ready(client: httpx.AsyncClient, *, within: float = 120) -> None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            if (await client.get("/api/v1/health/ready")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.5)
    raise SystemExit("the load-test server did not become ready")


class ProcessSampler:
    """CPU (as % of one core) and resident memory of a process, from /proc."""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._ticks = os.sysconf("SC_CLK_TCK")
        self._last: tuple[float, int] | None = None

    def sample(self) -> tuple[float | None, float]:
        with open(f"/proc/{self._pid}/stat") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()
        cpu_ticks = int(fields[11]) + int(fields[12])  # utime + stime
        rss_mb = int(fields[21]) * os.sysconf("SC_PAGE_SIZE") / 2**20
        now = time.monotonic()
        percent = None
        if self._last is not None:
            elapsed = now - self._last[0]
            percent = (cpu_ticks - self._last[1]) / self._ticks / elapsed * 100
        self._last = (now, cpu_ticks)
        return percent, rss_mb


# --- the virtual user ------------------------------------------------------------


@dataclass
class User:
    email: str
    token: str
    conversation_id: str


async def register(client: httpx.AsyncClient, index: int) -> User:
    email = f"load-{uuid.uuid4().hex[:10]}-{index}@example.com"
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": f"Load {index}"},
    )
    response.raise_for_status()
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    conversation = await client.post("/api/v1/conversations", json={}, headers=headers)
    conversation.raise_for_status()
    return User(email=email, token=token, conversation_id=conversation.json()["id"])


async def ask(client: httpx.AsyncClient, user: User, question: str) -> RunSample:
    started = time.monotonic()
    first_token: float | None = None
    terminal: str | None = None
    error: str | None = None
    try:
        async with client.stream(
            "POST",
            f"/api/v1/conversations/{user.conversation_id}/runs",
            json={"content": question},
            headers={
                "Authorization": f"Bearer {user.token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        ) as response:
            if response.status_code != 200:
                body = json.loads(await response.aread())
                error = f"http_{response.status_code}:{body['error']['code']}"
            else:
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    kind = event["type"]
                    if kind == "message.delta" and first_token is None:
                        first_token = time.monotonic() - started
                    elif kind in {"message.completed", "run.failed", "run.cancelled"}:
                        terminal = kind
                        if kind == "run.failed":
                            error = f"run_failed:{event['error']['code']}"
                        elif kind == "run.cancelled":
                            error = "run_cancelled"
    except httpx.HTTPError as exc:
        error = f"transport:{type(exc).__name__}"
    duration = time.monotonic() - started
    ok = error is None and terminal == "message.completed"
    if not ok and error is None:
        error = "stream_ended_early"
    overhead = first_token - BASELINE[question] if first_token is not None else None
    return RunSample(ok=ok, error=error, first_token_overhead=overhead, duration=duration)


async def virtual_user(
    client: httpx.AsyncClient,
    user: User,
    result: LevelResult,
    *,
    stop_at: float,
    think: float,
    offset: int,
) -> None:
    turn = offset
    while time.monotonic() < stop_at:
        question = SEARCH_QUESTION if turn % 2 == 0 else DIRECT_QUESTION
        turn += 1
        result.runs.append(await ask(client, user, question))
        await asyncio.sleep(think)


async def probe(client: httpx.AsyncClient, result: LevelResult, stop_at: float) -> None:
    while time.monotonic() < stop_at:
        started = time.monotonic()
        try:
            response = await client.get("/api/v1/health/live", timeout=10)
            if response.status_code == 200:
                result.probes.append(time.monotonic() - started)
            else:
                result.probe_failures += 1
        except httpx.HTTPError:
            result.probe_failures += 1
        await asyncio.sleep(0.25)


async def sample_process(sampler: ProcessSampler, result: LevelResult, stop_at: float) -> None:
    while time.monotonic() < stop_at:
        cpu, rss = sampler.sample()
        if cpu is not None:
            result.cpu_percent.append(cpu)
        result.rss_mb.append(rss)
        await asyncio.sleep(1)


async def login_storm(
    client: httpx.AsyncClient,
    users: list[User],
    result: LevelResult,
    *,
    rate: float,
    stop_at: float,
) -> None:
    """Sign in at ``rate`` per second, rotating accounts so no per-account
    limit is reached. Each sign-in costs one Argon2 verification."""
    index = 0

    async def one(user: User) -> None:
        started = time.monotonic()
        response = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        if response.status_code == 200:
            result.logins.append(time.monotonic() - started)

    tasks: set[asyncio.Task[None]] = set()
    while time.monotonic() < stop_at:
        task = asyncio.create_task(one(users[index % len(users)]))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        index += 1
        await asyncio.sleep(1 / rate)
    await asyncio.gather(*tasks)


# A realistic report: 30 pages of ordinary prose, about 1,400 characters each.
_PAGE = (
    "The committee reviewed quarterly results across every region and noted that "
    "revenue, margins, and headcount moved in line with the plan agreed in spring. "
) * 9


def upload_pdf() -> bytes:
    """A unique PDF each time, so duplicate detection never skips the work."""
    marker = uuid.uuid4().hex
    return make_pdf([f"Report {marker}, page {n}. {_PAGE}" for n in range(1, 31)])


async def upload_storm(
    client: httpx.AsyncClient,
    users: list[User],
    result: LevelResult,
    *,
    rate: float,
    stop_at: float,
) -> None:
    """Upload a fresh 30-page PDF at ``rate`` per second, each into a new
    conversation (a conversation holds five at most), rotating accounts."""
    index = 0

    async def one(user: User) -> None:
        headers = {"Authorization": f"Bearer {user.token}"}
        conversation = await client.post("/api/v1/conversations", json={}, headers=headers)
        started = time.monotonic()
        response = await client.post(
            f"/api/v1/conversations/{conversation.json()['id']}/documents",
            files={"file": ("report.pdf", upload_pdf(), "application/pdf")},
            headers=headers,
        )
        if response.status_code == 202:
            result.uploads.append(time.monotonic() - started)
        else:
            code = f"upload_{response.status_code}:{response.json()['error']['code']}"
            result.upload_errors[code] = result.upload_errors.get(code, 0) + 1

    tasks: set[asyncio.Task[None]] = set()
    while time.monotonic() < stop_at:
        task = asyncio.create_task(one(users[index % len(users)]))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        index += 1
        await asyncio.sleep(1 / rate)
    await asyncio.gather(*tasks)


async def run_level(
    client: httpx.AsyncClient,
    sampler: ProcessSampler,
    users: list[User],
    *,
    name: str,
    duration: float,
    think: float,
    login_users: list[User] | None = None,
    login_rate: float = 0.0,
    upload_rate: float = 0.0,
) -> LevelResult:
    result = LevelResult(name=name, users=len(users), seconds=duration)
    sampler.sample()  # reset the CPU baseline
    stop_at = time.monotonic() + duration
    tasks = [
        virtual_user(client, user, result, stop_at=stop_at, think=think, offset=i)
        for i, user in enumerate(users)
    ]
    tasks.append(probe(client, result, stop_at))
    tasks.append(sample_process(sampler, result, stop_at))
    if login_users and login_rate:
        tasks.append(login_storm(client, login_users, result, rate=login_rate, stop_at=stop_at))
    if login_users and upload_rate:
        tasks.append(upload_storm(client, login_users, result, rate=upload_rate, stop_at=stop_at))
    began = time.monotonic()
    await asyncio.gather(*tasks)
    # Users finish the run they are in after stop_at; count the real span.
    result.seconds = time.monotonic() - began
    return result


# --- reporting ------------------------------------------------------------------


def fmt(value: float | None, *, unit: str = "s", digits: int = 2) -> str:
    if value is None:
        return "-"
    if unit == "ms":
        return f"{value * 1000:.0f} ms"
    return f"{value:.{digits}f} {unit}".strip()


def summarize(result: LevelResult) -> dict[str, Any]:
    ok = [r for r in result.runs if r.ok]
    errors: dict[str, int] = {}
    for run in result.runs:
        if run.error:
            errors[run.error] = errors.get(run.error, 0) + 1
    overheads = [r.first_token_overhead for r in ok if r.first_token_overhead is not None]
    return {
        "phase": result.name,
        "users": result.users,
        "runs": len(result.runs),
        "failed": len(result.runs) - len(ok),
        "errors": errors,
        "runs_per_minute": len(ok) / result.seconds * 60 if result.seconds else 0,
        "first_token_overhead_p50": percentile(overheads, 50),
        "first_token_overhead_p95": percentile(overheads, 95),
        "duration_p95": percentile([r.duration for r in ok], 95),
        "probe_p50": percentile(result.probes, 50),
        "probe_p95": percentile(result.probes, 95),
        "probe_max": max(result.probes) if result.probes else None,
        "probe_failures": result.probe_failures,
        "cpu_mean": statistics.fmean(result.cpu_percent) if result.cpu_percent else None,
        "rss_max_mb": max(result.rss_mb) if result.rss_mb else None,
        "logins": len(result.logins),
        "login_p95": percentile(result.logins, 95),
        "uploads": len(result.uploads),
        "upload_p50": percentile(result.uploads, 50),
        "upload_p95": percentile(result.uploads, 95),
        "upload_errors": result.upload_errors,
    }


def print_row(s: dict[str, Any]) -> None:
    errors = ", ".join(f"{code} x{count}" for code, count in s["errors"].items()) or "-"
    cpu = "-" if s["cpu_mean"] is None else f"{s['cpu_mean']:.0f}%"
    rss = "-" if s["rss_max_mb"] is None else f"{s['rss_max_mb']:.0f} MB"
    cells = [
        s["phase"],
        s["users"],
        s["runs"],
        s["failed"],
        f"{s['runs_per_minute']:.0f}",
        fmt(s["first_token_overhead_p50"]),
        fmt(s["first_token_overhead_p95"]),
        fmt(s["probe_p95"], unit="ms"),
        fmt(s["probe_max"], unit="ms"),
        cpu,
        rss,
        errors,
    ]
    print("| " + " | ".join(str(cell) for cell in cells) + " |", flush=True)


HEADER = (
    "| phase | users | runs | failed | runs/min | first-token overhead p50 | p95 | "
    "probe p95 | probe max | server CPU | server RSS | errors |\n"
    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"
)


def healthy(s: dict[str, Any], *, max_overhead_p95: float, max_probe_p95: float) -> bool:
    overhead = s["first_token_overhead_p95"]
    probe_p95 = s["probe_p95"]
    return (
        s["failed"] == 0
        and s["probe_failures"] == 0
        and overhead is not None
        and overhead <= max_overhead_p95
        and probe_p95 is not None
        and probe_p95 <= max_probe_p95
    )


# --- main -----------------------------------------------------------------------


async def main(args: argparse.Namespace) -> int:
    levels = [int(n) for n in args.levels.split(",")]
    server = start_server(args.port)
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
    timeout = httpx.Timeout(120, connect=30)
    summaries: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{args.port}", limits=limits, timeout=timeout
        ) as client:
            await wait_ready(client)
            sampler = ProcessSampler(server.pid)

            pool_size = max(*levels, args.storm_users)
            print(f"registering {pool_size + args.login_users} users...", flush=True)
            started = time.monotonic()
            gate = asyncio.Semaphore(16)

            async def gated(index: int) -> User:
                async with gate:
                    return await register(client, index)

            users = list(
                await asyncio.gather(*(gated(i) for i in range(pool_size + args.login_users)))
            )
            elapsed = time.monotonic() - started
            print(
                f"registered {len(users)} users in {elapsed:.1f} s "
                f"({len(users) / elapsed:.1f}/s, each one an Argon2 hash)\n",
                flush=True,
            )
            stream_users, login_users = users[:pool_size], users[pool_size:]

            print(HEADER, flush=True)
            for level in levels:
                result = await run_level(
                    client,
                    sampler,
                    stream_users[:level],
                    name=f"{level} users",
                    duration=args.duration,
                    think=args.think,
                )
                summary = summarize(result)
                summaries.append(summary)
                print_row(summary)
                if not healthy(summary, max_overhead_p95=5.0, max_probe_p95=2.0):
                    print("(stopping: this level is past the breaking point)", flush=True)
                    break

            if args.storm_users and args.login_rate:
                users_for_storm = stream_users[: args.storm_users]
                for name, rate in (
                    (f"{args.storm_users} users, no sign-ins", 0.0),
                    (f"{args.storm_users} users + {args.login_rate:g} sign-ins/s", args.login_rate),
                ):
                    result = await run_level(
                        client,
                        sampler,
                        users_for_storm,
                        name=name,
                        duration=args.duration,
                        think=args.think,
                        login_users=login_users,
                        login_rate=rate,
                    )
                    summary = summarize(result)
                    summaries.append(summary)
                    print_row(summary)
                    if rate:
                        print(
                            f"  sign-ins completed: {summary['logins']}, "
                            f"p95 {fmt(summary['login_p95'])}",
                            flush=True,
                        )

            if args.storm_users and args.upload_rate:
                users_for_storm = stream_users[: args.storm_users]
                for name, rate in (
                    (f"{args.storm_users} users, no uploads", 0.0),
                    (
                        f"{args.storm_users} users + {args.upload_rate:g} uploads/s",
                        args.upload_rate,
                    ),
                ):
                    result = await run_level(
                        client,
                        sampler,
                        users_for_storm,
                        name=name,
                        duration=args.duration,
                        think=args.think,
                        login_users=login_users,
                        upload_rate=rate,
                    )
                    summary = summarize(result)
                    summaries.append(summary)
                    print_row(summary)
                    if rate:
                        errors = ", ".join(
                            f"{code} x{count}" for code, count in summary["upload_errors"].items()
                        )
                        print(
                            f"  uploads accepted: {summary['uploads']}, "
                            f"p50 {fmt(summary['upload_p50'])}, p95 {fmt(summary['upload_p95'])}"
                            f"; refused: {errors or 'none'}",
                            flush=True,
                        )

            sustainable = [
                s["users"]
                for s in summaries
                if s["phase"].endswith(" users")
                and healthy(s, max_overhead_p95=args.overhead_budget, max_probe_p95=0.25)
            ]
            print(
                "\nlargest level within budget "
                f"(no failures, first-token overhead p95 <= {args.overhead_budget:g} s, "
                f"probe p95 <= 250 ms): {max(sustainable) if sustainable else 'none'} users",
                flush=True,
            )
    finally:
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()

    if args.out:
        await asyncio.to_thread(Path(args.out).write_text, json.dumps(summaries, indent=2))
        print(f"results written to {args.out}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--levels", default="10,25,50,100,200,400", help="concurrent users")
    parser.add_argument("--duration", type=float, default=40, help="seconds per level")
    parser.add_argument("--think", type=float, default=2.0, help="pause between questions")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument(
        "--overhead-budget", type=float, default=1.0, help="acceptable first-token overhead p95"
    )
    parser.add_argument("--storm-users", type=int, default=50, help="users during the sign-ins")
    parser.add_argument("--login-rate", type=float, default=5.0, help="sign-ins per second")
    parser.add_argument("--login-users", type=int, default=100, help="accounts used to sign in")
    parser.add_argument(
        "--upload-rate", type=float, default=1.0, help="30-page PDF uploads per second"
    )
    parser.add_argument("--out", help="write the per-phase summary as JSON")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
