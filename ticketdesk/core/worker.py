# SPDX-License-Identifier: GPL-2.0-or-later
"""Run blocking work (HTTP, SQLite writes) off the UI thread and deliver results back to it."""

import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)


def glib_dispatch(fn, *args):
    """Call fn(*args) on the GLib main loop (the GTK thread)."""
    from gi.repository import GLib

    def call():
        fn(*args)
        return GLib.SOURCE_REMOVE
    GLib.idle_add(call)


def direct_dispatch(fn, *args):
    """Call immediately on the worker thread; used by tests."""
    fn(*args)


class Worker:
    def __init__(self, dispatch=glib_dispatch, max_workers=6):
        self._dispatch = dispatch
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='ticketdesk')

    def run(self, fn, *args, on_done=None, on_error=None):
        """Run fn(*args) in the pool; on_done(result) / on_error(exc) run on the UI thread."""
        future = self._pool.submit(fn, *args)

        def finished(f):
            try:
                result = f.result()
            except Exception as e:  # noqa: BLE001 - delivered to the caller
                if on_error:
                    self._dispatch(on_error, e)
                else:
                    log.warning('background task failed: %s', e, exc_info=e)
                return
            if on_done:
                self._dispatch(on_done, result)
        future.add_done_callback(finished)
        return future

    def shutdown(self, wait=False):
        self._pool.shutdown(wait=wait, cancel_futures=True)
