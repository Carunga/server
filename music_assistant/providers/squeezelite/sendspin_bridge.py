"""
Sendspin bridge for Squeezelite players (WIP).

Registers a Squeezelite (slimproto) player as an external Sendspin client so it
can take part in Sendspin synchronized playback, with Sendspin as the timing
master and the SlimProto device as the output.

Status: Phase 2 in progress. Registration, protocol linking, lifecycle, volume/mute
wiring (Phase 1) and the PCM audio path (a bounded queue behind the provider's
``/slimproto/sendspin`` route) are in place. The device is started paused and
unpaused at the Sendspin audible instant (``sendspin_audible_unix`` + jiffies
``unpause_at``). Drift correction is still TODO; see ``SENDSPIN_BRIDGE.md``.

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

from .constants import CONF_SENDSPIN_BRIDGE, DEFAULT_PLAYER_VOLUME

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from aiosendspin.server.roles import AudioChunk

    from music_assistant.models.player import Player

    from .player import SqueezelitePlayer
    from .provider import SqueezelitePlayerProvider


# Lead (ms) reported to Sendspin so it schedules the first chunk far enough ahead
# of the instant it wants audible. It has to cover fetching the bridge stream
# over HTTP plus filling the device's output buffer. SlimProto's default output
# threshold is ~200 KB, about 1.1 s at 44.1 kHz/16-bit stereo, so the cold value
# leaves a margin over that; a kept, already-connected device pays less.
SLIM_BRIDGE_COLD_START_LEAD_MS: int = 2500
SLIM_BRIDGE_WARM_START_LEAD_MS: int = 1200

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

# Cap on the diagnostic chunk history kept per bridge.
_CHUNK_HISTORY = 64


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
        self.squeezelite_player.end_sendspin_bridge_playback()
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
        while generation == self._stream_generation:
            chunk = await queue.get()
            if chunk is None:
                break
            try:
                await resp.write(chunk)
            except ConnectionError, RuntimeError:
                # aioslimproto closed its fetch (stream restart/stop) - not an error
                break
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
        self._bridge_role.set_timing(
            required_lead_time_ms=SLIM_BRIDGE_COLD_START_LEAD_MS,
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
        self.squeezelite_player.end_sendspin_bridge_playback()
        self._signal_pcm_end()
        self.logger.debug("Sendspin stream ended for %s", self.squeezelite_player.display_name)

    def _on_bridge_explicit_stop(self) -> None:
        """Handle an explicit stop from the Sendspin side."""
        self._is_streaming = False
        self._player_started = False
        self._first_chunk_audible_unix = None
        self._chunks.clear()
        self.squeezelite_player.end_sendspin_bridge_playback()
        self._signal_pcm_end()
        self.mass.create_task(self.squeezelite_player.client.stop())
        self.logger.debug("Sendspin explicit stop for %s", self.squeezelite_player.display_name)

    def _start_player_stream(self) -> None:
        """Start the SlimProto transport on the bridge PCM source."""
        if self._player_started:
            return
        self._player_started = True
        # The bridge anchors the start itself, so the player must not auto-unpause
        # on buffer ready (see SqueezelitePlayer._handle_buffer_ready).
        self.squeezelite_player.begin_sendspin_bridge_playback()
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
        """Point the SlimProto player at the bridge PCM source, then anchor the start."""
        try:
            await self.squeezelite_player.client.play_url(
                url=url,
                mime_type=BRIDGE_PCM_CONTENT_TYPE,
                metadata={"item_id": "sendspin-bridge", "title": "Sendspin"},
                # buffer only; the bridge unpauses at the Sendspin audible instant
                autostart=False,
                stream_threshold=_BRIDGE_STREAM_THRESHOLD_KB,
                output_threshold=_BRIDGE_OUTPUT_THRESHOLD_TENTHS,
            )
        except Exception:
            self.logger.warning(
                "Failed to start bridge playback on %s",
                self.squeezelite_player.display_name,
                exc_info=True,
            )
            self.squeezelite_player.end_sendspin_bridge_playback()
            return
        await self._anchor_start()

    async def _anchor_start(self) -> None:
        """Unpause the buffered device so its first sample lands on the Sendspin instant."""
        client = self.squeezelite_player.client
        audible_unix = self._first_chunk_audible_unix
        if audible_unix is None:
            await client.unpause_at(client.jiffies)
            return
        delay_ms = int((audible_unix - time.time()) * 1000)
        self.logger.debug(
            "Anchoring bridge start for %s in %d ms",
            self.squeezelite_player.display_name,
            delay_ms,
        )
        # jiffies run at 1 kHz, so milliseconds map 1:1 onto the player clock
        await client.unpause_at(client.jiffies + max(0, delay_ms))

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
