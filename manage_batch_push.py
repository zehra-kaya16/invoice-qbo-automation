"""Offline reconciliation only: never resend QBO writes."""
import argparse
import json
from app.services.persistence.batch_push import recover_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recover-run", required=True)
    parser.add_argument("--all-processes-stopped", action="store_true")
    args = parser.parse_args()
    if not args.all_processes_stopped:
        parser.error("Stop ALL API/worker processes first, then provide --all-processes-stopped")
    try:
        print(json.dumps(recover_run(args.recover_run), ensure_ascii=False))
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
