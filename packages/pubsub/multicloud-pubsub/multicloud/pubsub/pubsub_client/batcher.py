"""Generic in-memory batcher for grouping items and processing in batches.

The Batcher is a reusable, thread-safe batching infrastructure that groups
items based on configurable size and byte constraints, then processes them
in parallel batches via a handler function.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Condition, Lock
from typing import Callable, List, Optional, Protocol, TypeVar, runtime_checkable

from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
    SubstrateSdkException,
)

T = TypeVar("T")


class Batcher:
    """Generic in-memory batcher for grouping items and processing in batches.

    Implements context manager protocol for automatic cleanup. Always use
    shutdown_and_drain() in a finally block or use the context manager:

        with Batcher(handler) as batcher:
            batcher.add(item)

    The __del__ method provides a safety net, but explicit cleanup is preferred.
    """

    @runtime_checkable
    class SizableItem(Protocol):
        """Optional protocol for byte-size-aware batching."""

        def get_byte_size(self) -> int:
            """Return the item size in bytes."""

    @dataclass
    class Options:
        """Configuration options for the Batcher.

        Options is mutable to allow runtime-style configuration during setup
        before passing into Batcher. Validation is enforced in __post_init__.
        """

        max_handlers: int = 1
        min_batch_size: int = 1
        max_batch_size: int = 0
        max_batch_byte_size: int = 0

        def __post_init__(self) -> None:
            """Validate options after dataclass initialization."""
            if self.max_handlers <= 0:
                raise InvalidArgumentException("max_handlers must be greater than 0")
            if self.min_batch_size == 0:
                raise InvalidArgumentException("min_batch_size must be at least 1")
            if self.min_batch_size < 0:
                raise InvalidArgumentException("min_batch_size cannot be negative")
            if self.max_batch_size < 0:
                raise InvalidArgumentException("max_batch_size cannot be negative")
            if self.max_batch_byte_size < 0:
                raise InvalidArgumentException("max_batch_byte_size cannot be negative")

    def __init__(
        self,
        handler: Callable[[List[T]], None],
        options: Optional["Batcher.Options"] = None,
    ) -> None:
        self._options = options or Batcher.Options()
        self._handler = handler
        # Use a fixed-size pool so the underlying thread count never exceeds maxHandlers.
        self._lock = Lock()
        self._work_completed = Condition(self._lock)
        self._pending: List[tuple[T, Future[None]]] = []
        self._active_handlers = 0
        self._shutdown = False
        self._executor_service = ThreadPoolExecutor(
            max_workers=self._options.max_handlers, thread_name_prefix="Batcher-Handler"
        )

    def add_no_wait(self, item: T) -> Future[None]:
        """Add an item to the batcher and return immediately."""
        with self._lock:
            future = self._enqueue_item_locked(item)
            # Try to start a new batch handler if we have capacity
            self._try_start_new_batch_handler(ignore_min_batch_size=False)
            return future

    def add_all_no_wait(self, items: List[T]) -> List[Future[None]]:
        """Add a batch of items atomically and return immediately.

        All items are enqueued under a single lock hold before any handler is
        started, so a caller-supplied batch coalesces into one handler pass
        instead of fanning out into a thread-timing-dependent number of them.
        """
        futures: List[Future[None]] = []
        with self._lock:
            for item in items:
                futures.append(self._enqueue_item_locked(item))

            # Start the handler once, after the whole batch is pending.
            self._try_start_new_batch_handler(ignore_min_batch_size=False)
        return futures

    def _enqueue_item_locked(self, item: T) -> Future[None]:
        """Validate and enqueue one item.

        Caller must hold self._lock.
        """
        if item is None:
            raise InvalidArgumentException("Item cannot be null")

        future: Future[None] = Future()
        if self._shutdown:
            future.set_exception(FailedPreconditionException("Batcher is shut down"))
            return future

        item_size = self._get_item_byte_size(item)
        if (
            self._options.max_batch_byte_size > 0
            and item_size > self._options.max_batch_byte_size
        ):
            future.set_exception(
                InvalidArgumentException(
                    f"Item size {item_size} exceeds maximum batch byte size = "
                    f"{self._options.max_batch_byte_size}"
                )
            )
            return future

        self._pending.append((item, future))
        return future

    def add(self, item: T) -> None:
        """Add an item and block until processing completes."""
        try:
            self.add_no_wait(item).result()
        except Exception as e:
            if isinstance(e, SubstrateSdkException):
                raise
            raise FailedPreconditionException("Error executing batch handler") from e

    def shutdown_and_drain(self) -> None:
        """Stop accepting new work and flush pending items."""
        with self._lock:
            # Prevent new items
            self._shutdown = True
            # Wait for all work to complete using event-driven approach
            while True:
                # Process remaining items and wait for completion
                while self._try_start_new_batch_handler(ignore_min_batch_size=True):
                    # Eagerly start handlers for any remaining items
                    pass
                if not self._pending and self._active_handlers == 0:
                    break
                # If there's still work in progress, wait for signal
                self._work_completed.wait()

        # Now shutdown the executor
        self._executor_service.shutdown(wait=True, cancel_futures=False)

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown

    def __enter__(self) -> "Batcher[T]":
        """Enter context manager."""
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        """Exit context manager and ensure cleanup."""
        try:
            self.shutdown_and_drain()
        except Exception:
            if exc_type is None:
                raise

    def __del__(self) -> None:
        """Cleanup safety net - shutdown executor if not already done."""
        if not self._shutdown:
            self._executor_service.shutdown(wait=False)

    def _try_start_new_batch_handler(self, ignore_min_batch_size: bool) -> bool:
        if self._active_handlers >= self._options.max_handlers:
            return False
        batch = self._get_next_batch(ignore_min_batch_size)
        if not batch:
            return False
        self._active_handlers += 1
        self._executor_service.submit(self._process_handler, batch)
        return True

    def _get_next_batch(
        self, ignore_min_batch_size: bool
    ) -> Optional[List[tuple[T, Future[None]]]]:
        if not self._pending:
            return None
        if (
            not ignore_min_batch_size
            and len(self._pending) < self._options.min_batch_size
        ):
            return None

        # Optimization: If there's no byte size limit and all pending items can fit into a single
        # batch (i.e., less than maxBatchSize), we can take a fast path and avoid the iterator.
        if self._options.max_batch_byte_size == 0 and (
            self._options.max_batch_size == 0
            or len(self._pending) <= self._options.max_batch_size
        ):
            batch = list(self._pending)
            self._pending.clear()
            return batch

        # Build batch respecting size and byte constraints
        batch: List[tuple[T, Future[None]]] = []
        batch_byte_size = 0
        i = 0
        while i < len(self._pending):
            item, future = self._pending[i]
            item_byte_size = self._get_item_byte_size(item)
            reached_max_size = (
                self._options.max_batch_size > 0
                and len(batch) + 1 > self._options.max_batch_size
            )
            reached_max_byte_size = (
                self._options.max_batch_byte_size > 0
                and batch_byte_size + item_byte_size > self._options.max_batch_byte_size
            )
            if reached_max_size or reached_max_byte_size:
                break
            batch.append((item, future))
            batch_byte_size += item_byte_size
            i += 1

        if not batch:
            return None
        self._pending = self._pending[i:]
        return batch

    def _process_handler(self, initial_batch: List[tuple[T, Future[None]]]) -> None:
        batch = initial_batch
        try:
            while batch:
                # Extract items from the batch
                values = [item for item, _ in batch]
                error: Optional[Exception] = None
                try:
                    # Process the batch
                    self._handler(values)
                except (
                    Exception
                ) as exc:  # noqa: BLE001 — catch handler errors to set exception on futures
                    error = exc

                # Complete all futures in the batch
                for _, future in batch:
                    if error is None:
                        future.set_result(None)
                    else:
                        future.set_exception(error)

                # Check for more work
                with self._lock:
                    batch = self._get_next_batch(ignore_min_batch_size=False)
                    if not batch:
                        if self._pending and self._active_handlers == 1:
                            batch = self._get_next_batch(ignore_min_batch_size=True)
                        if not batch:
                            self._active_handlers -= 1
                            self._work_completed.notify_all()
        except (
            Exception
        ) as exc:  # noqa: BLE001 — fail all pending futures on thread crash
            # When a handler thread fails with an unexpected exception,
            # fail all pending items and exit.
            with self._lock:
                for item, future in self._pending:
                    future.set_exception(exc)
                self._pending.clear()
                self._active_handlers -= 1
                self._work_completed.notify_all()

    @staticmethod
    def _get_item_byte_size(item: T) -> int:
        if isinstance(item, Batcher.SizableItem):
            return item.get_byte_size()
        return 0

    @staticmethod
    def split(n_items: int, options: Optional[Batcher.Options]) -> List[int]:
        """Split n items into batch sizes based on the given options."""
        opts = options or Batcher.Options()
        if n_items < opts.min_batch_size:
            return []
        if opts.max_batch_size == 0:
            return [n_items]
        batches: List[int] = []
        remaining = n_items
        while remaining >= opts.min_batch_size and len(batches) < opts.max_handlers:
            size = min(remaining, opts.max_batch_size)
            batches.append(size)
            remaining -= size
        return batches
