import os
import signal
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StopController:
    def __init__(self, stop_flag_path: str | None = None, enable_signal_stop: bool = True):
        self.stop_flag_path = stop_flag_path
        self.enable_signal_stop = enable_signal_stop
        self._requested = False
        self._reason = ""
        self._requested_at = ""
        self._signal_count = 0
        self._previous_handler = None

        if self.enable_signal_stop and hasattr(signal, "SIGINT"):
            self._previous_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._handle_sigint)

    def _request_stop(self, reason: str) -> None:
        if self._requested:
            return
        self._requested = True
        self._reason = reason
        self._requested_at = utc_now_iso()

    def _handle_sigint(self, signum, frame) -> None:
        self._signal_count += 1
        if self._signal_count == 1:
            self._request_stop("signal")
            print("\nGraceful stop requested (Ctrl+C). Finishing current unit before stopping...")
            return
        raise KeyboardInterrupt("Forced interrupt requested (second Ctrl+C).")

    def poll(self) -> bool:
        if self._requested:
            return True

        if self.stop_flag_path and os.path.exists(self.stop_flag_path):
            self._request_stop("flag_file")
        return self._requested

    def stop_requested(self) -> bool:
        return self.poll()

    def stop_reason(self) -> str:
        self.poll()
        return self._reason

    def requested_at(self) -> str:
        self.poll()
        return self._requested_at

    def close(self) -> None:
        if self.enable_signal_stop and self._previous_handler is not None and hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, self._previous_handler)
            self._previous_handler = None

