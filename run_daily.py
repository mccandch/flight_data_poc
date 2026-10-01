"""Entry point for Windows Task Scheduler (run with pythonw.exe, so no console window).
Writes everything to output/serpapi/logs/run_YYYY-MM-DD.log. Exit code 1 on failure so the
task's retry setting kicks in; retries are safe because searches already done today are skipped."""
import sys
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
log_dir = HERE / "output" / "serpapi" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)

with open(log_dir / f"run_{datetime.now():%Y-%m-%d}.log", "a", encoding="utf-8") as log:
    sys.stdout = sys.stderr = log
    print(f"\n===== run started {datetime.now():%Y-%m-%d %H:%M:%S} =====")
    try:
        import serpapi_collect
        sys.argv = ["serpapi_collect.py"]
        serpapi_collect.main()
        status = 0
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else 1
    except Exception:
        traceback.print_exc()
        status = 1
    print(f"===== run finished {datetime.now():%H:%M:%S} (exit {status}) =====")
sys.exit(status)
