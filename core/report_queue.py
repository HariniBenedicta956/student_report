import heapq
import itertools
import logging
import queue
import threading
import time

import config
from core import ollama_client

log = logging.getLogger(__name__)


class TaskContext:
    """Passed to the handler for the work it is about to do."""

    def __init__(self, attempt, hosts, worker, last_error=None):
        self.attempt = attempt      # 1 on the first try, 2+ on a requeued retry
        self.hosts = hosts          # host order this worker should prefer
        self.worker = worker
        # Why the previous attempt failed, or None on the first try. Handlers start
        # from scratch on a retry, so without this the record of what went wrong is
        # lost the moment a later attempt succeeds -- and a report that took four
        # goes would be indistinguishable from one that worked first time.
        self.last_error = last_error


class QueueStats:
    def __init__(self):
        self.total = 0
        self.completed = 0
        self.failed = 0
        self.retries = 0
        self.elapsed_s = 0.0
        self.workers_started = 0

    def as_dict(self):
        return {
            "total": self.total, "completed": self.completed, "failed": self.failed,
            "retries": self.retries, "elapsed_s": round(self.elapsed_s, 2),
            "workers_started": self.workers_started,
        }


class _Task:
    __slots__ = ("seq", "item", "attempts", "last_error")

    def __init__(self, seq, item):
        self.seq = seq          # arrival position -- never changes, orders the queue
        self.item = item
        self.attempts = 0
        self.last_error = None


# Retrying forever is right for a host that is down or a model that returned bad
# JSON -- those come good on their own. It is exactly wrong for a bug in the handler:
# a TypeError from a bad call signature will raise identically on every attempt, so
# an unlimited retry budget turns a crash into a silent infinite loop that never
# surfaces (observed: a stale handler signature spun the queue indefinitely instead
# of failing). These give up immediately regardless of max_attempts.
NON_RETRYABLE = (
    TypeError, AttributeError, NameError, ImportError, SyntaxError, IndentationError,
)


def _rotate(seq, n):
    seq = list(seq)
    if not seq:
        return seq
    n %= len(seq)
    return seq[n:] + seq[:n]


class _Pipeline:
    """
    Priority queue + dynamic worker pool.

    Ordering. Every task carries the position it arrived in, and the queue is keyed
    on (priority, arrival). Fresh work is priority 0 and retries are priority 1, so a
    student being retried never delays a student who has not been attempted yet,
    while within each band the original order is preserved. This is purely about
    *scheduling* -- the order reports are displayed and zipped in comes from the
    manifest, which is fixed at batch creation, so tasks may finish in any order
    without affecting output order.

    Failure handling. A failed task is not retried in place, because that holds its
    worker and blocks everyone behind it. It goes onto a deferred heap with a
    not-before timestamp and the worker immediately takes the next task. A scheduler
    thread moves deferred tasks back into the ready queue once they are due.

    Scaling. Worker count comes from live capacity (see ollama_client.probe_capacity)
    rather than a constant, and the scheduler re-probes periodically and starts more
    workers if capacity grows -- e.g. a GPU coming online, or a second host being
    added to OLLAMA_HOSTS mid-run. Workers are never killed; they exit when the queue
    drains. Each worker gets its own rotation of the host list, so with several hosts
    the load spreads across them instead of every worker hammering the first one.
    """

    def __init__(self, items, handler, hosts_provider, max_workers, max_attempts,
                 base_delay_s, max_delay_s, on_retry, on_give_up, label):
        self._handler = handler
        # Resolved per task rather than captured once, so a host list that changes
        # while a batch is running is actually picked up: a server added to
        # OLLAMA_HOSTS mid-run starts taking work, and a host that was down when the
        # batch started is retried against its current address rather than a stale
        # snapshot. Capturing it up front made in-flight batches permanently blind to
        # both, which defeats the point of being able to add capacity on demand.
        self._hosts_provider = hosts_provider
        self._max_attempts = max_attempts        # 0 == retry forever
        self._base_delay = base_delay_s
        self._max_delay = max_delay_s
        self._on_retry = on_retry
        self._on_give_up = on_give_up
        self._label = f" [{label}]" if label else ""

        self._ready = queue.PriorityQueue()
        self._deferred = []                      # heap of (due_monotonic, seq, task)
        self._cv = threading.Condition()
        self._outstanding = len(items)
        self._done = threading.Event()
        self._threads = []
        self._worker_seq = itertools.count()

        self.stats = QueueStats()
        self.stats.total = len(items)

        for seq, item in enumerate(items):
            self._ready.put((0, seq, _Task(seq, item)))

        self._target_workers = max(1, min(max_workers, len(items)))

    # ---- scheduling -------------------------------------------------------

    def _requeue_later(self, task, delay_s):
        due = time.monotonic() + delay_s
        with self._cv:
            heapq.heappush(self._deferred, (due, task.seq, task))
            self._cv.notify_all()

    def _finish_one(self):
        with self._cv:
            self._outstanding -= 1
            if self._outstanding <= 0:
                self._done.set()
            self._cv.notify_all()

    def _backoff(self, attempts):
        return min(self._base_delay * (2 ** (attempts - 1)), self._max_delay)

    def _scheduler(self):
        last_probe = time.monotonic()
        while not self._done.is_set():
            with self._cv:
                now = time.monotonic()
                while self._deferred and self._deferred[0][0] <= now:
                    _, _, task = heapq.heappop(self._deferred)
                    self._ready.put((1, task.seq, task))   # retries yield to fresh work
                wait = 0.5
                if self._deferred:
                    wait = min(wait, max(self._deferred[0][0] - now, 0.05))
                self._cv.wait(wait)

            if time.monotonic() - last_probe >= config.REPORT_CAPACITY_PROBE_S:
                last_probe = time.monotonic()
                self._maybe_scale_up()

    def _maybe_scale_up(self):
        """Start extra workers if capacity has grown since the run began."""
        try:
            capacity, _ = ollama_client.probe_capacity(self._hosts_provider())
        except Exception:  # noqa: BLE001 -- probing must never break a running batch
            return
        with self._cv:
            remaining = self._outstanding
        target = max(1, min(capacity, config.REPORT_WORKERS_MAX, remaining))
        if target > len(self._threads):
            log.info("queue%s: capacity grew to %d, scaling %d -> %d workers",
                      self._label, capacity, len(self._threads), target)
            self._start_workers(target - len(self._threads))

    # ---- workers ----------------------------------------------------------

    def _worker(self, name, index):
        while not self._done.is_set():
            try:
                _, _, task = self._ready.get(timeout=0.3)
            except queue.Empty:
                continue

            task.attempts += 1
            ctx = TaskContext(task.attempts, _rotate(self._hosts_provider(), index),
                               name, task.last_error)
            try:
                self._handler(task.item, ctx)
            except Exception as exc:  # noqa: BLE001 -- containment is the point
                task.last_error = exc
                fatal = isinstance(exc, NON_RETRYABLE)
                exhausted = fatal or 0 < self._max_attempts <= task.attempts
                if exhausted:
                    log.error("queue%s: giving up on task %d after %d attempt(s)%s: %s",
                               self._label, task.seq, task.attempts,
                               " (not retryable)" if fatal else "", exc)
                    self.stats.failed += 1
                    self._safe(self._on_give_up, task.item, exc, task.attempts)
                    self._finish_one()
                else:
                    delay = self._backoff(task.attempts)
                    self.stats.retries += 1
                    log.warning("queue%s: task %d failed (attempt %d): %s -- requeued in %.0fs",
                                 self._label, task.seq, task.attempts, exc, delay)
                    self._safe(self._on_retry, task.item, exc, task.attempts, delay)
                    self._requeue_later(task, delay)
            else:
                self.stats.completed += 1
                self._finish_one()

    def _safe(self, fn, *args):
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("queue%s: callback failed", self._label)

    def _start_workers(self, count):
        for _ in range(count):
            n = next(self._worker_seq)
            name = f"report-worker-{n}"
            t = threading.Thread(
                target=self._worker, args=(name, n), name=name, daemon=True,
            )
            self._threads.append(t)
            self.stats.workers_started += 1
            t.start()

    def run(self):
        started = time.perf_counter()
        log.info("queue%s: %d task(s), starting %d worker(s) across %d host(s)",
                  self._label, self.stats.total, self._target_workers,
                  len(self._hosts_provider()))
        self._start_workers(self._target_workers)
        scheduler = threading.Thread(target=self._scheduler, name="report-scheduler",
                                      daemon=True)
        scheduler.start()

        self._done.wait()
        with self._cv:
            self._cv.notify_all()
        for t in self._threads:
            t.join(timeout=5)
        scheduler.join(timeout=5)

        self.stats.elapsed_s = time.perf_counter() - started
        log.info("queue%s: done -- %d ok, %d failed, %d retries in %.1fs (%d workers)",
                  self._label, self.stats.completed, self.stats.failed,
                  self.stats.retries, self.stats.elapsed_s, self.stats.workers_started)
        return self.stats


def process(items, handler, hosts=None, workers=None, max_attempts=None,
            base_delay_s=None, max_delay_s=None, on_retry=None, on_give_up=None,
            label=""):
    """
    Run `items` through `handler(item, ctx)` on a priority queue drained by a
    dynamically-sized worker pool. Returns QueueStats once every item has either
    completed or permanently failed.

    Items are queued in arrival order. A handler that raises does not fail the item
    outright -- it is requeued with exponential backoff and the worker moves straight
    on to the next one, so a student that cannot be generated never blocks the rest of
    the batch. Retries are scheduled behind not-yet-attempted work.

    max_attempts=0 (the default, config.REPORT_MAX_ATTEMPTS) means retry forever, so
    a report is eventually produced rather than substituted with a templated one. Set
    it to a positive number to bound that, in which case on_give_up(item, exc,
    attempts) is called and the caller can write a fallback.

    workers=None sizes the pool from live host capacity and grows it during the run
    if capacity increases; pass an integer to pin it.
    """
    items = list(items)
    stats = QueueStats()
    if not items:
        return stats

    if hosts is None:
        def hosts_provider():
            return list(config.OLLAMA_HOSTS) or ["default"]
    elif callable(hosts):
        hosts_provider = hosts
    else:
        snapshot = list(hosts) or ["default"]
        def hosts_provider():
            return snapshot

    if workers is None:
        capacity, detail = ollama_client.probe_capacity(hosts_provider())
        workers = min(capacity, config.REPORT_WORKERS_MAX)
        log.info("queue [%s]: detected capacity %d -- %s", label, capacity, detail)

    return _Pipeline(
        items, handler, hosts_provider,
        max_workers=workers,
        max_attempts=config.REPORT_MAX_ATTEMPTS if max_attempts is None else max_attempts,
        base_delay_s=config.REPORT_RETRY_BASE_DELAY_S if base_delay_s is None else base_delay_s,
        max_delay_s=config.REPORT_RETRY_MAX_DELAY_S if max_delay_s is None else max_delay_s,
        on_retry=on_retry, on_give_up=on_give_up, label=label,
    ).run()
