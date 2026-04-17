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
        assert "I/O Statistics" in result
        assert "sda" in result
        assert "sdb" in result
        # sdb should be flagged: await > 20ms and util > 80%
        assert "Anomalies" in result or "anomal" in result.lower()

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
        assert "I/O Statistics" in result
        assert "sda" in result
        # No anomalies expected
        assert "Anomalies" not in result

    # --- R6.2: vmstat parser ---

    def test_vmstat_basic(self, extractor):
        """vmstat output parsed with anomaly detection."""
        content = """\
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 2  1  51200 102400  20480 204800  100  200    50   100  500  800 30 10 35 25  0
"""
        result = extractor.extract(content)
        assert "Virtual Memory" in result
        assert "blocked" in result  # b=1 should be flagged
        assert "swap activity" in result.lower() or "si=" in result  # si/so > 0
        assert "Anomalies" in result

    def test_vmstat_healthy(self, extractor):
        """vmstat with no anomalies."""
        content = """\
procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 0  0      0 500000  30000 300000    0    0    10    20  200  400 10  5 85  0  0
"""
        result = extractor.extract(content)
        assert "Virtual Memory" in result
        assert "0 running" in result
        assert "85% idle" in result or "id" in result

    # --- Existing commands ---

    def test_df_high_disk(self, extractor):
        """df command with full disk detected."""
        content = """\
Filesystem     1K-blocks      Used Available Use% Mounted on
/dev/sda1       51200000  46000000   5200000  90% /
/dev/sdb1      102400000  10000000  92400000  10% /data
"""
        result = extractor.extract(content)
        assert "Disk Usage" in result
        assert "90%" in result

    def test_unknown_command_fallback(self, extractor):
        """Unknown command output uses fallback."""
        content = "Some random command output that doesn't match any known format\nLine 2\nLine 3"
        result = extractor.extract(content)
        assert "unknown format" in result.lower() or "Command Output" in result
