"""Tests for the SqueezePlay library menu handler."""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.squeezelite.library_menu import (
    HOME_NODE,
    ROOT_NODE,
    SqueezeliteLibraryMenu,
)


def _make_menu(play_media: Any = None, hass: Any = None) -> SqueezeliteLibraryMenu:
    """Build a menu handler over a minimal fake provider."""
    mass = types.SimpleNamespace(
        player_queues=types.SimpleNamespace(play_media=play_media or AsyncMock()),
        get_provider=lambda _domain: hass,
        streams=types.SimpleNamespace(base_url="http://192.0.2.10:8098"),
    )
    provider = types.SimpleNamespace(mass=mass, logger=MagicMock())
    return SqueezeliteLibraryMenu(provider)


def _cmd(command: str, player_id: str = "aa:bb", **kwargs: Any) -> Any:
    """Build a SlimCLICommand-like object."""
    return types.SimpleNamespace(player_id=player_id, command=command, args=[], kwargs=kwargs)


async def test_playlistcontrol_swallows_empty_playback_error() -> None:
    """A failed/empty selection must not raise: the CLI request needs an answer."""
    play_media = AsyncMock(side_effect=MediaNotFoundError("No playable items found"))
    menu = _make_menu(play_media)
    await menu._handle_playlistcontrol(
        _cmd("playlistcontrol", uri="library://playlist/1", cmd="play")
    )
    play_media.assert_awaited_once()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("play", QueueOption.PLAY), ("add", QueueOption.ADD), ("insert", QueueOption.NEXT)],
)
async def test_playlistcontrol_maps_modes(mode: str, expected: QueueOption) -> None:
    """The playlistcontrol mode maps to the matching queue option."""
    play_media = AsyncMock()
    menu = _make_menu(play_media)
    await menu._handle_playlistcontrol(
        _cmd("playlistcontrol", uri="library://playlist/1", cmd=mode)
    )
    assert play_media.call_args.kwargs["option"] == expected


async def test_browse_returns_placeholder_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty browse result yields a placeholder instead of an empty list."""
    menu = _make_menu()
    monkeypatch.setattr(menu, "_browse_items", AsyncMock(return_value=[]))
    result = await menu._handle_browse(_cmd("ma_browse", view="artists"))
    assert result["count"] == 1
    assert result["item_loop"] == [{"text": "No items found"}]


async def test_artist_items_are_navigable() -> None:
    """Artist rows carry an id/node and open their albums."""
    menu = _make_menu()
    artist = types.SimpleNamespace(item_id="1", name="An Artist")

    async def _iter_artists() -> Any:
        yield artist

    menu.mass.music = types.SimpleNamespace(
        artists=types.SimpleNamespace(iter_library_items=_iter_artists)
    )
    items = await menu._artist_items()
    assert items[0]["id"] == "artist-1"
    assert items[0]["node"] == HOME_NODE
    assert items[0]["actions"]["go"]["params"] == {"view": "albums", "artist_id": "1"}


async def test_album_items_fall_back_to_tracks() -> None:
    """An artist without library albums lists its library tracks instead."""
    menu = _make_menu()
    track = types.SimpleNamespace(name="A Track", uri="library://track/1")
    artists = types.SimpleNamespace(
        albums=AsyncMock(return_value=[]),
        tracks=AsyncMock(return_value=[track]),
    )
    menu.mass.music = types.SimpleNamespace(artists=artists)
    items = await menu._album_items("1")
    assert items[0]["text"] == "A Track"
    assert items[0]["actions"]["play"]["params"]["uri"] == "library://track/1"


def test_play_action_sets_next_window() -> None:
    """Play actions carry the LMS nextWindow so the UI leaves the loading state."""
    play = SqueezeliteLibraryMenu._play_action("library://playlist/1", "play")
    add = SqueezeliteLibraryMenu._play_action("library://playlist/1", "add")
    assert play["nextWindow"] == "nowPlaying"
    assert add["nextWindow"] == "refresh"


def test_home_entries_place_ha_scripts_at_root() -> None:
    """The HA scripts entry is at the root; the library entries stay under My Music."""
    menu = _make_menu(hass=object())
    items = {item["id"]: item for item in menu._home_entries()}
    assert items["ma_playlists"]["node"] == HOME_NODE
    assert items["ma_artists"]["node"] == HOME_NODE
    assert items["ma_discover"]["node"] == HOME_NODE
    assert items["ma_discover"]["text"] == "Discover"
    assert items["ma_ha_scripts"]["node"] == ROOT_NODE
    assert items["ma_ha_scripts"]["weight"] == 15
    assert items["ma_ha_scripts"]["icon"] == "http://192.0.2.10:8098/slimproto/ha_icon.png"


def test_home_entries_omit_ha_scripts_without_hass() -> None:
    """Without the hass plugin the HA scripts entry is not offered."""
    menu = _make_menu()
    assert all(item["id"] != "ma_ha_scripts" for item in menu._home_entries())


async def test_ha_script_items_sorted_alphabetically() -> None:
    """Scripts are listed alphabetically by friendly name."""
    hass = types.SimpleNamespace(
        hass=types.SimpleNamespace(
            get_entity_registry=AsyncMock(
                return_value=[
                    {"entity_id": "script.zulu", "name": "Zulu", "labels": ["l1"]},
                    {"entity_id": "script.alpha", "name": "Alpha", "labels": ["l1"]},
                    {"entity_id": "script.mike", "name": "Mike", "labels": ["l1"]},
                ]
            ),
            send_command=AsyncMock(return_value=[{"label_id": "l1", "name": "squeeze"}]),
        ),
        get_states=AsyncMock(
            return_value=[
                {"entity_id": "script.zulu", "attributes": {"friendly_name": "Zulu"}},
                {"entity_id": "script.alpha", "attributes": {"friendly_name": "Alpha"}},
                {"entity_id": "script.mike", "attributes": {"friendly_name": "Mike"}},
            ]
        ),
    )
    menu = _make_menu(hass=hass)
    items = await menu._ha_script_items()
    assert [item["text"] for item in items] == ["Alpha", "Mike", "Zulu"]


async def test_recommendation_folders_are_navigable() -> None:
    """Discover rows open their items, and disabled rows are skipped."""
    menu = _make_menu()
    folder = types.SimpleNamespace(
        provider="tidal", item_id="row1", name="For You", enabled_by_default=True
    )
    hidden = types.SimpleNamespace(
        provider="tidal", item_id="row2", name="Hidden", enabled_by_default=False
    )
    menu.mass.music = types.SimpleNamespace(
        recommendations=types.SimpleNamespace(
            get_recommendations=AsyncMock(return_value=[folder, hidden])
        )
    )
    items = await menu._recommendation_folders()
    assert [item["text"] for item in items] == ["For You"]
    assert items[0]["node"] == HOME_NODE
    assert items[0]["actions"]["go"]["params"] == {
        "view": "recommendation_items",
        "provider": "tidal",
        "item_id": "row1",
    }


async def test_recommendation_items_render_playable() -> None:
    """Row items are playable, and folder items are expanded one level."""
    menu = _make_menu()
    track = types.SimpleNamespace(media_type=MediaType.TRACK, uri="tidal://track/1", name="A Track")
    nested = types.SimpleNamespace(
        media_type=MediaType.TRACK, uri="tidal://track/2", name="Nested Track"
    )
    folder = types.SimpleNamespace(media_type=MediaType.FOLDER, name="Folder", items=[nested])
    menu.mass.music = types.SimpleNamespace(
        recommendations=types.SimpleNamespace(
            get_recommendation_items=AsyncMock(return_value=[track, folder])
        )
    )
    items = await menu._recommendation_items("tidal", "row1")
    assert [item["text"] for item in items] == ["A Track", "Nested Track"]
    assert items[0]["actions"]["play"]["params"]["uri"] == "tidal://track/1"


async def test_ha_script_items_explain_when_no_labelled_scripts() -> None:
    """With no matching scripts, the menu shows a non-actionable hint."""
    hass = types.SimpleNamespace(
        hass=types.SimpleNamespace(
            get_entity_registry=AsyncMock(
                return_value=[{"entity_id": "script.foo", "name": "Foo", "labels": []}]
            ),
            send_command=AsyncMock(return_value=[{"label_id": "l1", "name": "squeeze"}]),
        ),
        get_states=AsyncMock(return_value=[]),
    )
    menu = _make_menu(hass=hass)

    items = await menu._ha_script_items()

    assert len(items) == 1
    assert "actions" not in items[0]
    assert "squeeze" in items[0]["text"]


async def test_ha_script_items_explain_when_hass_is_missing() -> None:
    """Without the hass plugin the menu explains that Home Assistant is missing."""
    menu = _make_menu()

    items = await menu._ha_script_items()

    assert len(items) == 1
    assert "actions" not in items[0]
    assert "Home Assistant" in items[0]["text"]


async def test_contextmenu_returns_play_actions() -> None:
    """The context menu offers play now / add to queue / play next."""
    menu = _make_menu()

    result = await menu._handle_contextmenu(_cmd("ma_contextmenu", uri="library://track/1"))

    assert result["isContextMenu"] == 1
    texts = [item["text"] for item in result["item_loop"]]
    assert texts == ["Play now", "Add to queue", "Play next"]
    assert result["item_loop"][0]["actions"]["do"]["params"] == {
        "uri": "library://track/1",
        "cmd": "play",
    }


def test_playable_item_carries_context_menu_action() -> None:
    """Playable rows expose a 'more' action that opens a context menu."""
    menu = _make_menu()
    item = menu._playable_item("A Track", "library://track/1")
    more = item["actions"]["more"]
    assert more["cmd"] == ["ma_contextmenu"]
    assert more["window"] == {"isContextMenu": 1}


def _queue_page_mass() -> Any:
    """Build a fake mass exposing a three-item queue."""
    queue_items = [
        types.SimpleNamespace(
            name=f"Track {index}",
            queue_item_id=f"qi{index}",
            media_item=types.SimpleNamespace(uri=f"library://track/{index}"),
        )
        for index in range(3)
    ]
    queue = types.SimpleNamespace(queue_id="q1", items=3, current_index=1)
    return types.SimpleNamespace(
        get_active_queue=lambda _player_id: queue,
        items=lambda _queue_id, limit, offset: queue_items[offset : offset + limit],
    )


async def test_build_playlist_page_returns_real_queue() -> None:
    """The playlist page reports the real queue size and jump actions."""
    menu = _make_menu()
    menu.mass.player_queues = _queue_page_mass()

    page = await menu.build_playlist_page("aa:bb", 0, 200)

    assert page is not None
    assert page["count"] == 3
    assert page["playlist_tracks"] == 3
    assert page["playlist_cur_index"] == 1
    assert page["offset"] == 0
    assert len(page["item_loop"]) == 3
    assert page["item_loop"][0]["actions"]["go"] == {
        "player": 0,
        "cmd": ["playlist", "index", 0],
    }
    assert page["item_loop"][0]["actions"]["more"]["window"] == {"isContextMenu": 1}


async def test_build_playlist_page_dash_offset_returns_metadata_only() -> None:
    """A '-' offset (now playing) returns only the queue metadata, not the item_loop."""
    menu = _make_menu()
    menu.mass.player_queues = _queue_page_mass()

    page = await menu.build_playlist_page("aa:bb", "-", 10)

    # item_loop/count/offset stay with aioslimproto's rich current/next item_loop
    assert page == {"playlist_tracks": 3, "playlist_cur_index": 1}


async def test_contextmenu_command_resolves_playlist_index() -> None:
    """The built-in contextmenu command returns the queue-row menu."""
    menu = _make_menu()
    menu.mass.player_queues = _queue_page_mass()

    result = await menu._handle_contextmenu_command(
        _cmd("contextmenu", "aa:bb", playlist_index=1, context="playlist")
    )

    assert result["isContextMenu"] == 1
    assert [item["text"] for item in result["item_loop"]] == [
        "Play now",
        "Play next",
        "Remove from queue",
        "Clear playlist",
    ]
    assert result["item_loop"][0]["actions"]["do"]["cmd"] == ["playlist", "index", 1]
    assert result["item_loop"][1]["actions"]["do"]["cmd"] == ["ma_queue_move_next"]
    assert result["item_loop"][1]["actions"]["do"]["params"] == {"index": 1}
    assert result["item_loop"][3]["actions"]["do"]["cmd"] == ["ma_queue_clear"]


def test_media_context_menu_closes_or_switches() -> None:
    """Play now opens Now Playing; add/next just close the context menu."""
    menu = _make_menu()

    result = menu._media_context_menu("library://track/1")

    assert [item["actions"]["do"]["nextWindow"] for item in result["item_loop"]] == [
        "nowPlaying",
        "parent",
        "parent",
    ]


async def test_queue_move_next_moves_item() -> None:
    """Play next moves the existing queue item (pos_shift 0), it does not copy it."""
    menu = _make_menu()
    player_queues = _queue_page_mass()
    player_queues.move_item = MagicMock()
    menu.mass.player_queues = player_queues

    await menu._handle_queue_move_next(_cmd("ma_queue_move_next", "aa:bb", index=1))

    player_queues.move_item.assert_called_once_with("q1", "qi1", 0)


async def test_queue_remove_deletes_item() -> None:
    """Remove from queue deletes the queue item."""
    menu = _make_menu()
    player_queues = _queue_page_mass()
    player_queues.delete_item = MagicMock()
    menu.mass.player_queues = player_queues

    await menu._handle_queue_remove(_cmd("ma_queue_remove", "aa:bb", index=1))

    player_queues.delete_item.assert_called_once_with("q1", "qi1")


async def test_queue_clear_clears_queue() -> None:
    """Clear playlist clears the player queue."""
    menu = _make_menu()
    player_queues = _queue_page_mass()
    player_queues.clear = MagicMock()
    menu.mass.player_queues = player_queues

    await menu._handle_queue_clear(_cmd("ma_queue_clear", "aa:bb"))

    player_queues.clear.assert_called_once_with("q1")
