import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("resource_guard", ROOT / "scripts" / "resource_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def sensor(*values):
    """A reader returning VALUES in turn, then the last one forever."""
    remaining = list(values)
    return lambda: remaining.pop(0) if len(remaining) > 1 else remaining[0]


class ReaderTests(unittest.TestCase):
    def test_hwmon_takes_the_hottest_sensor_of_the_named_driver(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name, device, temps in (("k10temp", "hwmon0", (78000, 61000)), ("nvme", "hwmon1", (47000,))):
                (root / device).mkdir()
                (root / device / "name").write_text(f"{name}\n")
                for i, value in enumerate(temps, 1):
                    (root / device / f"temp{i}_input").write_text(f"{value}\n")
            self.assertEqual(guard.hwmon_max(guard.CPU_DRIVERS, root), 78)
            self.assertEqual(guard.hwmon_max({"nvme"}, root), 47)
            self.assertIsNone(guard.hwmon_max({"amdgpu"}, root))

    def test_disks_ini_counts_sata_drives_that_report_a_temperature(self):
        ini = ('["parity"]\nname="parity"\ndevice="sdf"\ntemp="43"\n'
               '["disk1"]\nname="disk1"\ndevice="sdb"\ntemp="*"\n'
               '["disk2"]\nname="disk2"\ndevice="sde"\ntemp="46"\n'
               '["cache"]\nname="cache"\ndevice="nvme0n1"\ntemp="60"\n'
               '["parity2"]\nname="parity2"\ndevice=""\ntemp="*"\n')
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "disks.ini"
            path.write_text(ini)
            self.assertEqual(guard.disk_temp(path), 46)
            self.assertIsNone(guard.disk_temp(Path(raw) / "missing.ini"))

    def test_ram_percent_uses_available_memory(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "meminfo"
            path.write_text("MemTotal:  1000 kB\nMemFree:  10 kB\nMemAvailable:  250 kB\n")
            self.assertAlmostEqual(guard.ram_pct(path), 75)


class GuardTests(unittest.TestCase):
    def test_blank_or_invalid_limits_turn_a_sensor_off(self):
        limits = guard.configured({"THROTTLE_GPU_TEMP": "80", "THROTTLE_CPU_TEMP": "", "THROTTLE_RAM_PCT": "lots"})
        self.assertEqual([item[1] for item in limits], ["GPU"])

    def test_no_wait_under_the_limit_or_when_a_sensor_cannot_be_read(self):
        slept = []
        limits = [(80, "GPU", "C", sensor(60)), (90, "CPU", "C", sensor(None))]
        guard.wait(limits, 5, sleep=slept.append)
        self.assertEqual(slept, [])

    def test_holds_until_every_sensor_is_below_limit_less_margin(self):
        slept = []
        # 82: over. 78: under the limit but inside the margin, so still held. 74: clear.
        limits = [(80, "GPU", "C", sensor(82, 78, 74))]
        with patch("builtins.print") as out:
            guard.wait(limits, 5, poll=30, sleep=slept.append)
        self.assertEqual(slept, [30, 30])
        self.assertIn("GPU 82C (limit 80C)", out.call_args_list[0].args[0])
        self.assertIn("resuming", out.call_args_list[-1].args[0])

    def test_any_enabled_sensor_holds_the_job(self):
        limits = [(80, "GPU", "C", sensor(50)), (90, "RAM", "%", sensor(95, 10))]
        with patch("builtins.print") as out:
            guard.wait(limits, 5, sleep=lambda _s: None)
        self.assertIn("RAM 95%", out.call_args_list[0].args[0])

    def test_main_returns_at_once_with_no_limits(self):
        with patch.object(guard, "configured", return_value=[]), patch.object(guard, "wait") as wait:
            self.assertEqual(guard.main(["x", "wait"]), 0)
        wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
