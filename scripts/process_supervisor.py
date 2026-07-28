"""Bounded child-process execution shared by OCR and benchmark commands."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional


def kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    if hasattr(os, "killpg"):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def read_capture(path: Path, limit: int) -> str:
    try:
        data = path.read_bytes()[: limit + 1]
    except OSError:
        return ""
    suffix = " [truncated]" if len(data) > limit else ""
    return data[:limit].decode("utf-8", errors="replace") + suffix


def run_captured_process(
    command: list[str],
    *,
    work_dir: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    timeout_label: str,
    timeout_error_factory: Optional[Callable[[list[str], int], BaseException]] = None,
) -> subprocess.CompletedProcess[str]:
    work_dir.mkdir(parents=True, exist_ok=True)
    stdout_tmp = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=work_dir,
        prefix=".stdout-",
        suffix=".part",
    )
    stderr_tmp = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=work_dir,
        prefix=".stderr-",
        suffix=".part",
    )
    stdout_path = Path(stdout_tmp.name)
    stderr_path = Path(stderr_tmp.name)
    stdout_tmp.close()
    stderr_tmp.close()
    process: Optional[subprocess.Popen[bytes]] = None
    try:
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = subprocess.Popen(
                command,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                try:
                    if (
                        stdout_path.stat().st_size > max_output_bytes
                        or stderr_path.stat().st_size > max_output_bytes
                    ):
                        kill_process_tree(process)
                        process.wait()
                        raise RuntimeError(f"{timeout_label} process output exceeded the capture limit")
                except FileNotFoundError:
                    pass
                if time.monotonic() >= deadline:
                    kill_process_tree(process)
                    process.wait()
                    if timeout_error_factory is not None:
                        raise timeout_error_factory(command, timeout_seconds)
                    raise RuntimeError(f"{timeout_label} timed out after {timeout_seconds}s")
                time.sleep(0.05)
            return_code = process.wait()
            if (
                stdout_path.stat().st_size > max_output_bytes
                or stderr_path.stat().st_size > max_output_bytes
            ):
                raise RuntimeError(f"{timeout_label} process output exceeded the capture limit")
        return subprocess.CompletedProcess(
            command,
            return_code,
            stdout=read_capture(stdout_path, max_output_bytes),
            stderr=read_capture(stderr_path, max_output_bytes),
        )
    except OSError as exc:
        raise RuntimeError(f"{timeout_label} process could not start: {exc}") from exc
    finally:
        if process is not None and process.poll() is None:
            kill_process_tree(process)
            process.wait()
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
