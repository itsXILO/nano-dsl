"""Tests for the isolated Docker integration layer (docker_probe).

All external boundaries (Docker SDK import, docker CLI subprocess) are
monkeypatched so this suite passes identically on machines with or
without Docker installed.
"""
import subprocess

import pytest

from nano_logic.plugins import docker_probe
from nano_logic.plugins.docker_probe import (
    DAEMON_UNREACHABLE,
    NOT_AVAILABLE,
    _classify_cli_failure,
    _cpu_percent,
    _fmt_bytes,
    format_containers,
    format_stats,
)
from nano_logic.dsl import execute_command


# ═══════════════════════════════════════════════
#  Pure formatting helpers
# ═══════════════════════════════════════════════

class TestFormatting:
    def test_format_containers_empty(self):
        assert "none running" in format_containers([])

    def test_format_containers_rows(self):
        rows = [{
            "id": "abc123", "name": "web", "image": "nginx:latest",
            "status": "Up 2 minutes", "ports": "0.0.0.0:8080->80/tcp",
        }]
        out = format_containers(rows)
        assert "web" in out and "nginx:latest" in out and "8080->80/tcp" in out
        assert out.startswith("Docker Containers (1):")

    def test_format_stats_empty(self):
        assert "no running containers" in format_stats([])

    def test_format_stats_rows(self):
        rows = [{"name": "db", "cpu_perc": "1.50%", "mem_perc": "3.20%",
                 "mem_usage": "100MiB / 3GiB"}]
        out = format_stats(rows)
        assert "db" in out and "1.50%" in out and "100MiB / 3GiB" in out
        assert out.startswith("Docker Stats (1):")

    @pytest.mark.parametrize("value,expected", [
        (512, "512.0B"), (2048, "2.0KiB"), (5 * 1024 ** 2, "5.0MiB"),
        (3 * 1024 ** 3, "3.0GiB"),
    ])
    def test_fmt_bytes(self, value, expected):
        assert _fmt_bytes(value) == expected


# ═══════════════════════════════════════════════
#  CPU% math (same formula as `docker stats`)
# ═══════════════════════════════════════════════

class TestCpuPercent:
    def _stats(self, total, prev_total, system, prev_system, online=2):
        return {
            "cpu_stats": {"cpu_usage": {"total_usage": total},
                          "system_cpu_usage": system, "online_cpus": online},
            "precpu_stats": {"cpu_usage": {"total_usage": prev_total},
                             "system_cpu_usage": prev_system},
        }

    def test_normal_case(self):
        # delta_cpu=200_000_000 of system delta=1_000_000_000 across 2 cpus -> 40%
        assert _cpu_percent(self._stats(300_000_000, 100_000_000,
                                        2_000_000_000, 1_000_000_000)) == pytest.approx(40.0)

    def test_zero_system_delta_returns_zero(self):
        assert _cpu_percent(self._stats(10, 0, 0, 0)) == 0.0

    def test_missing_keys_return_zero(self):
        assert _cpu_percent({}) == 0.0


# ═══════════════════════════════════════════════
#  Failure classification
# ═══════════════════════════════════════════════

class TestClassifyCliFailure:
    def test_success_is_none(self):
        assert _classify_cli_failure(0, "") is None

    def test_missing_binary(self):
        assert _classify_cli_failure(127, "") == NOT_AVAILABLE

    def test_daemon_connection_error(self):
        stderr = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
        assert _classify_cli_failure(1, stderr) == DAEMON_UNREACHABLE

    def test_daemon_permission_denied(self):
        stderr = "permission denied while trying to connect to the Docker daemon socket"
        assert _classify_cli_failure(1, stderr) == DAEMON_UNREACHABLE

    def test_other_errors_stay_graceful_strings(self):
        result = _classify_cli_failure(125, "some other failure")
        assert isinstance(result, str) and "error" in result.lower()


# ═══════════════════════════════════════════════
#  Reports — graceful fallbacks & happy path
# ═══════════════════════════════════════════════

class TestReports:
    def test_not_installed_returns_graceful_message(self, monkeypatch):
        monkeypatch.setattr(docker_probe.shutil, "which", lambda _: None)
        monkeypatch.setattr(docker_probe, "_sdk_client", lambda: None)
        assert docker_probe.docker_ps_report() == NOT_AVAILABLE
        assert docker_probe.docker_stats_report() == NOT_AVAILABLE

    def test_unreachable_daemon_returns_graceful_message(self, monkeypatch):
        monkeypatch.setattr(docker_probe.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(docker_probe, "_sdk_client", lambda: None)

        def fake_run(cmd, capture_output, text, timeout):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="",
                stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
            )

        monkeypatch.setattr(docker_probe.subprocess, "run", fake_run)
        assert docker_probe.docker_ps_report() == DAEMON_UNREACHABLE
        assert docker_probe.docker_stats_report() == DAEMON_UNREACHABLE

    def test_happy_path_subprocess_parsing(self, monkeypatch):
        monkeypatch.setattr(docker_probe.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(docker_probe, "_sdk_client", lambda: None)

        responses = {
            ("ps",): "abc123|web|nginx|Up 1 min|0.0.0.0:80->80/tcp",
            ("stats",): "web|1.23%|4.56%|120MiB / 2GiB",
        }

        def fake_run(cmd, capture_output, text, timeout):
            key = tuple(cmd[1:2])
            return subprocess.CompletedProcess(cmd, 0, stdout=responses[key], stderr="")

        monkeypatch.setattr(docker_probe.subprocess, "run", fake_run)

        ps_out = docker_probe.docker_ps_report()
        assert "Docker Containers (1)" in ps_out and "web" in ps_out

        stats_out = docker_probe.docker_stats_report()
        assert "Docker Stats (1)" in stats_out and "1.23%" in stats_out

    def test_no_running_containers(self, monkeypatch):
        monkeypatch.setattr(docker_probe.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(docker_probe, "_sdk_client", lambda: None)
        monkeypatch.setattr(
            docker_probe.subprocess, "run",
            lambda cmd, capture_output, text, timeout: subprocess.CompletedProcess(cmd, 0, "", ""),
        )
        assert "none running" in docker_probe.docker_ps_report()
        assert "no running containers" in docker_probe.docker_stats_report()


# ═══════════════════════════════════════════════
#  DSL integration — commands route through the layer
# ═══════════════════════════════════════════════

class TestDslIntegration:
    @pytest.mark.parametrize("cmd", ["docker.ps", "docker.stats"])
    def test_commands_always_return_string(self, cmd, monkeypatch):
        """Even with every backend broken, execution must not crash."""
        monkeypatch.setattr(docker_probe.shutil, "which", lambda _: None)
        monkeypatch.setattr(docker_probe, "_sdk_client",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        result = execute_command(cmd)
        assert isinstance(result, str)
        assert NOT_AVAILABLE in result

    def test_docker_ps_via_transformer(self, monkeypatch):
        monkeypatch.setattr(docker_probe, "docker_ps_report",
                            lambda: "Docker Containers (1):\n  web")
        assert "web" in execute_command("docker.ps")

    def test_docker_stats_via_transformer(self, monkeypatch):
        monkeypatch.setattr(docker_probe, "docker_stats_report",
                            lambda: "Docker Stats (1):\n  web")
        assert "1.23%" in execute_command("docker.stats") or "Stats" in execute_command("docker.stats")


# ═══════════════════════════════════════════════
#  Live check — only runs when Docker actually works locally
# ═══════════════════════════════════════════════

class TestLiveDocker:
    @pytest.fixture
    def require_live_docker(self):
        if not docker_probe.is_daemon_reachable():
            pytest.skip("live Docker daemon not reachable")

    def test_live_ps(self, require_live_docker):
        out = docker_probe.docker_ps_report()
        assert isinstance(out, str) and out.strip()

    def test_live_stats(self, require_live_docker):
        out = docker_probe.docker_stats_report()
        assert isinstance(out, str) and out.strip()
