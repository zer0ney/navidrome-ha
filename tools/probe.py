"""Optional authorized read-only protocol probe; credentials never enter command arguments."""

import argparse
import asyncio
import getpass
import time

from aiohttp import ClientSession

from custom_components.navidrome.api import NavidromeClient, NavidromeError
from custom_components.navidrome.const import WATCHDOG


async def probe(url: str, username: str, event_limit: int) -> None:
    password = await asyncio.to_thread(getpass.getpass, "Navidrome administrator password: ")
    async with ClientSession() as session:
        api = NavidromeClient(session, url, username, password)
        await api.validate()
        print(f"Navidrome version: {api.version}")
        print(f"Users: {len(await api.users())}; active sessions: {len(await api.now_playing())}")
        async with api.events() as events:
            for _ in range(event_limit):
                async with asyncio.timeout(WATCHDOG):
                    event = await anext(events)
                if event.event in {"serverStart", "keepAlive", "nowPlayingCount"}:
                    print(f"{time.monotonic():.3f} received {event.event}")
                if event.event == "nowPlayingCount":
                    started = time.monotonic()
                    snapshot = await api.now_playing()
                    elapsed = (time.monotonic() - started) * 1000
                    print(f"Snapshot: {len(snapshot)} sessions, HTTP {elapsed:.1f} ms")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--username", required=True)
    parser.add_argument("--events", type=int, default=5)
    args = parser.parse_args()
    try:
        asyncio.run(probe(args.url, args.username, max(1, args.events)))
    except (NavidromeError, ValueError, TimeoutError):
        parser.exit(
            1, "Probe failed. Check credentials, admin access, URL, TLS and SSE proxy settings.\n"
        )
    except KeyboardInterrupt:
        parser.exit(130, "Probe cancelled.\n")


if __name__ == "__main__":
    main()
