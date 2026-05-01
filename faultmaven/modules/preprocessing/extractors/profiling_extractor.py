"""
Profiling Hotspot Extraction for PROFILING_DATA data type

Analyzes performance profiling output to identify CPU/memory hotspots and performance bottlenecks.
No LLM calls required - pure parsing and statistical analysis.
"""

import re

from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    has_content,
)


class ProfilingDataExtractor:
    """Profiling hotspot extraction for performance profiling data (0 LLM calls)"""

    # Profiling data formats
    FORMAT_CPROFILE = "cProfile"
    FORMAT_FLAME_GRAPH = "flame_graph"
    FORMAT_PERF = "perf"
    FORMAT_UNKNOWN = "unknown"

    @property
    def strategy_name(self) -> str:
        return "profiling_hotspot"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> ExtractResult:
        """
        Profiling Hotspot Extraction algorithm:
        1. Detect profiling format (cProfile, flame graph, perf, etc.)
        2. Parse function call data
        3. Calculate cumulative time percentages
        4. Identify top hotspots (> 5% of total time)
        5. Detect recursive patterns
        6. Generate actionable summary
        """
        content = content.lstrip("\ufeff")
        if len(content) > 50_000_000:
            return ExtractResult(
                file_extract="[File exceeds 50MB maximum size limit for extraction]"
            )

        if not has_content(content):
            return ExtractResult(file_extract=EMPTY_CONTENT_RESPONSE)

        # Detect format
        prof_format = self._detect_format(content)

        parsers = {
            self.FORMAT_CPROFILE: self._extract_cprofile,
            self.FORMAT_FLAME_GRAPH: self._extract_flame_graph,
            self.FORMAT_PERF: self._extract_perf,
        }

        parser = parsers.get(prof_format, self._fallback_extraction)
        result, metadata = parser(content)

        file_meta: dict = {
            "format": prof_format,
            "size_bytes": len(content.encode("utf-8", errors="replace")),
        }
        if metadata.get("functions_profiled") is not None:
            file_meta["functions_profiled"] = metadata["functions_profiled"]
        if metadata.get("top_function") is not None:
            file_meta["top_function"] = metadata["top_function"]
        return ExtractResult(file_extract=result, file_meta=file_meta)

    def _detect_format(self, content: str) -> str:
        """Detect profiling data format"""
        # Check for cProfile header
        if re.search(r"\bncalls\s+tottime\s+percall\s+cumtime", content, re.IGNORECASE):
            return self.FORMAT_CPROFILE

        # Check for flame graph format (stack notation)
        if re.search(r"[\w\.]+(?:;[\w\.]+)+\s+\d+", content):
            return self.FORMAT_FLAME_GRAPH

        # Check for perf format (perf stat or perf report)
        if re.search(
            r"Performance counter stats|\bcycles\b|\binstructions\b",
            content,
            re.IGNORECASE,
        ):
            return self.FORMAT_PERF
        if re.search(r"Overhead\s+Command\s+Shared Object\s+Symbol", content):
            return self.FORMAT_PERF

        return self.FORMAT_UNKNOWN

    def _extract_cprofile(self, content: str) -> tuple[str, dict]:
        """Extract insights from Python cProfile output.

        cProfile output may contain multiple sorted views over the same
        function table (e.g. ``pstats.print_stats('cumulative')`` followed
        by ``pstats.print_stats('time')``). Each view replays the same
        functions, so naive row counting double-counts. This parser
        deduplicates rows by qualname (``filename:lineno(function)``) and,
        for duplicates, keeps the row with the larger cumtime/tottime —
        the cumulative-view row is authoritative for cumtime, the
        internal-view row for tottime, but they should match.

        Returns:
            (summary, metadata) where metadata carries
            ``functions_profiled`` (distinct functions, not row count) and
            ``top_function`` (highest-cumtime non-wrapper function).
        """
        lines = content.split("\n")

        # Find each header line — there can be multiple (one per view).
        header_indices = [
            i for i, line in enumerate(lines) if "ncalls" in line and "cumtime" in line
        ]

        if not header_indices:
            return self._fallback_extraction(content)

        # Parse function entries from every view, deduplicating by location.
        functions: dict[str, dict] = {}
        for header_idx in header_indices:
            # Parse up to 100 rows per view.
            data_lines = lines[header_idx + 1 : header_idx + 101]
            for line in data_lines:
                if not line.strip():
                    continue

                # Stop at the next view's header (defensive — slice should already
                # exclude it, but views can be closer together than 100 rows).
                if "ncalls" in line and "cumtime" in line:
                    break

                # cProfile format: ncalls  tottime  percall  cumtime  percall filename:lineno(function)
                match = re.match(
                    r"\s*(\d+(?:/\d+)?)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+(.+)",
                    line,
                )

                if not match:
                    continue

                ncalls, tottime, _, cumtime, _, location = match.groups()
                location = location.strip()
                tottime_f = float(tottime)
                cumtime_f = float(cumtime)

                existing = functions.get(location)
                if existing is None:
                    functions[location] = {
                        "ncalls": ncalls,
                        "tottime": tottime_f,
                        "cumtime": cumtime_f,
                        "location": location,
                    }
                else:
                    # Merge: prefer the larger cumtime/tottime across views.
                    if cumtime_f > existing["cumtime"]:
                        existing["cumtime"] = cumtime_f
                    if tottime_f > existing["tottime"]:
                        existing["tottime"] = tottime_f

        if not functions:
            return self._fallback_extraction(content)

        functions_list = list(functions.values())

        # Calculate total time
        total_time = max(fn["cumtime"] for fn in functions_list)

        # Find hotspots (> 5% of total time), excluding wrappers.
        if total_time > 0:
            hotspot_candidates = [
                fn
                for fn in functions_list
                if (fn["cumtime"] / total_time) > 0.05
                and not self._is_wrapper(fn, total_time)
            ]
        else:
            hotspot_candidates = []

        # Sort by cumulative time
        hotspot_candidates.sort(key=lambda x: x["cumtime"], reverse=True)

        summary = self._generate_cprofile_summary(
            functions_list, hotspot_candidates, total_time
        )
        metadata: dict = {"functions_profiled": len(functions_list)}
        if hotspot_candidates:
            metadata["top_function"] = self._simplify_function_name(
                hotspot_candidates[0]["location"]
            )
        else:
            # No real hotspots after wrapper-filtering. Fall back to the
            # first parsed entry so coverage metadata still has a value.
            metadata["top_function"] = self._simplify_function_name(
                functions_list[0]["location"]
            )
        return summary, metadata

    def _is_wrapper(self, fn: dict, total_time: float) -> bool:
        """Return True if a function is a structural wrapper, not a hotspot.

        Wrappers are functions whose runtime is essentially all spent in
        callees rather than in their own body. Two cases:

        1. ``<module>`` — the script entry point. By definition wraps the
           entire run, always shows ~100% cumtime. Never actionable.
        2. ``cumtime_pct >= 99%`` AND ``self_time_pct <= 5%`` — pure
           wrapper (e.g. a ``main()`` function that does nothing but call
           the real workhorse).
        """
        location = fn.get("location", "")
        if "<module>" in location:
            return True
        if total_time <= 0:
            return False
        cumtime_pct = (fn["cumtime"] / total_time) * 100
        self_time_pct = (fn["tottime"] / total_time) * 100
        return cumtime_pct >= 99.0 and self_time_pct <= 5.0

    def _is_builtin_location(self, location: str) -> bool:
        """Return True if the location refers to a built-in / C extension.

        cProfile renders these as ``{built-in method ...}`` or
        ``{method '...' of '...' objects}`` — they have no Python source
        to optimize directly, so optimization suggestions should target
        the Python caller instead.
        """
        return location.startswith("{") and location.endswith("}")

    def _select_optimization_target(self, hotspots: list[dict]) -> dict | None:
        """Pick the function to recommend optimizing.

        The recommendation should target the highest-tottime (self-time)
        leaf function, since that's where wall time is actually spent.
        If the leaf is a built-in/C extension, fall back to the highest-
        tottime non-builtin function — i.e. the Python caller — because
        that's the code path the user can realistically modify.

        Returns the chosen function dict, or ``None`` if no hotspots.
        """
        if not hotspots:
            return None
        by_self = sorted(hotspots, key=lambda f: f["tottime"], reverse=True)
        leaf = by_self[0]
        if not self._is_builtin_location(leaf["location"]):
            return leaf
        # Leaf is a built-in. Fall back to the highest-tottime non-builtin.
        for fn in by_self[1:]:
            if not self._is_builtin_location(fn["location"]):
                # Mark so the summary can frame the recommendation around
                # the C extension that this Python function calls into.
                fn = dict(fn)
                fn["_calls_builtin"] = leaf["location"]
                return fn
        # Everything is a builtin — return the leaf as a last resort.
        return leaf

    def _generate_cprofile_summary(
        self, functions: list[dict], hotspots: list[dict], total_time: float
    ) -> str:
        """Generate natural language summary for cProfile data.

        ``hotspots`` is already wrapper-filtered by ``_extract_cprofile``.
        """
        lines = [
            "Profiling Analysis (cProfile format)",
            f"- Total functions analyzed: {len(functions)}",
            f"- Total execution time: {total_time:.2f}s",
            f"- Performance hotspots identified: {len(hotspots)}",
            "",
        ]

        if hotspots:
            lines.append("🔥 Top Performance Hotspots:")
            for i, fn in enumerate(hotspots[:5], 1):  # Top 5
                pct = (fn["cumtime"] / total_time) * 100 if total_time > 0 else 0
                lines.append(
                    f"{i}. {self._simplify_function_name(fn['location'])} "
                    f"({fn['cumtime']:.2f}s, {pct:.1f}% of total)"
                )
                lines.append(f"   - Called {fn['ncalls']} times")
                lines.append(f"   - Self time: {fn['tottime']:.2f}s")

                # Add recommendation
                if pct > 30:
                    lines.append(
                        f"   ⚠️  CRITICAL: This function consumes {pct:.1f}% of execution time"
                    )
                elif pct > 15:
                    lines.append("   ⚡ Significant optimization opportunity")

                lines.append("")

            # Add optimization suggestions
            lines.append("💡 Optimization Suggestions:")
            target = self._select_optimization_target(hotspots)
            if target is not None:
                target_name = self._simplify_function_name(target["location"])
                self_pct = (
                    (target["tottime"] / total_time) * 100 if total_time > 0 else 0
                )
                calls_builtin = target.get("_calls_builtin")
                if calls_builtin:
                    builtin_simple = self._simplify_function_name(calls_builtin)
                    lines.append(
                        f"  - Optimize the code path in {target_name} that calls "
                        f"{builtin_simple}"
                    )
                    lines.append(
                        f"    The C extension {builtin_simple} dominates self-time, "
                        f"but is reached through {target_name} "
                        f"({target['tottime']:.2f}s self, {self_pct:.1f}% of total)"
                    )
                else:
                    lines.append(f"  - Focus on optimizing {target_name}")
                    lines.append(
                        f"    Highest self-time function "
                        f"({target['tottime']:.2f}s, {self_pct:.1f}% of total)"
                    )

            # Check for I/O operations
            io_functions = [
                fn for fn in hotspots if self._is_io_function(fn["location"])
            ]
            if io_functions:
                lines.append(
                    "  - Consider async I/O or caching for file/network operations"
                )

            # Check for recursive calls
            recursive = [fn for fn in functions if "/" in str(fn["ncalls"])]
            if recursive:
                lines.append(
                    f"  - {len(recursive)} recursive functions detected - consider memoization"
                )

        return "\n".join(lines)

    def _extract_flame_graph(self, content: str) -> tuple[str, dict]:
        """Extract insights from flame graph format.

        Returns:
            (summary, metadata). ``functions_profiled`` counts unique leaf
            frames (what CPU was executing when sampled — the R3-useful
            signal for "is function X present in this profile"). ``top_function``
            is the leaf of the highest-sampled stack.
        """
        lines = content.split("\n")

        # Parse flame graph entries: function;call;stack 123
        stacks = []
        for line in lines:
            match = re.match(r"([\w\.;]+)\s+(\d+)", line.strip())
            if match:
                stack_path, samples = match.groups()
                stacks.append({"stack": stack_path, "samples": int(samples)})

        if not stacks:
            return self._fallback_extraction(content)

        # Calculate total samples
        total_samples = sum(s["samples"] for s in stacks)

        # Find hotspots
        stacks.sort(key=lambda x: x["samples"], reverse=True)
        hotspots = stacks[:10]  # Top 10

        # Generate summary
        lines = [
            f"Profiling Analysis (Flame Graph format)",
            f"- Total stacks analyzed: {len(stacks)}",
            f"- Total samples: {total_samples}",
            "",
            "🔥 Top Call Stacks:",
        ]

        for i, stack in enumerate(hotspots, 1):
            pct = (stack["samples"] / total_samples) * 100 if total_samples > 0 else 0
            call_chain = " → ".join(stack["stack"].split(";")[-3:])  # Last 3 in chain
            lines.append(f"{i}. {call_chain}")
            lines.append(f"   - {stack['samples']} samples ({pct:.1f}% of total)")

        # Metadata: unique leaf frames across all stacks
        unique_leaves = {s["stack"].split(";")[-1] for s in stacks}
        metadata = {
            "functions_profiled": len(unique_leaves),
            "top_function": stacks[0]["stack"].split(";")[-1],
        }
        return "\n".join(lines), metadata

    def _extract_perf(self, content: str) -> tuple[str, dict]:
        """Extract insights from perf stat and perf report output.

        perf stat: Parses counter lines, calculates IPC. Not function-level,
          so the returned metadata is empty — callers relying on
          ``functions_profiled`` / ``top_function`` will simply not see
          those fields in coverage metadata.
        perf report: Parses overhead table for top functions; metadata
          carries ``functions_profiled`` + ``top_function``.
        """
        if re.search(r"Overhead\s+Command\s+Shared Object\s+Symbol", content):
            return self._extract_perf_report(content)
        elif (
            "Performance counter stats" in content
            or "cycles" in content
            or "instructions" in content
        ):
            return self._extract_perf_stat(content), {}
        else:
            # Fallback to basic extraction — no function-level data
            lines = content.split("\n")
            summary = ["Profiling Analysis (perf format)", "", "Performance Counters:"]
            for line in lines:
                if (
                    "cycles" in line
                    or "instructions" in line
                    or "seconds time elapsed" in line
                ):
                    summary.append(f"  - {line.strip()}")
            return "\n".join(summary), {}

    def _extract_perf_stat(self, content: str) -> str:
        """Parse perf stat output with IPC calculation."""
        lines = content.split("\n")
        summary = ["Profiling Analysis (perf stat)", ""]

        counters = {}
        anomalies = []

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("Performance"):
                continue

            # Match counter lines like: "1,234,567      cycles" or "1234567 instructions"
            match = re.match(r"([\d,\.]+)\s+(\S+.*?)(?:\s+#.*)?$", line)
            if match:
                raw_value = match.group(1).replace(",", "")
                counter_name = match.group(2).strip()
                try:
                    value = float(raw_value)
                    counters[counter_name] = value
                    summary.append(f"  {counter_name}: {int(value):,}")
                except ValueError:
                    pass

            # Also capture "seconds time elapsed"
            elapsed_match = re.search(r"([\d\.]+)\s+seconds time elapsed", line)
            if elapsed_match:
                elapsed = float(elapsed_match.group(1))
                counters["elapsed"] = elapsed
                summary.append(f"  Time elapsed: {elapsed:.3f}s")

        # Calculate IPC
        cycles = counters.get("cycles", 0)
        instructions = counters.get("instructions", 0)
        if cycles > 0 and instructions > 0:
            ipc = instructions / cycles
            summary.append("")
            summary.append(f"IPC (Instructions Per Cycle): {ipc:.2f}")
            if ipc < 1.0:
                anomalies.append(f"low IPC ({ipc:.2f}) — possible memory stalls")

        # Check cache miss rate
        cache_refs = counters.get("cache-references", 0)
        cache_misses = counters.get("cache-misses", 0)
        if cache_refs > 0 and cache_misses > 0:
            miss_rate = (cache_misses / cache_refs) * 100
            summary.append(f"Cache miss rate: {miss_rate:.1f}%")
            if miss_rate > 10:
                anomalies.append(f"high cache-miss rate ({miss_rate:.1f}%)")

        if anomalies:
            summary.append("")
            summary.append("Anomalies:")
            for a in anomalies:
                summary.append(f"  - {a}")

        return "\n".join(summary)

    def _extract_perf_report(self, content: str) -> tuple[str, dict]:
        """Parse perf report output for top functions by overhead.

        Returns:
            (summary, metadata). Metadata carries ``functions_profiled``
            (symbol count) and ``top_function`` (highest-overhead symbol).
        """
        lines = content.split("\n")
        summary = ["Profiling Analysis (perf report)", ""]

        # Find header line
        header_idx = None
        for i, line in enumerate(lines):
            if re.search(r"Overhead\s+Command\s+Shared Object\s+Symbol", line):
                header_idx = i
                break

        if header_idx is None:
            summary.append("Unable to parse perf report header")
            return "\n".join(summary), {}

        # Parse function entries
        functions = []
        for line in lines[header_idx + 1 :]:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # Match: "12.34%  command  libfoo.so  [.] function_name"
            match = re.match(r"([\d\.]+)%\s+(\S+)\s+(\S+)\s+\[.\]\s+(.+)", line)
            if match:
                functions.append(
                    {
                        "overhead": float(match.group(1)),
                        "command": match.group(2),
                        "shared_object": match.group(3),
                        "symbol": match.group(4).strip(),
                    }
                )

        if not functions:
            summary.append("No function entries found")
            return "\n".join(summary), {}

        summary.append(f"Top Functions by Overhead ({len(functions)} total):")
        for i, fn in enumerate(functions[:10], 1):
            summary.append(f"  {i}. {fn['symbol']} ({fn['overhead']:.1f}%)")
            summary.append(f"     {fn['shared_object']} [{fn['command']}]")

        metadata = {
            "functions_profiled": len(functions),
            "top_function": functions[0]["symbol"],
        }
        return "\n".join(summary), metadata

    def _simplify_function_name(self, location: str) -> str:
        """Simplify function location for readability"""
        # Extract function name from "filename:lineno(function)"
        match = re.search(r"\(([^)]+)\)", location)
        if match:
            func_name = match.group(1)
            # Also try to get filename
            file_match = re.match(r"([^:]+):", location)
            if file_match:
                filename = file_match.group(1).split("/")[-1]  # Get last part of path
                return f"{filename}::{func_name}"
            return func_name

        return location

    def _is_io_function(self, location: str) -> bool:
        """Check if function is I/O related"""
        io_keywords = ["read", "write", "file", "socket", "request", "fetch", "query"]
        return any(keyword in location.lower() for keyword in io_keywords)

    def _fallback_extraction(self, content: str) -> tuple[str, dict]:
        """Fallback for unknown profiling formats. No function-level data,
        so metadata is empty."""
        lines = content.split("\n")[:30]  # First 30 lines

        summary = [
            "Profiling Data (partial extraction - unknown format)",
            "",
            "Content preview:",
        ]

        for line in lines:
            if line.strip():
                summary.append(f"  {line}")

        summary.append(
            "\nNote: Unable to fully parse profiling data. Supported formats: cProfile, flame graphs, perf."
        )

        return "\n".join(summary), {}
