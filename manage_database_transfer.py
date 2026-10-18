"""Consistent SQLite snapshot, then offline import into empty PostgreSQL."""
import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from app.core import database
from app.core.config import settings
from app.services.deployment.transfer import transfer, TransferError


def snapshot(output, source=None):
    if source is None:
        url=make_url(settings.database_url or 'sqlite:///./receipt_ai.db')
        if url.get_backend_name()!='sqlite' or not url.database or url.database==':memory:':
            raise TransferError('Snapshot requires the existing file-based SQLite database')
        source=url.database
    source,output=Path(source).resolve(),Path(output).resolve()
    if not source.is_file():raise TransferError('Source SQLite file does not exist')
    if source==output:raise TransferError('Snapshot source and output must differ')
    output.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    try:
        deadline=time.monotonic()+60
        def progress(*args):
            if time.monotonic()>deadline:raise TransferError('Snapshot timed out; confirm every source process is stopped')
        with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as original,sqlite3.connect(output) as copy:
            original.backup(copy,pages=100,progress=progress)
            if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise TransferError('SQLite snapshot integrity check failed')
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {'mode':'snapshot','output':str(output),'integrity':'ok'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    save=commands.add_parser('snapshot');save.add_argument('--source');save.add_argument('--output',required=True)
    copy=commands.add_parser('import');copy.add_argument('--source',required=True)
    copy.add_argument('--local-from',help='Original complete uploads directory mounted read-only')
    copy.add_argument('--apply',action='store_true',help='Copy only into an empty target; default is validation only')
    for child in [save,copy]:child.add_argument('--all-processes-stopped',action='store_true')
    args=parser.parse_args()
    if not args.all_processes_stopped:
        parser.error('Stop every local/Docker API and worker first; pass --all-processes-stopped')
    source=None
    try:
        if args.command=='snapshot':
            result=snapshot(args.output,args.source)
        else:
            path=Path(args.source).resolve()
            if not path.is_file():raise TransferError('Source SQLite snapshot does not exist')
            source=create_engine(URL.create('sqlite',database='file:'+path.as_posix(),query={'mode':'ro','uri':'true'}))
            result=transfer(source,database.engine,local_from=args.local_from,apply=args.apply)
        print(json.dumps(result,sort_keys=True))
    except Exception as exc:
        parser.exit(1,(str(exc) if isinstance(exc,TransferError) else 'Transfer/snapshot failed; no target row commit; inspect source/schema/storage and file paths')+'\n')
    finally:
        if source is not None:source.dispose()


if __name__=='__main__':
    main()
