import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from helpers import monitor  # noqa: E402

MON = ROOT / "helpers" / "monitor.py"

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     3958.
Pages active:                                  64000.
Pages inactive:                                93907.
Pages wired down:                              32000.
Pages occupied by compressor:                  32000.
"""


def mon(action, edit_dir, *extra):
    return subprocess.run([sys.executable, str(MON), action, "--edit-dir", str(edit_dir), *extra],
                          capture_output=True, text=True, timeout=30)


def test_parsers():
    assert monitor.parse_vm_stat(VM_STAT) == 2000.0  # 128000 pages * 16 KiB
    assert monitor.parse_swapusage("vm.swapusage: total = 6144.00M  used = 4647.88M  free = 1496.12M") == 4647.9
    assert monitor.parse_swapusage("vm.swapusage: total = 2.00G  used = 1.50G  free = 0.50G") == 1536.0
    assert monitor.parse_swapusage("garbage") == 0.0


def test_start_stop_summary(tmp_path):
    assert mon("start", tmp_path, "--interval", "0.2").returncode == 0
    pid = int((tmp_path / "monitor.pid").read_text())
    assert mon("start", tmp_path).returncode == 1  # refuses a second sampler
    time.sleep(0.8)
    assert mon("stop", tmp_path).returncode == 0
    assert not (tmp_path / "monitor.pid").exists()
    time.sleep(0.1)
    try:
        import os
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive
    rows = [json.loads(l) for l in (tmp_path / "monitor.jsonl").read_text().splitlines()]
    assert len(rows) >= 3
    assert set(rows[0]) == {"t", "used_mb", "swap_used_mb"}
    assert rows[0]["used_mb"] > 100
    p = mon("summary", tmp_path)
    assert p.returncode == 0
    s = json.loads((tmp_path / "monitor_summary.json").read_text())
    for k in ("baseline_used_mb", "peak_used_mb", "peak_extra_mb", "peak_swap_mb", "swap_growth_mb"):
        assert k in s
    assert s["baseline_used_mb"] == rows[0]["used_mb"]
    assert s["peak_extra_mb"] == round(s["peak_used_mb"] - s["baseline_used_mb"], 1)


def test_summary_math(tmp_path):
    lines = [{"t": 1, "used_mb": 5000, "swap_used_mb": 100},
             {"t": 2, "used_mb": 6500, "swap_used_mb": 300},
             {"t": 3, "used_mb": 5200, "swap_used_mb": 250}]
    (tmp_path / "monitor.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
    assert mon("summary", tmp_path).returncode == 0
    s = json.loads((tmp_path / "monitor_summary.json").read_text())
    assert (s["baseline_used_mb"], s["peak_used_mb"], s["peak_extra_mb"]) == (5000, 6500, 1500)
    assert (s["peak_swap_mb"], s["swap_growth_mb"]) == (300, 200)


def test_stop_without_start_fails(tmp_path):
    p = mon("stop", tmp_path)
    assert p.returncode == 1 and "not running" in p.stderr
