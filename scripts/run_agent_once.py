#!/usr/bin/env python3
"""Manually triggers a single agent loop iteration - useful for testing without the scheduler.

Usage: python scripts/run_agent_once.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.loop import AgentLoop  # noqa: E402
from app.config import ROOT_DIR  # noqa: E402

if __name__ == "__main__":
    result = AgentLoop(ROOT_DIR).run_once()
    print(json.dumps(result, indent=2, ensure_ascii=False))
