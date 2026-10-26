"""The relay between a solution and an interactor (interactive problems).

The two programs are not connected directly: each one talks to the runner through its own pair of
pipes, and one thread copies the bytes across. This costs a little latency per message, but it makes
the verdict deterministic: the relay sees exactly which side closed its output first, and the other
side can learn about it only through the relay (its stdin reaches EOF, or its writes start failing).
Comparing the exit times of the two processes cannot do that: the real gap is far below the jitter
of waking up threads and of isolate's own teardown.

Memory is bounded: at most `BUFFER_BYTES` are held per direction. The relay reads from a side only
while that direction's buffer has room, so a slow reader blocks the writer (backpressure through the
pipe), exactly as with a direct pipe. When a reader is gone, the relay closes the writer's pipe too,
so the writer gets EPIPE / SIGPIPE, again as with a direct pipe.

`closed` lists "solution" / "interactor" in the order their output reached EOF. Two EOFs seen in the
same poll round are a tie (neither side could have reacted to the other): `tie` is set. Any failure
inside the relay is kept in `error`; the relay then closes all its pipe ends, both programs see EOF or
a broken pipe and finish quickly, and the judge reports IE.
"""
from __future__ import annotations

import errno
import os
import select
import threading
from typing import Optional

BUFFER_BYTES = 65536

SOLUTION, INTERACTOR = "solution", "interactor"


class _Direction:
    """Bytes from one program's stdout (src) to the other program's stdin (dst)."""

    def __init__(self, name: str, src: int, dst: int) -> None:
        self.name = name
        self.src: Optional[int] = src
        self.dst: Optional[int] = dst
        self.buf = bytearray()
        self.eof = False
        os.set_blocking(dst, False)

    def wants_read(self) -> bool:
        return self.src is not None and not self.eof and len(self.buf) < BUFFER_BYTES

    def wants_write(self) -> bool:
        return self.dst is not None and bool(self.buf)

    def close_src(self) -> None:
        if self.src is not None:
            _close(self.src)
            self.src = None

    def close_dst(self) -> None:
        if self.dst is not None:
            _close(self.dst)
            self.dst = None


class Relay:
    """Copies solution stdout -> interactor stdin and interactor stdout -> solution stdin."""

    def __init__(self) -> None:
        sol_in, sol_out, int_in, int_out = os.pipe(), os.pipe(), os.pipe(), os.pipe()  # (read, write)
        self.solution_fds = (sol_in[0], sol_out[1])        # (stdin, stdout) for the solution's sandbox
        self.interactor_fds = (int_in[0], int_out[1])      # (stdin, stdout) for the interactor's sandbox
        self._dirs = (_Direction(SOLUTION, sol_out[0], int_in[1]), _Direction(INTERACTOR, int_out[0], sol_in[1]))
        self._wake = os.pipe()  # stop() writes here
        self.closed: list = []
        self.tie = False
        self.error: Optional[BaseException] = None
        self.relayed = {SOLUTION: 0, INTERACTOR: 0}  # bytes copied from each side
        self._thread: Optional[threading.Thread] = None

    def close_program_ends(self) -> None:
        """Call once both programs started: the runner must not keep their ends open (no EOF otherwise)."""
        for fd in (*self.solution_fds, *self.interactor_fds):
            _close(fd)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="interactive-relay", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Call after both programs ended: finishes the relay and closes everything."""
        try:
            os.write(self._wake[1], b"x")
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive() and self.error is None:
                self.error = RuntimeError("the interactive relay did not stop")
        _close(self._wake[1])

    def abort(self) -> None:
        """A program could not be started: close every end so nothing waits on the relay."""
        self.close_program_ends()
        if self._thread is None:
            for d in self._dirs:
                d.close_src()
                d.close_dst()
            _close(self._wake[0])
            _close(self._wake[1])
        else:
            self.stop()

    def first_closed(self) -> Optional[str]:
        """Which program closed its output first; None if neither did or both did in the same round."""
        if not self.closed or self.tie:
            return None
        return self.closed[0]

    # ------------------------------------------------------------------ the loop
    def _run(self) -> None:
        wake = self._wake[0]
        poller = select.poll()
        registered: dict = {}  # fd -> mask currently registered with the poller

        def close(d: _Direction, which: str) -> None:
            fd = getattr(d, which)
            if fd is not None and fd in registered:
                poller.unregister(fd)  # before close(): the fd number may be reused at once
                del registered[fd]
            d.close_src() if which == "src" else d.close_dst()

        try:
            poller.register(wake, select.POLLIN)
            while True:
                masks: dict = {}
                for d in self._dirs:
                    if d.wants_read():
                        masks[d.src] = masks.get(d.src, 0) | select.POLLIN
                    if d.wants_write():
                        masks[d.dst] = masks.get(d.dst, 0) | select.POLLOUT
                if not masks:
                    return  # both outputs ended; everything was delivered or its reader is gone
                for fd in [fd for fd in registered if fd not in masks]:
                    poller.unregister(fd)
                    del registered[fd]
                for fd, mask in masks.items():
                    if registered.get(fd) != mask:
                        (poller.modify if fd in registered else poller.register)(fd, mask)
                        registered[fd] = mask
                ready = dict(poller.poll())
                ended = []
                for d in self._dirs:
                    if d.wants_write() and d.dst in ready:
                        self._write(d, close)
                    if d.wants_read() and d.src in ready:
                        chunk = os.read(d.src, BUFFER_BYTES - len(d.buf))
                        if chunk:
                            self.relayed[d.name] += len(chunk)
                            d.buf += chunk
                            self._write(d, close)  # usually the reader is waiting: deliver in this round
                        else:
                            d.eof = True
                            ended.append(d.name)
                    if d.eof and not d.buf:
                        close(d, "dst")  # output ended and was delivered: the peer now reads EOF
                if ended:
                    if len(ended) == 2 and not self.closed:
                        self.tie = True
                    self.closed.extend(ended)
                if wake in ready:
                    return  # stop() after this round: its EOFs are recorded first
        except BaseException as exc:  # noqa: BLE001  (any relay failure becomes IE)
            self.error = exc
        finally:
            for fd in registered:
                poller.unregister(fd)
            for d in self._dirs:
                d.close_src()
                d.close_dst()
            _close(wake)

    @staticmethod
    def _write(d: _Direction, close) -> None:
        if d.dst is None or not d.buf:
            return
        try:
            n = os.write(d.dst, d.buf)
            del d.buf[:n]
        except BlockingIOError:
            pass
        except OSError as exc:
            if exc.errno != errno.EPIPE:
                raise
            # the reader is gone: what it never read is dropped, and the writer's pipe is closed as well,
            # so the writer gets EPIPE / SIGPIPE just as it would with a direct pipe
            d.buf.clear()
            close(d, "dst")
            close(d, "src")


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
