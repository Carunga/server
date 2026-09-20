"""
Sendspin bridge for Squeezelite players (WIP).

Registers a Squeezelite (slimproto) player as an external Sendspin client so it
can take part in Sendspin synchronized playback, with Sendspin as the timing
master and the SlimProto device as the output.

Status: Phase 1. Registration, protocol linking, lifecycle, volume/mute and the
bridge role wiring are in place. The audio path (Sendspin PushStream -> a PCM
HTTP source -> SlimProto ``play_url``, anchored on the Sendspin timeline and
drift-corrected with skip/pause) is scaffolded but not finished; see
``DESIGN-sendspin-bridge.md`` for the remaining phases.

Precision: SlimProto is a method-2 (server-corrected) protocol with no frame
timestamps and no client-side scheduling, so this bridge can only ever be
near-synced (single-digit to tens of milliseconds), never sample-accurate.
"""

from __future__ import annotations

from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING, cast

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

# Cap on the diagnostic chunk history kept per bridge.
_CHUNK_HISTORY = 64


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
        # Diagnostic history of the chunks received for the current stream. The
        # audio path is not wired to the device yet (Phase 2).
        self._chunks: deque[AudioChunk] = deque(maxlen=_CHUNK_HISTORY)

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    @property
    def bridge_client_id(self) -> str | None:
        """Return the Sendspin client_id registered for this bridge."""
        return self._bridge_client_id

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
        self._chunks.clear()
        if self._sendspin_client and self._bridge_client_id:
            with suppress(Exception):
                await self.sendspin_server.remove_client(self._bridge_client_id)
        self._sendspin_client = None
        self._bridge_role = None
        self.logger.debug("Sendspin bridge stopped for %s", self.squeezelite_player.display_name)

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
        """
        Handle the PushStream actually starting to deliver audio.

        Phase 2 will start the SlimProto transport here and anchor it on the
        Sendspin timeline.
        """
        self.logger.debug(
            "Sendspin stream started for %s; awaiting first chunk",
            self.squeezelite_player.display_name,
        )

    def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        """
        Receive a timestamped audio chunk from Sendspin.

        Phase 2 will queue this into a PCM HTTP source the SlimProto device pulls
        from, then anchor and drift-correct playback against ``chunk.timestamp_us``.
        """
        self._chunks.append(chunk)

    def _on_volume_change(self, volume: int) -> None:
        """Forward a Sendspin volume change to the Squeezelite player."""
        self.mass.create_task(self.squeezelite_player.volume_set(volume))

    def _on_mute_change(self, muted: bool) -> None:
        """Forward a Sendspin mute change to the Squeezelite player."""
        self.mass.create_task(self.squeezelite_player.volume_mute(muted))

    def _on_bridge_stream_end(self) -> None:
        """Handle the Sendspin stream ending."""
        self._is_streaming = False
        self._chunks.clear()
        self.logger.debug("Sendspin stream ended for %s", self.squeezelite_player.display_name)

    def _on_bridge_explicit_stop(self) -> None:
        """Handle an explicit stop from the Sendspin side."""
        self._is_streaming = False
        self._chunks.clear()
        self.logger.debug("Sendspin explicit stop for %s", self.squeezelite_player.display_name)


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
