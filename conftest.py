"""
Put the project root on sys.path so tests can import engine/ and agents/
regardless of where pytest is invoked from.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))