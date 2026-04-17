"""
Tests for ProfilingDataExtractor.

Covers:
- R6.3: Enhanced perf parser (perf stat with IPC, perf report)
- Existing cProfile and flame graph support
"""

import pytest

from faultmaven.modules.preprocessing.extractors.profiling_extractor import (
    ProfilingDataExtractor,
)


class TestProfilingExtractor:
    @pytest.fixture
    def extractor(self):
        return ProfilingDataExtractor()

    def test_properties(self, extractor):
        assert extractor.strategy_name == "profiling_hotspot"
        assert extractor.llm_calls_used == 0

    # --- R6.3: perf stat with IPC ---

    def test_perf_stat_ipc(self, extractor):
        """perf stat output with IPC calculation."""
        content = """\
 Performance counter stats for './myapp':

     3,000,000,000      cycles
     2,100,000,000      instructions
        50,000,000      cache-references
         5,000,000      cache-misses

       1.500000000 seconds time elapsed
"""
        result = extractor.extract(content)
        assert "perf stat" in result
        assert "IPC" in result
        # IPC = 2.1B / 3B = 0.7
        assert "0.70" in result
        # Low IPC flagged
        assert "low IPC" in result or "memory stalls" in result
        # Cache miss rate = 5M/50M = 10%
        assert "Cache miss" in result or "cache" in result.lower()

    def test_perf_stat_good_ipc(self, extractor):
        """perf stat with good IPC (>1.0) — no anomaly."""
        content = """\
 Performance counter stats for './efficient':

     1,000,000,000      cycles
     2,500,000,000      instructions

       0.500000000 seconds time elapsed
"""
        result = extractor.extract(content)
        assert "IPC" in result
        # IPC = 2.5 — good
        assert "2.50" in result

    # --- R6.3: perf report ---

    def test_perf_report_top_functions(self, extractor):
        """perf report output with function overhead."""
        content = """\
# Overhead  Command      Shared Object        Symbol
# ........  .......  .................  ..............
    30.25%  myapp    libcrypto.so       [.] EVP_EncryptUpdate
    15.10%  myapp    myapp              [.] process_request
     8.50%  myapp    libc.so.6          [.] memcpy
     5.00%  myapp    myapp              [.] parse_json
"""
        result = extractor.extract(content)
        assert "perf report" in result
        assert "EVP_EncryptUpdate" in result
        assert "process_request" in result
        assert "30.2" in result or "30.25" in result

    # --- Existing: cProfile ---

    def test_cprofile_hotspot(self, extractor):
        """cProfile output with hotspot detection."""
        content = """\
         200 function calls in 10.000 seconds

   Ordered by: cumulative time

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    5.000    5.000   10.000   10.000 main.py:1(main)
       50    3.000    0.060    4.500    0.090 db.py:10(query)
       50    2.000    0.040    2.000    0.040 io.py:5(read_file)
       99    0.100    0.001    0.100    0.001 {built-in method builtins.len}
"""
        result = extractor.extract(content)
        assert "cProfile" in result
        assert "Hotspots" in result or "hotspot" in result.lower()
        assert "main" in result or "query" in result

    # --- Existing: flame graph ---

    def test_flame_graph(self, extractor):
        """Flame graph format parsed."""
        content = """\
main;process;db_query 500
main;process;compute 200
main;init 50
main;cleanup 30
"""
        result = extractor.extract(content)
        assert "Flame Graph" in result
        assert "db_query" in result or "process" in result
