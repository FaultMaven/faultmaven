"""
Command Output Parser for COMMAND_OUTPUT data type

Parses shell command output to extract system state, resource usage, and anomalies.
Supports: top, ps, iostat, netstat, df, free, vmstat, and other common commands.
No LLM calls required - pure tabular parsing and threshold-based analysis.
"""

import re
from typing import TYPE_CHECKING, Dict, List, Optional

from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    has_content,
)

if TYPE_CHECKING:
    from faultmaven.models.interfaces import ISanitizer, ITracer, IVectorStore


class CommandOutputExtractor:
    """Command output parsing for shell command results (0 LLM calls)"""

    # Resource thresholds for anomaly detection
    THRESHOLDS = {
        "cpu_high": 70.0,  # % CPU usage
        "mem_high": 80.0,  # % Memory usage
        "disk_high": 85.0,  # % Disk usage
    }

    @property
    def strategy_name(self) -> str:
        return "command_parsing"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> str:
        """
        Command output extraction

        Steps:
        1. Classify format (lsof, netstat, ps, etc)
        2. Apply specialized parser if available
        3. Fallback to generic tabular / unstructured patterns
        """
        content = content.lstrip("\ufeff")
        if len(content) > 50_000_000:
            return ExtractResult(
                file_extract="[File exceeds 50MB maximum size limit for extraction]"
            )

        if not has_content(content):
            return ExtractResult(file_extract=EMPTY_CONTENT_RESPONSE)

        total_lines = len(content.split("\n"))

        # Detect command type
        command_type = self._detect_command_type(content)

        parsers = {
            "top": self._parse_top,
            "ps": self._parse_ps,
            "iostat": self._parse_iostat,
            "netstat": self._parse_netstat,
            "df": self._parse_df,
            "free": self._parse_free,
            "vmstat": self._parse_vmstat,
        }

        parser = parsers.get(command_type, self._fallback_extraction)
        result = parser(content)

        return ExtractResult(
            file_extract=result,
            file_meta={
                "command": command_type,
                "lines": total_lines,
                "size_bytes": len(content.encode("utf-8", errors="replace")),
            },
        )

    def _detect_command_type(self, content: str) -> str:
        """Detect which command generated this output"""
        # Check first 200 chars for command signatures
        header = content[:200]

        if re.search(r"top\s+-|Tasks:|%Cpu\(s\)|KiB Mem", header, re.IGNORECASE):
            return "top"
        elif re.search(r"PID\s+USER\s+%CPU\s+%MEM\s+VSZ\s+RSS", header):
            return "ps"
        elif re.search(r"avg-cpu:|Device.*tps.*kB_read", header):
            return "iostat"
        elif re.search(
            r"Proto\s+Recv-Q\s+Send-Q.*Local Address.*Foreign Address", header
        ):
            return "netstat"
        elif re.search(r"Filesystem\s+1K-blocks\s+Used\s+Available\s+Use%", header):
            return "df"
        elif re.search(
            r"total\s+used\s+free\s+shared\s+buff/cache\s+available", header
        ):
            return "free"
        elif re.search(r"procs.*memory.*swap.*io.*system.*cpu", header):
            return "vmstat"

        return "unknown"

    def _parse_top(self, content: str) -> str:
        """Parse top command output"""
        lines = content.split("\n")

        # Extract system load
        load_avg = self._extract_load_average(lines)
        cpu_usage = self._extract_cpu_usage(lines)
        mem_usage = self._extract_memory_usage_top(lines)

        # Extract processes
        processes = self._extract_top_processes(lines)

        # Find resource hogs
        cpu_hogs = [
            p for p in processes if p.get("cpu", 0) > self.THRESHOLDS["cpu_high"]
        ]
        mem_hogs = [
            p for p in processes if p.get("mem", 0) > self.THRESHOLDS["mem_high"]
        ]

        # Generate summary
        return self._generate_top_summary(
            load_avg, cpu_usage, mem_usage, cpu_hogs, mem_hogs, processes
        )

    def _extract_load_average(self, lines: list[str]) -> str | None:
        """Extract load average from top output"""
        for line in lines[:5]:
            match = re.search(
                r"load average:\s*([\d\.]+,?\s*[\d\.]+,?\s*[\d\.]+)",
                line,
                re.IGNORECASE,
            )
            if match:
                return match.group(1)
        return None

    def _extract_cpu_usage(self, lines: list[str]) -> dict[str, float]:
        """Extract CPU usage breakdown"""
        for line in lines[:10]:
            # Match: %Cpu(s):  5.2 us,  2.1 sy,  0.0 ni, 92.5 id
            match = re.search(
                r"%Cpu\(s\):\s*([\d\.]+)\s*us,\s*([\d\.]+)\s*sy.*?([\d\.]+)\s*id",
                line,
                re.IGNORECASE,
            )
            if match:
                return {
                    "user": float(match.group(1)),
                    "system": float(match.group(2)),
                    "idle": float(match.group(3)),
                }
        return {}

    def _extract_memory_usage_top(self, lines: list[str]) -> dict[str, float]:
        """Extract memory usage from top"""
        for line in lines[:10]:
            # Match: KiB Mem : 16384000 total,  8192000 free,  7000000 used
            match = re.search(
                r"(?:KiB|MiB)\s+Mem\s*:\s*([\d\.]+)\s*total,\s*([\d\.]+)\s*free,\s*([\d\.]+)\s*used",
                line,
                re.IGNORECASE,
            )
            if match:
                total = float(match.group(1))
                free = float(match.group(2))
                used = float(match.group(3))
                return {
                    "total_kb": total,
                    "used_kb": used,
                    "free_kb": free,
                    "used_pct": (used / total * 100) if total > 0 else 0,
                }
        return {}

    def _extract_top_processes(self, lines: list[str]) -> list[dict]:
        """Extract process list from top or ps output"""
        processes = []

        # Find header line with flexible matching
        header_idx = None
        col_map = {}
        for i, line in enumerate(lines):
            header = line.upper()
            if (
                "PID" in header
                and ("%CPU" in header or "CPU" in header)
                and "COMMAND" in header
            ):
                header_idx = i
                # Dynamically map columns
                parts = line.split()
                for col_idx, col_name in enumerate(parts):
                    name = col_name.upper()
                    if name in ("PID",):
                        col_map["pid"] = col_idx
                    elif name in ("USER", "UID"):
                        col_map["user"] = col_idx
                    elif name in ("%CPU", "CPU"):
                        col_map["cpu"] = col_idx
                    elif name in ("%MEM", "MEM"):
                        col_map["mem"] = col_idx
                    elif name in ("COMMAND", "CMD"):
                        col_map["command"] = col_idx
                break

        if header_idx is None or "pid" not in col_map or "command" not in col_map:
            return []

        # Parse process lines. If COMMAND is the last column, use maxsplit so
        # the rest of the line (which may contain spaces) stays joined as a
        # single field; otherwise a plain whitespace split is sufficient.
        command_idx = col_map["command"]
        command_is_last = command_idx == max(col_map.values())

        for line in lines[header_idx + 1 :]:
            line = line.strip()
            if not line:
                continue

            parts = (
                line.split(maxsplit=command_idx) if command_is_last else line.split()
            )

            if len(parts) <= command_idx:
                continue

            pid_str = parts[col_map["pid"]]
            if not pid_str.isdigit():
                continue

            try:
                # NOTE: consumers (cpu_hogs/mem_hogs filters, summary formatters)
                # read keys `cpu` and `mem`. The refactor that introduced
                # `cpu_percent`/`mem_percent` silently broke hog detection —
                # every lookup fell through to a default of 0 and no hogs were
                # ever reported. Keep the short names to preserve the contract.
                processes.append(
                    {
                        "pid": int(pid_str),
                        "user": (
                            parts[col_map["user"]]
                            if "user" in col_map and col_map["user"] < len(parts)
                            else "unknown"
                        ),
                        "cpu": (
                            float(parts[col_map["cpu"]])
                            if "cpu" in col_map and col_map["cpu"] < len(parts)
                            else 0.0
                        ),
                        "mem": (
                            float(parts[col_map["mem"]])
                            if "mem" in col_map and col_map["mem"] < len(parts)
                            else 0.0
                        ),
                        "command": parts[command_idx],
                    }
                )
                if len(processes) >= 20:
                    break
            except ValueError:
                continue

        return processes

    def _generate_top_summary(
        self,
        load_avg: str | None,
        cpu_usage: dict,
        mem_usage: dict,
        cpu_hogs: list[dict],
        mem_hogs: list[dict],
        all_processes: list[dict],
    ) -> str:
        """Generate natural language summary for top output"""
        lines = ["System State Analysis (top command)", ""]

        # System load
        if load_avg:
            lines.append(f"📊 Load Average: {load_avg}")

        # CPU usage
        if cpu_usage:
            cpu_busy = 100 - cpu_usage.get("idle", 0)
            lines.append(
                f"⚡ CPU Usage: {cpu_busy:.1f}% busy ({cpu_usage.get('user', 0):.1f}% user, {cpu_usage.get('system', 0):.1f}% system)"
            )

        # Memory usage
        if mem_usage:
            used_pct = mem_usage.get("used_pct", 0)
            lines.append(
                f"💾 Memory Usage: {used_pct:.1f}% ({mem_usage.get('used_kb', 0) / 1024:.0f} MB / {mem_usage.get('total_kb', 0) / 1024:.0f} MB)"
            )

        lines.append("")

        # Resource hogs
        if cpu_hogs or mem_hogs:
            lines.append("🔥 Resource Hogs Detected:")

            if cpu_hogs:
                lines.append(
                    f"\n  CPU Hogs ({len(cpu_hogs)} processes > {self.THRESHOLDS['cpu_high']}%):"
                )
                for proc in sorted(cpu_hogs, key=lambda x: x["cpu"], reverse=True)[:5]:
                    lines.append(f"    - PID {proc['pid']}: {proc['command'][:50]}")
                    lines.append(
                        f"      CPU: {proc['cpu']:.1f}%, Mem: {proc['mem']:.1f}%, User: {proc['user']}"
                    )

            if mem_hogs:
                lines.append(
                    f"\n  Memory Hogs ({len(mem_hogs)} processes > {self.THRESHOLDS['mem_high']}%):"
                )
                for proc in sorted(mem_hogs, key=lambda x: x["mem"], reverse=True)[:5]:
                    lines.append(f"    - PID {proc['pid']}: {proc['command'][:50]}")
                    lines.append(
                        f"      Mem: {proc['mem']:.1f}%, CPU: {proc['cpu']:.1f}%, User: {proc['user']}"
                    )

        elif all_processes:
            # No hogs, show top 3 processes
            lines.append("Top Processes:")
            for i, proc in enumerate(all_processes[:3], 1):
                lines.append(f"{i}. {proc['command'][:50]} (PID {proc['pid']})")
                lines.append(f"   CPU: {proc['cpu']:.1f}%, Mem: {proc['mem']:.1f}%")

        return "\n".join(lines)

    def _parse_ps(self, content: str) -> str:
        """Parse ps command output"""
        lines = content.split("\n")
        processes = self._extract_top_processes(lines)  # Reuse top parser

        summary = [
            "Process List (ps command)",
            f"Total processes: {len(processes)}",
            "",
        ]

        if processes:
            summary.append("Top Processes by CPU:")
            for proc in sorted(processes, key=lambda x: x.get("cpu", 0), reverse=True)[
                :10
            ]:
                summary.append(
                    f"  - {proc['command'][:60]} ({proc.get('cpu', 0):.1f}% CPU)"
                )

        return "\n".join(summary)

    def _parse_iostat(self, content: str) -> str:
        """Parse iostat -x command output.

        Extracts avg-cpu summary and per-device I/O statistics.
        Flags devices with high utilization (>80%) or high await (>20ms).
        """
        lines = content.split("\n")

        summary = ["I/O Statistics (iostat command)", ""]

        # Parse avg-cpu section
        cpu_line = None
        for i, line in enumerate(lines):
            if "avg-cpu" in line:
                # Next non-empty line has the values
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].strip():
                        cpu_line = lines[j].strip()
                        break
                break

        if cpu_line:
            parts = cpu_line.split()
            if len(parts) >= 6:
                summary.append(
                    f"CPU: {parts[0]}% user, {parts[2]}% system, {parts[5]}% idle"
                )
                summary.append("")

        # Parse device stats table
        header_idx = None
        for i, line in enumerate(lines):
            if re.search(r"Device\b", line) and (
                "tps" in line or "r/s" in line or "w/s" in line or "util" in line
            ):
                header_idx = i
                break

        if header_idx is None:
            summary.append("No device statistics found")
            return "\n".join(summary)

        header_parts = lines[header_idx].split()
        # Find column indices for key metrics
        col_map = {}
        for idx, col in enumerate(header_parts):
            col_lower = col.lower().replace("%", "")
            if col_lower in ("tps",):
                col_map["tps"] = idx
            elif col_lower in ("await",):
                col_map["await"] = idx
            elif col_lower in ("util", "%util"):
                col_map["util"] = idx
            elif (
                "kb_read" in col_lower or "rkb/s" in col_lower or "r_await" in col_lower
            ):
                if "r_await" in col_lower:
                    col_map["r_await"] = idx
            elif "kb_wrtn" in col_lower or "wkb/s" in col_lower:
                pass

        devices = []
        anomalies = []
        for line in lines[header_idx + 1 :]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < len(header_parts):
                continue

            device_name = parts[0]
            device_info = {"name": device_name}

            # NOTE: `if idx:` is wrong — column 0 is a legitimate index and
            # was silently treated as "missing" here. Use ``is not None``.
            tps_idx = col_map.get("tps")
            if tps_idx is not None and tps_idx < len(parts):
                try:
                    device_info["tps"] = float(parts[tps_idx])
                except ValueError:
                    pass

            await_idx = col_map.get("await")
            if await_idx is not None and await_idx < len(parts):
                try:
                    device_info["await"] = float(parts[await_idx])
                    if device_info["await"] > 20:
                        anomalies.append(
                            f"{device_name}: await={device_info['await']:.1f}ms (>20ms)"
                        )
                except ValueError:
                    pass

            util_idx = col_map.get("util")
            if util_idx is not None and util_idx < len(parts):
                try:
                    device_info["util"] = float(parts[util_idx])
                    if device_info["util"] > 80:
                        anomalies.append(
                            f"{device_name}: %util={device_info['util']:.1f}% (>80%)"
                        )
                except ValueError:
                    pass

            devices.append(device_info)

        if devices:
            summary.append(f"Devices: {len(devices)}")
            for d in devices:
                parts_str = []
                if "tps" in d:
                    parts_str.append(f"tps={d['tps']:.1f}")
                if "await" in d:
                    parts_str.append(f"await={d['await']:.1f}ms")
                if "util" in d:
                    parts_str.append(f"%util={d['util']:.1f}%")
                summary.append(f"  - {d['name']}: {', '.join(parts_str)}")

        if anomalies:
            summary.append("")
            summary.append(f"Anomalies ({len(anomalies)}):")
            for a in anomalies:
                summary.append(f"  - {a}")

        return "\n".join(summary)

    def _parse_netstat(self, content: str) -> str:
        """Parse netstat command output"""
        lines = content.split("\n")

        # Count connections by state
        states = {}
        for line in lines:
            match = re.search(
                r"\b(ESTABLISHED|LISTEN|TIME_WAIT|CLOSE_WAIT|SYN_SENT)\b", line
            )
            if match:
                state = match.group(1)
                states[state] = states.get(state, 0) + 1

        summary = ["Network Connections (netstat command)", ""]

        if states:
            summary.append("Connection States:")
            for state, count in sorted(
                states.items(), key=lambda x: x[1], reverse=True
            ):
                summary.append(f"  - {state}: {count}")

        return "\n".join(summary)

    def _parse_df(self, content: str) -> str:
        """Parse df (disk free) command output"""
        lines = content.split("\n")

        # Parse filesystem entries
        filesystems = []
        for line in lines[1:]:  # Skip header
            match = re.match(r"(\S+)\s+\d+\s+\d+\s+\d+\s+(\d+)%\s+(.+)", line)
            if match:
                filesystems.append(
                    {
                        "filesystem": match.group(1),
                        "use_pct": int(match.group(2)),
                        "mount": match.group(3),
                    }
                )

        # Find full disks
        full_disks = [
            fs for fs in filesystems if fs["use_pct"] > self.THRESHOLDS["disk_high"]
        ]

        summary = ["Disk Usage (df command)", ""]

        if full_disks:
            summary.append(
                f"⚠️  {len(full_disks)} filesystem(s) over {self.THRESHOLDS['disk_high']}% capacity:"
            )
            for fs in sorted(full_disks, key=lambda x: x["use_pct"], reverse=True):
                summary.append(
                    f"  - {fs['mount']}: {fs['use_pct']}% full ({fs['filesystem']})"
                )
        else:
            summary.append("All filesystems under capacity limits")
            for fs in filesystems:
                summary.append(f"  - {fs['mount']}: {fs['use_pct']}%")

        return "\n".join(summary)

    def _parse_free(self, content: str) -> str:
        """Parse free (memory) command output"""
        lines = content.split("\n")

        for line in lines:
            if "Mem:" in line:
                parts = line.split()
                if len(parts) >= 4:
                    total = int(parts[1])
                    used = int(parts[2])
                    used_pct = (used / total * 100) if total > 0 else 0

                    return f"Memory Usage (free command)\n\nTotal: {total / 1024:.0f} MB\nUsed: {used / 1024:.0f} MB ({used_pct:.1f}%)"

        return "Memory Usage (free command)\n\n" + content[:200]

    def _parse_vmstat(self, content: str) -> str:
        """Parse vmstat command output.

        Extracts process, memory, swap, I/O, system, and CPU statistics.
        Flags swap activity (si/so > 0), high I/O wait (>20%), blocked procs.
        """
        lines = content.split("\n")

        summary = ["Virtual Memory Statistics (vmstat command)", ""]

        # Find the column header line (r, b, swpd, free, ...)
        header_idx = None
        for i, line in enumerate(lines):
            if re.search(r"\br\b.*\bb\b.*\bswpd\b", line):
                header_idx = i
                break

        if header_idx is None:
            summary.append("Unable to parse vmstat header")
            return "\n".join(summary)

        header_parts = lines[header_idx].split()
        col_map = {col: idx for idx, col in enumerate(header_parts)}

        # Parse data rows (may be multiple samples)
        data_rows = []
        for line in lines[header_idx + 1 :]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= len(header_parts):
                try:
                    row = {col: int(parts[idx]) for col, idx in col_map.items()}
                    data_rows.append(row)
                except (ValueError, IndexError):
                    continue

        if not data_rows:
            summary.append("No data rows found")
            return "\n".join(summary)

        # Use last row as current state
        latest = data_rows[-1]

        anomalies = []

        # Procs
        r_val = latest.get("r", 0)
        b_val = latest.get("b", 0)
        summary.append(f"Procs: {r_val} running, {b_val} blocked")
        if b_val > 0:
            anomalies.append(f"{b_val} blocked process(es)")

        # Memory
        swpd = latest.get("swpd", 0)
        free = latest.get("free", 0)
        buff = latest.get("buff", 0)
        cache = latest.get("cache", 0)
        summary.append(
            f"Memory: free={free}K, buff={buff}K, cache={cache}K, swpd={swpd}K"
        )

        # Swap activity
        si = latest.get("si", 0)
        so = latest.get("so", 0)
        if si > 0 or so > 0:
            summary.append(f"Swap: si={si}K/s, so={so}K/s")
            anomalies.append(f"swap activity detected (si={si}, so={so})")

        # CPU
        us = latest.get("us", 0)
        sy = latest.get("sy", 0)
        idle = latest.get("id", 0)
        wa = latest.get("wa", 0)
        summary.append(f"CPU: {us}% user, {sy}% system, {wa}% wait, {idle}% idle")
        if wa > 20:
            anomalies.append(f"high I/O wait: {wa}%")

        if anomalies:
            summary.append("")
            summary.append(f"Anomalies ({len(anomalies)}):")
            for a in anomalies:
                summary.append(f"  - {a}")

        return "\n".join(summary)

    def _fallback_extraction(self, content: str) -> str:
        """Fallback for unknown command output"""
        lines = content.split("\n")[:20]

        return "Command Output (unknown format)\n\n" + "\n".join(lines)
