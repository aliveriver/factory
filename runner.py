"""Standalone local/SSH experiment supervisor; requires Python 3.10+ only."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def checkpoint(directory: Path, status: dict) -> None:
    """Publish status despite short Windows reader/antivirus sharing locks.

    Args:
        directory: Run evidence directory.
        status: Complete supervisor state.

    Raises:
        PermissionError: If sharing locks persist beyond the bounded retry.
    """
    staging = directory / "status.tmp"
    staging.write_text(json.dumps(status), encoding="utf-8")
    for attempt in range(50):
        try:
            staging.replace(directory / "status.json")
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.02)


def confine_windows_children() -> None:
    """Keep supervisor descendants in a job that dies when the supervisor exits.

    Raises:
        OSError: If Windows cannot enforce the required process lifetime.
    """
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t),
            ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "read_operations",
                "write_operations",
                "other_operations",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", IoCounters),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    # Intentionally keep this OS handle open for the supervisor's lifetime.
    # Process exit closes it and terminates every remaining job descendant.


def main() -> None:
    """Supervise one durable run without importing AstrBot or any harness."""
    directory = Path(sys.argv[1]).resolve()
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    # Exclusive claim prevents uncertain launches from being blindly replayed.
    try:
        (directory / "claimed").mkdir()
    except FileExistsError:
        return
    started = time.time()
    status = {
        "id": request["id"],
        "state": "starting",
        "supervisor_pid": os.getpid(),
        "started": started,
    }
    checkpoint(directory, status)
    process = None
    try:
        if os.name == "nt":
            confine_windows_children()
        with (
            (directory / "stdout.log").open("wb") as stdout,
            (directory / "stderr.log").open("wb") as stderr,
        ):
            process = subprocess.Popen(
                request["argv"],
                cwd=request["cwd"],
                stdout=stdout,
                stderr=stderr,
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
            status.update(state="running", pid=process.pid)
            checkpoint(directory, status)
            while process.poll() is None:
                cancelled = (directory / "cancel").exists()
                expired = time.time() - started >= request["timeout"]
                if cancelled or expired:
                    status["state"] = "cancelled" if cancelled else "timed_out"
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            capture_output=True,
                            check=False,
                            timeout=10,
                        )
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if os.name != "nt":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    break
                time.sleep(0.1)
            code = process.wait()
            if status["state"] == "running":
                status["state"] = "succeeded" if code == 0 else "failed"
            status["exit_code"] = code
    except Exception as exc:
        status.update(state="failed", error=type(exc).__name__)
    finally:
        try:
            if process is not None:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                if process.poll() is None:
                    process.wait(timeout=5)
        except Exception as exc:
            status["cleanup_error"] = type(exc).__name__
        finally:
            status["ended"] = time.time()
            checkpoint(directory, status)


if __name__ == "__main__":
    main()
