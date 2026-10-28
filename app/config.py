from pathlib import Path
import os

# DB config
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("INVESTIGATION_DB", BASE_DIR / "tasks" / "data" / "seed.sqlite")).resolve()
MAX_ROWS = 100
QUERY_SECONDS = 2
MAX_CELL_CHARS = 2000

# LLMs config
MAX_TOOL_CALLS = 6
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
