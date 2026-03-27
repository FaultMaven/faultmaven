"""
Profiling Hotspot Extraction for PROFILING_DATA data type

Analyzes performance profiling output to identify CPU/memory hotspots and performance bottlenecks.
No LLM calls required - pure parsing and statistical analysis.
"""

import re
from typing import TYPE_CHECKING

from faultmaven.services.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    format_coverage_metadata,
    has_content,
)

if TYPE_CHECKING:
    pass


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

    def extract(self, content: str) -> str:
        """
        Profiling Hotspot Extraction algorithm:
        1. Detect profiling format (cProfile, flame graph, perf, etc.)
        2. Parse function call data
        3. Calculate cumulative time percentages
        4. Identify top hotspots (> 5% of total time)
        5. Detect recursive patterns
        6. Generate actionable summary
        """
        if not has_content(content):
            return EMPTY_CONTENT_RESPONSE

        # Detect format
        prof_format = self._detect_format(content)

        parsers = {
            self.FORMAT_CPROFILE: self._extract_cprofile,
            self.FORMAT_FLAME_GRAPH: self._extract_flame_graph,
            self.FORMAT_PERF: self._extract_perf,
        }

        parser = parsers.get(prof_format, self._fallback_extraction)
        result = parser(content)

        # Coverage metadata
        result += format_coverage_metadata(
            Format=prof_format,
        )
        return result

    def _detect_format(self, content: str) -> str:
        """Detect profiling data format"""
        # Check for cProfile header
        if re.search(r"\bncalls\s+tottime\s+percall\s+cumtime", content, re.IGNORECASE):
            return self.FORMAT_CPROFILE

        # Check for flame graph format (stack notation)
        if re.search(r"[\w\.]+(?:;[\w\.]+)+\s+\d+", content):
            return self.FORMAT_FLAME_GRAPH

        # Check for perf format (perf stat or perf report)
        if re.search(r"Performance counter stats", content, re.IGNORECASE):
            return self.FORMAT_PERF
        if re.search(r"Overhead\s+Command\s+Shared Object\s+Symbol", content):
            return self.FORMAT_PERF

        return self.FORMAT_UNKNOWN

    def _extract_cprofile(self, content: str) -> str:
        """Extract insights from Python cProfile output"""
        lines = content.split("\n")

        # Find the header line
        header_idx = None
        for i, line in enumerate(lines):
            if "ncalls" in line and "cumtime" in line:
                header_idx = i
                break

        if header_idx is None:
            return self._fallback_extraction(content)

        # Parse function entries
        functions = []
        data_lines = lines[
            header_idx + 1 : header_idx + 101
        ]  # Parse up to 100 functions

        for line in data_lines:
            if not line.strip():
                continue

            # cProfile format: ncalls  tottime  percall  cumtime  percall filename:lineno(function)
            match = re.match(
                r"\s*(\d+(?:/\d+)?)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+(.+)",
                line,
            )

            if match:
                ncalls, tottime, _, cumtime, _, location = match.groups()
                functions.append(
                    {
                        "ncalls": ncalls,
                        "tottime": float(tottime),
                        "cumtime": float(cumtime),
                        "location": location.strip(),
                    }
                )

        if not functions:
            return self._fallback_extraction(content)

        # Calculate total time
        total_time = max(fn["cumtime"] for fn in functions) if functions else 0

        # Find hotspots (> 5% of total time)
        hotspots = (
            [fn for fn in functions if (fn["cumtime"] / total_time) > 0.05]
            if total_time > 0
            else []
        )

        # Sort by cumulative time
        hotspots.sort(key=lambda x: x["cumtime"], reverse=True)

        # Generate summary
        return self._generate_cprofile_summary(functions, hotspots, total_time)

    def _generate_cprofile_summary(
        self, functions: list[dict], hotspots: list[dict], total_time: float
    ) -> str:
        """Generate natural language summary for cProfile data"""
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
            top_hotspot = hotspots[0]
            top_pct = (
                (top_hotspot["cumtime"] / total_time) * 100 if total_time > 0 else 0
            )

            if top_pct > 40:
                lines.append(
                    f"  - Focus on optimizing {self._simplify_function_name(top_hotspot['location'])}"
                )
                lines.append(
                    f"    This single function accounts for {top_pct:.1f}% of execution time"
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

    def _extract_flame_graph(self, content: str) -> str:
        """Extract insights from flame graph format"""
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
            "Profiling Analysis (Flame Graph format)",
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

        return "\n".join(lines)

    def _extract_perf(self, content: str) -> str:
        """Extract insights from perf stat and perf report output.

        perf stat: Parses counter lines, calculates IPC.
        perf report: Parses overhead table for top functions.
        """
        if "Performance counter stats" in content:
            return self._extract_perf_stat(content)
        elif re.search(r"Overhead\s+Command\s+Shared Object\s+Symbol", content):
            return self._extract_perf_report(content)
        else:
            # Fallback to basic extraction
            lines = content.split("\n")
            summary = ["Profiling Analysis (perf format)", "", "Performance Counters:"]
            for line in lines:
                if (
                    "cycles" in line
                    or "instructions" in line
                    or "seconds time elapsed" in line
                ):
                    summary.append(f"  - {line.strip()}")
            return "\n".join(summary)

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

    def _extract_perf_report(self, content: str) -> str:
        """Parse perf report output for top functions by overhead."""
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
            return "\n".join(summary)

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
            return "\n".join(summary)

        summary.append(f"Top Functions by Overhead ({len(functions)} total):")
        for i, fn in enumerate(functions[:10], 1):
            summary.append(f"  {i}. {fn['symbol']} ({fn['overhead']:.1f}%)")
            summary.append(f"     {fn['shared_object']} [{fn['command']}]")

        return "\n".join(summary)

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

    def _fallback_extraction(self, content: str) -> str:
        """Fallback for unknown profiling formats"""
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

        return "\n".join(summary)
