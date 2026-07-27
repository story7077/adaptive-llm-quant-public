from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from trading.research.webgpt_scout import WebGptActiveResearchScout

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = REPO_ROOT / "scripts" / "research_webgpt_bridge.mjs"
NODE_TEST = Path(__file__).with_name("test_research_webgpt_bridge.mjs")


def test_node_bridge_contract_suite() -> None:
    completed = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_cli_fails_closed_without_opening_chrome_or_disclosing_arguments() -> None:
    marker = "secret-marker-that-must-not-be-echoed"
    completed = subprocess.run(
        [
            "node",
            str(BRIDGE),
            "preflight",
            "--agbrowse-root",
            marker,
            "--cdp-endpoint",
            "http://127.0.0.1:9222",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=10,
    )

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert marker not in completed.stdout
    assert json.loads(completed.stdout) == {
        "ok": False,
        "error": "agbrowse_root_not_absolute",
    }


def test_public_bridge_has_no_prompt_send_or_private_path_dependency() -> None:
    source = BRIDGE.read_text(encoding="utf-8")

    assert "connectOverCDP" in source
    assert "node_modules', 'playwright-core" in source
    assert "'prepare-active-browse'," in source
    assert "'rebind'," in source
    assert "'await-assistant'," in source
    assert "'postflight'," in source
    assert "GPT-5.6 Sol Pro" in source
    assert "web-ai\", \"send" not in source
    assert "page.fill(" not in source
    assert "prompt-file" not in source
    assert re.search(r"(?:^|[\s\"'])[A-Za-z]:[\\/]", source) is None
    assert re.search(r"/(?:home|Users)/[^/\s]+", source) is None


def test_scout_passes_browser_session_binding_to_every_bridge_stage() -> None:
    source = Path(WebGptActiveResearchScout.__module__.replace(".", "/") + ".py")
    source_text = (REPO_ROOT / "src" / source).read_text(encoding="utf-8")

    assert source_text.count('"--browser-session-id"') == 5
    assert 'browser_session_id=None' in source_text
