"""The measured workloads — A64-028.5 §54.

Each returns `Result`s the harness can render and compare. They are written
to be *rerun*: every one creates its own accounts, and none needs the
database to be in a particular state first.
"""

import asyncio
import json
import secrets
import time
import uuid
from collections.abc import Sequence
from typing import Any

import httpx
import websockets
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.load.harness import Result, Sample, run_for, timed
from tests.load.workload import OPENING, Player, frame, seeded_cohort, ws_ticket


def ws_url(base_url: str) -> str:
    return base_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"


# --- P01 / P02: HTTP reads ---------------------------------------------------


async def http_reads(
    base_url: str, *, path: str, player: Player | None, levels: Sequence[int], duration_s: float
) -> list[Result]:
    """One endpoint at rising concurrency until it stops getting faster.

    Progressive rather than a single number, because a single concurrency is
    not a capacity measurement — it is one point on a curve whose shape is
    the actual finding.
    """
    results: list[Result] = []
    headers = player.auth if player else {}
    for concurrency in levels:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
            limits=httpx.Limits(max_connections=concurrency + 10),
            headers=headers,
        ) as client:

            async def one(_: int, client: httpx.AsyncClient = client) -> int:
                return (await client.get(path)).status_code

            results.append(
                await run_for(
                    f"{path}", operation=one, concurrency=concurrency, duration_s=duration_s
                )
            )
    return results


# --- P03: login --------------------------------------------------------------


async def login_load(
    base_url: str, players: Sequence[Player], *, levels: Sequence[int], duration_s: float
) -> list[Result]:
    """Sign-in, which is Argon2 and therefore CPU rather than I/O.

    Deliberately measured apart from reads: folding a ~20 ms hash into a
    mixed average is how a platform convinces itself its reads are slow.
    """
    from tests.load.workload import PASSWORD

    results: list[Result] = []
    for concurrency in levels:
        async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:

            async def one(index: int, client: httpx.AsyncClient = client) -> int:
                player = players[index % len(players)]
                response = await client.post(
                    "/api/v1/auth/login",
                    json={"email": player.email, "password": PASSWORD},
                )
                return response.status_code

            results.append(
                await run_for(
                    "auth/login", operation=one, concurrency=concurrency, duration_s=duration_s
                )
            )
    return results


# --- P04: refresh rotation ---------------------------------------------------


async def refresh_load(base_url: str, *, sessions: int, duration_s: float) -> Result:
    """One rotating session per worker — §9's "realistic independent
    sessions", not one token hammered from many places.

    Each worker owns a cookie jar, so every refresh rotates its own token
    exactly as a browser tab does. A shared jar would be the abuse case, and
    A64-028.2 already has tests for that.
    """
    from tests.load.workload import PASSWORD

    players = await seeded_cohort(sessions, prefix="ref")
    clients = []
    for player in players:
        client = httpx.AsyncClient(base_url=base_url, timeout=30.0)
        signed_in = await client.post(
            "/api/v1/auth/browser/login",
            json={"email": player.email, "password": PASSWORD},
        )
        signed_in.raise_for_status()
        clients.append(client)

    try:

        async def one(index: int) -> int:
            return (await clients[index].post("/api/v1/auth/browser/refresh")).status_code

        return await run_for(
            "auth/browser/refresh",
            operation=one,
            concurrency=sessions,
            duration_s=duration_s,
        )
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))


# --- P05 / P06: matchmaking --------------------------------------------------


async def matchmaking_burst(
    base_url: str, engine: AsyncEngine, *, users: int, wait_s: float
) -> Result:
    """`users` accounts join the queue at once; how many get matched, and
    how fast.

    Time-to-match is measured from the join's acknowledgement to the moment
    the player's pending match appears, polled — the honest client view.
    """
    players = await seeded_cohort(users, prefix="mm")
    joined_at: dict[str, float] = {}
    samples: list[Sample] = []
    started = time.perf_counter()

    async with httpx.AsyncClient(
        base_url=base_url, timeout=60.0, limits=httpx.Limits(max_connections=users + 20)
    ) as client:

        async def join(player: Player) -> Sample:
            async def call() -> int:
                response = await client.post(
                    "/api/v1/matchmaking/queue",
                    headers=player.auth,
                    json={"variant": "russian_8x8", "speed_class": "blitz", "rated": False},
                )
                if response.status_code < 400:
                    joined_at[player.user_id] = time.perf_counter()
                return response.status_code

            return await timed(call)

        samples = list(await asyncio.gather(*(join(player) for player in players)))

        # Pairing is a scheduled sweep, so matching is not synchronous with
        # the join. Poll until the sweep has had time to work.
        matched: dict[str, float] = {}
        deadline = time.perf_counter() + wait_s
        while time.perf_counter() < deadline and len(matched) < len(joined_at):
            for player in players:
                if player.user_id in matched:
                    continue
                pending = await client.get(
                    "/api/v1/matchmaking/matches/pending", headers=player.auth
                )
                if pending.status_code == 200 and pending.json().get("data"):
                    matched[player.user_id] = time.perf_counter()
            await asyncio.sleep(0.25)

    waits = sorted(matched[user] - joined_at[user] for user in matched if user in joined_at)
    async with engine.connect() as connection:
        doubled = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM (SELECT player_id FROM ("
                    "  SELECT light_player_id AS player_id FROM game.match"
                    "   WHERE status <> 'completed'"
                    "  UNION ALL SELECT dark_player_id FROM game.match"
                    "   WHERE status <> 'completed'"
                    ") s GROUP BY player_id HAVING count(*) > 1) d"
                )
            )
        ).scalar_one()

    result = Result(
        scenario=f"matchmaking burst x{users}",
        concurrency=users,
        duration_s=time.perf_counter() - started,
        samples=samples,
    )
    result.notes = {
        "users": users,
        "joins_accepted": sum(1 for s in samples if s.ok),
        "matched": len(matched),
        "leftover": len(joined_at) - len(matched),
        "time_to_match_p50_s": round(waits[len(waits) // 2], 2) if waits else None,
        "time_to_match_p95_s": round(waits[max(0, round(0.95 * len(waits)) - 1)], 2)
        if waits
        else None,
        "players_in_two_matches": doubled,
    }
    return result


# --- P07 / P08: game moves over WebSockets -----------------------------------


async def _seed_match(engine: AsyncEngine, light: str, dark: str) -> uuid.UUID:
    match_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO game.match (id, pairing_id, variant, rated, engine_version, "
                "light_player_id, light_accepted_at, dark_player_id, dark_accepted_at, "
                "status, origin, settled_at, ply_number, created_at, acceptance_deadline) "
                "VALUES (:i, :p, 'russian_8x8', false, 2, :l, now(), :d, now(), 'active', "
                "'challenge', now(), 0, now(), now() + interval '2 hours')"
            ),
            {"i": match_id, "p": uuid.uuid4(), "l": light, "d": dark},
        )
    return match_id


async def live_games(
    engine: AsyncEngine,
    *,
    node_urls: Sequence[str],
    games: int,
    moves_per_game: int,
) -> Result:
    """`games` concurrent games, seats split across the given instances.

    Each game plays a fixed opening, so every submission is a move that
    validates, applies, commits, writes the outbox and fans out — the whole
    path, not the rejection path.

    The latency reported is the **command** round trip (submit to
    `game.move.accepted`). Cross-instance frame latency is measured
    separately by `frame_latency`, because they answer different questions.
    """
    players = await seeded_cohort(games * 2, prefix="lg")
    pairs = [(players[i * 2], players[i * 2 + 1]) for i in range(games)]
    matches = [await _seed_match(engine, a.user_id, b.user_id) for a, b in pairs]

    samples: list[Sample] = []
    started = time.perf_counter()

    async def play(index: int) -> None:
        light, dark = pairs[index]
        match_id = matches[index]
        # Seats on different instances when more than one was given, which
        # is the shape a real deployment has behind a load balancer.
        light_url = node_urls[index % len(node_urls)]
        dark_url = node_urls[(index + 1) % len(node_urls)]

        async with httpx.AsyncClient(timeout=30.0) as http:
            light_ticket = await ws_ticket(
                httpx.AsyncClient(base_url=light_url, timeout=30.0), light
            )
            dark_ticket = await ws_ticket(httpx.AsyncClient(base_url=dark_url, timeout=30.0), dark)
            del http

        async with (
            websockets.connect(f"{ws_url(light_url)}?ticket={light_ticket}") as light_ws,
            websockets.connect(f"{ws_url(dark_url)}?ticket={dark_ticket}") as dark_ws,
        ):
            for socket in (light_ws, dark_ws):
                await _read_until(socket, {"connection.ready"})
                await socket.send(
                    frame(
                        "game.resume",
                        "game",
                        {"match_id": str(match_id), "last_known_sequence": 0},
                    )
                )
                await _read_until(socket, {"game.snapshot", "game.resumed", "error"})

            for ply in range(moves_per_game):
                mover = light_ws if ply % 2 == 0 else dark_ws
                origin, target = OPENING[ply % len(OPENING)]

                async def submit(
                    socket: Any = mover, origin: str = origin, target: str = target
                ) -> int | None:
                    await socket.send(
                        frame(
                            "game.move.submit",
                            "game",
                            {"match_id": str(match_id), "path": [origin, target]},
                            request_id=secrets.token_hex(4),
                        )
                    )
                    answer, _ = await _read_until(
                        socket, {"game.move.accepted", "game.move.rejected", "error"}
                    )
                    return None if answer and answer["type"] == "game.move.accepted" else 500

                samples.append(await timed(submit))

    await asyncio.gather(*(play(index) for index in range(games)))

    async with engine.connect() as connection:
        recorded = (
            await connection.execute(
                text("SELECT count(*) FROM game.move WHERE match_id = ANY(:ids)"),
                {"ids": matches},
            )
        ).scalar_one()

    result = Result(
        scenario=f"live games x{games}",
        concurrency=games,
        duration_s=time.perf_counter() - started,
        samples=samples,
    )
    result.notes = {
        "games": games,
        "instances": len(node_urls),
        "moves_attempted": len(samples),
        "moves_in_durable_log": recorded,
        "lost_durable_moves": max(0, sum(1 for s in samples if s.ok) - recorded),
    }
    return result


async def _read_until(
    socket: Any, wanted: set[str], patience_s: float = 15.0
) -> tuple[dict[str, Any] | None, list[str]]:
    seen: list[str] = []
    try:
        async with asyncio.timeout(patience_s):
            while True:
                message = json.loads(await socket.recv())
                seen.append(message["type"])
                if message["type"] in wanted:
                    return message, seen
    except TimeoutError:
        return None, seen


# --- P09: idle sockets -------------------------------------------------------


async def idle_sockets(base_url: str, *, count: int, hold_s: float) -> Result:
    """How many sockets the process holds while doing nothing.

    Kept apart from `live_games` on purpose (§16): an idle connection costs
    memory and a file descriptor, an active game costs those plus a
    database round trip per move, and inferring one capacity from the other
    is how a platform promises numbers it cannot serve.
    """
    players = await seeded_cohort(count, prefix="idle")
    samples: list[Sample] = []
    started = time.perf_counter()
    sockets: list[Any] = []

    async with httpx.AsyncClient(
        base_url=base_url, timeout=60.0, limits=httpx.Limits(max_connections=count + 20)
    ) as client:

        async def connect(player: Player) -> None:
            async def open_socket() -> int | None:
                ticket = await ws_ticket(client, player)
                socket = await websockets.connect(f"{ws_url(base_url)}?ticket={ticket}")
                await _read_until(socket, {"connection.ready"})
                sockets.append(socket)
                return None

            samples.append(await timed(open_socket))

        await asyncio.gather(*(connect(player) for player in players))
        await asyncio.sleep(hold_s)
        alive = sum(1 for socket in sockets if socket.state.name == "OPEN")
        await asyncio.gather(*(socket.close() for socket in sockets), return_exceptions=True)

    result = Result(
        scenario=f"idle sockets x{count}",
        concurrency=count,
        duration_s=time.perf_counter() - started,
        samples=samples,
    )
    result.notes = {
        "requested": count,
        "connected": sum(1 for s in samples if s.ok),
        "still_open_after_hold": alive,
        "unexpected_disconnects": sum(1 for s in samples if s.ok) - alive,
    }
    return result


# --- P10: cross-instance frame latency ---------------------------------------


async def frame_latency(engine: AsyncEngine, *, node_urls: Sequence[str], rounds: int) -> Result:
    """Submit on one instance, receive on another, timed on one clock.

    Monotonic and in one process, so there is no clock skew to explain away
    (§17). What it measures is the whole path: command, commit, publish to
    the addressee's stream, the forwarder's next pass, and the socket.

    ## Why fresh sockets per match

    `OPENING` is a fixed **legal** sequence six plies long. Playing more
    than that on one match would submit an illegal move and measure the
    rejection path (§13), and reusing one socket pair across matches leaves
    frames from the previous room in the receive queue, which desynchronises
    the reader. Both were tried; both produced a scenario that reported the
    platform as broken when the harness was.
    """
    per_match = len(OPENING)
    matches_needed = max(1, -(-rounds // per_match))
    players = await seeded_cohort(2 * matches_needed, prefix="fl")

    samples: list[Sample] = []
    started = time.perf_counter()

    for index in range(matches_needed):
        light, dark = players[2 * index], players[2 * index + 1]
        match_id = await _seed_match(engine, light.user_id, dark.user_id)

        async with (
            httpx.AsyncClient(base_url=node_urls[0], timeout=30.0) as first,
            httpx.AsyncClient(base_url=node_urls[-1], timeout=30.0) as second,
        ):
            light_ticket = await ws_ticket(first, light)
            dark_ticket = await ws_ticket(second, dark)

        async with (
            websockets.connect(f"{ws_url(node_urls[0])}?ticket={light_ticket}") as light_ws,
            websockets.connect(f"{ws_url(node_urls[-1])}?ticket={dark_ticket}") as dark_ws,
        ):
            for socket in (light_ws, dark_ws):
                await _read_until(socket, {"connection.ready"})
                await socket.send(
                    frame(
                        "game.resume",
                        "game",
                        {"match_id": str(match_id), "last_known_sequence": 0},
                    )
                )
                await _read_until(socket, {"game.snapshot", "game.resumed", "error"})

            for ply in range(min(per_match, rounds - len(samples))):
                mover, watcher = (light_ws, dark_ws) if ply % 2 == 0 else (dark_ws, light_ws)
                origin, target = OPENING[ply]
                sent = time.perf_counter()
                await mover.send(
                    frame(
                        "game.move.submit",
                        "game",
                        {"match_id": str(match_id), "path": [origin, target]},
                        request_id=secrets.token_hex(4),
                    )
                )
                seen, _ = await _read_until(watcher, {"game.move.applied"}, patience_s=15.0)
                samples.append(
                    Sample(elapsed_s=time.perf_counter() - sent)
                    if seen
                    else Sample(elapsed_s=0.0, error="FrameNotDelivered")
                )
                await _read_until(
                    mover, {"game.move.accepted", "game.move.rejected"}, patience_s=15.0
                )

    result = Result(
        scenario="cross-instance frame",
        concurrency=1,
        duration_s=time.perf_counter() - started,
        samples=samples,
    )
    result.notes = {
        "instances": len(set(node_urls)),
        "rounds": len(samples),
        "matches": matches_needed,
        "undelivered": sum(1 for s in samples if not s.ok),
    }
    return result


# --- P12 / P13: outbox -------------------------------------------------------


async def outbox_drain(engine: AsyncEngine, *, events: int, patience_s: float) -> Result:
    """Seed a backlog and time how long the relay takes to clear it.

    The events are a type nothing consumes, so they exercise the relay's
    claim, dispatch and ledger without invoking a real side effect — which
    is what makes this measurable without sending anything to anybody.
    """
    aggregate = uuid.uuid4()
    marker = f"a64_028_5.load.{secrets.token_hex(3)}"
    async with engine.begin() as connection:
        for _ in range(events):
            await connection.execute(
                text(
                    "INSERT INTO platform.outbox (id, aggregate_type, aggregate_id, event_type, "
                    "event_version, payload, attempt_count) VALUES "
                    "(:i, 'match', :a, :t, 1, '{}'::jsonb, 0)"
                ),
                {"i": uuid.uuid4(), "a": aggregate, "t": marker},
            )

    started = time.perf_counter()
    drained_at: float | None = None
    max_attempts = 0
    while time.perf_counter() - started < patience_s:
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT count(*) FILTER (WHERE published_at IS NULL) AS pending, "
                            "coalesce(max(attempt_count), 0) AS attempts "
                            "FROM platform.outbox WHERE event_type = :t"
                        ),
                        {"t": marker},
                    )
                )
                .mappings()
                .one()
            )
        max_attempts = max(max_attempts, row["attempts"])
        if row["pending"] == 0:
            drained_at = time.perf_counter()
            break
        await asyncio.sleep(0.2)

    elapsed = (drained_at or time.perf_counter()) - started
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM platform.outbox WHERE event_type = :t"), {"t": marker}
        )

    result = Result(
        scenario=f"outbox drain {events}",
        concurrency=1,
        duration_s=elapsed,
        samples=[Sample(elapsed_s=elapsed / events)] * (events if drained_at else 0),
    )
    result.notes = {
        "events": events,
        "drained": drained_at is not None,
        "drain_seconds": round(elapsed, 2),
        "events_per_s": round(events / elapsed, 1) if drained_at and elapsed > 0 else None,
        "max_attempt_count": max_attempts,
    }
    return result
