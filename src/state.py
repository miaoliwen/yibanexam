"""断点状态与单实例锁。

单次机会考试最怕两件事：
1. 进程中途死掉，examuserId 丢了 → 下次重跑再调一次 start，可能白扔一次机会；
2. 两个实例同时跑（GUI 点两次、CLI 与 GUI 并行）→ 互相覆盖状态、重复开考。

这里用「原子写 + 文件锁」把两件事按住。所有写入都走 tmp + os.replace，
断电/被杀最多丢最后一次更新，不会留下半截 JSON。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

SCHEMA = 1


# ============================================================ 原子写
def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def log_line(path: Path, text: str) -> None:
    """追加一行运行日志。磁盘满/被占用时静默失败，绝不影响主流程。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


# ============================================================ 考试状态
@dataclass
class ExamState:
    schema: int = SCHEMA
    group_id: int = 0
    exam_id: str = ""
    exam_name: str = ""
    # idle -> starting -> started -> solving -> solved -> submitting -> submitted -> done
    phase: str = "idle"
    examuser_id: str = ""
    started_at: float = 0.0
    deadline_at: float = 0.0
    duration_minutes: float = 0.0
    answers: dict[str, dict] = field(default_factory=dict)
    submitted: bool = False
    submit_attempts: int = 0
    submit_confirmed: bool = False
    last_heartbeat_at: float = 0.0
    last_error: str = ""
    saved_at: str = ""
    updated_at: float = 0.0

    def touch(self) -> None:
        self.updated_at = time.time()

    def answer_count(self) -> int:
        return sum(1 for a in self.answers.values() if a.get("answer") or a.get("text"))

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: Any) -> "ExamState":
        if not isinstance(d, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in d.items() if k in known}
        st = cls(**clean)
        if not isinstance(st.answers, dict):
            st.answers = {}
        st.answers = {
            str(k): v for k, v in st.answers.items() if isinstance(v, dict)
        }
        try:
            st.exam_id = str(st.exam_id or "")
            st.examuser_id = str(st.examuser_id or "")
        except Exception:
            st.exam_id = ""
            st.examuser_id = ""
        return st


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> ExamState | None:
        if not self.path.exists():
            return None
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception:
            self.quarantine("unreadable")
            return None
        if not isinstance(data, dict):
            self.quarantine("not-object")
            return None
        return ExamState.from_dict(data)

    def save(self, st: ExamState) -> Path:
        st.touch()
        st.saved_at = time.strftime("%Y-%m-%d %H:%M:%S")
        atomic_write_text(
            self.path, json.dumps(st.to_dict(), ensure_ascii=False, indent=2)
        )
        return self.path

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def quarantine(self, reason: str) -> Path | None:
        """坏文件留档而不是直接删，方便事后查为什么。"""
        if not self.path.exists():
            return None
        dest = self.path.with_name(
            f"{self.path.name}.corrupt.{int(time.time())}.{reason}"
        )
        try:
            os.replace(self.path, dest)
            return dest
        except OSError:
            return None


# ============================================================ 单实例锁
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.GetExitCodeProcess.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:
            # 判断不了就当活着，宁可拒绝启动也不要并发开考
            return True
    try:
        os.kill(pid, 0)  # POSIX：只探测，不发信号
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


class RunLock:
    """data/run.lock 文件锁，防止同一台机器上两个实例同时操作一场考试。"""

    def __init__(self, path: Path, *, ttl_seconds: int = 12 * 3600):
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                info = self._read_info()
                if info and self._is_live(info):
                    return False
                # 陈旧锁（上次崩溃没清）→ 删掉重试
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            except OSError:
                return False
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "pid": os.getpid(),
                            "started_at": time.time(),
                            "started_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "argv": sys.argv[:4],
                        },
                        f,
                        ensure_ascii=False,
                    )
                self._held = True
                return True
            except OSError:
                return False
        return False

    def release(self) -> None:
        if not self._held:
            return
        info = self._read_info()
        if info and int(info.get("pid") or 0) == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self._held = False

    # ------------------------------------------------------------------
    def _read_info(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _is_live(self, info: dict) -> bool:
        try:
            pid = int(info.get("pid") or 0)
            ts = float(info.get("started_at") or 0)
        except (TypeError, ValueError):
            return False
        if ts and time.time() - ts > self.ttl_seconds:
            return False
        return _pid_alive(pid)
