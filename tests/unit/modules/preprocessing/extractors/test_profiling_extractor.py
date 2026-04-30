"""
Tests for ProfilingDataExtractor.

Covers:
- R6.3: Enhanced perf parser (perf stat with IPC, perf report)
- Existing cProfile and flame graph support
- ISS-035: dual-view dedupe, wrapper filtering, leaf-aware optimization target
"""

from pathlib import Path

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
        assert "perf stat" in result.file_extract
        assert "IPC" in result.file_extract
        # IPC = 2.1B / 3B = 0.7
        assert "0.70" in result.file_extract
        # Low IPC flagged
        assert (
            "low IPC" in result.file_extract or "memory stalls" in result.file_extract
        )
        # Cache miss rate = 5M/50M = 10%
        assert (
            "Cache miss" in result.file_extract
            or "cache" in result.file_extract.lower()
        )

    def test_perf_stat_good_ipc(self, extractor):
        """perf stat with good IPC (>1.0) — no anomaly."""
        content = """\
 Performance counter stats for './efficient':

     1,000,000,000      cycles
     2,500,000,000      instructions

       0.500000000 seconds time elapsed
"""
        result = extractor.extract(content)
        assert "IPC" in result.file_extract
        # IPC = 2.5 — good
        assert "2.50" in result.file_extract

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
        assert "perf report" in result.file_extract
        assert "EVP_EncryptUpdate" in result.file_extract
        assert "process_request" in result.file_extract
        assert "30.2" in result.file_extract or "30.25" in result.file_extract

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
        assert "cProfile" in result.file_extract
        assert (
            "Hotspots" in result.file_extract
            or "hotspot" in result.file_extract.lower()
        )
        assert "main" in result.file_extract or "query" in result.file_extract

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
        assert "Flame Graph" in result.file_extract
        assert "db_query" in result.file_extract or "process" in result.file_extract

    # --- ISS-035: dual-view dedupe + wrapper filter + leaf-aware optimizer ---

    def test_iss035_dual_view_dedupe(self, extractor):
        """Two sorted views over the same 5 functions must yield 5 unique
        rows in metadata + hotspot list, not 10."""
        content = """\
         100 function calls in 5.000 seconds

   Ordered by: cumulative time

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.100    0.100    5.000    5.000 app.py:1(top_func)
        1    0.500    0.500    4.500    4.500 app.py:5(middle_func)
       50    2.000    0.040    3.500    0.070 app.py:10(worker)
       50    1.500    0.030    1.500    0.030 app.py:20(compute)
      100    0.500    0.005    0.500    0.005 util.py:3(helper)


         100 function calls in 5.000 seconds

   Ordered by: internal time

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
       50    2.000    0.040    3.500    0.070 app.py:10(worker)
       50    1.500    0.030    1.500    0.030 app.py:20(compute)
        1    0.500    0.500    4.500    4.500 app.py:5(middle_func)
      100    0.500    0.005    0.500    0.005 util.py:3(helper)
        1    0.100    0.100    5.000    5.000 app.py:1(top_func)
"""
        result = extractor.extract(content)

        # Five distinct functions, not ten.
        assert result.file_meta["functions_profiled"] == 5

        # Hotspot list should mention each function name at most once.
        # Restrict the check to the hotspots section (the optimization
        # suggestion legitimately repeats the top function name).
        hotspots_section = result.file_extract.split("Top Performance Hotspots", 1)[
            1
        ].split("Optimization Suggestions", 1)[0]
        hotspot_names = ["top_func", "middle_func", "worker", "compute", "helper"]
        for name in hotspot_names:
            assert hotspots_section.count(f"::{name}") <= 1, (
                f"Function {name} appears more than once in hotspots — "
                "dual-view dedupe failed"
            )

    def test_iss035_module_excluded_from_hotspots(self, extractor):
        """`<module>` (script entry) shows ~100% cumtime by definition. It
        must NOT be in the top hotspot ranking; the real workhorse must."""
        content = """\
         500 function calls in 10.000 seconds

   Ordered by: cumulative time

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.000    0.000   10.000   10.000 app.py:1(<module>)
        1    8.000    8.000    9.500    9.500 app.py:5(real_hotspot)
       10    0.500    0.050    0.500    0.050 util.py:3(helper)
"""
        result = extractor.extract(content)

        # `<module>` should not be referenced in the hotspot section.
        assert "<module>" not in result.file_extract
        # Top function metadata should not be `<module>` either.
        assert "<module>" not in (result.file_meta.get("top_function") or "")
        # The real hotspot IS in the ranking.
        assert "real_hotspot" in result.file_extract

    def test_iss035_optimization_excludes_module_names_real_function(self, extractor):
        """Optimization suggestion must not point at `<module>` and must
        name the real hotspot (highest tottime non-wrapper)."""
        content = """\
         500 function calls in 10.000 seconds

   Ordered by: cumulative time

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.000    0.000   10.000   10.000 app.py:1(<module>)
        1    8.000    8.000    9.500    9.500 app.py:5(real_hotspot)
       10    0.500    0.050    0.500    0.050 util.py:3(helper)
"""
        result = extractor.extract(content)

        # Locate the optimization-suggestions block and assert on its content.
        assert "Optimization Suggestions" in result.file_extract
        suggestions = result.file_extract.split("Optimization Suggestions", 1)[1]

        assert (
            "<module>" not in suggestions
        ), "Optimization suggestion should not name the script entry point"
        assert (
            "real_hotspot" in suggestions
        ), "Optimization suggestion should name the real high-tottime function"

    def test_iss035_pure_wrapper_filtered(self, extractor):
        """A function that is not `<module>` but spends ~all its time in
        callees (>=99% cumtime, <=5% tottime) is also a wrapper and must
        be filtered out of hotspot ranking."""
        content = """\
         500 function calls in 10.000 seconds

   Ordered by: cumulative time

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.001    0.001    9.990    9.990 app.py:5(thin_wrapper)
       50    8.000    0.160    8.500    0.170 app.py:10(real_work)
"""
        result = extractor.extract(content)

        assert "thin_wrapper" not in result.file_extract
        assert "real_work" in result.file_extract

    def test_iss035_optimization_falls_back_for_builtin_leaf(self, extractor):
        """When highest-tottime function is a built-in/C extension, the
        optimization suggestion should target the highest-tottime Python
        caller and frame the recommendation around that code path."""
        content = """\
         500 function calls in 10.000 seconds

   Ordered by: cumulative time

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
       50    1.000    0.020    8.000    0.160 app.py:5(call_native)
      500    5.000    0.010    5.000    0.010 {built-in method native.compute}
       10    0.500    0.050    0.500    0.050 util.py:3(helper)
"""
        result = extractor.extract(content)

        assert "Optimization Suggestions" in result.file_extract
        suggestions = result.file_extract.split("Optimization Suggestions", 1)[1]

        # The Python caller, not the C-extension, should be the recommendation
        # target.
        assert "call_native" in suggestions
        # Recommendation should frame the work as optimizing the calling
        # code path, not literally optimizing the built-in.
        assert "code path" in suggestions or "calls" in suggestions

    def test_iss035_real_fixture_regression(self, extractor):
        """Regression test on the real cprofile-embedding-pipeline fixture.

        Skips gracefully if the fixture is not present (it lives in a
        sibling repo, not in this one)."""
        fixture = Path(
            "/home/swhouse/product/fm-data-exam/test-data/synthetic/"
            "cprofile-embedding-pipeline.txt"
        )
        if not fixture.exists():
            pytest.skip(f"Fixture not present: {fixture}")

        content = fixture.read_text()
        result = extractor.extract(content)

        # 23 distinct functions, not 46.
        assert result.file_meta["functions_profiled"] == 23

        # Top-3 hotspots must not contain `<module>`. Find the section
        # between the hotspot header and the optimization suggestions.
        hotspots_section = result.file_extract.split("Top Performance Hotspots", 1)[
            1
        ].split("Optimization Suggestions", 1)[0]
        # Pull out lines numbered 1./2./3.
        top3 = "\n".join(
            line
            for line in hotspots_section.splitlines()
            if line.lstrip().startswith(("1.", "2.", "3."))
        )
        assert "<module>" not in top3

        # Optimization suggestion should point at the embedding path
        # (scaled_dot_product_attention is the legitimate hotspot).
        assert "Optimization Suggestions" in result.file_extract
        suggestions = result.file_extract.split("Optimization Suggestions", 1)[1]
        assert "<module>" not in suggestions
        assert "scaled_dot_product_attention" in suggestions
