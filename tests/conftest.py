import atexit
import os
import shutil
import sys
import tempfile

# Must run before research_paper_search is imported: it reads configuration
# from the environment at import time. Indexes go to a throwaway directory so
# tests never touch the user's real index cache.
_INDEX_DIR = tempfile.mkdtemp(prefix="rps-test-index-")
atexit.register(shutil.rmtree, _INDEX_DIR, ignore_errors=True)
os.environ["RPS_INDEX_DIR"] = _INDEX_DIR
os.environ.setdefault("RPS_LOG_LEVEL", "CRITICAL")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
for path in (ROOT, TESTS):
    if path not in sys.path:
        sys.path.insert(0, path)
