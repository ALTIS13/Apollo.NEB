"""Persist the owner panel's single public DevFee pause flag."""

from pathlib import Path
from threading import Lock

from neb_core import store
from neb_devfee import _MAX_BODY, _parse


class OwnerPolicy:
    def __init__(self, path):
        self._path = Path(path)
        self._lock = Lock()

    def _read(self):
        try:
            with self._path.open("rb") as source:
                data = source.read(_MAX_BODY + 1)
            if len(data) > _MAX_BODY:
                raise ValueError("Policy file is too large")
            return {"paused": _parse(data) == "paused"}
        except FileNotFoundError:
            return {"paused": True}
        except (OSError, ValueError, RecursionError):
            raise OSError("Настройка DevFee недоступна.") from None

    def status(self):
        with self._lock:
            return self._read()

    def set_paused(self, paused):
        if type(paused) is not bool:
            raise ValueError("Для паузы DevFee требуется логическое значение.")
        with self._lock:
            self._read()
            policy = {"paused": paused}
            store(self._path, policy)
            return policy
