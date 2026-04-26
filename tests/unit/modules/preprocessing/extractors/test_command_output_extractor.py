"""
Tests for CommandOutputExtractor.

Covers:
- R6.1: iostat parser
- R6.2: vmstat parser
- Existing commands (top, df) still work
"""

import pytest

from faultmaven.modules.preprocessing.extractors.command_output_extractor import (
    CommandOutputExtractor,
)


class TestCommandOutputExtractor:
    @pytest.fixture
    def extractor(self):
        return CommandOutputExtractor()

    def test_properties(self, extractor):
        assert extractor.strategy_name == "command_parsing"
        assert extractor.llm_calls_used == 0

    # --- R6.1: iostat parser ---

    def test_iostat_basic(self, extractor):
        """iostat output parsed with device stats."""
        content = """\
Linux 5.4.0-42-generic (server1)    01/15/2024

avg-cpu:  %user   %nice %system %iowait  %steal   %idle
           5.20    0.00    2.10    1.50    0.00   91.20

Device            tps    kB_read/s    kB_wrtn/s    kB_dscd/s    kB_read    kB_wrtn    kB_dscd   await  %util
sda             45.00       120.00       250.00         0.00    1200000    2500000          0    5.20   12.30
sdb            200.00       500.00      1200.00         0.00    5000000   12000000          0   25.00   85.50
"""
        result = extractor.extract(content)
        assert "I/O Statistics" in result.file_extract
        assert "sda" in result.file_extract
        assert "sdb" in result.file_extract
        # sdb should be flagged: await > 20ms and util > 80%
        assert (
            "Anomalies" in result.file_extract
            or "anomal" in result.file_extract.lower()
        )

    def test_iostat_no_anomalies(self, extractor):
        """iostat with all healthy devices."""
        content = """\
Linux 5.4.0 (host)

avg-cpu:  %user   %nice %system %iowait  %steal   %idle
           2.00    0.00    1.00    0.50    0.00   96.50

Device            tps    kB_read/s    kB_wrtn/s    await  %util
sda             10.00        50.00       100.00     2.00    5.00
"""
        result = extractor.extract(content)
        assert "I/O Statistics" in result.file_extract
        assert "sda" in result.file_extract
        # No anomalies expected
        assert "Anomalies" not in result.file_extract

    # --- R6.2: vmstat parser ---

    def test_vmstat_basic(self, extractor):
        """vmstat output parsed with anomaly detection."""
        content = """\
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 2  1  51200 102400  20480 204800  100  200    50   100  500  800 30 10 35 25  0
"""
        result = extractor.extract(content)
        assert "Virtual Memory" in result.file_extract
        assert "blocked" in result.file_extract  # b=1 should be flagged
        assert (
            "swap activity" in result.file_extract.lower()
            or "si=" in result.file_extract
        )  # si/so > 0
        assert "Anomalies" in result.file_extract

    def test_vmstat_healthy(self, extractor):
        """vmstat with no anomalies."""
        content = """\
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 0  0      0 500000  30000 300000    0    0    10    20  200  400 10  5 85  0  0
"""
        result = extractor.extract(content)
        assert "Virtual Memory" in result.file_extract
        assert "0 running" in result.file_extract
        assert "85% idle" in result.file_extract or "id" in result.file_extract

    # --- Existing commands ---

    def test_df_high_disk(self, extractor):
        """df command with full disk detected."""
        content = """\
Filesystem     1K-blocks      Used Available Use% Mounted on
/dev/sda1       51200000  46000000   5200000  90% /
/dev/sdb1      102400000  10000000  92400000  10% /data
"""
        result = extractor.extract(content)
        assert "Disk Usage" in result.file_extract
        assert "90%" in result.file_extract

    def test_unknown_command_fallback(self, extractor):
        """Unknown command output uses fallback."""
        content = "Some random command output that doesn't match any known format\nLine 2\nLine 3"
        result = extractor.extract(content)
        assert (
            "unknown format" in result.file_extract.lower()
            or "Command Output" in result.file_extract
        )

    # --- top parser: CPU/memory hog detection ---

    def test_top_detects_cpu_hog(self, extractor):
        """Regression: a CPU-heavy process must surface in the Resource Hogs
        section. A prior refactor renamed the internal process dict keys to
        ``cpu_percent``/``mem_percent`` but left every consumer reading
        ``cpu``/``mem``, so ``cpu_hogs`` was always empty and the summary
        quietly omitted the finding."""
        content = """\
top - 10:00:01 up 1 day, load average: 1.10, 1.20, 1.15
Tasks: 100 total
%Cpu(s):  5.2 us,  2.1 sy,  0.0 ni, 92.5 id
KiB Mem : 16384000 total,  8192000 free,  7000000 used

  PID USER      %CPU %MEM    VSZ   RSS COMMAND
 1234 alice     95.0  5.0  12345  5678 /usr/bin/hungry_worker
 5678 bob        1.0  2.0   2345  1234 sshd
"""
        result = extractor.extract(content)
        assert "Resource Hogs" in result.file_extract
        assert "hungry_worker" in result.file_extract
        assert "1234" in result.file_extract  # PID of the hog

    def test_top_detects_mem_hog(self, extractor):
        """Regression: a memory-heavy process must surface in Resource Hogs."""
        content = """\
top - 10:00:01 up 1 day, load average: 0.10, 0.20, 0.15
Tasks: 50 total
%Cpu(s):  1.2 us,  0.5 sy,  0.0 ni, 98.3 id
KiB Mem : 16384000 total,  1000000 free, 15000000 used

  PID USER      %CPU %MEM    VSZ   RSS COMMAND
 9999 carol      2.0 88.5 123456 78901 /usr/bin/bloated_proc
 1111 dave       0.5  1.0   2345  1234 bash
"""
        result = extractor.extract(content)
        assert "Resource Hogs" in result.file_extract
        assert "bloated_proc" in result.file_extract
        assert "9999" in result.file_extract

    def test_top_no_hogs_when_all_idle(self, extractor):
        """Healthy top output has no hogs and no Resource Hogs section."""
        content = """\
top - 10:00:01 up 1 day, load average: 0.05, 0.10, 0.08
Tasks: 50 total
%Cpu(s):  1.0 us,  0.5 sy,  0.0 ni, 98.5 id
KiB Mem : 16384000 total, 10000000 free,  6000000 used

  PID USER      %CPU %MEM    VSZ   RSS COMMAND
 1111 alice      2.0  3.0   2345  1234 sshd
 2222 bob        0.5  1.0   2345  1234 bash
"""
        result = extractor.extract(content)
        assert "Resource Hogs" not in result.file_extract

    def test_iostat_minimal_three_column_layout(self, extractor):
        """A minimal iostat layout with tps/await/util directly after the
        Device column. Prior code used ``if tps_idx:`` for column-index
        guards — truthy-checking an integer that is a legitimately-zero
        index. In the current iostat layouts Device is always parts[0],
        which made the bug latent, but the guard pattern is wrong: use
        ``is not None``. This test pins the contract that *whichever*
        numeric column is present is parsed and thresholded."""
        content = """\
Linux test

avg-cpu:  %user   %nice %system %iowait  %steal   %idle
           2.00    0.00    1.00    0.50    0.00   96.50

Device  tps  await  %util
sda     50.0  25.0   85.0
sdb     10.0   2.0    5.0
"""
        result = extractor.extract(content)
        assert "sda" in result.file_extract
        # Both anomalies must be reported (await > 20ms AND %util > 80%).
        assert "await=25.0ms" in result.file_extract
        assert "%util=85.0%" in result.file_extract
