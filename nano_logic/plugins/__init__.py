"""Central plugin registry.

Discovery (see nano_logic.__init__) hands every instantiated PluginBase
subclass to register_plugin(). This module owns the resulting registries:

  PLUGINS          name -> plugin instance
  COMMAND_HANDLERS "docker.ps" -> callable(*children) -> str
  METRIC_PROBES    metric name -> callable() -> float | None

The DSL and engine read these registries instead of importing backends
directly, so new plugins drop in without touching core code.
"""
from __future__ import annotations

from nano_logic.plugins.base import PluginBase

PLUGINS: dict[str, PluginBase] = {}
COMMAND_HANDLERS: dict[str, callable] = {}
METRIC_PROBES: dict[str, callable] = {}


def register_plugin(plugin: PluginBase) -> None:
    """Merge one plugin instance into the registries. Idempotent by name."""
    PLUGINS[plugin.name] = plugin
    for key, handler in getattr(plugin, "command_handlers", {}).items():
        COMMAND_HANDLERS[key] = handler
    for metric_name, probe in getattr(plugin, "probe_registry", {}).items():
        METRIC_PROBES.setdefault(metric_name, probe)
    try:
        plugin.register(None)  # no app object yet; plugins must tolerate None
    except Exception:  # noqa: BLE001 — registration issues are non-fatal
        pass


def get_command_handler(key: str) -> callable | None:
    """Resolve a dotted command key ("docker.ps") to its handler, or None."""
    return COMMAND_HANDLERS.get(key)


def get_plugin(name: str) -> PluginBase | None:
    return PLUGINS.get(name)


def reset_registries() -> None:
    """Test helper — empty all registries."""
    PLUGINS.clear()
    COMMAND_HANDLERS.clear()
    METRIC_PROBES.clear()
