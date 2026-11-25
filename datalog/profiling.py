"""Standard-library CPU stack samples and Python allocation snapshots."""

from collections import Counter
from contextlib import contextmanager
import json
import os
import sys
import threading
import time
import tracemalloc
import uuid
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import warnings


def frames(frame):
    names = []
    while frame is not None and len(names) < 64:
        names.append(frame.f_code.co_name[:160])
        frame = frame.f_back
    return tuple(reversed(names))


class Profiler:
    def __init__(self, service, project="local", base_url="http://127.0.0.1:4310", key=None):
        self.service = service
        self.endpoint = base_url.rstrip("/") + "/api/v1/platform/profiles?" + urlencode({"project": project})
        self.key = key or os.environ.get("DATALOG_INGESTION_KEY")

    def publish(self, kind, samples, trace_id, span_id, started_at=None):
        if not samples or not self.key:
            return
        body = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "service": self.service,
            "runtime": "python",
            "trace_id": trace_id,
            "span_id": span_id,
            "stacks": [{"frames": stack, "weight": weight} for stack, weight in samples.most_common(1000)],
        }
        if started_at is not None:
            body["started_at"] = started_at
        try:
            request = Request(
                self.endpoint,
                json.dumps(body).encode(),
                {"Content-Type": "application/json", "Authorization": "Bearer " + self.key},
            )
            with urlopen(request, timeout=5):
                pass
        except OSError:
            warnings.warn("Datalog: failed to export profile", RuntimeWarning, stacklevel=2)

    @contextmanager
    def cpu(self, trace_id=None, span_id=None, interval=0.01, *, _target=None, _started_at=None):
        """Sample the current request thread while it executes."""
        if not 0.002 <= interval <= 1:
            raise ValueError("Profile interval must be between 2 ms and 1 s")
        target, stopped, samples = _target or threading.get_ident(), threading.Event(), Counter()
        if not hasattr(time, "pthread_getcpuclockid"):
            raise RuntimeError("CPU sampling requires a per-thread POSIX clock")
        clock = time.pthread_getcpuclockid(target)

        def sample():
            before = time.clock_gettime_ns(clock)
            while not stopped.wait(interval):
                now = time.clock_gettime_ns(clock)
                current = sys._current_frames().get(target)
                if current is not None and now > before:
                    samples[frames(current)] += now - before
                before = now

        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join()
            self.publish("cpu", samples, trace_id, span_id, _started_at)

    def memory(self, trace_id=None, span_id=None, *, started_at=None):
        """Sample existing allocations after the application enables tracemalloc."""
        if not tracemalloc.is_tracing():
            raise RuntimeError("Enable tracemalloc in the observed process")
        samples = Counter()
        for sample in tracemalloc.take_snapshot().statistics("traceback")[:1000]:
            # Do not include absolute paths or allocated object values.
            stack = tuple(f"{os.path.basename(item.filename)[:140]}:{item.lineno}" for item in sample.traceback[:64])
            samples[stack] += sample.size
        self.publish("memory", samples, trace_id, span_id, started_at)

    @contextmanager
    def continuous(self, *, window=30, interval=0.01, cpu=True, memory=False):
        """Profile the calling service thread in rolling windows while it stays alive."""
        if not self.key or not 5 <= window <= 300 or not 0.002 <= interval <= 1 or not (cpu or memory):
            raise ValueError("Provide a key, a 5 to 300 s window and at least one profile signal")
        if cpu and not hasattr(time, "pthread_getcpuclockid"):
            raise RuntimeError("CPU sampling requires a per-thread POSIX clock")
        target, stopped = threading.get_ident(), threading.Event()
        owns_tracer = memory and not tracemalloc.is_tracing()
        if owns_tracer:
            tracemalloc.start(16)

        def collect():
            while not stopped.is_set():
                started_at = time.time()
                if cpu:
                    with self.cpu(interval=interval, _target=target, _started_at=started_at):
                        stopped.wait(window)
                else:
                    stopped.wait(window)
                if memory:
                    self.memory(started_at=started_at)

        worker = threading.Thread(target=collect, name="datalog-profiler", daemon=True)
        worker.start()
        try:
            yield
        finally:
            stopped.set()
            worker.join()
            if owns_tracer:
                tracemalloc.stop()
