#!/usr/bin/env python3
"""Manually runs one desk once (default: music) - useful for testing without the scheduler.

Appends to the same queue the service plays (data/queue.json), ignoring the fill level.

Usage: python scripts/run_agent_once.py [desk]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.desk import DeskRunner  # noqa: E402
from app.config import DATA_DIR, ROOT_DIR  # noqa: E402
from app.program.queue import ProgramQueue  # noqa: E402

if __name__ == "__main__":
    desk = sys.argv[1] if len(sys.argv) > 1 else "music"
    queue = ProgramQueue(DATA_DIR / "queue.json", DATA_DIR / "player_cursor.json")
    result = DeskRunner(ROOT_DIR, queue).run(desk, trigger="manual")
    print(json.dumps(result, indent=2, ensure_ascii=False))
