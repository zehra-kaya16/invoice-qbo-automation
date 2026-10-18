"""Operator checks for the PostgreSQL/Redis/Celery deployment."""
import argparse
import json
from app.services.deployment import validate_runtime, check_stack, beat_healthy, worker_healthy


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['validate-runtime','check','beat-health','worker-health'])
    args=parser.parse_args()
    try:
        if args.command=='validate-runtime':
            validate_runtime()
        elif args.command=='check':
            print(json.dumps(check_stack()))
        elif not (beat_healthy() if args.command=='beat-health' else worker_healthy()):
            parser.exit(1,'Service health check failed\n')
    except Exception as exc:
        # URL/passwords, SMTP destinations, SQL bind parameters never go to logs.
        parser.exit(1, (str(exc) if type(exc) is ValueError else 'Deployment check failed; inspect service status and configuration')+'\n')


if __name__=='__main__':
    main()
