import os
import sys
import tempfile
from pathlib import Path

# isolated database, offline, no LLM - must be set before the app is imported
_tmp = tempfile.mkdtemp(prefix="tmdcde_test_")
os.environ["TMD_CDE_DB_URL"] = f"sqlite:///{Path(_tmp) / 'test.db'}"
os.environ["TMD_CDE_DATA_DIR"] = _tmp
os.environ["TMD_CDE_ONLINE"] = "0"
os.environ["TMD_CDE_LLM_PROVIDER"] = "none"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
