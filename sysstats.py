"""Live readings of the Raspberry Pi: CPU, memory, temperature and throttling."""
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

TEMPERATURE = Path("/sys/class/thermal/thermal_zone0/temp")
FREQUENCY = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
MAX_AGE_S = 1.5
SEPARATOR = " │ "
TREND_SAMPLES = 12
TREND_STEP_S = 10  # 12 bars cover the last two minutes
TREND_BLOCKS = "▁▂▃▄▅▆▇█"
TREND_MIN_C = 40
TREND_MAX_C = 85
# Only the bits for "right now"; the higher bits stay set until the next reboot.
THROTTLE_FLAGS = ((0x1, "UNDERVOLTAGE"), (0x4, "THROTTLED"), (0x2, "CAPPED"), (0x8, "TEMP LIMIT"))


@dataclass
class Reading:
    cpu_percent: int
    mhz: int
    temp_c: float
    ram_used_mb: int
    ram_total_mb: int
    swap_used_mb: int
    model_mb: int | None
    status: str
    temp_trend: str = ""


class SystemStats:
    def __init__(self):
        self.last_cpu = self._cpu_times()
        self.temps = deque(maxlen=TREND_SAMPLES)
        self.trend_at = 0.0
        self.reading = None
        self.read_at = 0.0
        self.read_pid = None

    def read(self, pid=None):
        """Returns current values; calls within MAX_AGE_S for the same model share one measurement."""
        now = time.monotonic()
        if self.reading is None or now - self.read_at >= MAX_AGE_S or pid != self.read_pid:
            self.reading = self._measure(pid, now)
            self.read_at = now
            self.read_pid = pid
        return self.reading

    def _measure(self, pid, now):
        memory = self._meminfo()
        temp = int(TEMPERATURE.read_text()) / 1000
        # A new bar every TREND_STEP_S; until then the newest bar follows the current value.
        if self.temps and now - self.trend_at < TREND_STEP_S:
            self.temps[-1] = temp
        else:
            self.temps.append(temp)
            self.trend_at = now
        return Reading(
            cpu_percent=self._cpu_percent(),
            mhz=int(FREQUENCY.read_text()) // 1000,
            temp_c=temp,
            temp_trend=self._trend(),
            ram_used_mb=(memory["MemTotal"] - memory["MemAvailable"]) // 1024,
            ram_total_mb=memory["MemTotal"] // 1024,
            swap_used_mb=(memory["SwapTotal"] - memory["SwapFree"]) // 1024,
            model_mb=self._process_mb(pid),
            status=self._throttle_status(),
        )

    def _trend(self):
        """The last temperatures as a small bar chart, scaled from 40 to 85 °C."""
        top = len(TREND_BLOCKS) - 1
        steps = (round((t - TREND_MIN_C) / (TREND_MAX_C - TREND_MIN_C) * top) for t in self.temps)
        return "".join(TREND_BLOCKS[min(top, max(0, step))] for step in steps)

    def _cpu_percent(self):
        total, idle = self._cpu_times()
        last_total, last_idle = self.last_cpu
        self.last_cpu = (total, idle)
        elapsed = total - last_total
        return round(100 * (elapsed - (idle - last_idle)) / elapsed) if elapsed else 0

    @staticmethod
    def _cpu_times():
        with open("/proc/stat") as stat:
            values = [int(value) for value in stat.readline().split()[1:9]]
        return sum(values), values[3] + values[4]

    @staticmethod
    def _meminfo():
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.split()[0])
        return memory

    @staticmethod
    def _process_mb(pid):
        if pid is None:
            return None
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
        except OSError:
            pass
        return None

    @staticmethod
    def _throttle_status():
        try:
            output = subprocess.run(
                ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2
            ).stdout
            value = int(output.strip().split("=")[1], 16)
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            return "?"
        for bit, label in THROTTLE_FLAGS:
            if value & bit:
                return label
        return "OK"


def format_line(reading, width):
    """One line of readings; less important fields go first on narrow screens."""
    fields = {
        "cpu": f"CPU {reading.cpu_percent}%",
        "mhz": f"{reading.mhz} MHz",
        "temp": f"{reading.temp_c:.0f}°C {reading.temp_trend}".rstrip(),
        "ram": f"RAM {reading.ram_used_mb}/{reading.ram_total_mb} MB",
        "swap": f"Swap {reading.swap_used_mb} MB",
        "status": reading.status,
    }
    if reading.model_mb is not None:
        fields["model"] = f"Model {reading.model_mb} MB"
        fields["status"] = fields.pop("status")

    line = SEPARATOR.join(fields.values())
    for name in ("model", "swap", "mhz", "trend"):
        if len(line) < width:
            break
        if name == "trend":
            fields["temp"] = f"{reading.temp_c:.0f}°C"
        else:
            fields.pop(name, None)
        line = SEPARATOR.join(fields.values())
    return (" " + line)[:width]
