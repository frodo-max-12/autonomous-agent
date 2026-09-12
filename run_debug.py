"""
Diagnostic launcher for SemiSales AI Agent.
Runs main.py but keeps the window open and prints the exact exit reason.
Use this instead of `python main.py` when troubleshooting "auto-close" issues.
"""
import sys
import traceback
import faulthandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

faulthandler.enable()

print("=" * 70)
print("SemiSales AI Agent - DEBUG LAUNCHER")
print("This window will stay open after the process exits.")
print("=" * 70)
print()

exit_reason = "unknown"
try:
    import main
    main.main()
    exit_reason = "main() returned normally (uvicorn.run exited without error)"
except KeyboardInterrupt:
    exit_reason = "KeyboardInterrupt (Ctrl+C pressed, or window focus interrupt)"
except SystemExit as e:
    exit_reason = f"SystemExit (code={e.code})"
except BaseException as e:
    exit_reason = f"{type(e).__name__}: {e}"
    print()
    print("=" * 70)
    print("FULL TRACEBACK:")
    print("=" * 70)
    traceback.print_exc()

print()
print("=" * 70)
print(f"EXIT REASON: {exit_reason}")
print("=" * 70)
print()
input("Press Enter to close this window...")
