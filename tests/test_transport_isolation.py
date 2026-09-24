from __future__ import annotations

import socket
import subprocess

import pytest


@pytest.mark.parametrize("command", [
    ["hermes", "k3-support-telegram", "send-buttons"],
    ["lark-cli", "mail", "+messages"], ["ssh", "board-host"],
    ["curl", "https://example.invalid"], ["codex", "exec", "send a message"],
    ["git", "push", "origin", "HEAD"], ["bash", "-c", "hermes status"],
])
def test_missing_transport_double_cannot_launch_real_command(command):
    with pytest.raises(AssertionError, match="live command blocked"):
        subprocess.run(command, check=True)


def test_network_socket_is_blocked_before_connection():
    with socket.socket() as connection, pytest.raises(AssertionError, match="live network blocked"):
        connection.connect(("192.0.2.1", 443))
