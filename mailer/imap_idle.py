"""IMAP IDLE 推送监听。

轮询的问题是要么慢、要么费连接；IDLE（RFC 2177）能让服务器在信箱变化时立刻推一个
untagged EXISTS，客户端收到再去取信即可。这里用一个独立线程跑常驻连接，
只负责"有没有新邮件"的信号，取信仍然交给 imap_client 的短连接去做，互不干扰。

要点：
- IDLE 前必须 select 成功；服务端回 "+ idling" 才算进入 idle
- 服务端一般允许 idle 约 29 分钟，所以每次最多等 idle_timeout 秒就 DONE 重进
- 退出 idle 要发 DONE，并读完这条命令的 tag 响应，否则连接状态会错乱
- SSL 连接要额外看 socket.pending()，否则已解密的数据可能等不到 select 可读
"""

from __future__ import annotations

import contextlib
import logging
import select
import threading
import time
from typing import Callable

from .imap_client import IMAPSettings, _close, _connect

DEFAULT_IDLE_TIMEOUT = 1700
DEFAULT_POLL_SLICE = 30


class IdleWatcher:
    """在后台线程里维持一条 IDLE 连接，有新邮件时回调 on_new_mail。"""

    def __init__(
        self,
        settings: IMAPSettings,
        on_new_mail: Callable[[], None],
        stop_event: threading.Event | None = None,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.on_new_mail = on_new_mail
        self.stop_event = stop_event or threading.Event()
        self.idle_timeout = max(60, idle_timeout)
        self.logger = logger or logging.getLogger(__name__)
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"imap-idle-{self.settings.username}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=10)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------------------------------------------------------------- 内部实现

    def _run(self) -> None:
        backoff = 5
        while not self.stop_event.is_set():
            client = None
            try:
                client = _connect(self.settings)
                typ, _ = client.select(self.settings.mailbox, readonly=True)
                if typ != "OK":
                    raise ValueError(f"无法打开信箱 {self.settings.mailbox}")
                self.logger.info(
                    "imap idle: %s 已连接 %s", self.settings.username, self.settings.host
                )
                backoff = 5
                self._idle_loop(client)
            except Exception as exc:  # noqa: BLE001 - 断线就退避重连，不往外抛
                if not self.stop_event.is_set():
                    self.logger.warning(
                        "imap idle: %s 连接中断: %s", self.settings.username, exc
                    )
            finally:
                if client is not None:
                    _close(client)
            if self.stop_event.is_set():
                break
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, 300)

    def _idle_loop(self, client) -> None:
        while not self.stop_event.is_set():
            woke = self._wait_for_event(client)
            if woke:
                self._safe_notify()

    def _wait_for_event(self, client) -> bool:
        tag = client._new_tag()  # noqa: SLF001 - imaplib 没有公开的 tag 生成接口
        client.send(tag + b" IDLE\r\n")

        # 等 "+ idling"，期间如果直接拿到 tag 响应说明服务端拒绝了 IDLE
        while True:
            line = client.readline()
            if not line:
                raise ConnectionError("IMAP 连接被服务端关闭")
            if line.startswith(b"+"):
                break
            if line.startswith(tag):
                raise ConnectionError(f"服务端拒绝 IDLE: {line!r}")

        deadline = time.time() + self.idle_timeout
        woke = False
        try:
            while time.time() < deadline and not self.stop_event.is_set():
                if not self._socket_readable(client, min(DEFAULT_POLL_SLICE, 5)):
                    continue
                line = client.readline()
                if not line:
                    raise ConnectionError("IMAP 连接在 idle 期间断开")
                upper = line.upper()
                if b"EXISTS" in upper or b"RECENT" in upper:
                    woke = True
                    break
        finally:
            client.send(b"DONE\r\n")
            while True:
                line = client.readline()
                if not line:
                    raise ConnectionError("IMAP 连接在退出 idle 时断开")
                if line.startswith(tag):
                    break
        return woke

    @staticmethod
    def _socket_readable(client, timeout: float) -> bool:
        sock = getattr(client, "sock", None)
        if sock is None:
            return False
        pending = getattr(sock, "pending", None)
        if callable(pending):
            with contextlib.suppress(Exception):
                if pending():
                    return True
        try:
            ready, _, _ = select.select([sock], [], [], timeout)
        except (OSError, ValueError) as exc:
            raise ConnectionError(f"select 失败: {exc}") from exc
        return bool(ready)

    def _safe_notify(self) -> None:
        try:
            self.on_new_mail()
        except Exception:  # noqa: BLE001 - 回调异常不应该打断监听
            self.logger.exception("imap idle: 新邮件回调失败")
