# PingMonitor

Windows 多目标实时 Ping 监控工具（单文件 Python 实现）。

连续 Ping 指定的 IP 列表，实时统计丢包率与最小/最大/平均延迟，并以动态折线图展示。

## 功能特性

- 基于 Windows 原生 `IcmpSendEcho` API，**无需管理员权限**，中英文系统通用
- 每个目标独立线程，UI 不卡顿；支持运行时添加/移除目标（IP 或域名）
- 横轴真实时间（DateAxisItem）：拖动/缩放自由回看，"跟随最新"一键恢复，拖回最右缘自动恢复跟随
- 选区统计联动：表格的丢包率/P50/P95/抖动按图表当前可见范围现算（150ms 防抖），累计汇总后置最后一列
- PyQtGraph 高性能动态折线图：均值线 + min/max 包络阴影带（带宽即波动幅度）
- 丢包事件标记：连续丢包聚合为单个目标同色 ✕，锚定在曲线缺口处，事件越长标记越大
- TradingView 风格十字光标：吸附最近采样点，悬浮窗显示该时刻所有目标的延迟/丢包详情
- 系统托盘驻留：最小化隐藏到托盘（关闭按钮直接退出），连续丢包托盘气泡告警
- 时间范围切换（1分钟 ~ 24小时），环形缓冲 + 峰值降采样，长周期不卡顿
- 目标列表持久化：`%APPDATA%\PingMonitor\config.json`，添加/移除即保存，重启自动恢复
- 表格/图表高度比例可拖动（QSplitter），分隔条位置随配置持久化
- 目标别名：双击行或右键菜单设置，表格/图例/悬浮窗统一显示"别名 (host)"

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

## 自动发布（GitHub Actions）

推送 `v*` 标签即自动打包并创建 Release（附带 `PingMonitor-<tag>-win64.zip`）：

```powershell
git tag v1.1.0
git push origin v1.1.0
```

也可在 GitHub 的 Actions 页面手动触发 `Build and Release` 工作流，产物以构建 artifact 形式提供下载（不创建 Release）。

## 配置

文件顶部配置区可调整：Ping 周期（`PING_INTERVAL`）、超时阈值（`PING_TIMEOUT_MS`）、
连续丢包告警阈值（`CONSEC_LOSS_ALERT`）、降采样点数（`MAX_PLOT_POINTS`）、
默认监控目标（`DEFAULT_TARGETS`）等。
