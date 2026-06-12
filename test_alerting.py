"""告警状态机单元测试（纯逻辑，无 Qt 依赖）。

时间戳均为显式传入的浮点秒，便于模拟任意时序。
"""
import unittest

from ping_monitor import AlertManager


def feed(mgr, host, t0, pattern, interval=1.0):
    """按 pattern（'x'=丢包，'o'=成功）逐秒喂入，返回全部事件列表。"""
    events = []
    for i, ch in enumerate(pattern):
        rtt = None if ch == "x" else 20.0
        events.extend(mgr.update(host, t0 + i * interval, rtt))
    return events


class TestDownRecovery(unittest.TestCase):
    def setUp(self):
        self.mgr = AlertManager()
        self.mgr.add_target("a")

    def test_three_consecutive_losses_fires_single_down_event(self):
        events = feed(self.mgr, "a", 0.0, "xxxx")
        downs = [e for e in events if e.kind == "down"]
        self.assertEqual(len(downs), 1)
        self.assertEqual(downs[0].host, "a")
        self.assertEqual(downs[0].ts, 2.0)  # 第 3 次丢包时刻

    def test_recovery_needs_five_consecutive_successes(self):
        # xxx 后 4 个成功不足以恢复，第 5 个成功（ts=7.0）才触发
        events = feed(self.mgr, "a", 0.0, "xxxoooo")
        self.assertEqual([e.kind for e in events], ["down"])
        events = feed(self.mgr, "a", 7.0, "o")
        self.assertEqual([e.kind for e in events], ["recovered"])
        # 故障时长 = 首个恢复成功时刻(3.0) - 故障起始丢包时刻(0.0)
        self.assertEqual(events[0].data["duration"], 3.0)

    def test_down_event_carries_recent_window_stats(self):
        # 气泡展示近期窗口丢包而非全程累计丢包率（长期运行时后者失真）
        events = feed(self.mgr, "a", 0.0, "ooooo" + "xxx")
        down = events[0]
        self.assertEqual(down.data["window_loss"], 3)
        self.assertEqual(down.data["window_size"], 8)

    def test_partial_success_mid_outage_does_not_rearm_or_recover(self):
        events = feed(self.mgr, "a", 0.0, "xxxooxxx")
        self.assertEqual([e.kind for e in events], ["down"])

    def test_down_fires_again_after_full_recovery(self):
        # interval=200s 使两次故障间隔超过告警冷却期
        events = feed(self.mgr, "a", 0.0, "xxx" + "ooooo" + "xxx",
                      interval=200.0)
        self.assertEqual([e.kind for e in events],
                         ["down", "recovered", "down"])


class TestDegraded(unittest.TestCase):
    def setUp(self):
        self.mgr = AlertManager()
        self.mgr.add_target("a")

    def test_scattered_loss_in_window_fires_degraded_once(self):
        # 每 3 个一组 oox：无连续 3 丢包，但 20 包窗口内丢包达 5 → degraded
        events = feed(self.mgr, "a", 0.0, "ooxooxooxooxoox" * 2)
        kinds = [e.kind for e in events]
        self.assertEqual(kinds.count("degraded"), 1)
        self.assertNotIn("down", kinds)
        # 第 5 次丢包是第 15 个包（索引 14）
        deg = next(e for e in events if e.kind == "degraded")
        self.assertEqual(deg.ts, 14.0)
        self.assertEqual(deg.data["window_loss"], 5)
        self.assertEqual(deg.data["window_size"], 15)

    def test_degraded_clears_when_window_loss_drops(self):
        # 劣化后持续成功：窗口内丢包降到 ≤2 时解除（迟滞下限）
        events = feed(self.mgr, "a", 0.0, "ooxooxooxooxoox" + "o" * 15)
        self.assertEqual([e.kind for e in events],
                         ["degraded", "degraded_recovered"])
        # 丢包位于 idx 2/5/8/11/14；idx8 在第 28 包滑出窗口后只剩 2 个
        self.assertEqual(events[1].ts, 28.0)

    def test_degraded_escalates_to_down_then_full_cycle_rearms(self):
        # 劣化 → 连续丢包升级为 down → 恢复 → 再次散布丢包应能再劣化
        # interval=60s 使两次劣化间隔超过告警冷却期
        pattern = ("ooxooxooxooxoox" + "xx" + "ooooo"
                   + "ooxooxooxooxoox")
        events = feed(self.mgr, "a", 0.0, pattern, interval=60.0)
        self.assertEqual([e.kind for e in events],
                         ["degraded", "down", "recovered", "degraded"])


class TestCooldownAndCorrelation(unittest.TestCase):
    def setUp(self):
        self.mgr = AlertManager()
        self.mgr.add_target("a")

    def test_flap_within_cooldown_is_silenced(self):
        # 1 秒间隔：第二次故障落在冷却期内 → down 与配套 recovered 均静默
        events = feed(self.mgr, "a", 0.0, "xxx" + "ooooo" + "xxx" + "ooooo")
        self.assertEqual([e.kind for e in events], ["down", "recovered"])

    def test_down_announced_again_after_cooldown_elapses(self):
        feed(self.mgr, "a", 0.0, "xxx" + "ooooo")
        events = feed(self.mgr, "a", 400.0, "xxx" + "ooooo")
        self.assertEqual([e.kind for e in events], ["down", "recovered"])

    def test_all_targets_down_collapses_to_single_alert(self):
        self.mgr.add_target("b")
        ev_a = feed(self.mgr, "a", 0.0, "xxx")
        self.assertEqual([e.kind for e in ev_a], ["down"])
        ev_b = feed(self.mgr, "b", 0.0, "xxx")
        self.assertEqual([e.kind for e in ev_b], ["all_down"])
        self.assertEqual(ev_b[0].data["count"], 2)

    def test_partial_outage_stays_per_target(self):
        self.mgr.add_target("b")
        feed(self.mgr, "b", 0.0, "ooo")          # b 正常
        ev_a = feed(self.mgr, "a", 0.0, "xxx")   # 仅 a 故障
        self.assertEqual([e.kind for e in ev_a], ["down"])


if __name__ == "__main__":
    unittest.main()
