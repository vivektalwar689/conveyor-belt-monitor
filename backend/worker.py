"""Analysis worker as its own process (the production shape).

    python -m backend.worker

Running analysis inside the API server makes ingestion requests and the analysis
cycle compete for the Python GIL, which throttles intake. As a separate process
it gets its own core; the two share only the SQLite database (WAL mode).
"""
from __future__ import annotations

import os
import time
import traceback

from backend.analysis import Analyzer
from backend.app import DB_PATH
from backend.db import connect


def main() -> None:
    con = connect(DB_PATH)
    an = Analyzer(con)
    interval = float(os.environ.get("BELT_ANALYSIS_INTERVAL", "1.0"))
    print(f"analysis worker on {DB_PATH}", flush=True)
    while True:
        try:
            an.cycle()
        except Exception:  # keep going; the error is recorded in the state table by the next cycle
            traceback.print_exc()
        time.sleep(interval)


if __name__ == "__main__":
    main()
