"""
SqueezePlay menu handler that exposes the Music Assistant library on the player.

SqueezePlay (Radio/Touch/Boom) builds its home menu from the server's ``menu``
CLI response and browses lists by issuing the ``go`` command of the selected
item. This module answers those commands so the library (playlists, artists,
discover) shows up under "My Music" and is directly playable.

The handler is injected into aioslimproto via ``cli_command_handler`` and only
handles its own commands; everything else raises ``NotImplementedError`` so the
built-in handlers and the CLI event path stay in charge.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import MusicAssistantError

if TYPE_CHECKING:
    from aioslimproto.cli import SlimCLICommand

    from .provider import SqueezelitePlayerProvider

LOGGER = logging.getLogger(__name__)

# library entries are placed under the local "My Music" node
HOME_NODE = "myMusic"
# the root of the home menu, for entries that must not live under My Music
ROOT_NODE = "home"
# presets use weight 35; lower weights sort above them
HOME_ENTRIES: tuple[tuple[str, str, str, int, str], ...] = (
    ("ma_playlists", "Playlists", HOME_NODE, 20, "playlists"),
    ("ma_artists", "Artists", HOME_NODE, 21, "artists"),
    ("ma_discover", "Discover", HOME_NODE, 22, "recommendations"),
)
# only added when the Home Assistant plugin is available; lives at the root,
# below the built-in My Music node (weight 11) and above Radio (weight 20)
HA_SCRIPTS_ENTRY: tuple[str, str, str, int, str] = (
    "ma_ha_scripts",
    "HA scripts",
    ROOT_NODE,
    15,
    "ha_scripts",
)
# Home Assistant label whose scripts are listed in the HA scripts menu
HA_LABEL = "squeeze"
MAX_ITEMS = 250


class SqueezeliteLibraryMenu:
    """Answer SlimProto CLI menu/browse/playlist commands from the MA library."""

    def __init__(self, provider: SqueezelitePlayerProvider) -> None:
        """Initialize the menu handler."""
        self.provider = provider
        self.mass = provider.mass

    async def handle(self, cmd: SlimCLICommand) -> Any:
        """
        Handle a library menu related CLI command.

        Raises NotImplementedError for anything this handler does not own.
        """
        if cmd.command == "menu":
            return await self._handle_menu(cmd)
        if cmd.command == "ma_browse":
            return await self._handle_browse(cmd)
        if cmd.command == "playlistcontrol":
            await self._handle_playlistcontrol(cmd)
            return None
        if cmd.command == "ma_run_script":
            await self._handle_run_script(cmd)
            return None
        raise NotImplementedError(cmd.command)

    # ------------------------------------------------------------------
    # home menu
    # ------------------------------------------------------------------

    async def _handle_menu(self, cmd: SlimCLICommand) -> dict[str, Any]:
        """Return the home menu: built-in items plus our library entries."""
        result: dict[str, Any] = {"item_loop": [], "offset": 0, "count": 0}
        if not cmd.player_id:
            raise NotImplementedError
        # start from the built-in menu (presets) so we don't lose them
        if cli := getattr(self.provider.slimproto, "cli", None):
            try:
                result = await cli._handle_menu(cmd.player_id, *cmd.args, **cmd.kwargs)
            except Exception:  # pragma: no cover
                LOGGER.exception("Unable to build built-in menu")
        # only add our entries to the top-level menu (not to submenu requests)
        if not cmd.kwargs.get("item_id"):
            items = list(result.get("item_loop") or [])
            items.extend(self._home_entries())
            result["item_loop"] = items
            result["count"] = len(items)
        return result

    def _home_entries(self) -> list[dict[str, Any]]:
        """Build the library entries and the root-level Home Assistant entry."""
        entries = list(HOME_ENTRIES)
        if self.mass.get_provider("hass") is not None:
            entries.append(HA_SCRIPTS_ENTRY)
        items: list[dict[str, Any]] = []
        for entry_id, text, node, weight, view in entries:
            item: dict[str, Any] = {
                "id": entry_id,
                "node": node,
                "text": text,
                "actions": {
                    "go": {
                        "player": 0,
                        "cmd": ["ma_browse"],
                        "params": {"view": view},
                    }
                },
            }
            if weight is not None:
                item["weight"] = weight
            if entry_id == HA_SCRIPTS_ENTRY[0]:
                item["icon"] = f"{self.mass.streams.base_url}/slimproto/ha_icon.png"
            items.append(item)
        return items

    # ------------------------------------------------------------------
    # browsing
    # ------------------------------------------------------------------

    async def _handle_browse(self, cmd: SlimCLICommand) -> dict[str, Any]:
        """Return the item list for a browse request."""
        view = str(cmd.kwargs.get("view") or "")
        items = await self._browse_items(view, cmd.kwargs)
        if not items:
            # an empty item_loop leaves the player on a blank menu; show an explicit
            # placeholder so the browse result is visible and the list is not a dead end
            items = [{"text": "No items found"}]
        return {"count": len(items), "offset": 0, "item_loop": items}

    async def _browse_items(self, view: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Dispatch a browse view to the matching library query."""
        try:
            if view == "playlists":
                return await self._playlist_items()
            if view == "recommendations":
                return await self._recommendation_folders()
            if view == "recommendation_items":
                return await self._recommendation_items(
                    str(params.get("provider") or ""), str(params.get("item_id") or "")
                )
            if view == "artists":
                return await self._artist_items()
            if view == "albums":
                return await self._album_items(str(params.get("artist_id") or ""))
            if view == "tracks":
                return await self._track_items(str(params.get("album_id") or ""))
            if view == "ha_scripts":
                return await self._ha_script_items()
        except Exception:
            LOGGER.exception("Error building browse view %s", view)
        return []

    async def _playlist_items(self) -> list[dict[str, Any]]:
        """Return library playlists."""
        items: list[dict[str, Any]] = []
        async for playlist in self.mass.music.playlists.iter_library_items():
            items.append(self._playable_item(playlist.name, playlist.uri))
            if len(items) >= MAX_ITEMS:
                break
        return items

    async def _recommendation_folders(self) -> list[dict[str, Any]]:
        """
        Return the recommendation rows (library rows plus streaming providers).

        The recommendations subcontroller aggregates every provider that declares
        the RECOMMENDATIONS feature, so no provider is hardcoded here.
        """
        folders = await self.mass.music.recommendations.get_recommendations()
        items: list[dict[str, Any]] = []
        for folder in folders:
            # rows off by default are noisier and hidden in the MA UI until opted in
            if getattr(folder, "enabled_by_default", True) is False:
                continue
            items.append(
                {
                    "id": f"rec-{folder.provider}-{folder.item_id}",
                    "node": HOME_NODE,
                    "text": folder.name,
                    "actions": {
                        "go": {
                            "player": 0,
                            "cmd": ["ma_browse"],
                            "params": {
                                "view": "recommendation_items",
                                "provider": str(folder.provider),
                                "item_id": str(folder.item_id),
                            },
                        }
                    },
                }
            )
            if len(items) >= MAX_ITEMS:
                break
        return items

    async def _recommendation_items(self, provider: str, item_id: str) -> list[dict[str, Any]]:
        """Return the playable items of a single recommendation row."""
        if not provider or not item_id:
            return []
        row_items = await self.mass.music.recommendations.get_recommendation_items(
            provider, item_id
        )
        items: list[dict[str, Any]] = []
        for row_item in row_items:
            if getattr(row_item, "media_type", None) == MediaType.FOLDER:
                # a folder row: list its nested items so it is not a dead end
                for child in getattr(row_item, "items", None) or []:
                    if uri := getattr(child, "uri", None):
                        items.append(self._playable_item(child.name, uri))
            elif uri := getattr(row_item, "uri", None):
                items.append(self._playable_item(row_item.name, uri))
            if len(items) >= MAX_ITEMS:
                break
        return items

    async def _artist_items(self) -> list[dict[str, Any]]:
        """Return library artists; selecting one opens its albums."""
        items: list[dict[str, Any]] = []
        async for artist in self.mass.music.artists.iter_library_items():
            items.append(
                {
                    "id": f"artist-{artist.item_id}",
                    "node": HOME_NODE,
                    "text": artist.name,
                    "actions": {
                        "go": {
                            "player": 0,
                            "cmd": ["ma_browse"],
                            "params": {
                                "view": "albums",
                                "artist_id": str(artist.item_id),
                            },
                        }
                    },
                }
            )
            if len(items) >= MAX_ITEMS:
                break
        return items

    async def _album_items(self, artist_id: str) -> list[dict[str, Any]]:
        """Return the albums of an artist; selecting one opens its tracks."""
        if not artist_id:
            return []
        albums = await self.mass.music.artists.albums(artist_id, "library")
        items: list[dict[str, Any]] = []
        for album in albums:
            items.append(
                {
                    "id": f"album-{album.item_id}",
                    "node": HOME_NODE,
                    "text": album.name,
                    "style": "itemplay",
                    "actions": {
                        "go": {
                            "player": 0,
                            "cmd": ["ma_browse"],
                            "params": {
                                "view": "tracks",
                                "album_id": str(album.item_id),
                            },
                        },
                        "play": self._play_action(album.uri, "play"),
                        "add": self._play_action(album.uri, "add"),
                    },
                }
            )
        if items:
            return items
        # No in-library albums (common for an artist only known through library
        # tracks): fall back to the artist's library tracks so selecting an artist
        # is not a dead end.
        tracks = await self.mass.music.artists.tracks(artist_id, "library")
        return [self._playable_item(track.name, track.uri) for track in tracks]

    async def _track_items(self, album_id: str) -> list[dict[str, Any]]:
        """Return the tracks of an album."""
        if not album_id:
            return []
        tracks = await self.mass.music.albums.tracks(album_id, "library")
        return [self._playable_item(track.name, track.uri) for track in tracks]

    # ------------------------------------------------------------------
    # Home Assistant scripts
    # ------------------------------------------------------------------

    def _hass_provider(self) -> Any | None:
        """Return the Home Assistant plugin provider, if available."""
        return self.mass.get_provider("hass")

    async def _ha_script_items(self) -> list[dict[str, Any]]:
        """
        Return Home Assistant scripts carrying the configured label.

        Falls back to matching scripts whose name or entity_id contains the label
        text when the label registry is unavailable or has no such label.
        """
        hass = self._hass_provider()
        if hass is None:
            return []
        try:
            registry = await hass.hass.get_entity_registry()
        except Exception:
            LOGGER.exception("Unable to fetch Home Assistant entity registry")
            return []
        label_ids = await self._label_ids(hass)
        entity_ids = [
            entity_id
            for entry in registry or []
            if (entity_id := str(entry.get("entity_id") or "")).startswith("script.")
            and self._script_matches(entry, entity_id, label_ids)
        ]
        if not entity_ids:
            return []
        names = await self._friendly_names(hass, entity_ids)
        rows = sorted(
            ((names.get(entity_id) or entity_id, entity_id) for entity_id in entity_ids),
            key=lambda row: row[0].casefold(),
        )
        return [self._run_script_item(name, entity_id) for name, entity_id in rows]

    async def _label_ids(self, hass: Any) -> set[str]:
        """Return the label ids whose name matches the configured label."""
        try:
            labels = await hass.hass.send_command("config/label_registry/list")
        except Exception:
            LOGGER.debug("Unable to fetch Home Assistant label registry", exc_info=True)
            return set()
        return {
            str(label["label_id"])
            for label in labels or []
            if str(label.get("name") or "").strip().lower() == HA_LABEL and label.get("label_id")
        }

    @staticmethod
    def _script_matches(entry: dict[str, Any], entity_id: str, label_ids: set[str]) -> bool:
        """Return whether a script entry belongs in the HA scripts menu."""
        if label_ids:
            return bool(set(entry.get("labels") or []) & label_ids)
        # fall back to a name/entity_id match when the label is not available
        haystack = " ".join(
            str(entry.get(key) or "") for key in ("name", "original_name", "entity_id")
        )
        return HA_LABEL in (haystack or entity_id).lower()

    async def _friendly_names(self, hass: Any, entity_ids: list[str]) -> dict[str, str]:
        """Return friendly names for the given entities."""
        try:
            states = await hass.get_states(entity_ids=entity_ids)
        except Exception:
            LOGGER.debug("Unable to fetch script states", exc_info=True)
            return {}
        return {
            str(state["entity_id"]): (state.get("attributes") or {}).get("friendly_name")
            or str(state["entity_id"])
            for state in states or []
            if state.get("entity_id")
        }

    @staticmethod
    def _run_script_item(text: str, entity_id: str) -> dict[str, Any]:
        """Build a menu item that runs a Home Assistant script when selected."""
        return {
            "text": text,
            "actions": {
                "do": {
                    "player": 0,
                    "cmd": ["ma_run_script"],
                    "params": {"script": entity_id},
                }
            },
        }

    # ------------------------------------------------------------------
    # playback
    # ------------------------------------------------------------------

    async def _handle_playlistcontrol(self, cmd: SlimCLICommand) -> None:
        """Handle the playlistcontrol action emitted by a menu item."""
        if not cmd.player_id:
            raise NotImplementedError
        uri = cmd.kwargs.get("uri")
        if not uri:
            return
        mode = str(cmd.kwargs.get("cmd") or "play").lower()
        if mode == "add":
            option = QueueOption.ADD
        elif mode == "insert":
            option = QueueOption.NEXT
        else:
            option = QueueOption.PLAY
        self.provider.logger.debug(
            "Library menu: %s on player %s (mode=%s)", uri, cmd.player_id, mode
        )
        try:
            await self.mass.player_queues.play_media(
                queue_id=cmd.player_id, media=str(uri), option=option
            )
        except MusicAssistantError as err:
            # A selection that resolves to nothing (empty playlist, stale cache) must not
            # escape: an unanswered CLI request leaves the player on a loading screen
            # forever. Log it and answer normally so the UI moves on.
            self.provider.logger.warning(
                "Library menu: could not play %s on %s: %s", uri, cmd.player_id, err
            )

    async def _handle_run_script(self, cmd: SlimCLICommand) -> None:
        """Run a Home Assistant script selected from the HA scripts menu."""
        entity_id = cmd.kwargs.get("script")
        if not entity_id:
            return
        hass = self._hass_provider()
        if hass is None:
            return
        self.provider.logger.debug("HA scripts menu: running %s", entity_id)
        await hass.hass.call_service(
            domain="script", service="turn_on", target={"entity_id": str(entity_id)}
        )

    # ------------------------------------------------------------------
    # item helpers
    # ------------------------------------------------------------------

    def _playable_item(self, text: str, uri: str) -> dict[str, Any]:
        """Build a menu item that plays (and can enqueue) the given uri."""
        return {
            "text": text,
            "style": "itemplay",
            "actions": {
                "go": self._play_action(uri, "play"),
                "play": self._play_action(uri, "play"),
                "add": self._play_action(uri, "add"),
            },
        }

    @staticmethod
    def _play_action(uri: str, mode: str) -> dict[str, Any]:
        """Build a playlistcontrol action for the given uri and mode."""
        return {
            "player": 0,
            "cmd": ["playlistcontrol"],
            "params": {"uri": uri, "cmd": mode},
            "nextWindow": "nowPlaying" if mode == "play" else "refresh",
        }
