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
