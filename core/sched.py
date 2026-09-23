"""定时同步的公共节拍器。

为什么不用「睡够间隔再醒」
--------------------------
早先两个模块的调度线程都是这个写法：

    while True:
        if 该跑: 跑一次
        stop.wait(interval_hours * 3600)      # ← 一次睡足 12 小时

它有三个毛病，全都表现为用户眼里的「自动同步不生效，只能手动点」：

1. **配置改完要等一整轮才生效**。启用/关闭开关、改间隔、补上 ERP 密码，
   这些都只在「醒来那一刻」被读到一次；下一次读要等满 12 小时。
   于是用户在页面上把自动同步打开、等半天没有任何变化，只能去点「立即同步」。
2. **停机期间错过的到点永远补不回来**。进程重启后从零开始睡，
   关机两天再开机，也要再等满一个间隔才跑第一次。
3. **休眠 / 时钟跳变会让到点被整段跳过**。睡的是时长，不是「到点时刻」。

这里改成**每分钟醒一次、按墙上时钟判断是否到点**：
配置改完最多一分钟生效；停机期间错过的到点在启动后立刻补上；
判断依据是「上次同步时间 vs 间隔」，与进程活了多久无关。
"""

from __future__ import annotations

import threading

# 醒来判断的节奏。1 分钟对「小时级」的同步间隔足够密，
# 又不会给 sqlite 读配置带来任何压力。
TICK_SECONDS = 60

# 启动后的首次判断稍等一下，别和主服务的初始化抢资源。
START_DELAY_SECONDS = 15


class Ticker:
    """按节拍触发 `run()`；`due()` 决定这一拍该不该跑。

    `due()` 返回 `(是否到点, 原因)`。原因只在**真到点**时打印 ——
    每一拍都打一行「还没到点」会把日志刷成没人看的样子。
    `run()` 自己负责「已经在跑就跳过」，节拍器不关心同步的并发控制。

    幂等：`start()` 重复调用不会起第二个线程。两套节拍各自触发一次同步，
    会变成两个线程同时拉 ERP、写同一个库。
    """

    def __init__(self, name: str, due, run,
                 tick: int = TICK_SECONDS,
                 start_delay: int = START_DELAY_SECONDS) -> None:
        self.name = name
        self._due = due
        self._run = run
        self._tick = max(1, int(tick))
        self._start_delay = max(0, int(start_delay))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> dict:
        if self.alive:
            return {"started": False, "reason": "已在运行"}
        self._stop.clear()
        t = threading.Thread(target=self._loop, name=self.name, daemon=True)
        t.start()
        self._thread = t
        return {"started": True}

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(self._start_delay):
            return
        while not self._stop.is_set():
            try:
                ok, why = self._due()
                if ok:
                    print(f"[{self.name}] 到点：{why}", flush=True)
                    self._run()
            except Exception as exc:                            # noqa: BLE001
                # 节拍器**绝不能**因为一次判断失败就退出 ——
                # 线程一死，自动同步从此不再发生，而页面上完全看不出来。
                print(f"[{self.name}] 本轮检查失败："
                      f"{type(exc).__name__}: {exc}", flush=True)
            if self._stop.wait(self._tick):
                return
