"""Tests for the Squeezelite provider config handling."""

from __future__ import annotations

from music_assistant.providers.squeezelite.provider import is_bridge_only_config_change

_LOG = "values/log_level"
_BRIDGE = "values/sendspin_bridge"


def test_bridge_only_toggle_is_applied_without_reload() -> None:
    """Toggling only the Sendspin bridge option is a soft change."""
    assert is_bridge_only_config_change({_BRIDGE}) is True
    assert is_bridge_only_config_change({_BRIDGE, _LOG}) is True


def test_structural_or_no_value_change_reloads() -> None:
    """Ports/discovery and no-op changes keep the default reload behaviour."""
    assert is_bridge_only_config_change({"values/port"}) is False
    assert is_bridge_only_config_change({_BRIDGE, "values/port"}) is False
    assert is_bridge_only_config_change({_LOG}) is False
    assert is_bridge_only_config_change(set()) is False
    assert is_bridge_only_config_change({"name"}) is False
