"""
Profiling Hotspot Extraction for PROFILING_DATA data type

Analyzes performance profiling output to identify CPU/memory hotspots and performance bottlenecks.
No LLM calls required - pure parsing and statistical analysis.
"""

import re
from typing import List, Dict, Optional, Tuple


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
        # Detect format
        prof_format = self._detect_format(content)

        if prof_format == self.FORMAT_CPROFILE:
            return self._extract_cprofile(content)
        elif prof_format == self.FORMAT_FLAME_GRAPH:
            return self._extract_flame_graph(content)
        elif prof_format == self.FORMAT_PERF:
            return self._extract_perf(content)
        else:
            return self._fallback_extraction(content)

    def _detect_format(self, content: str) -> str:
        """Detect profiling data format"""
        # Check for cProfile header
        if re.search(r'\bncalls\s+tottime\s+percall\s+cumtime', content, re.IGNORECASE):
            return self.FORMAT_CPROFILE

        # Check for flame graph format (stack notation)
        if re.search(r'[\w\.]+(?:;[\w\.]+)+\s+\d+', content):
            return self.FORMAT_FLAME_GRAPH

        # Check for perf format
        if re.search(r'Performance counter stats', content, re.IGNORECASE):
            return self.FORMAT_PERF

        return self.FORMAT_UNKNOWN

    def _extract_cprofile(self, content: str) -> str:
        """Extract insights from Python cProfile output"""
        lines = content.split('\n')

        # Find the header line
        header_idx = None
        for i, line in enumerate(lines):
            if 'ncalls' in line and 'cumtime' in line:
                header_idx = i
                break

        if header_idx is None:
            return self._fallback_extraction(content)

        # Parse function entries
        functions = []
        data_lines = lines[header_idx + 1:header_idx + 101]  # Parse up to 100 functions

        for line in data_lines:
            if not line.strip():
                continue

            # cProfile format: ncalls  tottime  percall  cumtime  percall filename:lineno(function)
            match = re.match(
                r'\s*(\d+(?:/\d+)?)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+(.+)',
                line
            )

            if match:
                ncalls, tottime, _, cumtime, _, location = match.groups()
                functions.append({
                    'ncalls': ncalls,
                    'tottime': float(tottime),
                    'cumtime': float(cumtime),
                    'location': location.strip()
                })

        if not functions:
            return self._fallback_extraction(content)

        # Calculate total time
        total_time = max(fn['cumtime'] for fn in functions) if functions else 0

        # Find hotspots (> 5% of total time)
        hotspots = [
            fn for fn in functions
            if (fn['cumtime'] / total_time) > 0.05
        ] if total_time > 0 else []

        # Sort by cumulative time
        hotspots.sort(key=lambda x: x['cumtime'], reverse=True)

        # Generate summary
        return self._generate_cprofile_summary(functions, hotspots, total_time)

    def _generate_cprofile_summary(
        self,
        functions: List[Dict],
        hotspots: List[Dict],
        total_time: float
    ) -> str:
        """Generate natural language summary for cProfile data"""
        lines = [
            f"Profiling Analysis (cProfile format)",
            f"- Total functions analyzed: {len(functions)}",
            f"- Total execution time: {total_time:.2f}s",
            f"- Performance hotspots identified: {len(hotspots)}",
            ""
        ]

        if hotspots:
            lines.append("🔥 Top Performance Hotspots:")
            for i, fn in enumerate(hotspots[:5], 1):  # Top 5
                pct = (fn['cumtime'] / total_time) * 100 if total_time > 0 else 0
                lines.append(
                    f"{i}. {self._simplify_function_name(fn['location'])} "
                    f"({fn['cumtime']:.2f}s, {pct:.1f}% of total)"
                )
                lines.append(f"   - Called {fn['ncalls']} times")
                lines.append(f"   - Self time: {fn['tottime']:.2f}s")

                # Add recommendation
                if pct > 30:
                    lines.append(f"   ⚠️  CRITICAL: This function consumes {pct:.1f}% of execution time")
                elif pct > 15:
                    lines.append(f"   ⚡ Significant optimization opportunity")

                lines.append("")

            # Add optimization suggestions
            lines.append("💡 Optimization Suggestions:")
            top_hotspot = hotspots[0]
            top_pct = (top_hotspot['cumtime'] / total_time) * 100 if total_time > 0 else 0

            if top_pct > 40:
                lines.append(f"  - Focus on optimizing {self._simplify_function_name(top_hotspot['location'])}")
                lines.append(f"    This single function accounts for {top_pct:.1f}% of execution time")

            # Check for I/O operations
            io_functions = [fn for fn in hotspots if self._is_io_function(fn['location'])]
            if io_functions:
                lines.append("  - Consider async I/O or caching for file/network operations")

            # Check for recursive calls
            recursive = [fn for fn in functions if '/' in str(fn['ncalls'])]
            if recursive:
                lines.append(f"  - {len(recursive)} recursive functions detected - consider memoization")

        return "\n".join(lines)

    def _extract_flame_graph(self, content: str) -> str:
        """Extract insights from flame graph format"""
        lines = content.split('\n')

        # Parse flame graph entries: function;call;stack 123
        stacks = []
        for line in lines:
            match = re.match(r'([\w\.;]+)\s+(\d+)', line.strip())
            if match:
                stack_path, samples = match.groups()
                stacks.append({
                    'stack': stack_path,
                    'samples': int(samples)
                })

        if not stacks:
            return self._fallback_extraction(content)

        # Calculate total samples
        total_samples = sum(s['samples'] for s in stacks)

        # Find hotspots
        stacks.sort(key=lambda x: x['samples'], reverse=True)
        hotspots = stacks[:10]  # Top 10

        # Generate summary
        lines = [
            f"Profiling Analysis (Flame Graph format)",
            f"- Total stacks analyzed: {len(stacks)}",
            f"- Total samples: {total_samples}",
            "",
            "🔥 Top Call Stacks:"
        ]

        for i, stack in enumerate(hotspots, 1):
            pct = (stack['samples'] / total_samples) * 100 if total_samples > 0 else 0
            call_chain = " → ".join(stack['stack'].split(';')[-3:])  # Last 3 in chain
            lines.append(f"{i}. {call_chain}")
            lines.append(f"   - {stack['samples']} samples ({pct:.1f}% of total)")

        return "\n".join(lines)

    def _extract_perf(self, content: str) -> str:
        """Extract insights from perf stat output"""
        lines = content.split('\n')

        summary = [
            "Profiling Analysis (perf format)",
            "",
            "Performance Counters:"
        ]

        # Extract key metrics
        for line in lines:
            # Look for metric lines
            if 'cycles' in line or 'instructions' in line or 'seconds time elapsed' in line:
                summary.append(f"  - {line.strip()}")

        return "\n".join(summary)

    def _simplify_function_name(self, location: str) -> str:
        """Simplify function location for readability"""
        # Extract function name from "filename:lineno(function)"
        match = re.search(r'\(([^)]+)\)', location)
        if match:
            func_name = match.group(1)
            # Also try to get filename
            file_match = re.match(r'([^:]+):', location)
            if file_match:
                filename = file_match.group(1).split('/')[-1]  # Get last part of path
                return f"{filename}::{func_name}"
            return func_name

        return location

    def _is_io_function(self, location: str) -> bool:
        """Check if function is I/O related"""
        io_keywords = ['read', 'write', 'file', 'socket', 'request', 'fetch', 'query']
        return any(keyword in location.lower() for keyword in io_keywords)

    def _fallback_extraction(self, content: str) -> str:
        """Fallback for unknown profiling formats"""
        lines = content.split('\n')[:30]  # First 30 lines

        summary = [
            "Profiling Data (partial extraction - unknown format)",
            "",
            "Content preview:"
        ]

        for line in lines:
            if line.strip():
                summary.append(f"  {line}")

        summary.append("\nNote: Unable to fully parse profiling data. Supported formats: cProfile, flame graphs, perf.")

        return "\n".join(summary)
