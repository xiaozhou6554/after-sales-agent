"""目标 MCP/Worker 进程的停止信号等待与退出恢复。

封装 SIGINT/SIGTERM 到停止事件的转换：收到信号后等待循环退出，调用方可在
进程结束前完成资源逆序关闭；不注册信号处理器的环境（如非主线程）按显式
停止事件运行。
"""

from __future__ import annotations

import signal
import threading


class StopAndWait:
    """停止信号等待器：wait 阻塞直到收到停止请求。"""

    def __init__(self) -> None:
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def is_stopped(self) -> bool:
        return self._stop.is_set()

    def register_signals(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum: int, frame: object) -> None:
        self._stop.set()

    def wait(self, check_interval_s: float = 0.5) -> None:
        """阻塞等待停止请求；间隔轮询以保证及时响应信号。"""
        while not self._stop.is_set():
            self._stop.wait(check_interval_s)
