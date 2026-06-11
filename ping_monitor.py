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
5. 系统托盘驻留：最小化按钮隐藏到托盘（关闭按钮直接退出程序）；
   托盘右键菜单（显示主界面 / 开始·暂停监控 / 退出程序）；
   双击托盘图标恢复主界面；连续丢包达到阈值时弹出托盘气泡通知。
6. 丢包可视化标记：连续丢包聚合为"丢包事件"，以目标同色的 ✕ 锚定在
   该目标曲线的缺口处（相邻有效样本插值定位），事件越长标记越大
   （对数缩放封顶）。颜色 + 位置双重归属目标，且不占用图表顶部空间；
   选区内全程丢包、无锚点的目标退化到底部车道按行错开。
7. 时间范围切换：1分钟 / 5分钟 / 1小时 / 6小时 / 24小时。
   底层为 numpy 预分配环形缓冲区（24h = 86400 点，O(1) 追加），
   展示长周期时按桶降采样，屏幕上恒定 ≤ MAX_PLOT_POINTS 个点，
   切换/缩放始终流畅。

v3 新增
-------
8. 抖动（Jitter）列：选区内相邻成功样本差值的绝对值均值。只捕捉
   相邻样本间的"跳动"——延迟稳定时哪怕绝对值很高，抖动也趋近 0；
   比标准差更能反映真实的网络波动。
9. 选区统计：表格的丢包率/P50/P95/抖动按图表当前可见 X 范围现算
   （范围变化 150ms 防抖），拖动/缩放图表即联动；P95 替代 max，
   不会被单次偶发尖刺永久污染。累计"发送/丢失(率)"合并为最后一列。
10. 均值±包络阴影带：降采样每桶顺便计算 min/mean/max——主线画均值，
    FillBetweenItem 在 min-max 之间填充半透明色。带子窄 = 稳定，
    带子突然变宽 = 抖动发作，扫一眼即可定位波动时段。
11. 目标列表持久化：保存于 %APPDATA%\\PingMonitor\\config.json，
    添加/移除目标即原子写回，重启自动恢复；配置损坏时回退默认列表。
12. TradingView 风格十字光标：虚线十字线跟随鼠标，竖线吸附到最近
    采样时刻；悬浮信息窗显示该时刻的墙钟时间/运行时长/光标延迟，
    以及每个目标在该时刻的 RTT（丢包以红色标出），靠近视图边缘时
    自动翻转停靠方向。基于 refresh_ui 缓存的窗口数组做二分查找，
    鼠标事件处理为 O(log n)，不影响绘图性能。

v4 新增
-------
13. 表格/图表高度可拖动：QSplitter 垂直分割，表格最小保留表头+1 行、
    图表最小 200px，防止任一侧被拖没；分隔条位置防抖写入配置，
    重启自动恢复。
14. 目标别名：双击表格行或右键菜单"设置别名"编辑；表格/图例/十字光标
    悬浮窗统一显示"别名 (host)"，无别名只显示 host。内部仍以 host 为
    键（表格行经 UserRole 关联），别名只影响展示层。config.json 的
    targets 升级为 [{"host", "alias"}] 对象数组，兼容读取旧版纯字符串
    格式并在写回时自动迁移。
15. 真实时间轴 + 自由浏览：横轴为墙钟时间（DateAxisItem，数据 x 为
    Unix 时间戳，由 monotonic 推导避免系统调时跳变）。拖动/缩放即进入
    手动浏览模式（停止自动跟随），"跟随最新"按钮或把视图拖回最右缘
    可恢复跟随；时间范围下拉框 = 跟随模式的窗口宽度。绘图与表格统计
    都只针对当前可见 X 范围计算。

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

DEFAULT_TARGETS = ["223.5.5.5", "114.114.114.114"]   # 首次运行/配置损坏时的默认目标
PING_INTERVAL   = 1.0     # 每个目标的 Ping 周期（秒）
PING_TIMEOUT_MS = 1000    # 单次 Ping 超时（毫秒），超时即记一次丢包
REFRESH_MS      = 500     # UI 刷新周期（毫秒）：采集与绘制解耦的关键

HISTORY_SECONDS  = 24 * 3600                              # 保留 24 小时历史
RING_CAPACITY    = int(HISTORY_SECONDS / PING_INTERVAL)   # 86400 点
MAX_PLOT_POINTS  = 1500   # 屏幕上单条曲线最多绘制的点数（降采样目标）
CONSEC_LOSS_ALERT = 3     # 连续丢包达到该次数 → 托盘气泡告警
STATS_DEBOUNCE_MS = 150   # 视图范围变化 -> 选区统计重算的防抖间隔

# 时间范围选项（跟随模式下的窗口宽度）：(显示文本, 秒数)
TIME_RANGES = [
    ("1 分钟",           60),
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


def read_config() -> dict:
    """读取整个配置文件为 dict；缺失/损坏时返回空 dict，
    绝不让一个坏配置文件阻止程序启动。"""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        print("[PingMonitor] 配置文件顶层不是对象，已忽略", file=sys.stderr)
    except FileNotFoundError:
        pass                               # 首次运行，正常情况
    except (json.JSONDecodeError, OSError) as e:
        print(f"[PingMonitor] 配置文件损坏，已忽略: {e}", file=sys.stderr)
    return {}


def save_config(**updates) -> None:
    """读-改-写合并指定键后原子落盘（保留文件中的其他配置键）：
    先写临时文件再 os.replace 替换，进程中途被杀也不会留下半截 JSON。
    写失败只告警，不影响监控。"""
    try:
        cfg = read_config()
        cfg.update(updates)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except OSError as e:
        print(f"[PingMonitor] 配置保存失败: {e}", file=sys.stderr)


def load_targets() -> list:
    """读取持久化的目标列表，返回 [(host, alias), ...]。

    兼容两种格式（写回时统一为新版）：
      旧版：纯字符串数组   ["8.8.8.8", ...]            -> alias 自动迁移为空
      新版：对象数组       [{"host": "...", "alias": "..."}, ...]
    清洗（去空白/按 host 去重/保序）；为空或异常时回退到默认列表。"""
    try:
        targets, seen = [], set()
        for entry in read_config().get("targets", []):
            if isinstance(entry, dict):
                host = str(entry.get("host", "")).strip()
                alias = str(entry.get("alias", "")).strip()
            else:
                host, alias = str(entry).strip(), ""   # 旧版纯字符串格式
            if host and host not in seen:
                seen.add(host)
                targets.append((host, alias))
        if targets:
            return targets
    except (TypeError, AttributeError) as e:
        print(f"[PingMonitor] targets 字段格式错误，已回退默认: {e}",
              file=sys.stderr)
    return [(h, "") for h in DEFAULT_TARGETS]

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
    """生成绘图序列：返回 (t_line, v_line, v_lo, v_hi, loss_flag) 五元组，
    loss_flag 为与 t_line 对齐的布尔数组（该点/该桶是否发生过丢包）。

    点数 > max_points 时按桶聚合（降采样）：
      - v_line = 桶内均值（滚动均值线）
      - v_lo / v_hi = 桶内 min / max（包络阴影带的上下边界）
      - 桶内只要出现过 NaN（丢包），该桶 loss_flag 为 True
      - 整桶全为 NaN 时输出 NaN，折线/包络在该处保持断开
    点数不多时：v_line = 原始数据，包络带用滑动窗口 min/max 计算，
      带宽依然直观反映短期波动。
    """
    n = len(t)
    if n == 0:
        empty_flag = np.empty(0, dtype=bool)
        return t, v, v, v, empty_flag

    if n <= max_points:                       # 不降采样：滑动窗口包络
        loss_flag = np.isnan(v)
        w = min(15, n)                        # 滑动窗口宽度（样本数）
        if w < 3:
            return t, v, v, v, loss_flag
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
        return t, v, v_lo, v_hi, loss_flag

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
    loss_flag = ~valid.all(axis=1)             # 桶内有任何丢包 → 标记
    return t_ds, v_line, v_lo, v_hi, loss_flag


def loss_markers(t: np.ndarray, v: np.ndarray, loss_flag: np.ndarray):
    """把连续丢包聚合为"丢包事件"标记，返回 (x, y, count) 三个数组。

    - 每个事件一个标记（而非每个丢包点一个），x 取事件中点；
      count 为事件覆盖的点/桶数，供调用方映射标记大小（越久越大）
    - y 锚定在曲线缺口处：优先用事件内仍有效的 v_line（部分丢包桶），
      否则取事件两侧相邻有效值的中值——标记贴着所属曲线，归属一目了然
    - 事件周围完全无有效值（选区内该目标全程丢包）时 y 为 NaN，
      由调用方放到图表底部车道
    """
    n = len(t)
    if n == 0 or not loss_flag.any():
        return _EMPTY, _EMPTY, _EMPTY
    f = loss_flag.astype(np.int8)
    d = np.diff(f)
    starts = np.flatnonzero(d == 1) + 1
    ends = np.flatnonzero(d == -1) + 1
    if f[0]:
        starts = np.concatenate(([0], starts))
    if f[-1]:
        ends = np.concatenate((ends, [n]))

    xs, ys, counts = [], [], []
    for s, e in zip(starts, ends):            # 事件数=故障次数，循环量极小
        xs.append((t[s] + t[e - 1]) / 2.0)
        seg_valid = v[s:e][~np.isnan(v[s:e])]
        prev_v = v[s - 1] if s > 0 else math.nan
        next_v = v[e] if e < n else math.nan
        if seg_valid.size:                    # 部分丢包桶：线在此处仍有值
            y = float(seg_valid.mean())
        elif not math.isnan(prev_v) and not math.isnan(next_v):
            y = (prev_v + next_v) / 2.0       # 缺口中点
        elif not math.isnan(prev_v):
            y = float(prev_v)
        elif not math.isnan(next_v):
            y = float(next_v)
        else:
            y = math.nan
        ys.append(y)
        counts.append(e - s)
    return np.asarray(xs), np.asarray(ys), np.asarray(counts, dtype=float)


# ========================= 统计模型 =========================


class TargetStats:
    """单目标的累计计数。增量更新，O(1)。

    延迟分布/抖动等指标不在此累计——表格刷新时基于图表当前可见
    X 范围的环形缓冲数据现算（选区统计），此处只保留全程汇总。
    """

    __slots__ = ("sent", "lost", "recv", "last", "consec_loss")

    def __init__(self):
        self.sent = 0
        self.lost = 0
        self.recv = 0
        self.last = None        # 最近一次 RTT；None 表示最近一次超时
        self.consec_loss = 0    # 当前连续丢包次数（用于托盘告警）

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

    @property
    def loss_rate(self):
        return (self.lost / self.sent * 100.0) if self.sent else 0.0


# ========================= 主窗口 =========================

COL_TARGET, COL_STATUS, COL_CUR, COL_LOSS, \
    COL_P50, COL_P95, COL_JITTER, COL_TOTAL = range(8)

# 丢包率/P50/P95/抖动均按图表当前可见 X 范围（选区）现算；
# 全程汇总后置到最后一列
TABLE_HEADERS = ["目标", "状态", "当前(ms)", "丢包率(选区)",
                 "P50(选区)", "P95(选区)", "抖动(选区)", "累计 发送/丢失"]


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
        self.wall_start = time.time()   # 运行秒数 -> 墙钟时间的换算基准
        self.running = True
        self.range_secs = TIME_RANGES[0][1]
        self.follow = True            # True=X 轴自动跟随最新; False=手动浏览
        self._setting_range = False   # 程序触发 setXRange 的标志位
        self._tray_tip_shown = False  # "已最小化到托盘"提示只弹一次

        self._build_ui()
        self._build_tray()

        # 恢复上次的分隔条位置（base64 -> QByteArray）
        state_b64 = read_config().get("splitter_state")
        if isinstance(state_b64, str) and state_b64:
            self.splitter.restoreState(
                QtCore.QByteArray.fromBase64(state_b64.encode("ascii")))

        # 从用户目录配置加载目标列表（首次运行为默认列表）；
        # 加载阶段不回写配置，避免每次启动都碰磁盘
        self._loading_config = True
        for host, alias in load_targets():
            self.add_target(host, alias)
        self._loading_config = False

        # UI 刷新定时器：采集（每秒/每目标 1 条）与绘制（每 500ms 一次）解耦
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_ui)
        self.refresh_timer.start(REFRESH_MS)

        # 视图 X 范围变化 -> 选区统计重算（防抖，拖动时不高频重算）
        self._stats_debounce = QtCore.QTimer(self)
        self._stats_debounce.setSingleShot(True)
        self._stats_debounce.setInterval(STATS_DEBOUNCE_MS)
        self._stats_debounce.timeout.connect(self.refresh_ui)
        # 信号在初始布局/添加目标全部完成后再接，避免启动期的
        # 自动范围调整被误判为用户操作
        self.plot.plotItem.vb.sigXRangeChanged.connect(
            self._on_x_range_changed)
        self.refresh_ui()   # 立即建立跟随窗口，不等首个定时周期

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
        # 跟随最新：选中=X 轴自动滚动；拖动/缩放图表自动退出跟随
        self.btn_follow = QtWidgets.QPushButton("跟随最新")
        self.btn_follow.setCheckable(True)
        self.btn_follow.setChecked(True)
        self.btn_follow.clicked.connect(self._on_follow_clicked)
        bar.addWidget(self.btn_follow)
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
        # 别名编辑入口：双击行 / 右键菜单
        self.table.itemDoubleClicked.connect(
            lambda item: self._edit_alias(self._row_host(item.row())))
        self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_menu)
        # 最小高度 = 表头 + 1 行数据 + 边框，防止被分隔条拖没
        self.table.setMinimumHeight(
            self.table.horizontalHeader().sizeHint().height()
            + self.table.verticalHeader().defaultSectionSize()
            + 2 * self.table.frameWidth())

        # 实时折线图：横轴为真实墙钟时间（x 数据 = Unix 时间戳）
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.setBackground("#101418")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("left", "延迟", units="ms")
        self.plot.setLabel("bottom", "时间")
        self.plot.addLegend(offset=(10, 10))
        # 鼠标只控制 X（平移/缩放）；Y 始终按可见数据自动适配，
        # 避免滚轮缩放后 Y 轴卡死在手动量程
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.enableAutoRange(axis="y")
        self.plot.plotItem.vb.setAutoVisible(y=True)
        self.plot.setMinimumHeight(200)

        # 表格/图表用垂直分隔条组装：高度比例可拖动调整，
        # 拖动停止 500ms 后将分隔条状态（base64）写入配置，重启恢复
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.plot)
        self.splitter.setStretchFactor(0, 0)   # 窗口缩放的增量空间优先给图表
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        self.splitter.setSizes([190, 460])     # 无保存状态时的默认比例
        layout.addWidget(self.splitter, stretch=1)

        self._splitter_save_timer = QtCore.QTimer(self)
        self._splitter_save_timer.setSingleShot(True)
        self._splitter_save_timer.setInterval(500)
        self._splitter_save_timer.timeout.connect(self._save_splitter_state)
        self.splitter.splitterMoved.connect(
            lambda *_: self._splitter_save_timer.start())  # 拖动中防抖

        # —— TradingView 风格十字光标 + 悬浮信息窗 ——
        cross_pen = pg.mkPen((168, 176, 184, 150), style=QtCore.Qt.DashLine)
        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=cross_pen)
        self.hline = pg.InfiniteLine(angle=0, movable=False, pen=cross_pen)
        self.hover_text = pg.TextItem(
            html="", anchor=(0, 0),
            fill=pg.mkBrush(16, 20, 24, 235),
            border=pg.mkPen((96, 105, 114)),
        )
        vb = self.plot.plotItem.vb
        for item in (self.vline, self.hline, self.hover_text):
            item.setZValue(100)               # 盖在所有曲线/标记之上
            item.setVisible(False)
            vb.addItem(item, ignoreBounds=True)  # 不参与自动缩放
        self._hover_timer = QtCore.QElapsedTimer()  # 高频鼠标事件节流
        self._hover_timer.start()
        self.plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.plot.viewport().installEventFilter(self)  # 鼠标移出图表时隐藏

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
        self.close()   # closeEvent 统一负责清理与退出

    def _hide_to_tray(self):
        self.hide()
        if self.tray is not None and not self._tray_tip_shown:
            self._tray_tip_shown = True
            self.tray.showMessage(
                "PingMonitor 仍在后台运行",
                "监控未停止。双击托盘图标恢复界面，右键菜单可退出。",
                QtWidgets.QSystemTrayIcon.Information, 3000)

    # ---------------- 目标管理 ----------------

    @staticmethod
    def _display_name(host: str, alias: str) -> str:
        """统一的展示名规则：有别名显示 '别名 (host)'，否则只显示 host。"""
        return f"{alias} ({host})" if alias else host

    def _row_host(self, row: int):
        """表格行 -> host。host 存于 UserRole，COL_TARGET 文本是展示名。"""
        item = self.table.item(row, COL_TARGET)
        return item.data(QtCore.Qt.UserRole) if item else None

    def add_target(self, target: str, alias: str = ""):
        target = target.strip()
        if not target or target in self.targets:
            return

        name = self._display_name(target, alias)
        color = CURVE_COLORS[len(self.targets) % len(CURVE_COLORS)]
        curve = self.plot.plot(
            [], [], pen=pg.mkPen(color=color, width=2),
            name=name, connect="finite",   # NaN 处断线，直观呈现丢包
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
        # 丢包事件标记：目标同色 ✕，锚定在该目标曲线的缺口处——
        # 颜色 + 位置双重归属，不再挤占图表顶部空间。
        # ignoreBounds=True → 不参与 Y 轴自动缩放（底部车道回退时
        # 标记位置依赖视野范围，避免反馈循环）。
        scatter = pg.ScatterPlotItem(
            symbol="x", brush=None,
            pen=pg.mkPen(color=color, width=2),
        )
        scatter.setZValue(5)                 # 盖在曲线之上，缺口处清晰可见
        self.plot.plotItem.vb.addItem(scatter, ignoreBounds=True)

        row = self.table.rowCount()
        self.table.insertRow(row)
        for col in range(len(TABLE_HEADERS)):
            item = QtWidgets.QTableWidgetItem("-")
            item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.table.setItem(row, col, item)
        target_item = self.table.item(row, COL_TARGET)
        target_item.setText(name)
        target_item.setData(QtCore.Qt.UserRole, target)  # 内部仍以 host 为键

        info = {
            "alias": alias,
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
            self._persist_targets()
        if self.running:
            self._start_worker(target)

    def _persist_targets(self):
        """统一写回新版格式 [{"host", "alias"}]（dict 保持插入顺序）。"""
        save_config(targets=[{"host": h, "alias": i["alias"]}
                             for h, i in self.targets.items()])

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
            legend.removeItem(info["curve"])   # 按对象移除，不受别名影响
        for row in range(self.table.rowCount()):
            if self._row_host(row) == target:
                self.table.removeRow(row)
                break
        self._persist_targets()

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
        # x 轴全链路使用 Unix 时间戳；由 monotonic 推导（wall_start +
        # 运行秒数），运行期间系统调时/NTP 跳变不会打乱数据单调性
        wall_ts = self.wall_start + (ts - self.start_time)
        info["buffer"].append(wall_ts, rtt if rtt is not None else math.nan)

        # 连续丢包恰好达到阈值时告警一次；恢复后 consec 归零，可再次触发
        if (rtt is None and st.consec_loss == CONSEC_LOSS_ALERT
                and self.tray is not None):
            self.tray.showMessage(
                "网络异常告警",
                f"{self._display_name(target, info['alias'])} "
                f"已连续丢包 {st.consec_loss} 次"
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
        self.remove_target(self._row_host(row))

    def _on_table_menu(self, pos):
        row = self.table.rowAt(pos.y())
        host = self._row_host(row) if row >= 0 else None
        if host is None:
            return
        menu = QtWidgets.QMenu(self.table)
        act_alias = menu.addAction("设置别名")
        act_remove = menu.addAction("移除目标")
        chosen = menu.exec_(self.table.viewport().mapToGlobal(pos))
        if chosen is act_alias:
            self._edit_alias(host)
        elif chosen is act_remove:
            self.remove_target(host)

    def _edit_alias(self, host):
        """弹窗编辑别名（_edit_alias 只管对话框，落地走 set_alias）。"""
        info = self.targets.get(host)
        if info is None:
            return
        alias, ok = QtWidgets.QInputDialog.getText(
            self, "设置别名", f"{host} 的别名（留空清除）：",
            text=info["alias"])
        if ok:
            self.set_alias(host, alias)

    def set_alias(self, host: str, alias: str):
        """更新别名并同步所有展示层（表格/图例），立即持久化。"""
        info = self.targets.get(host)
        if info is None:
            return
        info["alias"] = alias.strip()
        name = self._display_name(host, info["alias"])
        for row in range(self.table.rowCount()):       # 表格"目标"列
            if self._row_host(row) == host:
                self.table.item(row, COL_TARGET).setText(name)
                break
        legend = self.plot.plotItem.legend             # 图例标签
        if legend is not None:
            for sample, label in legend.items:
                if sample.item is info["curve"]:
                    label.setText(name)
                    break
        info["curve"].opts["name"] = name
        self._persist_targets()

    def _on_range_changed(self, _index: int):
        # 下拉框语义 = 跟随模式下的窗口宽度；切换时同时恢复跟随
        self.range_secs = self.range_combo.currentData()
        self._set_follow(True)
        self.refresh_ui()   # 立即按新窗口重绘，不等下一个定时器周期

    def _on_follow_clicked(self):
        self._set_follow(True)   # 点击总是恢复跟随并跳到最新
        self.refresh_ui()

    def _set_follow(self, follow: bool):
        self.follow = follow
        self.btn_follow.setChecked(follow)

    def _latest_ts(self) -> float:
        """当前时刻的 Unix 时间戳（monotonic 推导，与数据同源）。"""
        return self.wall_start + (time.monotonic() - self.start_time)

    def _on_x_range_changed(self, _vb, xrange):
        if self._setting_range:
            return               # 程序触发（跟随滚动），忽略
        # 用户拖动/缩放：右缘贴近最新数据 -> 恢复跟随；否则手动浏览
        x0, x1 = xrange
        near_edge = x1 >= self._latest_ts() - 0.02 * max(x1 - x0, 1.0)
        self._set_follow(near_edge)
        self._stats_debounce.start()   # 防抖重算选区统计与重绘

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

    # ---------------- 十字光标 + 悬浮信息窗 ----------------

    def eventFilter(self, obj, event):
        # 鼠标离开图表区域时隐藏十字线（sigMouseMoved 不会在离开时触发）
        if obj is self.plot.viewport() and event.type() == QtCore.QEvent.Leave:
            self._set_crosshair_visible(False)
        return super().eventFilter(obj, event)

    def _set_crosshair_visible(self, visible: bool):
        self.vline.setVisible(visible)
        self.hline.setVisible(visible)
        self.hover_text.setVisible(visible)

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        s = max(0, int(seconds))
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"

    def _on_mouse_moved(self, scene_pos):
        vb = self.plot.plotItem.vb
        if not vb.sceneBoundingRect().contains(scene_pos):
            self._set_crosshair_visible(False)
            return
        # 节流：鼠标事件可达数百 Hz，30ms 一次足够流畅
        if self._hover_timer.elapsed() < 30:
            return
        self._hover_timer.restart()

        pt = vb.mapSceneToView(scene_pos)
        x, y = pt.x(), pt.y()

        # 对每个目标在缓存的窗口数组上二分查找最近采样点（O(log n)）
        snap_x = None
        rows = []
        for target, info in self.targets.items():
            wt, wv = info.get("win_t"), info.get("win_v")
            if wt is None or len(wt) == 0:
                continue
            i = int(np.searchsorted(wt, x))
            if i >= len(wt):
                i = len(wt) - 1
            elif i > 0 and (x - wt[i - 1]) < (wt[i] - x):
                i -= 1
            if snap_x is None:
                snap_x = wt[i]            # 竖线吸附到最近采样时刻
            rows.append((self._display_name(target, info["alias"]),
                         info["color"], wv[i]))

        sx = x if snap_x is None else snap_x
        # sx 即 Unix 时间戳；运行时长 = 时间戳 - 启动基准
        wall = time.strftime("%H:%M:%S", time.localtime(sx))
        lines = [
            f"<b>{wall}</b>&nbsp;<span style='color:#9aa4ad'>"
            f"(运行 {self._fmt_duration(sx - self.wall_start)})</span>",
            f"<span style='color:#9aa4ad'>光标: {y:.1f} ms</span>",
        ]
        for name, color, v in rows:
            c = "#%02x%02x%02x" % color
            val = ("<b style='color:#ff5050'>丢包</b>" if math.isnan(v)
                   else f"<b>{v:.1f} ms</b>")
            lines.append(f"<span style='color:{c}'>●</span> {name}&nbsp;{val}")
        self.hover_text.setHtml(
            "<div style='font-size: 9pt; color: #dddddd; white-space: nowrap'>"
            + "<br/>".join(lines) + "</div>")

        # 信息窗停靠方向：靠近右/下边缘时翻转锚点，保证始终在视野内
        (x_lo, x_hi), (y_lo, y_hi) = vb.viewRange()
        anchor_x = 0 if x < (x_lo + x_hi) / 2 else 1
        anchor_y = 0 if y > (y_lo + y_hi) / 2 else 1
        self.hover_text.setAnchor((anchor_x, anchor_y))

        self.vline.setPos(sx)
        self.hline.setPos(y)
        self.hover_text.setPos(sx, y)
        self._set_crosshair_visible(True)

    # ---------------- 周期刷新 ----------------

    def refresh_ui(self):
        """周期刷新（500ms）/ 防抖回调：跟随滚动 X 窗口 -> 重绘可见区 ->
        按可见 X 范围重算选区统计。"""
        if self.follow:
            t_latest = self._latest_ts()
            self._set_x_range(t_latest - self.range_secs, t_latest)
        self._refresh_plot()
        self._refresh_table_stats()

    def _set_x_range(self, x0: float, x1: float):
        """程序触发的 X 范围设置：置标志位以便范围信号区分用户操作。"""
        self._setting_range = True
        try:
            self.plot.setXRange(x0, x1, padding=0.02)
        finally:
            self._setting_range = False

    def _refresh_plot(self):
        """重绘当前可见 X 范围：取数 -> 包络降采样 -> setData。"""
        vb = self.plot.plotItem.vb
        x0, x1 = vb.viewRange()[0]
        # 底部车道间距：仅用于"选区内全程丢包、无锚点"的退化场景
        y_lo, y_hi = vb.viewRange()[1]
        lane = (y_hi - y_lo) * 0.045 or 1.0

        for row in range(self.table.rowCount()):
            info = self.targets.get(self._row_host(row))
            if info is None:
                continue
            # 可见范围裁剪：window() 取左界，searchsorted 截右界
            ts, vs = info["buffer"].window(x0)
            j = int(np.searchsorted(ts, x1, side="right"))
            ts, vs = ts[:j], vs[:j]
            # 缓存原始（未降采样）可见数组：十字光标二分查找 +
            # 选区统计共用，避免重复拼接环形缓冲
            info["win_t"], info["win_v"] = ts, vs
            t_line, v_line, v_lo, v_hi, loss_flag = envelope_series(
                ts, vs, MAX_PLOT_POINTS)
            info["curve"].setData(t_line, v_line, connect="finite")
            info["band_lo"].setData(t_line, v_lo, connect="finite")
            info["band_hi"].setData(t_line, v_hi, connect="finite")

            # 丢包事件标记：连续丢包聚合为单个 ✕，锚在曲线缺口处；
            # 事件越长标记越大（对数增长，封顶），密度与噪声大幅下降
            mx, my, mc = loss_markers(t_line, v_line, loss_flag)
            if len(mx):
                no_anchor = np.isnan(my)
                if no_anchor.any():
                    # 选区内该目标全程丢包：退化到底部车道，按行错开
                    my = my.copy()
                    my[no_anchor] = y_lo + lane * (row + 1)
                sizes = np.clip(8 + 2 * np.log2(mc), 8, 16)
                info["scatter"].setData(x=mx, y=my, size=sizes)
            else:
                info["scatter"].setData(x=_EMPTY, y=_EMPTY)

    def _refresh_table_stats(self):
        """表格统计：丢包率/P50/P95/抖动按可见选区现算，累计后置最后一列。"""
        for row in range(self.table.rowCount()):
            info = self.targets.get(self._row_host(row))
            if info is None:
                continue
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
            self._set_cell(row, COL_CUR,
                           "超时" if st.last is None else f"{st.last:.0f}")

            # —— 选区统计：_refresh_plot 缓存的可见数组 ——
            wv = info.get("win_v")
            n = 0 if wv is None else len(wv)
            if n == 0:
                for col in (COL_LOSS, COL_P50, COL_P95, COL_JITTER):
                    self._set_cell(row, col, "-")
            else:
                lost = int(np.isnan(wv).sum())
                loss = lost / n * 100.0
                loss_color = ("#30d060" if loss < 1 else
                              "#ffc83c" if loss < 10 else "#ff5050")
                self._set_cell(row, COL_LOSS, f"{loss:.1f}%", loss_color)
                valid = wv[~np.isnan(wv)]
                if valid.size:
                    self._set_cell(row, COL_P50,
                                   f"{np.percentile(valid, 50):.0f}")
                    self._set_cell(row, COL_P95,
                                   f"{np.percentile(valid, 95):.0f}")
                else:
                    self._set_cell(row, COL_P50, "-")
                    self._set_cell(row, COL_P95, "-")
                # 选区抖动 = 相邻成功样本差值绝对值的均值
                if valid.size >= 2:
                    jit = float(np.mean(np.abs(np.diff(valid))))
                    j_color = ("#30d060" if jit < 5 else
                               "#ffc83c" if jit < 20 else "#ff5050")
                    self._set_cell(row, COL_JITTER, f"{jit:.1f}", j_color)
                else:
                    self._set_cell(row, COL_JITTER, "-")

            # —— 全程汇总（最后一列）——
            self._set_cell(row, COL_TOTAL,
                           f"{st.sent}/{st.lost} ({st.loss_rate:.1f}%)")

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

    def _save_splitter_state(self):
        state = bytes(self.splitter.saveState().toBase64()).decode("ascii")
        save_config(splitter_state=state)

    def closeEvent(self, event):
        # 关闭按钮 = 退出程序（最小化按钮仍是隐藏到托盘）
        self._save_splitter_state()   # 防抖定时器可能尚未触发，退出前兜底
        self.refresh_timer.stop()
        for info in self.targets.values():
            self._stop_worker_obj(info)
        if self.tray is not None:
            self.tray.hide()
        event.accept()
        # quitOnLastWindowClosed 已关闭（托盘驻留需要），须显式退出事件循环
        QtWidgets.QApplication.quit()


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
