import sys
from pathlib import Path

# Make the orchestrator/ root importable so tests can `from app...`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
