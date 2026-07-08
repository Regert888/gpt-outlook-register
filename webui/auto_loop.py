"""auto-loop 控制器：多 worker 并发，每个 worker 用独立代理。

设计：
  - 主控线程 manage_loop：监听 stop/pause、根据 concurrency 启停 worker
  - 多个 worker 线程：claim_next() → 注册 → 完成 → 继续
  - 代理池：每个 worker 按 worker index 取一个代理（round-robin），避免同 IP 多号
  - 状态机：stopped → running → paused → running / stopped
  - 优雅暂停/停止：当前 worker 跑完才退出，不强杀
  - 复用 registrar.start_registration：每个号开一个 run，由 worker 等其结束
"""
from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time
from typing import Optional

from proxy_utils import mask_proxy_url, parse_proxy_pool

from . import db, registrar


logger = logging.getLogger("auto_loop")


class AutoLoopState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"

class AutoLoopController:
    """多 worker auto-loop 控制器。

    options 关键字段：
      proxy:                单代理（兼容旧版，concurrency=1 时用）
      proxy_pool:           多代理字符串（每行一个；多 worker 会按 worker index 轮流取）
      concurrency:          并发 worker 数（1-20）
      cool_down_seconds:    每个 worker 跑完后冷却时间（默认 3）
      其余参数透传给 registrar.start_registration
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = AutoLoopState.STOPPED
        self._manage_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._options: dict = {}
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = 暂停
        # 进度统计
        self._started_at: float = 0.0
        self._registered_ok = 0
        self._registered_fail = 0
        # 当前每个 worker 在跑啥（worker_id → email）
        self._worker_status: dict[int, dict] = {}
        self._last_message = ""
        # 熔断状态
        self._consecutive_network_fails = 0
        self._circuit_break_threshold = 3
        self._last_break_reason = ""
        # SSE 订阅
        self._subscribers: list[queue.Queue] = []
        # 代理池 / 并发数
        self._proxy_pool: list[str] = []
        self._concurrency: int = 1
        # 代理池轮换状态
        self._auto_rotate_proxy: bool = True
        self._proxy_rotation_interval: int = 5
        self._proxy_rr_index: int = 0
        self._proxy_start_offset: int = 0
        self._accounts_started: int = 0
        self._current_batch_proxy: str = ""

    # ──────────────────────── 公共 API ────────────────────────

    def start(self, options: dict) -> dict:
        with self._lock:
            if self._state in (AutoLoopState.RUNNING, AutoLoopState.PAUSED):
                return {"ok": False, "error": f"已经在跑了 (state={self._state})"}
            # 重置
            self._stop_event.clear()
            self._pause_event.clear()
            self._options = dict(options or {})
            self._state = AutoLoopState.RUNNING
            self._started_at = time.time()
            self._registered_ok = 0
            self._registered_fail = 0
            self._worker_status.clear()
            self._consecutive_network_fails = 0
            self._last_message = "auto-loop 启动"
            # 解析并发参数
            self._concurrency = max(1, min(20, int(self._options.get("concurrency") or 1)))
            pool_text = self._options.get("proxy_pool") or ""
            self._proxy_pool = parse_proxy_pool(pool_text)
            self._auto_rotate_proxy = bool(self._options.get("auto_rotate_proxy", True))
            self._proxy_rotation_interval = max(
                1, int(self._options.get("rotate_proxy_every") or 5)
            )
            # 代理池随机起始位置，避免每次重启都从头开始
            if self._proxy_pool:
                self._proxy_start_offset = random.randint(0, len(self._proxy_pool) - 1)
                self._proxy_rr_index = self._proxy_start_offset
                logger.info(
                    f"[auto-loop] 代理池大小={len(self._proxy_pool)}, "
                    f"随机起始偏移={self._proxy_start_offset}"
                )
            else:
                self._proxy_start_offset = 0
                self._proxy_rr_index = 0
            self._accounts_started = 0
            self._current_batch_proxy = ""
            # 启 manage 线程
            self._manage_thread = threading.Thread(
                target=self._manage_loop, daemon=True, name="auto-loop-manage"
            )
            self._manage_thread.start()
        self._broadcast("state", self._snapshot())
        return {
            "ok": True,
            "state": self._state,
            "concurrency": self._concurrency,
            "proxy_pool_size": len(self._proxy_pool),
        }

    def pause(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.RUNNING:
                return {"ok": False, "error": f"当前 state={self._state}，不可暂停"}
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._last_message = "已请求暂停（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def resume(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.PAUSED:
                return {"ok": False, "error": f"当前 state={self._state}，不可恢复"}
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._last_message = "已恢复"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def stop(self) -> dict:
        with self._lock:
            if self._state == AutoLoopState.STOPPED:
                return {"ok": False, "error": "没在跑"}
            self._stop_event.set()
            self._pause_event.clear()
            self._last_message = "已请求停止（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True}

    def status(self) -> dict:
        return self._snapshot()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        try:
            q.put_nowait({"kind": "state", "data": self._snapshot()})
        except queue.Full:
            pass
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try: self._subscribers.remove(q)
            except ValueError: pass

    # ──────────────────────── 内部 ────────────────────────

    def _snapshot(self) -> dict:
        with self._lock:
            stats = db.stats()
            workers_info = [
                {
                    "id": wid,
                    "email": info.get("email", ""),
                    "run_id": info.get("run_id", ""),
                    "proxy": info.get("proxy", ""),
                    "started_at": info.get("started_at", 0),
                }
                for wid, info in sorted(self._worker_status.items())
            ]
            return {
                "state": self._state,
                "started_at": self._started_at,
                "elapsed": (time.time() - self._started_at) if self._started_at else 0,
                "registered_ok": self._registered_ok,
                "registered_fail": self._registered_fail,
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "auto_rotate_proxy": self._auto_rotate_proxy,
                "rotate_proxy_every": self._proxy_rotation_interval,
                "workers": workers_info,
                "last_message": self._last_message,
                "pool_stats": stats,
            }

    def _broadcast(self, kind: str, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"kind": kind, "data": data})
            except queue.Full:
                pass

    def _set_message(self, msg: str):
        with self._lock:
            self._last_message = msg
        self._broadcast("state", self._snapshot())

    def _proxy_for_worker(self, worker_id: int) -> str:
        """按 worker_id 固定分配代理（不轮换），加上随机起始偏移。空池时回退到 options.proxy。"""
        if self._proxy_pool:
            idx = (worker_id + self._proxy_start_offset) % len(self._proxy_pool)
            proxy = self._proxy_pool[idx]
            logger.info(
                f"[auto-loop] worker-{worker_id} 固定代理: {mask_proxy_url(proxy)} "
                f"(池索引={idx}, 偏移={self._proxy_start_offset})"
            )
            return proxy
        return self._options.get("proxy", "") or ""

    def _pick_proxy_for_run(self, worker_id: int) -> str:
        """根据前端配置决定代理分配策略，并跳过已达使用上限的代理。

        - 未启用轮换：每个 worker 优先固定代理， exhausted 则顺延
        - 启用轮换：每跑 `rotate_proxy_every` 个账号，全局切换到下一条可用代理
        """
        # 单代理模式
        if not self._proxy_pool:
            proxy = self._options.get("proxy", "") or ""
            if proxy:
                if not db.is_proxy_available(proxy):
                    raise RuntimeError(f"代理 {mask_proxy_url(proxy)} 已达到使用上限")
                logger.info(
                    f"[auto-loop] worker-{worker_id} 使用单代理: {mask_proxy_url(proxy)}"
                )
            return proxy

        # 固定代理模式：先按 worker 取，耗尽则顺延找可用
        if not self._auto_rotate_proxy:
            for i in range(len(self._proxy_pool)):
                idx = (worker_id + self._proxy_start_offset + i) % len(self._proxy_pool)
                proxy = self._proxy_pool[idx]
                if db.is_proxy_available(proxy):
                    logger.info(
                        f"[auto-loop] worker-{worker_id} 固定代理: {mask_proxy_url(proxy)} "
                        f"(池索引={idx}, 偏移={self._proxy_start_offset})"
                    )
                    return proxy
            raise RuntimeError("代理池所有代理均已达到使用上限")

        # 轮换模式
        with self._lock:
            rotated = False
            need_rotate = (
                self._accounts_started % self._proxy_rotation_interval == 0
                or not self._current_batch_proxy
                or not db.is_proxy_available(self._current_batch_proxy)
            )
            if need_rotate:
                found = False
                for _ in range(len(self._proxy_pool)):
                    idx = self._proxy_rr_index % len(self._proxy_pool)
                    proxy = self._proxy_pool[idx]
                    self._proxy_rr_index += 1
                    if db.is_proxy_available(proxy):
                        self._current_batch_proxy = proxy
                        found = True
                        break
                if not found:
                    raise RuntimeError("代理池所有代理均已达到使用上限")
                rotated = True

            proxy = self._current_batch_proxy
            self._accounts_started += 1
            account_no = self._accounts_started
            interval = self._proxy_rotation_interval
            logger.info(
                f"[auto-loop] worker-{worker_id} 第 {account_no} 个账号 "
                f"使用代理: {mask_proxy_url(proxy)} "
                f"(本批第 {((account_no - 1) % interval) + 1}/{interval} 个, "
                f"{'已轮换' if rotated else '同批复用'})"
            )
            return proxy

    def _record_finish(self, ok: bool, category: str):
        """worker 结束一个 run 后调，更新计数 + 熔断。"""
        with self._lock:
            if ok:
                self._registered_ok += 1
                self._consecutive_network_fails = 0
            else:
                self._registered_fail += 1
                if category == "network":
                    self._consecutive_network_fails += 1
                else:
                    self._consecutive_network_fails = 0
            self._last_message = (
                f"累计 ok={self._registered_ok} fail={self._registered_fail}"
            )
            trigger_break = (
                self._consecutive_network_fails >= self._circuit_break_threshold
                and self._state == AutoLoopState.RUNNING
            )

        if trigger_break:
            with self._lock:
                self._pause_event.set()
                self._state = AutoLoopState.PAUSED
                self._last_break_reason = (
                    f"连续 {self._consecutive_network_fails} 次网络/环境错误，"
                    f"自动暂停（号已自动 release，请检查代理后点恢复）"
                )
                self._last_message = self._last_break_reason
                self._consecutive_network_fails = 0
            logger.warning(self._last_break_reason)
            self._broadcast("circuit_break", {"reason": self._last_break_reason})

    def _manage_loop(self):
        """主控线程：启动 worker，等所有 worker 结束，更新最终状态。"""
        try:
            workers = []
            for wid in range(self._concurrency):
                t = threading.Thread(
                    target=self._worker_loop, args=(wid,),
                    daemon=True, name=f"auto-loop-worker-{wid}",
                )
                t.start()
                workers.append(t)
                # 每个 worker 之间错开 1s 启动，避免同时打 OpenAI
                time.sleep(1.0)
            self._workers = workers
            # 等所有 worker 退出
            for t in workers:
                t.join()
        except Exception as e:
            logger.exception(f"manage_loop 异常: {e}")
        finally:
            with self._lock:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                # 若中途已被熔断（余额不足 / 连续网络错误）就保留原因，
                # 否则用通用的"已停止（成功/失败）"汇总
                if not self._last_break_reason:
                    self._last_message = (
                        f"已停止（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                    )
                else:
                    self._last_message = (
                        f"已停止：{self._last_break_reason} "
                        f"（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                    )
            self._broadcast("state", self._snapshot())

    def _worker_loop(self, worker_id: int):
        """单 worker 循环：claim → 跑 → 等结束 → 继续。"""
        idle_round = 0
        logger.info(f"[worker-{worker_id}] 启动")

        while True:
            # 检查停止
            if self._stop_event.is_set():
                logger.info(f"[worker-{worker_id}] 已停止")
                return

            # 检查暂停
            if self._pause_event.is_set():
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)
                if self._stop_event.is_set():
                    return

            # 余额下限检查：每个 worker 在 claim 新号前查一次接码平台余额，
            # 不足则触发整体停止（其它 worker 下一轮也会命中并退出）
            try:
                registrar.check_sms_balance()
            except RuntimeError as e:
                reason = str(e)
                logger.warning(f"[worker-{worker_id}] {reason}")
                with self._lock:
                    self._stop_event.set()
                    self._state = AutoLoopState.STOPPED
                    self._last_break_reason = reason
                    self._last_message = reason
                self._broadcast("circuit_break", {"reason": reason})
                return

            # claim 下一个号（CF / OEP 模式用虚拟占位，无需 outlook 号池）
            mail_source = db.get_setting("mail_source", "outlook")
            if mail_source in ("cf_temp", "oep"):
                account = {
                    "email": f"placeholder_{int(time.time())}_{worker_id}@pool.local",
                    "password": "", "client_id": "", "refresh_token": "",
                }
            else:
                account = db.claim_next()
            if not account:
                idle_round += 1
                if idle_round == 1:
                    self._set_message(
                        f"worker-{worker_id} 号池空，等待新号..."
                    )
                # 空 10 轮（约 30s）就停掉这个 worker
                if idle_round >= 10:
                    logger.info(f"[worker-{worker_id}] 号池空 30s，停止")
                    return
                # 等 3s 再试
                for _ in range(30):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)
                continue
            idle_round = 0

            # 根据配置取代理：固定 or 按账号批量轮换
            proxy = self._pick_proxy_for_run(worker_id)
            run_options = dict(self._options)
            if proxy:
                run_options["proxy"] = proxy

            # 启一个 run
            try:
                run_id = registrar.start_registration(account, run_options)
            except Exception as e:
                logger.exception(f"[worker-{worker_id}] 启动注册失败: {e}")
                if mail_source not in ("cf_temp", "oep"):
                    db.release_unused(account["email"])
                time.sleep(2)
                continue

            with self._lock:
                self._worker_status[worker_id] = {
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": proxy,
                    "started_at": time.time(),
                }
            self._broadcast("state", self._snapshot())
            self._broadcast("run_started", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "proxy": proxy,
            })

            # 等当前 run 跑完
            ok, category, finished_email = self._wait_run_finish(run_id)
            # 无号池模式 create_mailbox 后 runs.email 会被更新为真实邮箱
            display_email = finished_email or account["email"]

            with self._lock:
                self._worker_status.pop(worker_id, None)
            self._record_finish(ok, category)
            self._broadcast("state", self._snapshot())
            self._broadcast("run_finished", {
                "worker_id": worker_id,
                "email": display_email,
                "run_id": run_id,
                "ok": ok,
                "category": category,
            })

            # 冷却（每个 worker 自己的节奏）
            cool_down = float(self._options.get("cool_down_seconds") or 3)
            if cool_down > 0:
                for _ in range(int(cool_down * 10)):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)

    def _wait_run_finish(self, run_id: str, timeout: int = 1800) -> tuple[bool, str, str]:
        """轮询 runs 表，等 run 跑完。返回 (ok, category, email)。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False, "", ""
            con = db._conn()
            cur = con.execute(
                "SELECT status, error_category, email FROM runs WHERE run_id=?", (run_id,)
            )
            row = cur.fetchone()
            if row:
                st = row["status"]
                email = (row["email"] or "")
                if st == "done":
                    return True, "", email
                if st == "failed":
                    return False, (row["error_category"] or ""), email
            time.sleep(1)
        logger.warning(f"run {run_id} 等了 {timeout}s 没结束，超时放弃")
        return False, "", ""


# 全局单例
CONTROLLER = AutoLoopController()
