"""Notification worker and explicit offline integration recovery."""
import argparse
import json
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--recover-receipt')
    mode.add_argument('--recover-notification')
    parser.add_argument('--all-processes-stopped', action='store_true')
    args = parser.parse_args()
    if args.recover_receipt or args.recover_notification:
        if not args.all_processes_stopped:
            parser.error('Stop all API and worker processes first, then pass --all-processes-stopped')
        from app.services.integrations.webhooks import recover_receipt
        from app.services.integrations.notifications import recover_notification
        try:
            print(json.dumps(recover_receipt(args.recover_receipt) if args.recover_receipt else recover_notification(args.recover_notification)))
        except ValueError as exc:
            parser.exit(1, str(exc) + '\n')
        return
    from app.services.integrations.notifications import run_once
    if args.once:
        print(json.dumps({'notification_id': run_once()}))
        return
    try:
        while True:
            if run_once() is None:
                time.sleep(2)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
