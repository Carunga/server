"""
Sendspin bridge for Squeezelite players (WIP).

Registers a Squeezelite (slimproto) player as an external Sendspin client so it
can take part in Sendspin synchronized playback, with Sendspin as the timing
master and the SlimProto device as the output.

Status: Phase 2/3 in progress. Registration, protocol linking, lifecycle, volume/mute
wiring (Phase 1) and the PCM audio path (a bounded queue behind the provider's
``/slimproto/sendspin`` route) are in place. The device starts as soon as it is
buffered; a monitor loop compares its position to the Sendspin timeline and
corrects drift with skip/pause. The now-playing metadata (title/artist/album/
artwork) is taken from the Sendspin player so the device shows the real track.

Precision: SlimProto is a method-2 (server-corrected) protocol with no frame
timestamps and no client-side scheduling, so this bridge can only ever be
near-synced (single-digit to tens of milliseconds), never sample-accurate.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiohttp import web
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aioslimproto.models import PlayerState as SlimPlayerState
from music_assistant_models.enums import IdentifierType

from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.providers.sendspin.bridge_manager import SendspinBridgeManagerBase
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_ROLE_ID,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)
from music_assistant.providers.sendspin.helpers import bridge_client_id_from_mac

from .constants import CONF_SENDSPIN_BRIDGE, CONF_SENDSPIN_BRIDGE_LEAD, DEFAULT_PLAYER_VOLUME

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from aiosendspin.server.roles import AudioChunk
    from aioslimproto.client import SlimClient
    from music_assistant_models.media_items import PlayerMedia

    from music_assistant.models.player import Player

    from .player import SqueezelitePlayer
    from .provider import SqueezelitePlayerProvider


# Default lead (ms) reported to Sendspin so it schedules the first chunk far
# enough ahead of the instant it wants audible. It has to cover fetching the
# bridge stream over HTTP plus filling the device's output buffer; the bridge
# learns the per-player value from the measured start error (see below).
SLIM_BRIDGE_COLD_START_LEAD_MS: int = 1800
SLIM_BRIDGE_WARM_START_LEAD_MS: int = 1650

# Lead learning: the per-player cold lead is adjusted from the measured start
# error (start_delta = device apparent start - intended audible start). The
# relation is ~1:1, so a full step would null it; the gain damps run-to-run
# WiFi variance instead. The value is persisted as a raw player config value.
_LEAD_LEARN_GAIN: float = 0.7
_LEAD_MIN_MS: int = 1200
_LEAD_MAX_MS: int = 2600
_LEAD_PERSIST_MIN_MS: int = 15
# A stream starting within this window reuses a warm transport (less lead).
_LEAD_WARM_WINDOW_S: float = 8.0
_LEAD_WARM_REDUCTION_MS: int = 150

# Ongoing buffer (ms) Sendspin keeps the bridge supplied with for the whole
# stream, so the device's ring stays fed without charging every other group
# member the full start lead in added latency.
SLIM_BRIDGE_MIN_BUFFER_MS: int = 1000

# Content type advertised by the bridge PCM source. aioslimproto parses the
# rate/channels/bitrate parameters from it to build the SlimProto codec message,
# so these must match the BridgePlayerRole audio requirements (44.1 kHz / 16-bit
# / stereo).
BRIDGE_PCM_CONTENT_TYPE: str = (
    f"audio/pcm;rate={BRIDGE_SAMPLE_RATE};channels={BRIDGE_CHANNELS};bitrate={BRIDGE_BIT_DEPTH}"
)

# SlimProto buffering for the bridged stream: stream threshold in KB and output
# threshold in tenths of a second fed to aioslimproto's play_url.
_BRIDGE_STREAM_THRESHOLD_KB: int = 200
_BRIDGE_OUTPUT_THRESHOLD_TENTHS: int = 20

# Bounded PCM buffer between the Sendspin callback and the HTTP source. It caps
# memory (and added latency) when the device fetches slower than Sendspin
# delivers; the oldest chunk is dropped first, since a small gap is preferable
# to an ever growing queue.
_PCM_QUEUE_MAX_CHUNKS: int = 512

# A read from the PCM queue that blocked longer than this (after the first chunk)
# means the device fetch ran dry - logged separately from controller corrections.
_PCM_STARVE_LOG_S: float = 0.5

# Cap on the diagnostic chunk history kept per bridge.
_CHUNK_HISTORY = 64

# Drift correction (Phase 3). The device position (from its own elapsed report)
# is compared to the Sendspin timeline; outside the deadband the difference is
# corrected with a damped skip/pause. Corrections are deliberately gentle (half
# the offset, capped) and only run after a settle and a stable sign, because a
# full-magnitude step at stream start overshoots and oscillates audibly.
_SYNC_DEADBAND_MS: int = 120
_SYNC_MAX_CORRECTION_MS: int = 300
_SYNC_CORRECTION_GAIN: float = 0.5
_SYNC_FIRST_CORRECTION_GAIN: float = 1.0
_SYNC_MIN_INTERVAL_S: float = 1.5
_SYNC_START_GRACE_S: float = 1.5
_SYNC_MONITOR_INTERVAL_S: float = 0.5
_SYNC_STABLE_SAMPLES: int = 2

# Self-heal: an output-protocol handover (grouping a bridged player) can stop the
# device by invalidating the native stream session it was fetching; the bridge
# then has nothing to play on. If the group is still streaming but the device is
# stopped, re-issue the bridge stream.
_SELF_HEAL_CHUNK_FRESH_S: float = 2.0
_SELF_HEAL_START_GRACE_S: float = 4.0
_SELF_HEAL_DEBOUNCE_S: float = 1.5
_SELF_HEAL_MIN_INTERVAL_S: float = 5.0
_SELF_HEAL_MAX_PER_STREAM: int = 5

# A stop that arrives while the group is still delivering audio is treated as a
# group reconfiguration/handover, not a real user stop, so the device is kept.
_HANDOVER_STOP_GRACE_S: float = 2.0


def self_heal_due(
    *,
    streaming: bool,
    chunk_age_s: float,
    device_stopped: bool,
    stream_age_s: float,
    stopped_age_s: float | None,
    since_last_heal_s: float,
    heal_count: int,
) -> bool:
    """
    Return whether the bridge should re-issue its stream because the device stopped.

    Only heals while audio is actually flowing (a fresh chunk), after a start
    grace and a debounce, rate-limited to avoid loops.

    :param streaming: Whether the bridge currently has an active stream.
    :param chunk_age_s: Seconds since the last audio chunk arrived.
    :param device_stopped: Whether the device reports STOPPED.
    :param stream_age_s: Seconds since the current stream started.
    :param stopped_age_s: Seconds the device has been stopped, or None if it just stopped.
    :param since_last_heal_s: Seconds since the last self-heal.
    :param heal_count: How many self-heals ran for this stream.
    """
    if not streaming or chunk_age_s > _SELF_HEAL_CHUNK_FRESH_S:
        return False
    if not device_stopped:
        return False
    if stream_age_s < _SELF_HEAL_START_GRACE_S:
        return False
    if stopped_age_s is None or stopped_age_s < _SELF_HEAL_DEBOUNCE_S:
        return False
    if heal_count >= _SELF_HEAL_MAX_PER_STREAM:
        return False
    return since_last_heal_s >= _SELF_HEAL_MIN_INTERVAL_S


def sendspin_audible_unix(audible_instant_us: int, sendspin_now_us: int, unix_now: float) -> float:
    """
    Map a Sendspin-clock audible instant to a unix epoch (seconds).

    Sendspin schedules playback on its own clock (``sendspin_server.clock.now_us()``)
    while the SlimProto start is timed on the server's wall clock. The two clocks
    share no epoch, so only the offset from now transfers: take how far
    ``audible_instant_us`` sits in the future on the Sendspin clock and apply that
    delta to a unix reading captured at the same instant. ``sendspin_now_us`` and
    ``unix_now`` must be sampled back to back.

    :param audible_instant_us: Sendspin-clock instant the first sample must be audible.
    :param sendspin_now_us: ``sendspin_server.clock.now_us()`` captured now.
    :param unix_now: ``time.time()`` captured at the same instant.
    :return: The unix epoch second that coincides with ``audible_instant_us``.
    """
    return unix_now + (audible_instant_us - sendspin_now_us) / 1_000_000


def resolve_lead_ms(cold_lead_ms: int, *, warm: bool) -> int:
    """
    Return the lead to report for a cold or warm transport.

    A warm transport (a stream starting right after a previous one) still has the
    device connected and buffered, so it needs less lead than a cold one.

    :param cold_lead_ms: The learned cold-start lead for the player.
    :param warm: Whether the transport is already warm.
    """
    if not warm:
        return cold_lead_ms
    return max(_LEAD_MIN_MS, cold_lead_ms - _LEAD_WARM_REDUCTION_MS)


def learn_lead_ms(current_lead_ms: int, start_delta_ms: float, *, gain: float) -> int:
    """
    Adjust a player's cold lead from the start error measured for a stream.

    ``start_delta_ms`` is ``device apparent start - intended audible start``: a
    positive value means the device started late (needs more lead), a negative
    value means it started early (needs less).

    :param current_lead_ms: The lead currently stored for the player.
    :param start_delta_ms: The measured start error in milliseconds.
    :param gain: How much of the error to fold in (0..1) to damp run-to-run variance.
    :return: The clamped, updated lead in milliseconds.
    """
    target = current_lead_ms - start_delta_ms
    updated = round(current_lead_ms + gain * (target - current_lead_ms))
    return max(_LEAD_MIN_MS, min(_LEAD_MAX_MS, updated))


def get_bridge_client_id(squeezelite_player: SqueezelitePlayer) -> str | None:
    """
    Get the Sendspin bridge client ID for a Squeezelite player.

    Uses the device MAC address as the client_id so protocol linking can match
    the resulting SendspinPlayer to the native SqueezelitePlayer.

    :param squeezelite_player: The Squeezelite player to bridge.
    :return: The Sendspin bridge client_id, or None when no valid MAC is known.
    """
    mac = squeezelite_player.player_id
    if is_valid_mac_address(mac):
        return bridge_client_id_from_mac(mac)
    return None


class SendspinSqueezeliteBridge:
    """
    Manage the Sendspin to SlimProto bridge for a single Squeezelite player.

    The bridge registers the player as an external Sendspin client and wires a
    ``BridgePlayerRole`` to receive the group's audio, volume and mute changes.
    """

    def __init__(
        self,
        provider: SqueezelitePlayerProvider,
        squeezelite_player: SqueezelitePlayer,
        sendspin_server: SendspinServer,
    ) -> None:
        """
        Initialize the bridge.

        :param provider: The Squeezelite provider instance.
        :param squeezelite_player: The Squeezelite player to bridge.
        :param sendspin_server: The Sendspin server to register with.
        """
        self.provider = provider
        self.mass = provider.mass
        self.squeezelite_player = squeezelite_player
        self.sendspin_server = sendspin_server
        self.logger = provider.logger.getChild(f"sendspin_bridge.{squeezelite_player.player_id}")

        self._sendspin_client: SendspinClient | None = None
        self._bridge_client_id: str | None = None
        self._bridge_role: BridgePlayerRole | None = None
        self._is_streaming = False
        # Learned cold-start lead (ms), persisted as a raw player config value.
        self._cold_lead_ms = int(
            self.mass.config.get_raw_player_config_value(
                self.squeezelite_player.player_id,
                CONF_SENDSPIN_BRIDGE_LEAD,
                SLIM_BRIDGE_COLD_START_LEAD_MS,
            )
        )
        self._persisted_lead_ms = self._cold_lead_ms
        # Latest start error for the current stream (device apparent start minus
        # intended audible start); folded into the lead when the stream ends.
        self._last_start_delta_ms: float | None = None
        self._last_stream_end_at: float = 0.0
        # Diagnostic history of the chunks received for the current stream.
        self._chunks: deque[AudioChunk] = deque(maxlen=_CHUNK_HISTORY)
        # PCM handed from the Sendspin callback to the HTTP source the SlimProto
        # player fetches. None is the end-of-stream sentinel.
        self._pcm_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        # Bumped per stream so a stale HTTP handler cannot outlive its stream.
        self._stream_generation = 0
        self._player_started = False
        self._player_start_task: asyncio.Task[None] | None = None
        self._first_chunk_timestamp_us: int | None = None
        # Unix instant the first buffered sample must be audible (start anchor).
        self._first_chunk_audible_unix: float | None = None
        # Phase 3 drift correction: periodic position comparison + skip/pause.
        self._monitor_task: asyncio.Task[None] | None = None
        self._stream_started_at: float = 0.0
        self._last_correction_at: float = 0.0
        # Consecutive samples outside the deadband with the same sign, so a
        # startup transient is not mistaken for real drift.
        self._offset_sign: int = 0
        self._offset_sign_run: int = 0
        # Signature of the now-playing media last pushed to the device, so the
        # monitor loop only updates the display on an actual track change.
        self._last_now_playing: tuple[object, ...] | None = None
        self._corrections_applied: int = 0
        # Self-heal / handover tracking.
        self._last_chunk_at: float = 0.0
        self._stopped_since: float | None = None
        self._last_self_heal_at: float = 0.0
        self._self_heal_count: int = 0

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    @property
    def bridge_client_id(self) -> str | None:
        """Return the Sendspin client_id registered for this bridge."""
        return self._bridge_client_id

    @property
    def is_streaming(self) -> bool:
        """Return whether the bridge is currently receiving audio from Sendspin."""
        return self._is_streaming

    @property
    def sendspin_group(self) -> object | None:
        """Return the Sendspin group this bridge belongs to, or None when unregistered."""
        return self._sendspin_client.group if self._sendspin_client else None

    async def start(self) -> None:
        """Register the Squeezelite player as an external Sendspin client."""
        self._bridge_client_id = get_bridge_client_id(self.squeezelite_player)
        if not self._bridge_client_id:
            self.logger.warning(
                "Cannot create Sendspin bridge for %s: no valid MAC address",
                self.squeezelite_player.display_name,
            )
            return

        hello = ClientHelloPayload(
            client_id=self._bridge_client_id,
            name=f"{self.squeezelite_player.display_name} (Squeezelite)",
            version=1,
            # The player@v1 role itself is never used for bridges, but aiosendspin
            # requires it to parse the player@v1_support object.
            supported_roles=[BRIDGE_ROLE_ID, "player@v1"],
            device_info=SendspinDeviceInfo(
                product_name=self.squeezelite_player.device_info.model,
                manufacturer=self.squeezelite_player.device_info.manufacturer,
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=[
                    SupportedAudioFormat(
                        codec=AudioCodec.PCM,
                        channels=BRIDGE_CHANNELS,
                        sample_rate=BRIDGE_SAMPLE_RATE,
                        bit_depth=BRIDGE_BIT_DEPTH,
                    )
                ],
                buffer_capacity=1_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
        )

        self.logger.debug(
            "Registering Sendspin bridge for %s with client_id=%s",
            self.squeezelite_player.display_name,
            self._bridge_client_id,
        )

        # Pre-register the native identifiers and the underlying player so the
        # resulting SendspinPlayer links to this SqueezelitePlayer.
        if sendspin_prov := cast(
            "SqueezelitePlayerProvider | None", self.mass.get_provider("sendspin")
        ):
            sendspin_prov.register_bridge_identifiers(
                self._bridge_client_id,
                {IdentifierType.MAC_ADDRESS: self.squeezelite_player.player_id},
            )
            sendspin_prov.register_bridge_underlying_player(
                self._bridge_client_id, self.squeezelite_player.player_id
            )

        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_stream_start
        )

        roles = self._sendspin_client.roles_by_family("player")
        if roles:
            self._bridge_role = cast("BridgePlayerRole", roles[0])
            self._bridge_role.set_callbacks(
                on_audio_chunk=self._on_audio_chunk,
                on_volume_change=self._on_volume_change,
                on_mute_change=self._on_mute_change,
                on_stream_start=self._on_bridge_stream_start,
                on_stream_end=self._on_bridge_stream_end,
                on_explicit_stop=self._on_bridge_explicit_stop,
                initial_volume=self.squeezelite_player.volume_level or DEFAULT_PLAYER_VOLUME,
                initial_muted=bool(self.squeezelite_player.volume_muted),
            )
            self._bridge_role.setup_audio_requirements()
            self._refresh_bridge_timing()

        self.logger.info(
            "Sendspin bridge registered for %s (client_id=%s)",
            self.squeezelite_player.display_name,
            self._bridge_client_id,
        )

    async def stop(self) -> None:
        """Stop and unregister the Sendspin bridge."""
        self._is_streaming = False
        self._player_started = False
        self._first_chunk_timestamp_us = None
        self._first_chunk_audible_unix = None
        self._chunks.clear()
        self._stop_monitor()
        if self._player_start_task and not self._player_start_task.done():
            self._player_start_task.cancel()
        self._player_start_task = None
        self._signal_pcm_end()
        if self._sendspin_client and self._bridge_client_id:
            with suppress(Exception):
                await self.sendspin_server.remove_client(self._bridge_client_id)
        self._sendspin_client = None
        self._bridge_role = None
        self.logger.debug("Sendspin bridge stopped for %s", self.squeezelite_player.display_name)

    async def serve_pcm_stream(self, request: web.Request) -> web.StreamResponse:
        """
        Serve the bridged PCM audio to the SlimProto player's HTTP fetch.

        aioslimproto fetches this while the device buffers the stream. Chunks are
        forwarded as Sendspin hands them over (Sendspin already paces delivery
        against the group timeline); when the device fetches slower than Sendspin
        delivers, the oldest queued chunk is dropped rather than buffering up.

        :param request: The incoming HTTP request from aioslimproto.
        """
        if not self._is_streaming:
            raise web.HTTPNotFound(reason="No active Sendspin stream for this player")

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": BRIDGE_PCM_CONTENT_TYPE,
                "Cache-Control": "no-store",
            },
        )
        await resp.prepare(request)
        if request.method != "GET":
            return resp

        # Capture this stream's queue and generation so a later stream can never
        # feed this handler (and vice versa).
        queue = self._pcm_queue
        generation = self._stream_generation
        self.logger.debug("Serving bridge PCM to %s", self.squeezelite_player.display_name)
        served_chunks = 0
        starved = 0
        while generation == self._stream_generation:
            waited_from = time.monotonic()
            chunk = await queue.get()
            wait_s = time.monotonic() - waited_from
            if chunk is None:
                break
            if served_chunks and wait_s > _PCM_STARVE_LOG_S:
                # the source had nothing ready for a while -> the device buffer
                # may underrun (glitch). Log so it can be told apart from a
                # controller correction.
                starved += 1
                self.logger.debug(
                    "Bridge PCM source starved %.0f ms for %s",
                    wait_s * 1000,
                    self.squeezelite_player.display_name,
                )
            served_chunks += 1
            try:
                await resp.write(chunk)
            except ConnectionError, RuntimeError:
                # aioslimproto closed its fetch (stream restart/stop) - not an error
                break
        if starved:
            self.logger.info(
                "Bridge PCM source starved %d time(s) for %s",
                starved,
                self.squeezelite_player.display_name,
            )
        with suppress(Exception):
            await resp.write_eof()
        return resp

    def sync_role_volume_state(self) -> None:
        """
        Adopt the Squeezelite player's volume and mute into the bridge role.

        The role is what the visible Sendspin player reports, so a change made on
        the SlimProto side must be reflected back into it.
        """
        if not self._bridge_role:
            return
        self._bridge_role.update_player_state(
            volume=self.squeezelite_player.volume_level,
            muted=bool(self.squeezelite_player.volume_muted),
        )

    def _refresh_bridge_timing(self) -> None:
        """Push the SlimProto startup lead and ongoing buffer to the bridge role."""
        if self._bridge_role is None:
            return
        warm = bool(
            self._last_stream_end_at
            and (time.time() - self._last_stream_end_at) < _LEAD_WARM_WINDOW_S
        )
        lead_ms = resolve_lead_ms(self._cold_lead_ms, warm=warm)
        self.logger.debug(
            "Sendspin bridge lead for %s: %d ms (%s)",
            self.squeezelite_player.display_name,
            lead_ms,
            "warm" if warm else "cold",
        )
        self._bridge_role.set_timing(
            required_lead_time_ms=lead_ms,
            min_buffer_ms=SLIM_BRIDGE_MIN_BUFFER_MS,
        )

    def _on_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle a stream start request from the Sendspin server."""
        self.logger.debug(
            "Sendspin stream start request for %s (reason=%s)",
            self.squeezelite_player.display_name,
            request.connection_reason,
        )
        self._chunks.clear()
        self._is_streaming = True
        self._last_start_delta_ms = None
        self._refresh_bridge_timing()

    def _on_bridge_stream_start(self) -> None:
        """Handle the PushStream actually starting to deliver audio."""
        # Bump the generation and release the previous queue so a stale HTTP
        # handler can neither feed from nor steal chunks of the new stream.
        self._stream_generation += 1
        self._signal_pcm_end()
        self._pcm_queue = asyncio.Queue()
        self._player_started = False
        self._first_chunk_timestamp_us = None
        self._first_chunk_audible_unix = None
        self._stream_started_at = time.time()
        self._last_correction_at = 0.0
        self._offset_sign = 0
        self._offset_sign_run = 0
        self._last_now_playing = None
        self._corrections_applied = 0
        self._last_chunk_at = 0.0
        self._stopped_since = None
        self._last_self_heal_at = 0.0
        self._self_heal_count = 0
        self._start_monitor()
        self.logger.debug(
            "Sendspin stream started for %s; awaiting first chunk",
            self.squeezelite_player.display_name,
        )

    def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        """
        Receive a timestamped audio chunk from Sendspin.

        The first chunk anchors the SlimProto start on the Sendspin timeline and
        starts the transport, which fetches the PCM source; the remaining chunks
        are queued for that fetch.
        """
        if not self._is_streaming:
            return
        self._last_chunk_at = time.time()
        self._chunks.append(chunk)
        if self._first_chunk_timestamp_us is None:
            self._first_chunk_timestamp_us = chunk.timestamp_us
            sendspin_now_us = self.sendspin_server.clock.now_us()
            unix_now = time.time()
            self._first_chunk_audible_unix = sendspin_audible_unix(
                chunk.timestamp_us, sendspin_now_us, unix_now
            )
            self._start_player_stream()
        self._enqueue_pcm(chunk.data)

    def _on_volume_change(self, volume: int) -> None:
        """Forward a Sendspin volume change to the Squeezelite player."""
        self.mass.create_task(self.squeezelite_player.volume_set(volume))

    def _on_mute_change(self, muted: bool) -> None:
        """Forward a Sendspin mute change to the Squeezelite player."""
        self.mass.create_task(self.squeezelite_player.volume_mute(muted))

    def _on_bridge_stream_end(self) -> None:
        """Handle the Sendspin stream ending."""
        self._is_streaming = False
        self._player_started = False
        self._first_chunk_audible_unix = None
        self._chunks.clear()
        self._stop_monitor()
        self._signal_pcm_end()
        self._log_stream_summary()
        self._learn_lead()
        self._last_stream_end_at = time.time()
        self.logger.debug("Sendspin stream ended for %s", self.squeezelite_player.display_name)

    def _on_bridge_explicit_stop(self) -> None:
        """Handle an explicit stop from the Sendspin side."""
        # A stop that arrives while audio is still flowing is a group
        # reconfiguration / output-protocol handover, not a real user stop: keep
        # the device on the bridge stream (self-heal re-asserts it if needed).
        if self._is_streaming and (time.time() - self._last_chunk_at) < _HANDOVER_STOP_GRACE_S:
            self.logger.debug(
                "Ignoring transient explicit stop for %s (stream still active)",
                self.squeezelite_player.display_name,
            )
            return
        self._is_streaming = False
        self._player_started = False
        self._first_chunk_audible_unix = None
        self._chunks.clear()
        self._stop_monitor()
        self._signal_pcm_end()
        self._log_stream_summary()
        self._learn_lead()
        self._last_stream_end_at = time.time()
        self.mass.create_task(self.squeezelite_player.client.stop())
        self.logger.debug("Sendspin explicit stop for %s", self.squeezelite_player.display_name)

    def _learn_lead(self) -> None:
        """Fold the measured start error into the player's stored cold lead."""
        start_delta_ms = self._last_start_delta_ms
        if start_delta_ms is None:
            return
        # Consume the measurement: stream-end and explicit-stop can both fire for
        # the same stream, and it must only be folded in once.
        self._last_start_delta_ms = None
        updated = learn_lead_ms(self._cold_lead_ms, start_delta_ms, gain=_LEAD_LEARN_GAIN)
        self.logger.debug(
            "Bridge lead for %s learned: %d -> %d ms (start_delta=%.0f ms)",
            self.squeezelite_player.display_name,
            self._cold_lead_ms,
            updated,
            start_delta_ms,
        )
        self._cold_lead_ms = updated
        if abs(updated - self._persisted_lead_ms) < _LEAD_PERSIST_MIN_MS:
            return
        self._persisted_lead_ms = updated
        self.mass.config.set_raw_player_config_value(
            self.squeezelite_player.player_id, CONF_SENDSPIN_BRIDGE_LEAD, updated
        )

    def _log_stream_summary(self) -> None:
        """Log how many drift corrections the stream that just ended used."""
        if self._corrections_applied:
            self.logger.info(
                "Bridge sync for %s used %d correction(s)",
                self.squeezelite_player.display_name,
                self._corrections_applied,
            )

    def _start_player_stream(self) -> None:
        """Start the SlimProto transport on the bridge PCM source."""
        if self._player_started:
            return
        self._player_started = True
        url = (
            f"{self.mass.streams.base_url}/slimproto/sendspin"
            f"?player_id={self.squeezelite_player.player_id}"
        )
        self.logger.debug(
            "Starting SlimProto bridge stream for %s: %s",
            self.squeezelite_player.display_name,
            url,
        )
        self._player_start_task = self.mass.create_task(self._play_bridge_url(url))

    async def _play_bridge_url(self, url: str) -> None:
        """Point the SlimProto player at the bridge PCM source and start it."""
        try:
            client = self.squeezelite_player.client
            # The bridge owns playback now: make sure the device is powered on
            # before the stream (play_url also does this, this is belt and braces).
            await client.power(True)
            await client.play_url(
                url=url,
                mime_type=BRIDGE_PCM_CONTENT_TYPE,
                metadata=self._build_play_metadata(),
                # start as soon as the device is buffered; the monitor loop
                # corrects any start offset (the Radio ignores a future unpause)
                autostart=True,
                stream_threshold=_BRIDGE_STREAM_THRESHOLD_KB,
                output_threshold=_BRIDGE_OUTPUT_THRESHOLD_TENTHS,
            )
        except Exception:
            self.logger.warning(
                "Failed to start bridge playback on %s",
                self.squeezelite_player.display_name,
                exc_info=True,
            )

    def _restart_player_stream(self) -> None:
        """Re-issue the bridge stream on the device (self-heal)."""
        if self._player_start_task and not self._player_start_task.done():
            self._player_start_task.cancel()
        self._player_start_task = None
        self._player_started = False
        self._start_player_stream()

    def _build_play_metadata(self) -> dict[str, object]:
        """Build the now-playing metadata for the bridge stream from the Sendspin player."""
        media = self._current_sendspin_media()
        if media is None:
            return {"item_id": "sendspin-bridge", "title": self.squeezelite_player.display_name}
        return self._media_metadata(media)

    def _current_sendspin_media(self) -> PlayerMedia | None:
        """
        Return the group's current media via the derived Sendspin player.

        A synced follower (which the bridge is) references the leader's media, so
        this carries the real title/artist/album/artwork even for a follower.
        """
        if self._bridge_client_id and (
            player := self.mass.players.get_player(self._bridge_client_id)
        ):
            return player.current_media
        return None

    def _media_metadata(self, media: PlayerMedia) -> dict[str, object]:
        """Build the SlimProto now-playing metadata dict from a Sendspin media item."""
        return {
            "item_id": media.uri or "sendspin-bridge",
            "title": media.title,
            "artist": media.artist,
            "album": media.album,
            "image_url": media.image_url,
            "duration": media.stream_duration or media.duration,
        }

    def _maybe_update_now_playing(self) -> None:
        """Push a per-track metadata update to the device when the media changed."""
        media = self._current_sendspin_media()
        if media is None:
            return
        signature: tuple[object, ...] = (
            media.queue_item_id or media.uri,
            media.title,
            media.artist,
            media.album,
            media.image_url,
        )
        if signature == self._last_now_playing:
            return
        self._last_now_playing = signature
        self.mass.create_task(
            self.squeezelite_player.client.update_now_playing(self._media_metadata(media))
        )

    def _enqueue_pcm(self, data: bytes) -> None:
        """Queue a PCM chunk, dropping the oldest when the buffer is full."""
        if self._pcm_queue.full():
            with suppress(asyncio.QueueEmpty):
                self._pcm_queue.get_nowait()
        with suppress(asyncio.QueueFull):
            self._pcm_queue.put_nowait(data)

    def _signal_pcm_end(self) -> None:
        """Tell the HTTP source there is no more audio for this stream."""
        if self._pcm_queue.full():
            with suppress(asyncio.QueueEmpty):
                self._pcm_queue.get_nowait()
        with suppress(asyncio.QueueFull):
            self._pcm_queue.put_nowait(None)

    def _start_monitor(self) -> None:
        """Start the drift-correction/diagnostics loop for the current stream."""
        self._stop_monitor()
        self._monitor_task = self.mass.create_task(self._monitor_loop())

    def _stop_monitor(self) -> None:
        """Stop the drift-correction/diagnostics loop."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None

    async def _monitor_loop(self) -> None:
        """Compare the device position to the Sendspin timeline and correct it."""
        while self._is_streaming:
            await asyncio.sleep(_SYNC_MONITOR_INTERVAL_S)
            if not self._is_streaming:
                break
            try:
                self._maybe_update_now_playing()
                self._maybe_self_heal()
                self._log_and_correct()
            except Exception:
                self.logger.debug("Drift monitor error", exc_info=True)

    def _maybe_self_heal(self) -> None:
        """Re-issue the bridge stream if the group is playing but the device stopped."""
        client = self.squeezelite_player.client
        now = time.time()
        device_stopped = client.state == SlimPlayerState.STOPPED
        if device_stopped and self._is_streaming and self._stopped_since is None:
            self._stopped_since = now
        due = self_heal_due(
            streaming=self._is_streaming,
            chunk_age_s=now - self._last_chunk_at,
            device_stopped=device_stopped,
            stream_age_s=now - self._stream_started_at,
            stopped_age_s=(now - self._stopped_since) if self._stopped_since else None,
            since_last_heal_s=now - self._last_self_heal_at,
            heal_count=self._self_heal_count,
        )
        if not device_stopped:
            self._stopped_since = None
        if not due:
            return
        self._self_heal_count += 1
        self._last_self_heal_at = now
        self._stopped_since = None
        self.logger.info(
            "Self-healing Sendspin bridge for %s: device stopped while streaming",
            self.squeezelite_player.display_name,
        )
        self._restart_player_stream()

    def _log_and_correct(self) -> None:
        """Log the current offset from the Sendspin timeline and correct it."""
        client = self.squeezelite_player.client
        audible_unix = self._first_chunk_audible_unix
        if audible_unix is None:
            return
        now = time.time()
        sendspin_pos_s = now - audible_unix
        device_pos_s = client.elapsed_milliseconds / 1000
        offset_ms = (device_pos_s - sendspin_pos_s) * 1000
        play_point = client.play_point
        start_delta_ms = (play_point[1] - audible_unix) * 1000 if play_point is not None else None
        if start_delta_ms is not None:
            self._last_start_delta_ms = start_delta_ms
        self.logger.debug(
            "Bridge sync %s: offset=%.0f ms (device=%.3fs sendspin=%.3fs) start_delta=%s state=%s",
            self.squeezelite_player.display_name,
            offset_ms,
            device_pos_s,
            sendspin_pos_s,
            f"{start_delta_ms:.0f}ms" if start_delta_ms is not None else "n/a",
            client.state,
        )
        self._track_offset_sign(offset_ms)
        if not self._should_correct(now, client):
            return
        if self._offset_sign == 0:
            return
        # The first fix takes the whole offset (still capped) so the start
        # converges in one step; later fixes are damped so an estimate that is
        # off is a gentle nudge rather than an audible 1.5 s pause.
        gain = (
            _SYNC_FIRST_CORRECTION_GAIN if self._corrections_applied == 0 else _SYNC_CORRECTION_GAIN
        )
        step_ms = max(1, int(abs(offset_ms) * gain))
        step_ms = min(step_ms, _SYNC_MAX_CORRECTION_MS)
        self._last_correction_at = now
        self._offset_sign_run = 0
        self._corrections_applied += 1
        if self._offset_sign > 0:
            # device is ahead of the timeline - hold it back
            self.logger.debug(
                "Bridge sync: pausing %s for %d ms",
                self.squeezelite_player.display_name,
                step_ms,
            )
            self.mass.create_task(client.pause_for(step_ms))
        else:
            # device is behind the timeline - jump it forward
            self.logger.debug(
                "Bridge sync: skipping %s ahead %d ms",
                self.squeezelite_player.display_name,
                step_ms,
            )
            self.mass.create_task(client.skip_over(step_ms))

    def _track_offset_sign(self, offset_ms: float) -> None:
        """Track how many consecutive samples share the same out-of-deadband sign."""
        if offset_ms > _SYNC_DEADBAND_MS:
            sign = 1
        elif offset_ms < -_SYNC_DEADBAND_MS:
            sign = -1
        else:
            sign = 0
        if sign == 0:
            self._offset_sign_run = 0
        elif sign == self._offset_sign:
            self._offset_sign_run += 1
        else:
            self._offset_sign_run = 1
        self._offset_sign = sign

    def _should_correct(self, now: float, client: SlimClient) -> bool:
        """Return whether a drift correction may run right now."""
        if client.state != SlimPlayerState.PLAYING:
            return False
        if now - self._stream_started_at < _SYNC_START_GRACE_S:
            return False
        if self._offset_sign_run < _SYNC_STABLE_SAMPLES:
            return False
        return now - self._last_correction_at >= _SYNC_MIN_INTERVAL_S


class SendspinBridgeManager(SendspinBridgeManagerBase[SendspinSqueezeliteBridge]):
    """Manage Sendspin bridges for all Squeezelite players."""

    def _bridge_client_id(self, player: Player) -> str | None:
        """Return the Sendspin client_id used to bridge a Squeezelite player."""
        return get_bridge_client_id(cast("SqueezelitePlayer", player))

    def _create_bridge(self, player: Player) -> SendspinSqueezeliteBridge:
        """Create a bridge instance for a Squeezelite player."""
        sendspin_server = self.sendspin_server
        assert sendspin_server is not None  # guaranteed by _lifecycle_allows_bridge
        return SendspinSqueezeliteBridge(
            cast("SqueezelitePlayerProvider", self.provider),
            cast("SqueezelitePlayer", player),
            sendspin_server,
        )

    def _should_have_bridge(self, player: Player) -> bool:
        """
        Return whether a Squeezelite player should have a Sendspin bridge.

        Gated behind the (default off) ``sendspin_bridge`` config option while the
        audio path is unfinished, so enabling it cannot silently take players away
        from native SlimProto playback.
        """
        if not self.provider.config.get_value(CONF_SENDSPIN_BRIDGE):
            return False
        return get_bridge_client_id(cast("SqueezelitePlayer", player)) is not None
