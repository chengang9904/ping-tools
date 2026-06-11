# -*- coding: utf-8 -*-
"""
PingMonitor —— Windows 多目标实时 Ping 监控工具（单文件版 v2）

核心特性
--------
1. 通过 Windows 原生 API IcmpSendEcho（iphlpapi.dll）发送 ICMP Echo：
   - 无需管理员权限（区别于 raw socket 方案）
   - 无需解析 ping.exe 的本地化文本输出（中文/英文系统通用）
2. 每个目标 IP 独立一个 QThread 工作线程，UI 永不卡死
3. 实时统计：丢包率 / 当前 / 最小 / 最大 / 平均延迟
4. PyQtGraph 高性能动态折线图，超时点以"断线缺口"呈现（NaN + connect='finite'）

v2 新增
-------
5. 系统托盘驻留：最小化/关闭均隐藏到托盘；托盘右键菜单（显示主界面 /
   开始·暂停监控 / 退出程序）；双击托盘图标恢复主界面；
   连续丢包达到阈值时弹出托盘气泡通知。
6. 丢包可视化标记：每条曲线配一个 ScatterPlotItem，在图表顶部"丢包带"
   上以醒目的红色 X 标出每次丢包的时刻（不参与 Y 轴自动缩放，无反馈回路）。
7. 时间范围切换：1分钟 / 5分钟 / 1小时 / 6小时 / 24小时。
   底层为 numpy 预分配环形缓冲区（24h = 86400 点，O(1) 追加），
   展示长周期时按桶降采样，屏幕上恒定 ≤ MAX_PLOT_POINTS 个点，
   切换/缩放始终流畅。

v3 新增
-------
8. 抖动（Jitter）列：RFC 3550 定义的指数滑动平均（平滑系数 1/16），
   O(1) 增量更新。只捕捉相邻样本间的"跳动"——延迟稳定时哪怕绝对值
   很高，抖动也趋近 0；比标准差更能反映真实的网络波动。
9. 滚动窗口统计：表格的 P50/P95 基于最近 STATS_WINDOW_SECONDS 秒
   窗口现算，反映"现在"而非全程累计；P95 替代 max，不会被单次
   偶发尖刺永久污染。
10. 均值±包络阴影带：降采样每桶顺便计算 min/mean/max——主线画均值，
    FillBetweenItem 在 min-max 之间填充半透明色。带子窄 = 稳定，
    带子突然变宽 = 抖动发作，扫一眼即可定位波动时段。
11. 目标列表持久化：保存于 %APPDATA%\\PingMonitor\\config.json，
    添加/移除目标即原子写回，重启自动恢复；配置损坏时回退默认列表。

依赖安装
--------
    pip install PyQt5 pyqtgraph

运行
----
    python ping_monitor.py
"""

import sys
import os
import json
import math
import time
import socket
import struct
import ctypes
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

# ============================ 全局配置 ============================

DEFAULT_TARGETS = ["8.8.8.8", "114.114.114.114"]   # 首次运行/配置损坏时的默认目标
PING_INTERVAL   = 1.0     # 每个目标的 Ping 周期（秒）
PING_TIMEOUT_MS = 1000    # 单次 Ping 超时（毫秒），超时即记一次丢包
REFRESH_MS      = 500     # UI 刷新周期（毫秒）：采集与绘制解耦的关键

HISTORY_SECONDS  = 24 * 3600                              # 保留 24 小时历史
RING_CAPACITY    = int(HISTORY_SECONDS / PING_INTERVAL)   # 86400 点
MAX_PLOT_POINTS  = 1500   # 屏幕上单条曲线最多绘制的点数（降采样目标）
CONSEC_LOSS_ALERT = 3     # 连续丢包达到该次数 → 托盘气泡告警
STATS_WINDOW_SECONDS = 60 # 表格 P50/P95 的滚动统计窗口（秒）

# 时间范围选项：(显示文本, 秒数)
TIME_RANGES = [
    ("实时（最近1分钟）", 60),
    ("5 分钟",           300),
    ("1 小时",           3600),
    ("6 小时",           21600),
    ("24 小时",          86400),
]

# 多条曲线的配色（循环使用）
CURVE_COLORS = [
    (0, 200, 255), (255, 170, 0), (0, 230, 120), (255, 80, 120),
    (170, 120, 255), (255, 255, 100), (120, 200, 160), (240, 130, 250),
]

_EMPTY = np.empty(0, dtype=np.float64)

# ===================== 配置持久化（用户目录 JSON） =====================
# 存放在 %APPDATA%\PingMonitor\config.json（漫游用户目录，重装系统盘外软件
# 或多机漫游场景下可随用户配置迁移）；APPDATA 缺失时退回用户主目录。

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "PingMonitor"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_targets() -> list:
    """读取持久化的目标列表。文件缺失/损坏/为空时回退到默认列表，
    绝不让一个坏配置文件阻止程序启动。"""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        targets, seen = [], set()
        for t in data.get("targets", []):
            t = str(t).strip()
            if t and t not in seen:        # 清洗：去空白、去重，保持顺序
                seen.add(t)
                targets.append(t)
        if targets:
            return targets
    except FileNotFoundError:
        pass                               # 首次运行，正常情况
    except (json.JSONDecodeError, OSError, TypeError, AttributeError) as e:
        print(f"[PingMonitor] 配置文件损坏，已回退默认目标: {e}", file=sys.stderr)
    return list(DEFAULT_TARGETS)


def save_targets(targets) -> None:
    """原子写回目标列表：先写临时文件再 os.replace 替换，
    进程中途被杀也不会留下半截 JSON。写失败只告警，不影响监控。"""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"targets": list(targets)}, f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except OSError as e:
        print(f"[PingMonitor] 配置保存失败: {e}", file=sys.stderr)

# ===================== Windows ICMP API 封装 =====================
# IcmpSendEcho 是同步阻塞调用，但我们只在工作线程里调用它，不影响 UI。


class IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Ttl",         ctypes.c_ubyte),
        ("Tos",         ctypes.c_ubyte),
        ("Flags",       ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.c_void_p),
    ]


class ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [
        ("Address",       ctypes.c_ulong),    # 应答方 IP
        ("Status",        ctypes.c_ulong),    # 0 = IP_SUCCESS
        ("RoundTripTime", ctypes.c_ulong),    # RTT（毫秒）
        ("DataSize",      ctypes.c_ushort),
        ("Reserved",      ctypes.c_ushort),
        ("Data",          ctypes.c_void_p),
        ("Options",       IP_OPTION_INFORMATION),
    ]


_iphlpapi = ctypes.WinDLL("iphlpapi.dll")

_IcmpCreateFile = _iphlpapi.IcmpCreateFile
_IcmpCreateFile.restype = wintypes.HANDLE

_IcmpCloseHandle = _iphlpapi.IcmpCloseHandle
_IcmpCloseHandle.argtypes = [wintypes.HANDLE]

_IcmpSendEcho = _iphlpapi.IcmpSendEcho
_IcmpSendEcho.restype = wintypes.DWORD
_IcmpSendEcho.argtypes = [
    wintypes.HANDLE,    # IcmpHandle
    ctypes.c_ulong,     # DestinationAddress（网络字节序 u32）
    ctypes.c_char_p,    # RequestData
    wintypes.WORD,      # RequestSize
    ctypes.c_void_p,    # RequestOptions（可为 NULL）
    ctypes.c_void_p,    # ReplyBuffer
    wintypes.DWORD,     # ReplySize
    wintypes.DWORD,     # Timeout（毫秒）
]

_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
_PAYLOAD = b"PingMonitor-payload-0123456789ab"  # 32 字节载荷，与系统 ping 一致


class IcmpPinger:
    """对一个 ICMP 句柄的轻量封装；每个工作线程持有自己的句柄。"""

    def __init__(self):
        self.handle = _IcmpCreateFile()
        if self.handle == _INVALID_HANDLE_VALUE or not self.handle:
            raise OSError("IcmpCreateFile 失败，无法创建 ICMP 句柄")

    def ping(self, addr_u32: int, timeout_ms: int):
        """对目标地址发送一次 Echo。成功返回 RTT（毫秒，float），失败/超时返回 None。"""
        reply_size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(_PAYLOAD) + 8
        reply_buf = ctypes.create_string_buffer(reply_size)
        ret = _IcmpSendEcho(
            self.handle, addr_u32, _PAYLOAD, len(_PAYLOAD),
            None, reply_buf, reply_size, timeout_ms,
        )
        if ret == 0:  # 超时或网络不可达，统一视为丢包
            return None
        reply = ctypes.cast(reply_buf, ctypes.POINTER(ICMP_ECHO_REPLY)).contents
        if reply.Status != 0:  # 目标主机不可达 / TTL 超限等
            return None
        return float(reply.RoundTripTime)

    def close(self):
        if self.handle:
            _IcmpCloseHandle(self.handle)
            self.handle = None


# ========================= 工作线程 =========================


class PingWorker(QtCore.QThread):
    """单个目标的连续 Ping 线程。所有阻塞操作（DNS 解析、ICMP 等待）都在此线程内完成。"""

    # (目标, monotonic 时间戳, RTT 毫秒或 None)
    result = QtCore.pyqtSignal(str, float, object)
    # (目标, 错误描述)：解析失败 / 句柄创建失败等致命错误
    error = QtCore.pyqtSignal(str, str)

    def __init__(self, target: str, interval: float, timeout_ms: int, parent=None):
        super().__init__(parent)
        self.target = target
        self.interval = interval
        self.timeout_ms = timeout_ms
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        # 1) DNS 解析放在线程里做（支持填域名），失败不会拖累 UI
        try:
            ip_str = socket.gethostbyname(self.target)
            addr_u32 = struct.unpack("<I", socket.inet_aton(ip_str))[0]
        except OSError as e:
            self.error.emit(self.target, f"解析失败: {e}")
            return

        # 2) 创建本线程专属的 ICMP 句柄
        try:
            pinger = IcmpPinger()
        except OSError as e:
            self.error.emit(self.target, str(e))
            return

        # 3) 固定节拍循环：无论 RTT 多大，整体保持 interval 的发送周期
        try:
            next_tick = time.monotonic()
            while not self._stop:
                rtt = pinger.ping(addr_u32, self.timeout_ms)
                self.result.emit(self.target, time.monotonic(), rtt)

                next_tick += self.interval
                # 落后太多（如超时 > 周期）则重新对齐，避免追赶式连发
                if time.monotonic() > next_tick:
                    next_tick = time.monotonic()
                # 分片休眠，保证 stop() 后 0.1s 内退出
                while not self._stop:
                    remain = next_tick - time.monotonic()
                    if remain <= 0:
                        break
                    time.sleep(min(remain, 0.1))
        finally:
            pinger.close()


# ===================== 环形缓冲区 + 降采样 =====================


class RingBuffer:
    """numpy 预分配环形缓冲区：追加 O(1)，无内存增长，容量即 24h 数据量。

    t 数组保存采样时刻（单调递增的运行秒数），v 数组保存 RTT（丢包记 NaN）。
    """

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.t = np.empty(capacity, dtype=np.float64)
        self.v = np.empty(capacity, dtype=np.float64)
        self.size = 0
        self.idx = 0  # 下一个写入位置

    def append(self, t: float, v: float):
        self.t[self.idx] = t
        self.v[self.idx] = v
        self.idx = (self.idx + 1) % self.capacity
        if self.size < self.capacity:
            self.size += 1

    def window(self, t_min: float):
        """返回 t >= t_min 的按时间排序的 (t, v) 视图（拷贝）。"""
        if self.size == 0:
            return _EMPTY, _EMPTY
        if self.size < self.capacity:          # 尚未写满：数据本身就是有序的
            t, v = self.t[:self.size], self.v[:self.size]
        else:                                   # 已回绕：拼接成时间有序
            t = np.concatenate((self.t[self.idx:], self.t[:self.idx]))
            v = np.concatenate((self.v[self.idx:], self.v[:self.idx]))
        i = np.searchsorted(t, t_min)           # t 单调递增 → 二分定位窗口起点
        return t[i:], v[i:]


def envelope_series(t: np.ndarray, v: np.ndarray, max_points: int):
    """生成绘图序列：返回 (t_line, v_line, v_lo, v_hi, loss_t) 五元组。

    点数 > max_points 时按桶聚合（降采样）：
      - v_line = 桶内均值（滚动均值线）
      - v_lo / v_hi = 桶内 min / max（包络阴影带的上下边界）
      - 桶内只要出现过 NaN（丢包），该桶时刻就记入 loss_t（红 X 标记）
      - 整桶全为 NaN 时输出 NaN，折线/包络在该处保持断开
    点数不多时：v_line = 原始数据，包络带用滑动窗口 min/max 计算，
      带宽依然直观反映短期波动。
    """
    n = len(t)
    if n == 0:
        return t, v, v, v, _EMPTY

    if n <= max_points:                       # 不降采样：滑动窗口包络
        loss_t = t[np.isnan(v)]
        w = min(15, n)                        # 滑动窗口宽度（样本数）
        if w < 3:
            return t, v, v, v, loss_t
        win = np.lib.stride_tricks.sliding_window_view(v, w)
        valid = ~np.isnan(win)
        has = valid.any(axis=1)
        # 用 +inf/-inf 占位无效值，规避 nanmin/nanmax 对全 NaN 窗口的告警
        lo = np.where(has, np.min(np.where(valid, win, np.inf), axis=1), np.nan)
        hi = np.where(has, np.max(np.where(valid, win, -np.inf), axis=1), np.nan)
        # 滑动结果比原序列短 w-1 个点，首尾用边缘值补齐以对齐 t
        pad_l = (w - 1) // 2
        pad_r = w - 1 - pad_l
        v_lo = np.concatenate((np.full(pad_l, lo[0]), lo, np.full(pad_r, lo[-1])))
        v_hi = np.concatenate((np.full(pad_l, hi[0]), hi, np.full(pad_r, hi[-1])))
        return t, v, v_lo, v_hi, loss_t

    step = -(-n // max_points)                # 桶宽 = ceil(n / max_points)
    m = (n // step) * step
    # 为对齐 reshape 丢弃最旧的 n-m 个零头点（最多 step-1 个，影响可忽略）
    t2 = t[n - m:].reshape(-1, step)
    v2 = v[n - m:].reshape(-1, step)

    valid = ~np.isnan(v2)
    has = valid.any(axis=1)
    cnt = np.maximum(valid.sum(axis=1), 1)
    v_line = np.where(has, np.where(valid, v2, 0.0).sum(axis=1) / cnt, np.nan)
    v_lo = np.where(has, np.min(np.where(valid, v2, np.inf), axis=1), np.nan)
    v_hi = np.where(has, np.max(np.where(valid, v2, -np.inf), axis=1), np.nan)
    t_ds = t2[:, 0]
    loss_t = t_ds[~valid.all(axis=1)]          # 桶内有任何丢包 → 标记
    return t_ds, v_line, v_lo, v_hi, loss_t


# ========================= 统计模型 =========================


class TargetStats:
    """单目标的累计计数 + RFC 3550 抖动。增量更新，O(1)。

    延迟分布指标（P50/P95）不在此累计——refresh_ui 每次刷新基于最近
    STATS_WINDOW_SECONDS 秒的环形缓冲窗口现算，保证反映"现在"。
    """

    __slots__ = ("sent", "lost", "recv", "last", "consec_loss",
                 "prev_rtt", "jitter")

    def __init__(self):
        self.sent = 0
        self.lost = 0
        self.recv = 0
        self.last = None        # 最近一次 RTT；None 表示最近一次超时
        self.consec_loss = 0    # 当前连续丢包次数（用于托盘告警）
        self.prev_rtt = None    # 上一次成功的 RTT（抖动计算用）
        self.jitter = 0.0       # RFC 3550 指数滑动平均抖动（ms）

    def update(self, rtt):
        self.sent += 1
        if rtt is None:
            self.lost += 1
            self.consec_loss += 1
            self.last = None
        else:
            self.recv += 1
            self.consec_loss = 0
            self.last = rtt
            # RFC 3550 抖动：相邻两次成功 RTT 差值的指数滑动平均，
            # 1/16 为 RFC 推荐平滑系数。只捕捉"跳动"——延迟稳定在
            # 200ms 时抖动趋近 0，比标准差更能反映真实波动。
            if self.prev_rtt is not None:
                diff = abs(rtt - self.prev_rtt)
                self.jitter += (diff - self.jitter) / 16.0
            self.prev_rtt = rtt

    @property
    def loss_rate(self):
        return (self.lost / self.sent * 100.0) if self.sent else 0.0


# ========================= 主窗口 =========================

COL_TARGET, COL_STATUS, COL_SENT, COL_LOST, COL_LOSS, \
    COL_CUR, COL_P50, COL_P95, COL_JITTER = range(9)

TABLE_HEADERS = ["目标", "状态", "发送", "丢失", "丢包率",
                 "当前(ms)", "P50(ms)", "P95(ms)", "抖动(ms)"]


def make_app_icon() -> QtGui.QIcon:
    """程序内绘制托盘/窗口图标（绿色圆底 + 白色波形线），无需外部图片文件。"""
    pm = QtGui.QPixmap(64, 64)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QColor("#1f9d4e"))
    p.drawEllipse(2, 2, 60, 60)
    p.setPen(QtGui.QPen(QtGui.QColor("white"), 6,
                        QtCore.Qt.SolidLine, QtCore.Qt.RoundCap,
                        QtCore.Qt.RoundJoin))
    p.setBrush(QtCore.Qt.NoBrush)
    p.drawPolyline(QtGui.QPolygon([
        QtCore.QPoint(12, 40), QtCore.QPoint(24, 26),
        QtCore.QPoint(34, 44), QtCore.QPoint(44, 22),
        QtCore.QPoint(52, 34),
    ]))
    p.end()
    return QtGui.QIcon(pm)


class MainWindow(QtWidgets.QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PingMonitor - 多目标实时网络监控")
        self.resize(1000, 680)
        self.setWindowIcon(make_app_icon())

        # target -> {worker, stats, buffer, curve, scatter, color, error}
        self.targets = {}
        self.start_time = time.monotonic()
        self.running = True
        self.range_secs = TIME_RANGES[0][1]
        self._really_quit = False     # 托盘退出菜单置位后，关闭才真正退出
        self._tray_tip_shown = False  # "已最小化到托盘"提示只弹一次

        self._build_ui()
        self._build_tray()

        # 从用户目录配置加载目标列表（首次运行为默认列表）；
        # 加载阶段不回写配置，避免每次启动都碰磁盘
        self._loading_config = True
        for t in load_targets():
            self.add_target(t)
        self._loading_config = False

        # UI 刷新定时器：采集（每秒/每目标 1 条）与绘制（每 500ms 一次）解耦
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_ui)
        self.refresh_timer.start(REFRESH_MS)

    # ---------------- UI 构建 ----------------

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # 工具栏：添加 / 移除 / 暂停 / 时间范围
        bar = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("添加目标")
        self.btn_remove = QtWidgets.QPushButton("移除选中")
        self.btn_pause = QtWidgets.QPushButton("暂停")
        self.btn_add.clicked.connect(self._on_add_clicked)
        self.btn_remove.clicked.connect(self._on_remove_clicked)
        self.btn_pause.clicked.connect(self.toggle_running)
        bar.addWidget(self.btn_add)
        bar.addWidget(self.btn_remove)
        bar.addWidget(self.btn_pause)
        bar.addSpacing(24)
        bar.addWidget(QtWidgets.QLabel("时间范围:"))
        self.range_combo = QtWidgets.QComboBox()
        for label, secs in TIME_RANGES:
            self.range_combo.addItem(label, secs)
        self.range_combo.currentIndexChanged.connect(self._on_range_changed)
        bar.addWidget(self.range_combo)
        bar.addStretch(1)
        layout.addLayout(bar)

        # 状态表格
        self.table = QtWidgets.QTableWidget(0, len(TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.Stretch)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setMaximumHeight(190)
        layout.addWidget(self.table)

        # 实时折线图
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#101418")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("left", "延迟", units="ms")
        self.plot.setLabel("bottom", "运行时间", units="s")
        self.plot.addLegend(offset=(10, 10))
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.enableAutoRange(axis="y")
        layout.addWidget(self.plot, stretch=1)

    # ---------------- 系统托盘 ----------------

    def _build_tray(self):
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None   # 无托盘环境（极少见）：关闭按钮退化为直接退出
            return

        self.tray = QtWidgets.QSystemTrayIcon(make_app_icon(), self)
        self.tray.setToolTip("PingMonitor - 网络监控运行中")

        menu = QtWidgets.QMenu()
        act_show = menu.addAction("显示主界面")
        act_show.triggered.connect(self.restore_window)
        self.act_pause = menu.addAction("暂停监控")
        self.act_pause.triggered.connect(self.toggle_running)
        menu.addSeparator()
        act_quit = menu.addAction("退出程序")
        act_quit.triggered.connect(self.quit_app)
        self.tray.setContextMenu(menu)

        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QtWidgets.QSystemTrayIcon.DoubleClick:
            self.restore_window()

    def restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self._really_quit = True
        self.close()
        QtWidgets.QApplication.quit()

    def _hide_to_tray(self):
        self.hide()
        if self.tray is not None and not self._tray_tip_shown:
            self._tray_tip_shown = True
            self.tray.showMessage(
                "PingMonitor 仍在后台运行",
                "监控未停止。双击托盘图标恢复界面，右键菜单可退出。",
                QtWidgets.QSystemTrayIcon.Information, 3000)

    # ---------------- 目标管理 ----------------

    def add_target(self, target: str):
        target = target.strip()
        if not target or target in self.targets:
            return

        color = CURVE_COLORS[len(self.targets) % len(CURVE_COLORS)]
        curve = self.plot.plot(
            [], [], pen=pg.mkPen(color=color, width=2),
            name=target, connect="finite",   # NaN 处断线，直观呈现丢包
        )
        # 包络阴影带：min-max 之间填充同色半透明——带宽即波动幅度，
        # 带子突然变宽 = 抖动发作。上下边界曲线本身不可见（pen=None）。
        band_lo = pg.PlotDataItem([], [], pen=None, connect="finite")
        band_hi = pg.PlotDataItem([], [], pen=None, connect="finite")
        band = pg.FillBetweenItem(band_lo, band_hi,
                                  brush=pg.mkBrush(color + (45,)))
        band.setZValue(-10)                  # 垫在所有折线下方
        self.plot.addItem(band_lo)
        self.plot.addItem(band_hi)
        self.plot.addItem(band)
        # 丢包标记：红色 X，挂在图表顶部"丢包带"上。
        # ignoreBounds=True → 不参与 Y 轴自动缩放，避免"标记抬高量程→
        # 标记位置又随量程上移"的正反馈循环。
        scatter = pg.ScatterPlotItem(
            symbol="x", size=11, brush=None,
            pen=pg.mkPen("#ff3030", width=2),
        )
        self.plot.plotItem.vb.addItem(scatter, ignoreBounds=True)

        row = self.table.rowCount()
        self.table.insertRow(row)
        for col in range(len(TABLE_HEADERS)):
            item = QtWidgets.QTableWidgetItem("-")
            item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.table.setItem(row, col, item)
        self.table.item(row, COL_TARGET).setText(target)

        info = {
            "stats": TargetStats(),
            "buffer": RingBuffer(RING_CAPACITY),  # 24h 环形缓冲
            "curve": curve,
            "band_lo": band_lo,
            "band_hi": band_hi,
            "band": band,
            "scatter": scatter,
            "color": color,
            "error": None,
            "worker": None,
        }
        self.targets[target] = info
        if not self._loading_config:
            save_targets(self.targets.keys())   # dict 保持插入顺序
        if self.running:
            self._start_worker(target)

    def _start_worker(self, target: str):
        worker = PingWorker(target, PING_INTERVAL, PING_TIMEOUT_MS, parent=self)
        worker.result.connect(self._on_result)   # 跨线程信号，自动排队到主线程
        worker.error.connect(self._on_error)
        self.targets[target]["worker"] = worker
        worker.start()

    def remove_target(self, target: str):
        info = self.targets.pop(target, None)
        if info is None:
            return
        self._stop_worker_obj(info)
        self.plot.removeItem(info["curve"])
        self.plot.removeItem(info["band"])
        self.plot.removeItem(info["band_lo"])
        self.plot.removeItem(info["band_hi"])
        self.plot.plotItem.vb.removeItem(info["scatter"])
        legend = self.plot.plotItem.legend
        if legend is not None:
            legend.removeItem(target)
        for row in range(self.table.rowCount()):
            if self.table.item(row, COL_TARGET).text() == target:
                self.table.removeRow(row)
                break
        save_targets(self.targets.keys())

    @staticmethod
    def _stop_worker_obj(info):
        worker = info["worker"]
        if worker is not None:
            worker.stop()
            worker.wait(2000)
            info["worker"] = None

    # ---------------- 信号槽（均在主线程执行） ----------------

    def _on_result(self, target: str, ts: float, rtt):
        info = self.targets.get(target)
        if info is None:  # 目标可能刚被移除
            return
        st = info["stats"]
        st.update(rtt)
        elapsed = ts - self.start_time
        info["buffer"].append(elapsed, rtt if rtt is not None else math.nan)

        # 连续丢包恰好达到阈值时告警一次；恢复后 consec 归零，可再次触发
        if (rtt is None and st.consec_loss == CONSEC_LOSS_ALERT
                and self.tray is not None):
            self.tray.showMessage(
                "网络异常告警",
                f"{target} 已连续丢包 {st.consec_loss} 次"
                f"（累计丢包率 {st.loss_rate:.1f}%）",
                QtWidgets.QSystemTrayIcon.Warning, 5000)

    def _on_error(self, target: str, msg: str):
        info = self.targets.get(target)
        if info is None:
            return
        info["error"] = msg
        if self.tray is not None:
            self.tray.showMessage("目标错误", f"{target}: {msg}",
                                  QtWidgets.QSystemTrayIcon.Warning, 5000)

    # ---------------- 交互 ----------------

    def _on_add_clicked(self):
        text, ok = QtWidgets.QInputDialog.getText(
            self, "添加目标", "输入 IP 地址或域名：")
        if ok and text.strip():
            if text.strip() in self.targets:
                QtWidgets.QMessageBox.information(self, "提示", "该目标已存在")
                return
            self.add_target(text.strip())

    def _on_remove_clicked(self):
        row = self.table.currentRow()
        if row < 0:
            return
        self.remove_target(self.table.item(row, COL_TARGET).text())

    def _on_range_changed(self, _index: int):
        self.range_secs = self.range_combo.currentData()
        self.refresh_ui()   # 立即按新窗口重绘，不等下一个定时器周期

    def toggle_running(self):
        """开始/暂停监控——主界面按钮与托盘菜单共用。"""
        if self.running:
            for t in self.targets:
                self._stop_worker_obj(self.targets[t])
            self.running = False
            label = "继续"
        else:
            for t in self.targets:
                self._start_worker(t)
            self.running = True
            label = "暂停"
        self.btn_pause.setText(label)
        if self.tray is not None:
            self.act_pause.setText("继续监控" if not self.running else "暂停监控")
            self.tray.setToolTip(
                "PingMonitor - 网络监控运行中" if self.running
                else "PingMonitor - 监控已暂停")

    # ---------------- 周期刷新 ----------------

    def refresh_ui(self):
        t_now = time.monotonic() - self.start_time
        t_min = max(0.0, t_now - self.range_secs)
        # X 轴跟随所选时间窗口滚动
        self.plot.setXRange(t_min, max(t_now, t_min + 1.0), padding=0.02)

        # 丢包标记画在当前视野顶部的"丢包带"上，多目标按行错开避免重叠
        y_lo, y_hi = self.plot.plotItem.vb.viewRange()[1]
        band = (y_hi - y_lo) * 0.045 or 1.0

        for row in range(self.table.rowCount()):
            target = self.table.item(row, COL_TARGET).text()
            info = self.targets.get(target)
            if info is None:
                continue

            # 1) 取窗口数据 → 均值线 + min/max 包络降采样 → 一次性 setData
            ts, vs = info["buffer"].window(t_min)
            t_line, v_line, v_lo, v_hi, loss_t = envelope_series(
                ts, vs, MAX_PLOT_POINTS)
            info["curve"].setData(t_line, v_line, connect="finite")
            info["band_lo"].setData(t_line, v_lo, connect="finite")
            info["band_hi"].setData(t_line, v_hi, connect="finite")
            if len(loss_t):
                y_band = y_hi - band * (row + 1)
                info["scatter"].setData(
                    x=loss_t, y=np.full(loss_t.shape, y_band))
            else:
                info["scatter"].setData(x=_EMPTY, y=_EMPTY)

            # 2) 表格统计
            st = info["stats"]
            if info["error"]:
                self._set_cell(row, COL_STATUS, "错误", "#ff5050")
                self.table.item(row, COL_STATUS).setToolTip(info["error"])
                continue
            if st.sent == 0:
                self._set_cell(row, COL_STATUS, "检测中…", "#aaaaaa")
                continue

            if st.last is None:
                self._set_cell(row, COL_STATUS, "超时", "#ff5050")
            else:
                self._set_cell(row, COL_STATUS, "正常", "#30d060")

            loss = st.loss_rate
            loss_color = ("#30d060" if loss < 1 else
                          "#ffc83c" if loss < 10 else "#ff5050")
            self._set_cell(row, COL_SENT, str(st.sent))
            self._set_cell(row, COL_LOST, str(st.lost))
            self._set_cell(row, COL_LOSS, f"{loss:.1f}%", loss_color)
            self._set_cell(row, COL_CUR,
                           "超时" if st.last is None else f"{st.last:.0f}")

            # P50/P95：基于最近 STATS_WINDOW_SECONDS 秒的滚动窗口现算，
            # 反映"现在"的延迟分布，不被启动以来的历史数据稀释。
            wt, wv = info["buffer"].window(t_now - STATS_WINDOW_SECONDS)
            valid = wv[~np.isnan(wv)]
            if valid.size:
                self._set_cell(row, COL_P50, f"{np.percentile(valid, 50):.0f}")
                self._set_cell(row, COL_P95, f"{np.percentile(valid, 95):.0f}")
            else:
                self._set_cell(row, COL_P50, "-")
                self._set_cell(row, COL_P95, "-")

            # 抖动：<5ms 绿 / <20ms 黄 / 其余红
            if st.recv >= 2:
                j = st.jitter
                j_color = ("#30d060" if j < 5 else
                           "#ffc83c" if j < 20 else "#ff5050")
                self._set_cell(row, COL_JITTER, f"{j:.1f}", j_color)
            else:
                self._set_cell(row, COL_JITTER, "-")

    def _set_cell(self, row: int, col: int, text: str, color: str = None):
        item = self.table.item(row, col)
        item.setText(text)
        if color:
            item.setForeground(pg.mkColor(color))

    # ---------------- 最小化 / 关闭 / 退出 ----------------

    def changeEvent(self, event):
        # 点最小化按钮 → 隐藏到托盘（延迟到事件处理完之后执行 hide）
        if (event.type() == QtCore.QEvent.WindowStateChange
                and self.isMinimized() and self.tray is not None):
            QtCore.QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(event)

    def closeEvent(self, event):
        # 点关闭按钮 → 隐藏到托盘；只有托盘菜单"退出程序"才真正退出
        if self.tray is not None and not self._really_quit:
            event.ignore()
            self._hide_to_tray()
            return
        self.refresh_timer.stop()
        for info in self.targets.values():
            self._stop_worker_obj(info)
        if self.tray is not None:
            self.tray.hide()
        event.accept()


def main():
    # 高 DPI 适配（4K / 缩放显示器下界面不模糊）
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv)
    # 统一 UI 字体：Windows 中文环境下 Qt 默认字体常解析为宋体（SimSun），
    # 小字号时中文走点阵渲染、缺字字符又按字符回退到雅黑等矢量字体，
    # 中西文混排便呈现粗细不一。显式指定雅黑 UI（中西文同源、全部矢量
    # 抗锯齿渲染），必须在创建任何窗口之前设置。
    available = set(QtGui.QFontDatabase().families())
    for family in ("Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei"):
        if family in available:
            app.setFont(QtGui.QFont(family, 9))
            break
    # 窗口隐藏到托盘后程序必须继续运行
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
