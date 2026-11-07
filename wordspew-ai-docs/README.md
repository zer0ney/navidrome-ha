# Navidrome Now Playing for Home Assistant

A read-only custom integration with **one persistent `media_player` per Navidrome
user**, including idle users. All listening clients share one native SSE connection.
No plugin, sidecar, MQTT broker, WebSocket server, or custom dashboard is required.

## Requirements and Privacy

- Home Assistant **2026.11.0 or newer**, using its Python 3.14 runtime.
- Stable Navidrome **0.64.2 or newer**, with `EnableNowPlaying=true` and
  `DevActivityPanel=true`. These are enabled by default in the source baseline.
- A **native administrator username and password**. External-auth-only/SSO login
  without native credentials is not supported. Non-admin accounts are rejected;
  their roster and library visibility cannot guarantee all-user coverage.
- Network access from HA to the server and a valid TLS certificate for HTTPS.

**All users' listening activity is exposed to HA and can enter its recorder/history.**
Apply your usual dashboard permissions, recorder exclusions, and retention policy.
The integration only logs in and reads users, events, snapshots and cover art. The
stored credential nevertheless retains full Navidrome administrator privileges.
Protect HA configuration storage and backups. No credentials or Navidrome auth
tokens are put in entity attributes or browser artwork URLs. HTTPS is recommended;
HTTP sends login credentials over the network unencrypted. Certificate verification
is always enabled. Requests never follow redirects with authentication headers.

## Install

For local installation, place this repository's `custom_components/navidrome`
directory inside HA's configuration directory at `custom_components/navidrome`,
then restart Home Assistant. In **Settings > Devices & Services > Add Integration**,
select **Navidrome Now Playing** and enter:

- Server base URL, for example `https://music.example.com` or
  `https://example.com/music` when using Navidrome's BasePath.
- Administrator username and password.

Do not append `/app`, `/api`, `/rest`, or an authentication query. Do not embed
credentials in the URL. Setup probes the actual SSE stream and initial snapshot.

The repository uses HACS' integration layout. Once hosted on GitHub, add that
repository URL to HACS' **Custom repositories** with category **Integration**.
It is not claimed to be listed or approved by HACS. Before public distribution,
set the manifest documentation URL to the hosted integration README, add the real
maintainer's codeowner and issue tracker, enable GitHub Actions, and publish a
versioned release. The current documentation URL points to upstream server docs;
no unverified GitHub repository URL or maintainer identity is embedded here.

Use the integration's **Reconfigure** menu for a URL or login change. Entity unique
IDs are `<config-entry-id>:<native-user-id>`, so reconfiguration and user renames
do not replace them. Use reconfiguration only for the same logical server. A
different server can have colliding native user IDs. Deleting and recreating the
HA config entry intentionally creates a new identity namespace. Duplicate
normalized URLs are rejected, but aliases of the same server cannot be detected.
Revoked credentials prompt HA reauthentication.

## What Is Displayed

Each entity exposes reported state, title, artist, album, album artist, duration,
artwork and supplied playback position. It has **no player controls** and does not
play audio. Additional attributes include username, source-client label, active
session count, playback rate and last observed meaningful change.

- `starting` becomes `buffering`; `playing` and `paused` retain those meanings.
- A successful empty snapshot is `idle` and clears media metadata.
- Missing or unrecognized reported state is `unknown`, not an invented pause/stop.
- A failed snapshot or known lost SSE stream makes entities `unavailable`.
- Missing positions are omitted. Positions at non-unit or missing playback rates
  are omitted because the native HA frontend assumes normal speed. Playback rate
  remains visible separately.
- Normal progress is interpolated by HA from a fixed position timestamp. There
  are no per-second state writes. Artwork is fetched separately through HA's image
  proxy with a bounded cache; fetching it does not block metadata publication.

Native HA card example (replace with the entity ID created in your installation):

```yaml
type: media-control
entity: media_player.navidrome_alice
```

### Concurrent Clients

When one user has several sessions, the display follows the **most recently
observed meaningful change**, even when that session is paused. Track changes,
new sessions, state/rate/metadata changes and seeks differing from expected
progress by more than two seconds count. Normal progress and unchanged reports do
not take selection away from another client.

Navidrome snapshots do not expose a stable client ID or last-report timestamp;
their `playerId` is only a snapshot index. Matching by client label, track, state
and position is heuristic. Duplicate labels are kept as a multiset, not overwritten.
Identical sessions cannot be distinguished. Multiple changes in one snapshot
prefer the existing selection when it also changed, otherwise source order
(newest started session first). Initial sync falls back to source order. Reconnect
preserves a matching prior selection; exact cross-client last-event ordering
cannot be reconstructed. If the chosen session disappears, another remaining
session is selected, or the user becomes idle. Expiry is not proof of explicit stop.

### Client Reporting Limits

The integration displays only what clients report to Navidrome. Modern
`reportPlayback` clients may report pause, resume, seek and speed; legacy
`scrobble(submission=false)` clients report playing only. Streaming/prefetch is not
proof of audible playback. A silent client can remain present until Navidrome
expires it (source baseline: approximately remaining playing duration plus five
seconds, or 30 minutes when paused, plus cleanup timing). No integration or plugin
can reconstruct actions the client never reported.

## SSE and Reverse Proxies

SSE is HTTP server push, not a literal WebSocket. Every `nowPlayingCount` event
invalidates the snapshot, **including repeated equal counts**. The reader never
waits for snapshot HTTP. One worker coalesces bursts with an immediate trailing
refresh, without an intentional debounce. Intermediate rapid skips may be missed;
the goal is current state, not playback history.

The stream can drop events and has no replay contract. A full snapshot every
60 seconds reconciles missed notifications. The complete paginated user roster
is refreshed every five minutes, on reconnect and when unknown usernames appear.
New users appear automatically; deleted users remain unavailable and their HA
registry entities may be removed. A newly created same-name account has a different
native ID and never inherits the deleted account's unique ID.

Allow long-lived GET requests to `[BasePath]/api/events`. Disable response buffering
and caching, preserve `X-ND-Authorization` request/response headers, and bypass
HTML/SSO login redirects for native authenticated API requests. Example Nginx
directives **inside your existing Navidrome proxy location**:

```nginx
proxy_http_version 1.1;
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 90s;
```

Preserve the configured BasePath in `proxy_pass` and pass both `/auth` and `/rest`
requests through that same path. The integration expects `serverStart` at connection
and a `keepAlive` roughly every 15 seconds. A 45-second complete-event watchdog
detects broken/buffered streams. Retry is approximately 1, 2, 4, ... 60 seconds with
jitter; reconnect always resnapshots. An HTTP 200 HTML page is rejected, not treated
as an empty now-playing list.

## Verification Status

| Surface | Evidence |
| --- | --- |
| Navidrome 0.64.2 | Released source inspected; source-shaped simulated HTTP/SSE contracts |
| HA 2026.11.0, Python 3.14.9 | Local executable unit and integration tests |
| User's actual server and clients | Not supplied; not live-tested |
| Older Navidrome / HA releases | Unsupported until separately tested |
| Later accepted stable servers | Capability-checked at setup, not a universal compatibility claim |
| HACS / hassfest hosted actions | Configured in CI; run when repository is published |

No live report-to-HA latency measurement is claimed. The design has no intentional
event debounce; the planned LAN target is less than 500 ms of integration-added
latency, excluding client reporting delay, server work and network delay. See
[docs/validation.md](docs/validation.md) for the remaining live acceptance procedure.

## Development

```sh
uv sync --frozen --group dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Only Python tooling is required; no Node/Yarn setup is needed. The lockfile pins
the HA test environment. Tests use an in-process Navidrome-shaped server with real
aiohttp sockets, not a live Navidrome installation. SSE tests include fragmented
UTF-8/CRLF/multiline data and oversized frames. `httpx-sse==0.4.3` supplies its
incremental decoder behind a bounded aiohttp adapter. That package's decoder API is
private, so its version is pinned and framing regressions must pass before upgrades.
`httpx` is an import-time dependency only; no second HTTP client is constructed.

Optional **read-only** probe from this checkout:

```sh
uv run python -m tools.probe https://music.example.com --username admin
```

The password is entered directly into a hidden terminal prompt, never as a command
argument. The probe prints only version, roster/session counts and event timing;
it never changes server settings or submits playback/scrobble reports.