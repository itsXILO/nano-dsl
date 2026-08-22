"""Plugin base module that defines the Plugin contract."""
from typing import Callable, Dict

# Typing alias for readability
ProbeCallable = Callable[..., Dict[str, float]]

class PluginBase:
    """
    All plugins must subclass this class and implement the required attributes
    and methods. The base class provides a contract for:
      - `name`: Short identifier used in DSL commands.
      - `probe_registry`: Mapping of metric names to callables that return metric values.
      - `commands`: List of Lark grammar fragments to extend the DSL.
      - `transformer_hooks`: Optional hooks to transform parsed Lark nodes.
      - `register(app)`: Called once when the plugin is discovered; used to
        register probes, command fragments, and UI help entries.
    """
    name: str
    probe_registry: Dict[str, ProbeCallable]
    commands: list[str]
    transformer_hooks: dict[str, Callable]

    def register(self, app: "DashboardApp") -> None:
        """
        Called by the Dashboard when the plugin is discovered.
        Plugins should override this method to:
          - Register their metric probes with the global registry.
          - Add their command fragments to the Lark parser.
          - Optionally register help text or UI elements.
        """
        raise NotImplementedError("Plugins must implement 'register(app)'")