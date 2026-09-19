"""Bounded recovery diagnostics; never persist audio or dictation text."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import threading


_FIELDS = frozenset({
    'state', 'attempt', 'session', 'generation', 'last_error', 'error',
    'error_type', 'status', 'operation', 'delay', 'elapsed', 'frames',
    'bytes', 'connected', 'ready', 'collecting', 'subscribed', 'pid',
    'reason', 'retry_in', 'frame_size', 'protocol', 'phase', 'seconds',
})


class RecoveryLog:
    def __init__(self, path: Path, *, max_bytes: int = 1024 * 1024,
                 backup_count: int = 3):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.RLock()
        self._handler = None

    def record(self, event: str, **fields) -> bool:
        if not isinstance(event, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', event):
            return False
        row = {'time': datetime.now(timezone.utc).isoformat(), 'event': event}
        for key, value in fields.items():
            if key not in _FIELDS or not isinstance(value, (str, int, float, bool, type(None))):
                continue
            row[key] = value[:4096] if isinstance(value, str) else value
        try:
            message = json.dumps(row, ensure_ascii=False, allow_nan=False)
            with self._lock:
                if self._handler is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._handler = RotatingFileHandler(
                        self.path, maxBytes=self.max_bytes, backupCount=self.backup_count,
                        encoding='utf-8', delay=True)
                    self._handler.setFormatter(logging.Formatter('%(message)s'))
                record = logging.LogRecord('miremote.recovery', logging.INFO,
                                           '', 0, message, (), None)
                # Use the handler's protected stream under our lock; failures remain
                # local and are observable to the caller, never to the audio loop.
                if self._handler.shouldRollover(record):
                    self._handler.doRollover()
                if self._handler.stream is None:
                    self._handler.stream = self._handler._open()
                self._handler.stream.write(message + '\n')
                self._handler.flush()
            return True
        except (OSError, ValueError, TypeError):
            return False

    def close(self):
        with self._lock:
            if self._handler is not None:
                handler = self._handler
                self._handler = None
                try:
                    handler.close()
                except OSError:
                    pass
