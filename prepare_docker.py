"""Prepare separate Docker runtime files without changing the local .env."""
import argparse
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values


def _write(path, text):
    # No clobber: rerunning cannot silently rotate DB/Redis credentials.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)


def prepare(source=Path('.env'), destination=Path('.'), bind='127.0.0.1'):
    source, destination = Path(source), Path(destination)
    if bind not in {'127.0.0.1', '0.0.0.0'}:
        raise ValueError('Bind must be 127.0.0.1 or 0.0.0.0')
    if not source.is_file():
        raise ValueError('Existing local .env is required')
    infra, runtime = destination / '.env.compose', destination / '.env.docker'
    if infra.exists() or runtime.exists():
        raise ValueError('Docker env files already exist; edit them explicitly instead of regenerating credentials')
    try:
        from app.core.config import Settings

        # Read the selected source file explicitly.
        # Values from this file must take priority over the local process
        # environment when preparing a separate Docker configuration.
        source_values = dict(dotenv_values(source))

        encryption_key = source_values.get('QBO_TOKEN_ENCRYPTION_KEY')

        if not encryption_key:
            raise ValueError('Missing encryption key')

        if not isinstance(encryption_key, str):
            raise ValueError('Invalid encryption key')

        Fernet(encryption_key.encode('utf-8'))

        # Reject multiline values before generating Docker env files.
        for key, value in source_values.items():
            if value is None:
                continue

            if any(char in value for char in '\r\n\x00'):
                raise ValueError(
                    f'Multiline Docker environment value: {key}'
                )

        resolved = Settings(
            _env_file=None,
            **source_values,
        ).model_dump()

    except (ValueError, TypeError):
        raise ValueError(
            'Source application settings are invalid; '
            'correct the local configuration first'
        ) from None
    values = {name.upper(): (str(value).lower() if isinstance(value, bool) else str(value))
              for name, value in resolved.items() if value is not None}

    values = {
        name.upper(): (
            str(value).lower() if isinstance(value, bool) else str(value)
        )
        for name, value in resolved.items()
        if value is not None
    }
    
    for value in values.values():
        if any(c in value for c in '\r\n\x00'):
            raise ValueError('Docker runtime values must be single-line; check the source .env')
    try:
        Fernet(values.get('QBO_TOKEN_ENCRYPTION_KEY', '').encode())
    except (ValueError, TypeError):
        raise ValueError('Keep a valid existing QBO_TOKEN_ENCRYPTION_KEY in the source .env') from None
    # Acceptance starts with no outgoing mail. Enable explicitly after importing
    # and checking preferences/outbox. Real AI runs only after explicit queuing.
    values.update(EMAIL_ENABLED='false', LOCAL_STORAGE_PATH='/app/uploads', DEPLOYMENT_MODE='compose')
    for key in ('DATABASE_URL', 'REDIS_URL'):
        values.pop(key, None)  # Compose supplies the shared container URLs.
    destination.mkdir(parents=True, exist_ok=True)
    text = '# Raw env_file: values are literal; do not add surrounding quotes.\n'
    text += ''.join(f'{key}={value}\n' for key, value in sorted(values.items()))
    config = '# Infrastructure credentials. Preserve this file across restarts.\n'
    config += f'POSTGRES_PASSWORD={secrets.token_hex(32)}\nREDIS_PASSWORD={secrets.token_hex(32)}\nAPP_BIND={bind}\nAPP_PORT=8000\n'
    _write(infra, config)
    try:
        _write(runtime, text)
    except Exception:
        infra.unlink()  # Only our newly-created file; never touch an old file.
        raise
    return ['.env.compose', '.env.docker']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', choices=['127.0.0.1','0.0.0.0'], default='127.0.0.1')
    args = parser.parse_args()
    try:
        prepare(bind=args.bind)
    except (ValueError, OSError) as exc:
        parser.exit(1, (str(exc) if isinstance(exc, ValueError) else 'Could not write Docker env files') + '\n')
    print('Created .env.compose and .env.docker. EMAIL_ENABLED=false for acceptance. Keep both files private.')


if __name__ == '__main__':
    main()
