# Navidrome Now Playing for Home Assistant

Quick connector for catching now playing events from Navidrome over SSE for quicker HA player reporting. One shotted with 6.1 Sol, I can't guarantee the quality of this but it does work :)

## Install

For local installation, place this repository's `custom_components/navidrome` directory inside HA's configuration directory at `custom_components/navidrome`, then restart Home Assistant.

For HACS/Marketplace, add this repository under three dots > Custom repositories and then install the integration.

After installing, in **Settings > Devices & Services > Add Integration**,
select **Navidrome Now Playing** and enter:

- Server base URL, for example `https://music.example.com` or
  `https://example.com/music` when using Navidrome's BasePath.
- Administrator username and password.

## How to use
Each user gets added as its own player entity in HA. The most recently interacted client is what shows - so pausing on one client and then immediately skipping a song on another will show the latter client.

## Static Cover Art

The player's normal `entity_picture` still uses Navidrome's artwork, including
animated WebP. A separate `cover_art_static` attribute exposes the current album's
public [Cover Art Archive](https://musicbrainz.org/doc/Cover_Art_Archive/API) URL:

```text
https://coverartarchive.org/release/{release-mbid}/front
```

The archive requires a MusicBrainz **release ID**, not a song/recording ID. The
integration obtains the selected track's `albumId`, looks up its album through
Navidrome's `getAlbum` endpoint, and uses the album's `musicBrainzId`. Albums must
have the corresponding MusicBrainz release tag in Navidrome.

The lookup runs in the background and checks availability with a HEAD request,
without downloading another image into HA. Requests are deduplicated per album;
successful lookups are cached for 24 hours, missing covers/IDs for one hour, and
temporary errors for five minutes, with at most 128 cached albums. Archive errors
do not make playback unavailable. No Navidrome credentials are sent to the archive.

`cover_art_static` is `null` while loading, when the album ID or release MBID is
missing/invalid, or when no cover can be retrieved. Switching albums never keeps
the previous album's static cover. A dashboard using this URL needs internet
access to the Cover Art Archive and its image host.

For templates:

```jinja
{{ state_attr('media_player.navidrome_alice', 'cover_art_static') }}
```

## Lyrics

The integration looks up the selected song with Navidrome's
`getLyricsBySongId` endpoint and publishes two player attributes:

- `lyrics_status`: `synced`, `unsynced`, or `none`. During a lookup this is
  `loading`; a temporary lookup failure is `error`, not `none`.
- `lyrics`: a structured object containing `song_id`, `synced`, `line`, and
  optional language/display fields and timing `offset`, or `null` when unavailable.
  Each line contains `value` and, for synced lyrics, `start` in milliseconds.

Lookups run separately from playback updates and are shared across users playing
the same song. Results are cached for one hour, missing lyrics for five minutes,
and errors for 30 seconds before a later snapshot can retry. Up to 64 songs are
cached, with each lyrics payload limited to 64 KiB. Changing tracks immediately
clears the previous song's lyrics from the player attributes.

### Lyrics-only Lovelace Card

The bundled card displays **only lyrics**, without artwork, artist, title,
album, or playback controls. Synced lyrics follow the reported playback position;
unsynced lyrics remain manually scrollable. Missing lyrics have an explicit empty
state. If the client does not report position, no timed highlight is invented.
Synchronization uses the configured player's `media_position` and
`media_position_updated_at`. Position advances locally only while playing; a new
report, seek, or pause immediately updates the highlighted line.

1. Update the installed integration and restart Home Assistant.
2. In **Settings > Dashboards > Resources**, add
  `/navidrome/lyrics-card.js?v=2` as a **JavaScript Module**. Resources may require
   Advanced Mode in your user profile.
3. Add **Navidrome Lyrics** from the card picker, or use:

```yaml
type: custom:navidrome-lyrics-card
entity: media_player.navidrome_alice
lyrics_offset: 0
```

The visual editor offers the player entity, timing offset, button/background
switches, and text alignment. `lyrics_offset`
is in milliseconds; positive values show lines sooner and negative values later,
in addition to any offset supplied by Navidrome. Manually scrolling suspends
auto-follow; the crosshair button resumes it. With buttons hidden, following
resumes automatically five seconds after the last manual scroll interaction.
The other button toggles fullscreen.
The card respects the HA theme and reduced-motion preferences.

| Option | Default | Behavior |
| --- | --- | --- |
| `show_buttons` | `true` | Show or hide the entire button toolbar, including its space. |
| `show_background` | `true` | When false, remove the background, border, shadow, edge fade, and scrollbar chrome. |
| `text_align` | `left` | Align lyrics `left`, `center`, or `right`. |

For a bare, centered lyrics display:

```yaml
type: custom:navidrome-lyrics-card
entity: media_player.navidrome_alice
show_buttons: false
show_background: false
text_align: center
```

The module is served by this integration, so no separate `www` copy, frontend
package installation, or external CDN is needed. Navidrome credentials remain
server-side. After updating the card, increment the resource URL's `v` query value
if a browser continues using an older cached version.

## Development

```sh
uv run pytest -q
node --test tests/test_lyrics_card.mjs
uv run ruff check .
uv run ruff format --check .
```

The standalone card preview uses synthetic lyrics and supports all lookup states:

```sh
uv run python -m http.server 8775 --bind 127.0.0.1
```

Open `http://127.0.0.1:8775/tools/lyrics-card-preview.html`.
