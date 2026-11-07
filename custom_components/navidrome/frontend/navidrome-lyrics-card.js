export function playbackPosition(entity, now = Date.now()) {
  const attributes = entity?.attributes ?? {};
  if (!Number.isFinite(attributes.media_position)) return null;
  let position = attributes.media_position;
  const updated = Date.parse(attributes.media_position_updated_at);
  if (entity.state === "playing" && Number.isFinite(updated)) {
    const rate = Number.isFinite(attributes.playback_rate) ? attributes.playback_rate : 1;
    position += Math.max(0, now - updated) / 1000 * rate;
  }
  const duration = Number.isFinite(attributes.media_duration) ? attributes.media_duration : Infinity;
  return Math.max(0, Math.min(position, duration));
}

export function activeLyricIndex(lyrics, position, adjustment = 0) {
  if (!lyrics?.synced || !Number.isFinite(position)) return -1;
  const lines = lyrics.line ?? [];
  const offset = Number.isFinite(lyrics.offset) ? lyrics.offset : 0;
  const time = position * 1000 + offset + adjustment;
  let lower = 0;
  let upper = lines.length;
  while (lower < upper) {
    const middle = Math.floor((lower + upper) / 2);
    if (lines[middle].start <= time) lower = middle + 1;
    else upper = middle;
  }
  return lower - 1;
}

export function lyricsView(entity) {
  if (!entity || ["unavailable", "unknown"].includes(entity.state)) {
    return { status: "unavailable", lyrics: null };
  }
  const attributes = entity.attributes ?? {};
  if (!attributes.media_content_id || ["idle", "off"].includes(entity.state)) {
    return { status: "idle", lyrics: null };
  }
  const lyrics = attributes.lyrics;
  if (lyrics && lyrics.song_id !== attributes.media_content_id) {
    return { status: "loading", lyrics: null };
  }
  if (lyrics?.line?.length && ["synced", "unsynced"].includes(attributes.lyrics_status)) {
    return { status: lyrics.synced ? "synced" : "unsynced", lyrics };
  }
  const status = ["loading", "error"].includes(attributes.lyrics_status)
    ? attributes.lyrics_status : "none";
  return { status, lyrics: null };
}

const CARD_DEFAULTS = Object.freeze({
  lyrics_offset: 0, show_buttons: true, show_background: true, text_align: "left",
});

export function normalizeCardConfig(config) {
  if (typeof config?.entity !== "string" || !config.entity.startsWith("media_player.")) {
    throw new Error("Choose a media_player entity.");
  }
  const result = { ...CARD_DEFAULTS, ...config };
  for (const key of Object.keys(CARD_DEFAULTS)) {
    if (result[key] === undefined) result[key] = CARD_DEFAULTS[key];
  }
  if (!Number.isFinite(result.lyrics_offset)) {
    throw new Error("lyrics_offset must be a number in milliseconds.");
  }
  for (const key of ["show_buttons", "show_background"]) {
    if (typeof result[key] !== "boolean") throw new Error(`${key} must be true or false.`);
  }
  if (!["left", "center", "right"].includes(result.text_align)) {
    throw new Error("text_align must be left, center, or right.");
  }
  return result;
}

const styles = `
  :host { display: block; min-width: 0; container-type: inline-size; }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  .card {
    position: relative; display: flex; flex-direction: column;
    height: var(--navidrome-card-height, 480px); min-height: 240px; overflow: hidden;
    color: var(--primary-text-color, #fafaf7);
    background: var(--ha-card-background, var(--card-background-color, #232624));
    border: 1px solid var(--ha-card-border-color, #ffffff15);
    border-radius: var(--ha-card-border-radius, 8px);
    font-family: var(--ha-font-family-body, var(--primary-font-family, inherit));
    letter-spacing: 0; text-align: left;
  }
  .card[data-align="center"] { text-align: center; }
  .card[data-align="right"] { text-align: right; }
  .card.no-background {
    --ha-card-background: transparent; --ha-card-border-color: transparent;
    --ha-card-border-width: 0; --ha-card-box-shadow: none;
    background: none; border: 0; border-radius: 0; box-shadow: none;
  }
  .no-background .lyric-window { mask-image: none; -webkit-mask-image: none; scrollbar-width: none; }
  .no-background .lyric-window::-webkit-scrollbar { display: none; }
  .toolbar {
    display: flex; align-items: center; justify-content: flex-end; gap: 4px;
    min-height: 48px; padding: 6px 10px; flex-shrink: 0;
  }
  button {
    display: inline-flex; align-items: center; justify-content: center; width: 36px;
    height: 36px; padding: 0; border: 0; border-radius: 6px; color: inherit;
    background: transparent; cursor: pointer;
  }
  button:hover { background: #ffffff12; }
  button:focus-visible, .lyric-window:focus-visible { outline: 2px solid currentColor; outline-offset: -2px; }
  button[aria-pressed="true"] { background: #ffffff15; }
  button:disabled { cursor: default; opacity: .25; }
  button ha-icon { --mdc-icon-size: 20px; width: 20px; height: 20px; }
  .lyrics { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; position: relative; padding: 0 24px 16px; }
  .lyric-window {
    height: 100%; width: 100%; max-width: 760px; margin: 0 auto; min-height: 0; overflow-y: auto; overflow-x: hidden;
    scrollbar-width: thin; scrollbar-color: #ffffff25 transparent;
    overscroll-behavior: contain; scroll-behavior: smooth;
    mask-image: linear-gradient(transparent, #000 10%, #000 85%, transparent);
  }
  .lines { padding: 30% 8px 50%; }
  .line {
    margin: 0 0 22px; font-size: 30px; line-height: 1.4; font-weight: 750;
    opacity: .3; overflow-wrap: anywhere; white-space: pre-wrap;
    transition: opacity 350ms, transform 350ms; transform-origin: left center;
  }
  .line.past { opacity: .42; }
  .line.active { opacity: 1; transform: translateX(4px); }
  [data-align="center"] .line { transform-origin: center; }
  [data-align="center"] .line.active { transform: none; }
  [data-align="right"] .line { transform-origin: right center; }
  [data-align="right"] .line.active { transform: translateX(-4px); }
  .unsynced .lines { padding: 28px 8px 70px; }
  .unsynced .line { opacity: .85; font-size: 25px; }
  .empty { display: flex; flex: 1; flex-direction: column; justify-content: center; align-items: flex-start; gap: 16px; text-align: inherit; padding: 24px; }
  [data-align="center"] .empty { align-items: center; }
  [data-align="right"] .empty { align-items: flex-end; }
  .empty ha-icon { --mdc-icon-size: 42px; width: 42px; height: 42px; opacity: .25; }
  .empty p { margin: 0; opacity: .68; font-size: 15px; line-height: 1.5; }
  .empty[data-status="loading"] ha-icon { animation: breathe 1.8s ease-in-out infinite alternate; }
  .card:fullscreen { width: 100vw; height: 100dvh; border-radius: 0; border: 0; }
  .card:fullscreen .lyrics { padding: 0 max(24px, 6vw) 24px; }
  .card:fullscreen .line { font-size: 38px; }
  @keyframes breathe { from { opacity: .15; } to { opacity: .55; } }
  @container (max-width: 620px) {
    .lyrics { padding: 0 16px 16px; }
    .line { font-size: 27px; margin-bottom: 20px; }
    .unsynced .line { font-size: 23px; }
    .lines { padding-top: 40%; }
    .card:fullscreen .line { font-size: 28px; }
  }
  @container (max-width: 340px) {
    .line { font-size: 24px; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
  }
`;

const ElementBase = globalThis.HTMLElement ?? class {};

export class NavidromeLyricsCard extends ElementBase {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>${styles}</style>
      <ha-card class="card" aria-label="Lyrics">
        <header class="toolbar">
          <button class="follow" title="Follow current lyric" aria-label="Follow current lyric" aria-pressed="true"><ha-icon icon="mdi:crosshairs-gps"></ha-icon></button>
          <button class="fullscreen" title="Fullscreen" aria-label="Fullscreen"><ha-icon icon="mdi:fullscreen"></ha-icon></button>
        </header>
        <section class="lyrics" aria-label="Lyrics">
          <div class="lyric-window" tabindex="0" aria-label="Lyrics"><div class="lines"></div></div>
          <div class="empty" role="status" hidden><ha-icon icon="mdi:format-quote-close"></ha-icon><p></p></div>
        </section>
      </ha-card>`;
    this._elements = {};
    for (const name of ["card", "toolbar", "lyrics", "lyric-window", "lines", "empty", "follow", "fullscreen"]) {
      this._elements[name] = this.shadowRoot.querySelector(`.${name}`);
    }
    this._following = true;
    this._active = -1;
    this._lineElements = [];
    this._elements.follow.addEventListener("click", () => this._resumeFollow());
    this._elements.fullscreen.hidden = !this._elements.card.requestFullscreen;
    this._elements.fullscreen.addEventListener("click", () => this._toggleFullscreen());
    const manualScroll = () => {
      clearTimeout(this._followTimer);
      this._followTimer = null;
      this._following = false;
      this._updateFollow();
      if (!this._config?.show_buttons && this._view?.status === "synced") {
        this._followTimer = setTimeout(() => this._resumeFollow(), 5000);
      }
    };
    for (const event of ["wheel", "touchstart", "pointerdown"]) {
      this._elements["lyric-window"].addEventListener(event, manualScroll, { passive: true });
    }
    this._elements["lyric-window"].addEventListener("keydown", (event) => {
      if (["ArrowDown", "ArrowUp", "PageDown", "PageUp", "Home", "End", " "].includes(event.key)) manualScroll();
    });
    this._visibility = () => this._updateTimer();
    this._resize = new ResizeObserver(() => {
      if (this._following) this._scrollActive(false);
    });
    this._fullscreenChanged = () => {
      const active = this.shadowRoot.fullscreenElement === this._elements.card;
      this._elements.fullscreen.querySelector("ha-icon").setAttribute("icon", active ? "mdi:fullscreen-exit" : "mdi:fullscreen");
      this._elements.fullscreen.title = active ? "Exit fullscreen" : "Fullscreen";
      this._elements.fullscreen.setAttribute("aria-label", this._elements.fullscreen.title);
      this._scrollActive(false);
    };
  }

  setConfig(config) {
    this._config = normalizeCardConfig(config);
    clearTimeout(this._followTimer);
    this._followTimer = null;
    if (!this._config.show_buttons) this._following = true;
    this._render();
    if (this._following) this._scrollActive(false);
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  connectedCallback() {
    document.addEventListener("visibilitychange", this._visibility);
    document.addEventListener("fullscreenchange", this._fullscreenChanged);
    this._resize.observe(this._elements["lyric-window"]);
    this._render();
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    clearTimeout(this._followTimer);
    this._timer = null;
    this._followTimer = null;
    this._resize.disconnect();
    document.removeEventListener("visibilitychange", this._visibility);
    document.removeEventListener("fullscreenchange", this._fullscreenChanged);
  }

  getCardSize() { return 9; }
  getGridOptions() { return { columns: 12, min_columns: 3, rows: 9, min_rows: 5 }; }

  static getConfigElement() {
    return document.createElement("navidrome-lyrics-card-editor");
  }

  static getStubConfig(hass) {
    const players = Object.values(hass?.states ?? {}).filter((entity) => entity.entity_id.startsWith("media_player."));
    const player = players.find((entity) => "lyrics_status" in entity.attributes) ?? players[0];
    return { entity: player?.entity_id ?? "", ...CARD_DEFAULTS };
  }

  _render() {
    if (!this._config) return;
    const entity = this._hass?.states?.[this._config.entity];
    this._entity = entity;
    const attributes = entity?.attributes ?? {};
    const view = lyricsView(entity);
    this._elements.toolbar.hidden = !this._config.show_buttons;
    this._elements.card.classList.toggle("no-background", !this._config.show_background);
    this._elements.card.dataset.align = this._config.text_align;
    const track = attributes.media_content_id;
    if (track !== this._track) {
      clearTimeout(this._followTimer);
      this._followTimer = null;
      this._track = track;
      this._following = true;
      this._active = -1;
      this._elements["lyric-window"].scrollTop = 0;
    }
    const signature = JSON.stringify([track, view.status, view.lyrics]);
    this._view = view;
    if (signature !== this._signature) {
      this._signature = signature;
      this._active = -1;
      this._lineElements = [];
      this._elements.lines.replaceChildren();
      this._elements.lyrics.classList.toggle("unsynced", view.status === "unsynced");
      this._elements["lyric-window"].hidden = !view.lyrics;
      this._elements.empty.hidden = Boolean(view.lyrics);
      this._elements.empty.dataset.status = view.status;
      if (view.lyrics) {
        const fragment = document.createDocumentFragment();
        for (const line of view.lyrics.line) {
          const paragraph = document.createElement("p");
          paragraph.className = "line";
          paragraph.textContent = line.value || "...";
          fragment.append(paragraph);
          this._lineElements.push(paragraph);
        }
        this._elements.lines.append(fragment);
        this._elements["lyric-window"].scrollTop = 0;
      } else {
        const messages = { loading: "Loading lyrics", none: "No lyrics available", error: "Lyrics unavailable", idle: "Nothing playing", unavailable: "Player unavailable" };
        this._elements.empty.querySelector("p").textContent = messages[view.status] ?? messages.none;
      }
    }
    this._updateFollow();
    this._tick();
    this._updateTimer();
  }

  _tick() {
    if (!this._config) return;
    const position = playbackPosition(this._entity);
    const active = activeLyricIndex(this._view?.lyrics, position, this._config.lyrics_offset);
    if (active !== this._active) {
      this._active = active;
      const activeStart = this._view?.lyrics?.line[active]?.start;
      this._lineElements.forEach((element, index) => {
        const current = active >= 0 && this._view.lyrics.line[index].start === activeStart;
        element.classList.toggle("active", current);
        element.classList.toggle("past", index < active && !current);
        if (current) element.setAttribute("aria-current", "true");
        else element.removeAttribute("aria-current");
      });
      if (this._following) this._scrollActive();
    }
  }

  _scrollActive(smooth = true) {
    const element = this._lineElements[this._active];
    if (!element) return;
    const viewport = this._elements["lyric-window"];
    const reduced = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    viewport.scrollTo({ top: element.offsetTop - viewport.offsetTop - viewport.clientHeight * .42 + element.clientHeight / 2, behavior: smooth && !reduced ? "smooth" : "instant" });
  }

  _resumeFollow() {
    clearTimeout(this._followTimer);
    this._followTimer = null;
    this._following = true;
    this._updateFollow();
    this._scrollActive();
  }

  _updateFollow() {
    this._elements.follow.disabled = this._view?.status !== "synced" || playbackPosition(this._entity) === null;
    this._elements.follow.setAttribute("aria-pressed", String(this._following));
    this._elements.follow.title = this._following ? "Following current lyric" : "Resume following lyrics";
    this._elements.follow.setAttribute("aria-label", this._elements.follow.title);
  }

  _updateTimer() {
    const running = this.isConnected && !document.hidden && this._entity?.state === "playing"
      && this._view?.status === "synced" && playbackPosition(this._entity) !== null;
    if (running && !this._timer) this._timer = setInterval(() => this._tick(), 200);
    if (!running && this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
    if (running) this._tick();
  }

  async _toggleFullscreen() {
    try {
      if (this.shadowRoot.fullscreenElement) await document.exitFullscreen();
      else await this._elements.card.requestFullscreen();
    } catch {
      this.dispatchEvent(new CustomEvent("hass-notification", {
        detail: { message: "Fullscreen is unavailable in this browser." }, bubbles: true, composed: true,
      }));
    }
  }
}

class NavidromeLyricsCardEditor extends ElementBase {
  setConfig(config) { this._config = config; this._render(); }
  set hass(hass) { this._hass = hass; this._render(); }

  _render() {
    if (!this._config || !this._hass) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.schema = [
        { name: "entity", required: true, selector: { entity: { domain: "media_player" } } },
        { name: "show_buttons", selector: { boolean: {} } },
        { name: "show_background", selector: { boolean: {} } },
        { name: "text_align", selector: { select: { mode: "dropdown", options: [
          { value: "left", label: "Left" }, { value: "center", label: "Center" }, { value: "right", label: "Right" },
        ] } } },
        { name: "lyrics_offset", selector: { number: { min: -10000, max: 10000, step: 100, unit_of_measurement: "ms", mode: "box" } } },
      ];
      const labels = {
        entity: "Navidrome player", lyrics_offset: "Lyrics timing offset", show_buttons: "Show buttons",
        show_background: "Show background", text_align: "Text alignment",
      };
      this._form.computeLabel = (field) => labels[field.name] ?? field.name;
      this._form.addEventListener("value-changed", (event) => {
        event.stopPropagation();
        this.dispatchEvent(new CustomEvent("config-changed", {
          detail: { config: { ...this._config, ...event.detail.value } }, bubbles: true, composed: true,
        }));
      });
      this.append(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = { ...CARD_DEFAULTS, ...this._config };
  }
}

if (globalThis.customElements) {
  if (!customElements.get("navidrome-lyrics-card")) customElements.define("navidrome-lyrics-card", NavidromeLyricsCard);
  if (!customElements.get("navidrome-lyrics-card-editor")) customElements.define("navidrome-lyrics-card-editor", NavidromeLyricsCardEditor);
  globalThis.customCards ??= [];
  if (!globalThis.customCards.some((card) => card.type === "navidrome-lyrics-card")) {
    globalThis.customCards.push({ type: "navidrome-lyrics-card", name: "Navidrome Lyrics", description: "A lyrics-only view with synchronized highlighting.", preview: true });
  }
}