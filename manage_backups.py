"""Encrypted application backups and explicit restore/release controls."""
import argparse
import json
from app.core import database
from app.services.deployment import backups,restore_guard


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    create=commands.add_parser('create');create.add_argument('--output',required=True);create.add_argument('--all-processes-stopped',action='store_true')
    verify=commands.add_parser('verify');verify.add_argument('--file',required=True)
    restore=commands.add_parser('restore');restore.add_argument('--file',required=True);restore.add_argument('--apply',action='store_true');restore.add_argument('--cloud-read-only',action='store_true');restore.add_argument('--all-processes-stopped',action='store_true')
    commands.add_parser('status')
    release=commands.add_parser('release');release.add_argument('--restore-id',required=True);release.add_argument('--external-changes-reviewed',action='store_true');release.add_argument('--all-processes-stopped',action='store_true')
    args=parser.parse_args()
    if args.command in {'create','restore','release'} and not args.all_processes_stopped:parser.error('Stop all API/worker/beat processes, then pass --all-processes-stopped')
    if args.command=='release' and not args.external_changes_reviewed:parser.error('Review QBO changes and email logs after the snapshot before releasing the hold; pass --external-changes-reviewed')
    try:
        if args.command=='create':result=backups.create(database.engine,args.output)
        elif args.command=='verify':result=backups.verify(args.file)
        elif args.command=='restore':result=backups.restore(database.engine,args.file,apply=args.apply,cloud_read_only=args.cloud_read_only)
        elif args.command=='status':result=restore_guard.status()
        else:result=restore_guard.release(args.restore_id)
        print(json.dumps(result,sort_keys=True))
    except Exception as exc:
        parser.exit(1,(str(exc) if type(exc) in {ValueError,backups.BackupError} else 'Backup operation failed; check schema, key, storage, file permissions and available space')+'\n')


if __name__=='__main__':main()
