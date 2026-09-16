"""让 `pytest` 在 toyrepo 目录下能 import csvlite（不打成包，保持零依赖）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
