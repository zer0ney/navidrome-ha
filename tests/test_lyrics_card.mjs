import assert from "node:assert/strict";
import test from "node:test";
import {
  activeLyricIndex, lyricsView, NavidromeLyricsCard, normalizeCardConfig, playbackPosition,
} from "../custom_components/navidrome/frontend/navidrome-lyrics-card.js";

const now = Date.parse("2026-11-07T12:00:10Z");
const player = {
  state: "playing",
  attributes: {
    media_content_id: "track-1",
    media_position: 10,
    media_position_updated_at: "2026-11-07T12:00:00Z",
    media_duration: 100,
    playback_rate: 1,
  },
};
const lyrics = {
  song_id: "track-1", synced: true,
  line: [{ start: 1000, value: "First" }, { start: 5000, value: "Second" }, { start: 10000, value: "Third" }],
};

test("playing positions extrapolate from the server timestamp", () => {
  assert.equal(playbackPosition(player, now), 20);
});
test("paused positions do not advance", () => {
  assert.equal(playbackPosition({ ...player, state: "paused" }, now), 10);
});
test("seek reports replace the previous anchor", () => {
  assert.equal(playbackPosition({ ...player, attributes: { ...player.attributes, media_position: 60 } }, now), 70);
});
test("a new reported position immediately resynchronizes the active lyric", () => {
  const reported = { ...player, attributes: { ...player.attributes, media_position: 2, media_position_updated_at: new Date(now).toISOString() } };
  assert.equal(activeLyricIndex(lyrics, playbackPosition(player, now)), 2);
  assert.equal(activeLyricIndex(lyrics, playbackPosition(reported, now)), 0);
});
test("playback rate and duration are respected", () => {
  assert.equal(playbackPosition({ ...player, attributes: { ...player.attributes, playback_rate: 2 } }, now), 30);
  assert.equal(playbackPosition(player, now + 200000), 100);
});
test("unknown positions are not invented", () => {
  assert.equal(playbackPosition({ state: "playing", attributes: {} }, now), null);
});
test("future server timestamps do not rewind playback", () => {
  assert.equal(playbackPosition(player, now - 20000), 10);
});
test("the exact lyric boundary selects the new line", () => {
  assert.equal(activeLyricIndex(lyrics, .5), -1);
  assert.equal(activeLyricIndex(lyrics, 4.999), 0);
  assert.equal(activeLyricIndex(lyrics, 5), 1);
  assert.equal(activeLyricIndex(lyrics, 10), 2);
});
test("positive OpenSubsonic offsets show lyrics sooner", () => {
  assert.equal(activeLyricIndex({ ...lyrics, offset: 1000 }, 4), 1);
  assert.equal(activeLyricIndex({ ...lyrics, offset: -1000 }, 5), 0);
  assert.equal(activeLyricIndex(lyrics, 4, 1000), 1);
});
test("unsynced lyrics and unknown positions have no active line", () => {
  assert.equal(activeLyricIndex({ ...lyrics, synced: false }, 20), -1);
  assert.equal(activeLyricIndex(lyrics, null), -1);
});
test("duplicate timestamps select the last simultaneous line", () => {
  assert.equal(activeLyricIndex({ ...lyrics, line: [{ start: 0 }, { start: 0 }] }, 0), 1);
});
test("stale lyrics never appear on a new song", () => {
  const entity = { ...player, attributes: { ...player.attributes, media_content_id: "track-2", lyrics_status: "synced", lyrics } };
  assert.deepEqual(lyricsView(entity), { status: "loading", lyrics: null });
});
test("synced and unsynced data remain distinct", () => {
  for (const synced of [true, false]) {
    const status = synced ? "synced" : "unsynced";
    const entity = { ...player, attributes: { ...player.attributes, lyrics_status: status, lyrics: { ...lyrics, synced } } };
    assert.equal(lyricsView(entity).status, status);
  }
});
test("missing, loading, error, idle and unavailable views are explicit", () => {
  assert.equal(lyricsView(player).status, "none");
  for (const status of ["loading", "error"]) {
    assert.equal(lyricsView({ ...player, attributes: { ...player.attributes, lyrics_status: status } }).status, status);
  }
  assert.equal(lyricsView({ ...player, state: "idle" }).status, "idle");
  assert.equal(lyricsView({ ...player, state: "unavailable" }).status, "unavailable");
  assert.equal(lyricsView(undefined).status, "unavailable");
});
test("the lyrics-only card config has no artwork or metadata options", () => {
  const entity = { ...player, entity_id: "media_player.navidrome", attributes: { ...player.attributes, lyrics_status: "synced" } };
  assert.deepEqual(NavidromeLyricsCard.getStubConfig({ states: { [entity.entity_id]: entity } }), {
    entity: entity.entity_id, lyrics_offset: 0, show_buttons: true, show_background: true, text_align: "left",
  });
});

test("display options preserve the existing appearance by default", () => {
  assert.deepEqual(normalizeCardConfig({ entity: "media_player.navidrome" }), {
    entity: "media_player.navidrome", lyrics_offset: 0,
    show_buttons: true, show_background: true, text_align: "left",
  });
});
test("buttons and background can be disabled independently", () => {
  for (const show_buttons of [true, false]) {
    for (const show_background of [true, false]) {
      const config = normalizeCardConfig({ entity: "media_player.navidrome", show_buttons, show_background });
      assert.equal(config.show_buttons, show_buttons);
      assert.equal(config.show_background, show_background);
    }
  }
});
test("all three text alignments are accepted", () => {
  for (const text_align of ["left", "center", "right"]) {
    assert.equal(normalizeCardConfig({ entity: "media_player.navidrome", text_align }).text_align, text_align);
  }
});
test("invalid display options are rejected", () => {
  for (const options of [{ show_buttons: "false" }, { show_background: 0 }, { text_align: "justify" }]) {
    assert.throws(() => normalizeCardConfig({ entity: "media_player.navidrome", ...options }));
  }
});
test("undefined options retain their defaults", () => {
  const config = normalizeCardConfig({ entity: "media_player.navidrome", show_buttons: undefined, text_align: undefined });
  assert.equal(config.show_buttons, true);
  assert.equal(config.text_align, "left");
});