"""Isolated Docker integration layer.

All Docker-specific logic lives here so it can later be promoted into a
full plugin (nano_logic/plugins/docker_plugin.py) with minimal refactoring:
the DSL transformer only calls the two report functions at the bottom.

Strategy:
  1. Prefer the Python Docker SDK (`docker` package) when installed.
  2. Fall back to the `docker` CLI via subprocess otherwise.
  3. Never raise: every public entry point returns a user-facing string,
     with graceful fallbacks like "Docker: not available".
"""
from __future__ import annotations
import shutil
import subprocess

DOCKER_BIN = "docker"
_CMD_TIMEOUT = 10  # seconds; docker stats --no-stream can be slow on busy hosts

NOT_AVAILABLE = "Docker: not available"
DAEMON_UNREACHABLE = "Docker: daemon not reachable"

# Substrings in the CLI's stderr that indicate a daemon/socket problem
# rather than a genuine command failure.
_DAEMON_ERROR_MARKERS = (
    "cannot connect",
    "is the docker daemon",
    "permission denied while trying to connect",
    "no such file or directory",  # missing /var/run/docker.sock
)


# ──────────────────────────────────────────────
#  Availability checks
# ──────────────────────────────────────────────

def is_docker_installed() -> bool:
    """True if the docker CLI binary exists on PATH (or SDK is importable)."""
    if shutil.which(DOCKER_BIN):
        return True
    try:
        import docker  # noqa: F401 — SDK-only installs are still usable
        return True
    except ImportError:
        return False


def _sdk_client():
    """Return a Docker SDK client, or None if the SDK is missing/unusable."""
    try:
        import docker
        return docker.from_env(timeout=_CMD_TIMEOUT)
    except Exception:
        return None


def is_daemon_reachable() -> bool:
    """True if the Docker daemon answers a cheap round-trip."""
    client = _sdk_client()
    if client is not None:
        try:
            return bool(client.ping())
        except Exception:
            return False
    _, returncode, _ = _run_cli(["info", "--format", "{{.ServerVersion}}"])
    return returncode == 0


# ──────────────────────────────────────────────
#  Data gatherers — list[dict] | str
#  A string return is a friendly failure message, never an exception.
# ──────────────────────────────────────────────

def get_containers() -> list[dict] | str:
    """Running containers, a friendly error string on failure."""
    client = _sdk_client()
    if client is not None:
        try:
            return [_sdk_container_row(c) for c in client.containers.list()]
        except Exception:
            pass  # fall through to the CLI before giving up
    output, returncode, stderr = _run_cli([
        "ps", "--format", "{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}",
    ])
    failure = _classify_cli_failure(returncode, stderr)
    if failure is not None:
        return failure
    rows = []
    for line in output.splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            rows.append({
                "id": parts[0],
                "name": parts[1],
                "image": parts[2],
                "status": parts[3],
                "ports": parts[4] if len(parts) > 4 else "",
            })
    return rows


def get_container_stats() -> list[dict] | str:
    """Per-container stats, a friendly error string on failure."""
    client = _sdk_client()
    if client is not None:
        try:
            return [_sdk_stat_row(c) for c in client.containers.list()]
        except Exception:
            pass  # fall through to the CLI before giving up
    output, returncode, stderr = _run_cli([
        "stats", "--no-stream", "--format",
        "{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}|{{.MemUsage}}",
    ])
    failure = _classify_cli_failure(returncode, stderr)
    if failure is not None:
        return failure
    rows = []
    for line in output.splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            rows.append({
                "name": parts[0],
                "cpu_perc": parts[1],
                "mem_perc": parts[2],
                "mem_usage": parts[3],
            })
    return rows


def _sdk_container_row(container) -> dict:
    ports_attr = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    bindings = []
    for container_port, hosts in ports_attr.items():
        if hosts:
            for h in hosts:
                bindings.append(f"{h.get('HostIp', '')}:{h.get('HostPort', '')}->{container_port}")
        else:
            bindings.append(container_port)
    return {
        "id": getattr(container, "short_id", "") or "",
        "name": container.name,
        "image": getattr(getattr(container, "image", None), "tags", [""])[0]
        if getattr(getattr(container, "image", None), "tags", None) else "",
        "status": getattr(container, "status", "") or "",
        "ports": ", ".join(bindings),
    }


def _sdk_stat_row(container) -> dict:
    raw = container.stats(stream=False)
    cpu = _cpu_percent(raw)
    used, limit = _mem_usage(raw)
    mem_pct = (used / limit * 100) if limit else 0.0
    return {
        "name": container.name,
        "cpu_perc": f"{cpu:.2f}%",
        "mem_perc": f"{mem_pct:.2f}%",
        "mem_usage": f"{_fmt_bytes(used)} / {_fmt_bytes(limit)}",
    }


def _cpu_percent(stats: dict) -> float:
    """CPU % using the same formula as `docker stats`."""
    try:
        cpu_delta = (
            stats["cpu_stats"]["cpu_usage"]["total_usage"]
            - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        system_delta = (
            stats["cpu_stats"].get("system_cpu_usage", 0)
            - stats["precpu_stats"].get("system_cpu_usage", 0)
        )
        online_cpus = stats["cpu_stats"].get("online_cpus") or len(
            stats["cpu_stats"]["cpu_usage"].get("percpu_usage") or []
        ) or 1
        if system_delta > 0 and cpu_delta > 0:
            return (cpu_delta / system_delta) * online_cpus * 100
    except (KeyError, TypeError, ZeroDivisionError):
        pass
    return 0.0


def _mem_usage(stats: dict) -> tuple[int, int]:
    try:
        mem = stats.get("memory_stats", {}) or {}
        cache = mem.get("stats", {}).get("cache") or mem.get("stats", {}).get("inactive_file") or 0
        return max(0, int(mem.get("usage", 0)) - int(cache)), int(mem.get("limit", 0))
    except (TypeError, ValueError):
        return 0, 0


def _fmt_bytes(num_bytes: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}TiB"


# ──────────────────────────────────────────────
#  CLI plumbing
# ──────────────────────────────────────────────

def _run_cli(args: list[str], timeout: int = _CMD_TIMEOUT) -> tuple[str, int, str]:
    """Run `docker <args>`, returning (stdout, returncode, stderr). Never raises."""
    cmd = [DOCKER_BIN, *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip(), proc.returncode, proc.stderr.strip()
    except FileNotFoundError:
        return "", 127, f"{DOCKER_BIN}: binary not found"
    except subprocess.TimeoutExpired:
        return "", 124, f"{DOCKER_BIN}: timed out after {timeout}s"
    except OSError as e:
        return "", 126, str(e)


def _classify_cli_failure(returncode: int, stderr: str) -> str | None:
    """Map a CLI failure to NOT_AVAILABLE / DAEMON_UNREACHABLE / None(=success)."""
    if returncode == 0:
        return None
    lowered = stderr.lower()
    if returncode == 127 or "binary not found" in lowered:
        return NOT_AVAILABLE
    if any(marker in lowered for marker in _DAEMON_ERROR_MARKERS):
        return DAEMON_UNREACHABLE
    return f"Docker: error - {stderr[:120]}" if stderr else f"Docker: error (exit {returncode})"


# ──────────────────────────────────────────────
#  Formatting helpers (pure — easy to unit-test)
# ──────────────────────────────────────────────

def format_containers(rows: list[dict]) -> str:
    if not rows:
        return "Docker Containers: none running"
    header = f"  {'NAME':22s} | {'IMAGE':28s} | {'STATUS':22s} | PORTS"
    lines = [f"Docker Containers ({len(rows)}):", header, "  " + "-" * len(header.strip())]
    for r in rows:
        lines.append(
            f"  {r['name'][:22]:22s} | {r['image'][:28]:28s} | "
            f"{r['status'][:22]:22s} | {r['ports']}"
        )
    return "\n".join(lines)


def format_stats(rows: list[dict]) -> str:
    if not rows:
        return "Docker Stats: no running containers"
    header = f"  {'NAME':22s} | {'CPU %':>8s} | {'MEM %':>8s} | MEM USAGE"
    lines = [f"Docker Stats ({len(rows)}):", header, "  " + "-" * len(header.strip())]
    for r in rows:
        lines.append(
            f"  {r['name'][:22]:22s} | {r['cpu_perc']:>8s} | "
            f"{r['mem_perc']:>8s} | {r['mem_usage']}"
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────
#  Public API — the only surface the DSL touches
# ──────────────────────────────────────────────

def docker_ps_report() -> str:
    """User-facing result of `docker.ps`."""
    if not is_docker_installed():
        return NOT_AVAILABLE
    result = get_containers()
    return result if isinstance(result, str) else format_containers(result)


def docker_stats_report() -> str:
    """User-facing result of `docker.stats`."""
    if not is_docker_installed():
        return NOT_AVAILABLE
    result = get_container_stats()
    return result if isinstance(result, str) else format_stats(result)
