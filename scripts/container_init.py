#!/usr/bin/env python3
"""Create private local Compose DB secret; never print the password or replace it."""
import argparse
import os
from pathlib import Path
import secrets


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=Path('.local-container'))
    args=parser.parse_args()
    folder=args.directory.resolve()
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    folder.chmod(0o700)
    password=folder/'postgres.password'
    try:
        fd=os.open(password,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        if password.is_symlink() or password.stat().st_mode & 0o077:
            raise ValueError('Existing password must be a private regular file')
    else:
        with os.fdopen(fd,'w') as output:output.write(secrets.token_urlsafe(40)+'\n')
    env=folder/'compose.env'
    if not env.exists():
        fd=os.open(env,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as output:output.write(f'AETERNITAS_DB_PASSWORD_FILE={password}\n')
    print(f'Private Compose secret ready. Use: docker compose --env-file {env} up --build -d')


if __name__=='__main__':main()
