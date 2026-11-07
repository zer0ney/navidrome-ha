# Live Acceptance Checklist

These are release gates to run on an explicitly authorized **test** instance.
They are not a claim that a user's installation has been tested. Do not change
production configuration or submit synthetic playback/scrobble reports there.

## Protocol and State

1. Use released Navidrome 0.64.2 and HA 2026.11.x. Record exact server, HA and
   client versions separately from source inspection and simulated tests.
2. Confirm the native login, complete admin roster, `text/event-stream`, initial
   `serverStart` (including its version/commit suffix), 15-second `keepAlive`, and
   successful all-user `getNowPlaying` with the read-only probe.
3. With test accounts and a known track, submit controlled `reportPlayback`
   playing, same-count skip, paused, seek/resume, and stopped reports. Verify each
   meaningful server-visible change invalidates the snapshot, even at equal counts.
   If pause/skip yields no usable snapshot, native SSE is insufficient for that
   server/client combination. Do not claim compatibility for it.
4. Create two users with two clients each and leave an additional user idle. Verify
   exactly one entity per user and that user A changes do not alter user B.
5. Seek/pause on the older concurrent session; it should become selected, even
   when another session is playing. Check source-order ties and reconnect fallback.
   Use identical client labels too; session counts must not silently collapse.
6. Rename a user, create/delete accounts, and switch clients. Check stable unique
   IDs, dynamic user additions, deleted-account unavailability and new native IDs
   for same-name replacement accounts. Reconfigure the URL and reload HA; existing
   entity IDs must remain intact.

## Availability, Security and Images

1. Restart Navidrome, interrupt the stream, reload/unload HA, and allow silent
   stream timeouts. Check immediate unavailability on known connection loss,
   cancellation of owned work, bounded reconnect backoff and fresh snapshots.
2. Use a test reverse proxy with buffering disabled, a nonempty BasePath, restart,
   HTML fallback, redirect and timeout scenarios. Bad TLS must fail closed. Check
   that no authenticated request follows a redirect to another host.
3. Expire/revoke tokens, change the admin password, and remove admin permission.
   Verify refreshed native tokens are reused and rejected credentials start HA
   reauthentication. Never put actual credentials in published fixtures or logs.
4. Drop an invalidation without disconnecting and verify 60-second reconciliation.
   A successful empty snapshot must be idle; a failed snapshot must be unavailable.
5. Load album artwork from a dashboard browser that cannot reach Navidrome.
   Confirm only HA's media image proxy is used, no Navidrome auth token appears in
   state attributes/URLs, and slow artwork never delays metadata publication.

## Reporting and Latency

1. Include a modern reportPlayback client and a legacy scrobble-only client.
   Document which states each actually reports. Test silent disappearance and
   expiry without claiming it proves an explicit stop.
2. Timestamp server playback report, SSE receipt, snapshot completion and HA state
   event separately. Target less than 500 ms integration-added latency on healthy
   LAN, excluding client/network/server delay. Report measured distributions,
   not a guarantee. Repeated rapid skips should promptly finish at the final track.
3. Check native HA local progress animation, paused/resumed position timestamps,
   missing positions and non-unit speeds. Recorder should not receive per-second
   progress writes or unchanged heartbeat writes.
4. Run all unit/integration tests, Ruff, and hosted hassfest/HACS actions before
   release. Supply the actual repository documentation/issue links and maintainer
   codeowners; no hosted validation can run in this currently unhosted workspace.

Source anchors: [SSE broker](https://github.com/navidrome/navidrome/blob/v0.64.2/server/events/sse.go),
[play tracker](https://github.com/navidrome/navidrome/blob/v0.64.2/core/scrobbler/play_tracker.go),
[snapshot mapping](https://github.com/navidrome/navidrome/blob/v0.64.2/server/subsonic/album_lists.go),
[native auth](https://github.com/navidrome/navidrome/blob/v0.64.2/server/auth.go),
[release version format](https://github.com/navidrome/navidrome/blob/v0.64.2/consts/version.go).