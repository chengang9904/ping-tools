# PingMonitor

Windows 多目标实时 Ping 监控工具（单文件 Python 实现）。

连续 Ping 指定的 IP 列表，实时统计丢包率与最小/最大/平均延迟，并以动态折线图展示。

## 功能特性

- 基于 Windows 原生 `IcmpSendEcho` API，**无需管理员权限**，中英文系统通用
- 每个目标独立线程，UI 不卡顿；支持运行时添加/移除目标（IP 或域名）
- 实时统计表：丢包率、当前延迟、P50/P95（最近 1 分钟滚动窗口）、RFC 3550 抖动
- PyQtGraph 高性能动态折线图：均值线 + min/max 包络阴影带（带宽即波动幅度），丢包时刻以红色 X 标记
- 系统托盘驻留：最小化/关闭隐藏到托盘，连续丢包托盘气泡告警
- 时间范围切换（1分钟 ~ 24小时），环形缓冲 + 峰值降采样，长周期不卡顿

## 运行

```powershell
pip install PyQt5 pyqtgraph
python ping_monitor.py
```

## 打包为独立 exe

```powershell
pip install pyinstaller
pyinstaller -F -w --name PingMonitor ping_monitor.py
# 产物：dist/PingMonitor.exe
```

## 配置

文件顶部配置区可调整：Ping 周期（`PING_INTERVAL`）、超时阈值（`PING_TIMEOUT_MS`）、
连续丢包告警阈值（`CONSEC_LOSS_ALERT`）、降采样点数（`MAX_PLOT_POINTS`）、
默认监控目标（`DEFAULT_TARGETS`）等。
