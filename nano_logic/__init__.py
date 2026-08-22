"""Nano Logic terminal dashboard package."""
# Auto‑discover plugins on package import
import importlib
import pkgutil
from .plugins.base import PluginBase

def discover_plugins() -> list[PluginBase]:
    """Import each sub‑module and register any Plugin subclasses."""
    plugins = []
    for _, mod_name, _ in pkgutil.iter_modules(__path__):
        if mod_name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{mod_name}")
        if hasattr(module, "Plugin"):
            plugin_cls = getattr(module, "Plugin")
            if isinstance(plugin_cls, type) and issubclass(plugin_cls, PluginBase):
                plugin = plugin_cls()
                plugin.register(app)  # type: ignore[name-defined]
                plugins.append(plugin)
    return plugins
