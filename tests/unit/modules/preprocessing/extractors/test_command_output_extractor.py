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


class TestTopTaskVsProcessSplit:
    """ISS-029: ``top`` reports two distinct counts that must not be conflated.

    The header line ``Tasks: N total, ...`` is the system-wide task count.
    The process table below the column header (``PID  USER  ...``) lists a
    *subset* of processes (top-by-CPU, defaults to roughly screen-height).

    Earlier behaviour surfaced neither value distinctly: ``file_meta`` had no
    task summary, and the natural-language summary mentioned neither count,
    so the agent had nothing to disambiguate from and answered "318
    processes" for the table size.

    Both values must be exposed as separate ``file_meta`` fields and both
    must appear in the formatted summary with labels that make their
    distinct meanings obvious to the agent reading FILE SUMMARY."""

    @pytest.fixture
    def extractor(self):
        return CommandOutputExtractor()

    @pytest.fixture
    def synthetic_top(self):
        """50 system-wide tasks but only 8 rows in the process table."""
        return """\
top - 12:00:00 up 5 days,  load average: 1.10, 1.20, 1.15
Tasks:  50 total,   5 running,  45 sleeping,   0 stopped,   0 zombie
%Cpu(s):  10.0 us,  2.0 sy,  0.0 ni, 88.0 id
KiB Mem : 16384000 total,  8000000 free,  8384000 used

  PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
 1001 alice     20   0  100000  50000  10000 S   5.0   0.3   0:01.00 proc-a
 1002 alice     20   0  200000  60000  11000 S   4.0   0.4   0:02.00 proc-b
 1003 bob       20   0  300000  70000  12000 S   3.0   0.4   0:03.00 proc-c
 1004 bob       20   0  400000  80000  13000 S   2.0   0.5   0:04.00 proc-d
 1005 carol     20   0  500000  90000  14000 S   1.0   0.5   0:05.00 proc-e
 1006 carol     20   0  600000 100000  15000 S   0.5   0.6   0:06.00 proc-f
 1007 dave      20   0  700000 110000  16000 S   0.3   0.6   0:07.00 proc-g
 1008 dave      20   0  800000 120000  17000 S   0.1   0.7   0:08.00 proc-h
"""

    def test_task_summary_total_in_meta(self, extractor, synthetic_top):
        """Header ``Tasks: 50 total`` is exposed as a distinct meta field."""
        result = extractor.extract(synthetic_top)
        assert result.file_meta.get("task_summary_total") == 50

    def test_process_table_rows_in_meta(self, extractor, synthetic_top):
        """The 8 displayed process rows are counted distinctly from the
        system task total."""
        result = extractor.extract(synthetic_top)
        assert result.file_meta.get("process_table_rows") == 8

    def test_summary_distinguishes_both_counts(self, extractor, synthetic_top):
        """The formatted FILE SUMMARY must mention both 50 and 8 with
        labels that make each one's meaning unambiguous. We don't pin exact
        wording, but the labels must clearly distinguish "system-wide tasks
        from the header" from "rows displayed in the process table"."""
        result = extractor.extract(synthetic_top)
        summary = result.file_extract

        # Both raw numbers must be present.
        assert "50" in summary
        assert "8" in summary

        # The header-derived count must be labelled as task summary / total
        # tasks, not as the process table size.
        assert "Task summary" in summary or "total tasks" in summary.lower()

        # The process table size must be labelled distinctly so the agent
        # cannot read "50 processes shown".
        assert (
            "Process table rows displayed" in summary
            or "process table rows" in summary.lower()
            or "rows displayed" in summary.lower()
        )

    def test_task_breakdown_preserved(self, extractor, synthetic_top):
        """Running/sleeping/stopped/zombie breakdown stays available."""
        result = extractor.extract(synthetic_top)
        breakdown = result.file_meta.get("task_summary_breakdown")
        assert breakdown is not None
        assert breakdown.get("running") == 5
        assert breakdown.get("sleeping") == 45
        assert breakdown.get("stopped") == 0
        assert breakdown.get("zombie") == 0

    def test_real_fixture_counts_are_distinct(self, extractor):
        """End-to-end check on the production-shaped fixture: 318 system
        tasks vs 17 rows displayed. This is the case that originally
        triggered ISS-029."""
        content = """\
top - 14:37:22 up 12 days,  3:41,  2 users,  load average: 6.84, 7.12, 6.93
Tasks: 318 total,   4 running, 314 sleeping,   0 stopped,   0 zombie
%Cpu(s): 82.3 us,  4.1 sy,  0.0 ni,  9.6 id,  3.7 wa,  0.0 hi,  0.3 si,  0.0 st
MiB Mem :  64113.2 total,   1247.8 free,  58342.1 used,   4523.3 buff/cache
MiB Swap:   8192.0 total,   6041.4 free,   2150.6 used.   2184.7 avail Mem

    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
  23471 appuser   20   0   42.1g  31.2g   1.4g R  391.1  49.8  48:17.33 python3
  23502 appuser   20   0   21.4g  12.8g 812116 R  185.7  20.5  22:04.11 python3
  23389 appuser   20   0    8.2g   7.1g  58244 S   62.3  11.3   8:53.44 uvicorn
  23401 appuser   20   0    8.1g   6.9g  57912 S   58.9  11.1   8:49.21 uvicorn
   1284 chromadb  20   0    4.8g   3.6g 114228 S   31.2   5.8   3:22.17 chromadb
  23410 appuser   20   0    2.3g   1.9g  41020 S   18.4   3.0   2:14.83 uvicorn
  23415 appuser   20   0    2.3g   1.8g  40188 S   17.9   2.9   2:11.09 uvicorn
   8821 redis     20   0  188424  78348   2384 S    4.2   0.1   0:47.92 redis-server
    512 root      20   0       0      0      0 S    1.1   0.0   0:12.44 kworker/u16:2
   9134 postgres  20   0  874512 312408  11244 S    0.9   0.5   0:09.71 postgres
  23388 appuser   20   0  315204  44812   9808 S    0.6   0.1   0:05.33 celery
    423 root      20   0  725112  18420   5532 S    0.2   0.0   0:01.84 containerd-shim
      1 root      20   0  167528  11300   8212 S    0.1   0.0   0:03.11 systemd
   9081 postgres  20   0  876048 148900  11200 S    0.0   0.2   0:01.04 postgres
  23440 appuser   20   0  128344  23016   7448 S    0.0   0.0   0:00.38 celery
   9142 postgres  20   0  874644  47588  11244 S    0.0   0.1   0:00.22 postgres
    217 root       0 -20       0      0      0 I    0.0   0.0   0:00.00 kworker/0:1H
"""
        result = extractor.extract(content)
        assert result.file_meta.get("task_summary_total") == 318
        assert result.file_meta.get("process_table_rows") == 17
