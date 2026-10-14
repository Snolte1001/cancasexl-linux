"""Best-effort CPU accounting; no scheduler/system settings are modified."""
from pathlib import Path
import time


def snapshot(proc=Path('/proc')):
    workers = {}
    errors = []
    for path in proc.glob('[0-9]*'):
        try:
            if (path/'comm').read_text().strip() != 'cancasexl-usb': continue
            fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
            identity = path.name+':'+fields[19]  # PID plus starttime (field 22).
            runtime = int((path/'schedstat').read_text().split()[0])
            workers[identity] = runtime
        except (OSError, ValueError, IndexError) as exc:
            if (path/'comm').exists(): errors.append(f'{path.name}: {type(exc).__name__}')
    return dict(wall=time.monotonic(), process_seconds=time.process_time(),
                workers_ns=workers, errors=errors)


def delta(before, after):
    elapsed = after['wall']-before['wall']
    process = after['process_seconds']-before['process_seconds']
    workers = None
    if (before['workers_ns'] and before['workers_ns'].keys() == after['workers_ns'].keys()
            and all(after['workers_ns'][k] >= v for k,v in before['workers_ns'].items())):
        workers = sum(after['workers_ns'][k]-v for k,v in before['workers_ns'].items())/1e9
    return dict(elapsed_seconds=elapsed, test_process_cpu_seconds=process,
                test_process_percent_one_core=100*process/elapsed if elapsed>0 else None,
                kernel_workers_cpu_seconds=workers,
                kernel_workers_percent_one_core=100*workers/elapsed if workers is not None and elapsed>0 else None,
                worker_count=len(after['workers_ns']),
                note='Kernel worker only; excludes IRQ/softirq work elsewhere. Null means unavailable or worker changed.',
                before=before, after=after)
