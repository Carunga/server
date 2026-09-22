# SlimProto ↔ Sendspin bridge — design & status

Canonical branch `feat/squeezelite-sendspin-bridge` (off upstream `dev`, fork
only, no PR yet).

## Branching

All Sendspin bridge changes live **only** on this branch, so a future PR is
independent of the other Squeezelite work:

- Base: upstream `dev`; the provider manifest keeps `aioslimproto==3.2.2` so the
  PR does not depend on the patched fork. Locally the dev container gets the
  sync primitives through its `PYTHONPATH=/opt/patched` overlay.
- Testing/integration: `feat/squeezelite-all` merges this branch together with
  `feat/squeezelite-menus`; it is a test target only, never a source.
- `feat/squeezelite-menus` must stay free of `sendspin_bridge.py`.
- The upstream Sendspin provider (`providers/sendspin/**`,
  `providers/sendspin_source/**`, `aiosendspin`) is **not modified** — the
  bridge only calls its public API.

## Goal

Let existing Squeezebox/Squeezelite players take part in **Sendspin-synchronized**
groups, with Sendspin as the timing master and the SlimProto device as the output.

## Precision expectation (important)

SlimProto is a **method-2** (server-corrected) protocol: the player gets a plain
byte stream, reports back only `jiffies` (1 kHz) and `elapsed_ms`, and has no
frame timestamps or client-side scheduling. Sendspin is **method-1**
(timestamped frames, client-side clock sync, drop/inject). The asymmetry means:

- **Strict / sample-accurate sync is not achievable** with SlimProto-only changes.
  That needs a Sendspin client on the endpoint (new firmware/hardware).
- This bridge can only ever be **near-synced**: start within ~5–20 ms, steady
  state ~10–50 ms (WiFi worse), glitch-free only with rate steering.

## Architecture

Mirrors the AirPlay bridge (`providers/airplay/sendspin_bridge.py`) and the shared
framework in `providers/sendspin/`:

1. **Registration** — the bridge registers each Squeezelite player as an external
   Sendspin client (`SendspinServer.register_external_player`) using
   `spb_<mac-without-colons>` as the client_id.
2. **Protocol linking** — before registration it calls
   `SendspinProvider.register_bridge_identifiers({MAC_ADDRESS: <mac>})` and
   `register_bridge_underlying_player(client_id, <squeezelite player_id>)`, so the
   resulting `SendspinPlayer` is parented to the native `SqueezelitePlayer`.
3. **Bridge role** — `BridgePlayerRole` (`providers/sendspin/bridge_role.py`)
   receives the group's audio, volume and mute changes; the bridge forwards
   volume/mute to the Squeezelite player.
4. **Timing** — `BridgePlayerRole.set_timing(required_lead_time_ms, min_buffer_ms)`
   tells Sendspin how far ahead to schedule the first chunk and how much to keep
   buffered. SlimProto's default output threshold is ~200 KB (~1.1 s at
   44.1 kHz/16-bit stereo), hence the cold lead constant.
5. **Lifecycle** — `SendspinBridgeManager` (subclass of
   `SendspinBridgeManagerBase`) reconciles bridges on player register/enable,
   config change and provider (un)load; wired into `SqueezelitePlayerProvider`.

## Status

**Phase 1 — done (this branch)**

- Registration + protocol linking + lifecycle.
- Bridge role wiring (audio/volume/mute/stream start/end/explicit stop callbacks).
- Volume/mute forwarding both ways.
- Timing values pushed to the role.
- Gated behind the (default **off**) `sendspin_bridge` provider config option, so
  enabling it cannot silently take players away from native SlimProto playback.
- Import-validated against `aiosendspin==9.1.1` + `music-assistant-models==1.1.212`;
  `ruff check`/`ruff format` clean.

**Phase 2 — audio path (first cut done)**

- Per-player **PCM HTTP source** served by the provider dynamic route
  `/slimproto/sendspin?player_id=<mac>` (`SqueezelitePlayerProvider._serve_sendspin_stream`
  → `SendspinSqueezeliteBridge.serve_pcm_stream`). The bridge fills a bounded
  `asyncio.Queue` from `BridgePlayerRole.on_audio_chunk`; the HTTP source drains
  it and advertises `audio/pcm;rate=44100;channels=2;bitrate=16`, which
  aioslimproto turns into the SlimProto codec message. Each stream gets its own
  queue so a stale handler can never steal the next stream's chunks.
- The first chunk starts the SlimProto transport (`play_url` on the bridge URL)
  with `autostart=False`, so the device buffers but does not start on its own
  (the player skips its buffer-ready auto-unpause while the bridge owns the
  start).
- **Start anchor:** the first chunk's `AudioChunk.timestamp_us` is mapped to a
  unix instant with `sendspin_audible_unix()`; the transport is then scheduled
  with `unpause_at(jiffies + delay)` so the first sample becomes audible at the
  Sendspin instant. This replaces the previous "start when the device buffer
  fills" behaviour, which played ~1 s early.
- Backpressure: the queue is bounded; on overflow the oldest chunk is dropped
  (a short gap instead of unbounded latency). Sendspin already paces delivery
  against the group timeline, so no extra client-side pacing is applied yet.
- Still TODO (finishing Phase 2): tune the lead/buffer constants from
  measurement and verify the anchor on hardware.

**Phase 3 — drift correction (TODO)**

- Compare the player's reported position (aioslimproto `play_point`) to the
  Sendspin timeline; correct with skip/pause (LMS-style gates, reuse the constants
  from the sync work). Rate steering (per-player resampling) is the tighter,
  glitch-free follow-up.

**Phase 4 — robustness (TODO)**

- Stream end/seek/next, late join, group add/remove, reconnect, leader handoff.
- Ensure a bridged player is excluded from native SlimProto sync groups.

## Dependency on the patched aioslimproto

Precise start alignment and drift correction rely on the server-side sync
primitives added in the `better-squeeze-sync` branch of `aioslimproto`
(`track_jiffies_epoch`, `play_point`, `start_at`, `skip_over`, `pause_for`).
The `dev` branch pins stock `aioslimproto==3.2.2`, so Phase 2/3 must either
depend on the fork or vendor those primitives.

## Decisions

- Near-sync accepted (strict requires new endpoints).
- Correction strategy: **skip/pause first**, rate steering later.
- PCM source: provider dynamic route (not generalized into the streams controller).
- Test endpoint: `sendspin-cli` / MA web player for now.
- Bridged players are excluded from native SlimProto sync groups.

## Testing plan

- **L1**: squeezelite containers + a Sendspin endpoint (`sendspin-cli` on the host,
  or the MA web player in a browser on a laptop with audio).
- **L2/L3**: real squeezelite/Radio + a Sendspin member; two-endpoint recording to
  measure start offset and drift over 10 minutes.

## Risks

- Method-2 precision ceiling (cannot be strict).
- HTTP-source latency/pacing; WiFi jitter.
- Grouping/lifecycle edge cases (one transport per player at a time).
