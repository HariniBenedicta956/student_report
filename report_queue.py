import logging
import queue
import threading
import time

log = logging.getLogger(__name__)


class QueueStats:
    """Plain counters for one drained queue -- what finished, what failed, how long."""

    def __init__(self, total):
        self.total = total
        self.completed = 0
        self.failed = 0
        self.elapsed_s = 0.0

    def as_dict(self):
        return {
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "elapsed_s": round(self.elapsed_s, 2),
        }


def process(items, handler, workers=1, on_error=None, label=""):
    """
    Feeds `items` through `handler` as a FIFO queue drained by `workers` threads,
    and returns QueueStats once every item has been handled.

    Why a queue rather than the previous straight `for` loop over the batch:

      * Isolation. Each item is pulled and handled on its own; an exception is
        caught here, reported through on_error, and the worker moves to the next
        item. One student who trips a failure can never abandon the rest of the
        batch, and nothing is shared between items but the arguments the caller
        closed over.
      * A single place to change throughput. `workers` is the one knob, so raising
        concurrency later doesn't mean restructuring the batch runner again.
      * Ordering. Items are handed out in submission order, so students are worked
        in the order the CSV listed them and progress in the UI reads top-to-bottom.

    On concurrency, measured rather than assumed: against the current CPU-only
    Ollama host, three concurrent generations took 8.4s versus 8.1s for the same
    three sequentially (0.96x -- the server served them one at a time and a single
    generation already saturates the CPU). So config.REPORT_WORKERS defaults to 1
    and this runs as an orderly single-worker queue; more workers only pay off once
    the host can genuinely serve more than one request at once. The implementation
    is identical either way -- there is no separate sequential path to keep in sync.

    `handler` must be safe to call from a worker thread. Anything it mutates that
    is shared across items needs its own locking (see storage._manifest_lock).
    """
    items = list(items)
    stats = QueueStats(len(items))
    if not items:
        return stats

    workers = max(1, min(int(workers), len(items)))
    pending = queue.Queue()
    for item in items:
        pending.put(item)

    lock = threading.Lock()
    started = time.perf_counter()
    log.info("queue%s: %d item(s), %d worker(s)",
             f" [{label}]" if label else "", len(items), workers)

    def drain():
        while True:
            try:
                item = pending.get_nowait()
            except queue.Empty:
                return
            try:
                handler(item)
                with lock:
                    stats.completed += 1
            except Exception as exc:  # noqa: BLE001 -- containment is the point
                with lock:
                    stats.failed += 1
                log.exception("queue%s: item failed", f" [{label}]" if label else "")
                if on_error is not None:
                    try:
                        on_error(item, exc)
                    except Exception:  # noqa: BLE001
                        # A failing error handler must not take the worker down and
                        # strand the rest of the queue.
                        log.exception("queue%s: on_error handler itself failed",
                                       f" [{label}]" if label else "")
            finally:
                pending.task_done()

    threads = [
        threading.Thread(target=drain, name=f"report-worker-{i}", daemon=True)
        for i in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stats.elapsed_s = time.perf_counter() - started
    log.info("queue%s: done -- %d ok, %d failed in %.1fs",
             f" [{label}]" if label else "", stats.completed, stats.failed, stats.elapsed_s)
    return stats
