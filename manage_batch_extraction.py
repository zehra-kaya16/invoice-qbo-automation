"""Run the durable extraction worker or recover a stopped worker's run."""
import argparse
import json
import time

from app.services.persistence.batch_extraction import recover_run, run_once


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Process at most one queued document and exit")
    parser.add_argument("--recover-run")
    parser.add_argument("--all-processes-stopped", action="store_true")
    args = parser.parse_args()
    if args.recover_run:
        if not args.all_processes_stopped:
            parser.error("Stop all Uvicorn and extraction/Celery workers, then add --all-processes-stopped")
        try:
            print(json.dumps(recover_run(args.recover_run), ensure_ascii=True), flush=True)
        except Exception:
            parser.exit(1, "Recovery refused; verify run ID and document/QBO guards\n")
        return
    print("Batch extraction worker ready. Ctrl+C stops it; queued requests remain in the database.", flush=True)
    try:
        while True:
            try:
                result = run_once()
                if result:
                    print(f"{result['run_id']} {result['status']} {result['counts']}", flush=True)
            except Exception:
                print("Worker could not finish a claim. Check running jobs before recovery; no automatic replay.", flush=True)
                if args.once:
                    parser.exit(1, "Worker turn failed\n")
                time.sleep(2)
                continue
            if args.once:
                return
            if result is None:
                time.sleep(2)
    except KeyboardInterrupt:
        print("Worker stopped. Recover an interrupted running job only with all processes stopped.", flush=True)


if __name__ == "__main__":
    main()
