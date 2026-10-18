"""The registry entry starts the server: `sdcmodeler mcp` answers initialize; server.json names the version."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import sdcmodeler
from sdcmodeler.mcp_server import SERVER_VERSION

ROOT = Path(__file__).resolve().parents[1]
SERVER_JSON = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


def _registry_args():
    return [a["value"] for a in SERVER_JSON["packages"][0].get("packageArguments", []) if a["type"] == "positional"]


@pytest.mark.parametrize("args", [pytest.param(_registry_args(), id="server.json packageArguments"), pytest.param(["mcp"], id="mcp")])
def test_the_launch_answers_initialize(args):
    proc = subprocess.run([sys.executable, "-m", "sdcmodeler.cli", *args], input=json.dumps(INITIALIZE) + "\n",
                          capture_output=True, text=True, timeout=60, env={"SDCSTUDIO_URL": "https://sdcstudio.test.local", "PATH": "", "PYTHONPATH": str(ROOT / "src")})
    assert proc.returncode == 0, proc.stderr
    response = json.loads([l for l in proc.stdout.splitlines() if l.strip()][0])
    assert response["result"]["serverInfo"] == {"name": "sdcmodeler", "version": SERVER_VERSION}


def test_server_json_names_the_released_version():
    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    assert SERVER_JSON["version"] == pyproject == SERVER_JSON["packages"][0]["version"] == sdcmodeler.__version__ == SERVER_VERSION
    assert _registry_args() == ["mcp"]
