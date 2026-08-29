"""Run the additive AgentDesk schema migration for AI-agent metadata.

Usage:
    python scripts/migrate_agent_profile.py

The application also performs the same idempotent additive check at startup
for local/demo databases. This command is useful for an explicit deployment
step and for recording the migration in release automation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.database import create_tables


if __name__ == "__main__":
    create_tables()
    print("Agent profile and knowledge chunk schema are ready.")
