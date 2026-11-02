"""Lease-based local worker. Claims commit before network I/O; recovery is explicit."""

import signal
import time
from uuid import uuid4

import run_repository as tasks
from research_service import ResearchService
from sources.sec import SourceError


def work(service, *, once=False, poll_seconds=1):
    worker_id=uuid4().hex
    def stop(signum,frame): raise KeyboardInterrupt
    old_term=signal.signal(signal.SIGTERM,stop)
    try:
        print(f'worker={worker_id} ready; lease-based claims; explicit resume only',flush=True)
        while True:
            with service.connection() as control:
                tasks.recover_expired(control,service.root)
                task=tasks.claim(control,service.root,worker_id)
            if task:
                rid=task['run_id']
                print(f'run={rid} step=starting',flush=True)
                service.execute(task)
                with service.connection() as control:
                    row=tasks.get(control,rid)
                print(f"run={rid} step={row['step']} status={row['status']}",flush=True)
            if once: return
            if not task: time.sleep(poll_seconds)
    finally:
        signal.signal(signal.SIGTERM,old_term)
