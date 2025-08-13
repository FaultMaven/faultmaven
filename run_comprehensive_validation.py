#!/usr/bin/env python3
"""
Comprehensive Validation Runner

This script executes and validates the complete 4-phase test rebuild:
- Phase 1: Logging Integration (38 tests)
- Phase 2: Service Layer (25+ tests) 
- Phase 3: API Layer (30+ tests)
- Phase 4: Infrastructure Integration (35+ tests)

It provides:
- Performance benchmarking across all phases
- Memory usage validation
- Integration testing
- Reliability metrics
- Comprehensive reporting
"""

import asyncio
import time
import subprocess
import sys
import json
import psutil
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
import argparse


@dataclass
class PhaseMetrics:
    """Metrics for a single test phase."""
    name: str
    test_count: int
    execution_time: float
    memory_usage_mb: float
    success_rate: float
    failures: List[str]
    improvements: Dict[str, Any]


@dataclass
class ComprehensiveValidationReport:
    """Complete validation report across all phases."""
    validation_timestamp: str
    overall_status: str
    total_test_count: int
    total_execution_time: float
    total_memory_usage: float
    overall_success_rate: float
    phase_metrics: List[PhaseMetrics]
    performance_improvements: Dict[str, Any]
    integration_validation: Dict[str, Any]
    recommendations: List[str]


class ComprehensiveValidator:
    """
    Executes and validates the complete 4-phase test architecture rebuild.
    """
    
    def __init__(self, args):
        self.args = args
        self.base_path = Path(__file__).parent
        self.test_path = self.base_path / "tests"
        self.results = {}
        self.start_time = time.time()
        self.process = psutil.Process()
        self.baseline_memory = self.process.memory_info().rss / 1024 / 1024
    
    async def validate_all_phases(self) -> ComprehensiveValidationReport:
        """Execute validation across all 4 phases."""
        print("🚀 Starting Comprehensive 4-Phase Test Validation")
        print("=" * 60)
        
        # Define phases and their test locations
        phases = [
            {
                "name": "logging_integration",
                "path": "tests/integration/logging_rebuilt",
                "description": "Phase 1: Real log coordination and content verification",
                "expected_improvements": {"execution_time": 0.8, "memory_usage": 0.7}
            },
            {
                "name": "service_layer", 
                "path": "tests/services",
                "pattern": "*_rebuilt.py",
                "description": "Phase 2: Real business logic and cross-service workflows",
                "expected_improvements": {"execution_time": 0.8, "memory_usage": 0.6}
            },
            {
                "name": "api_layer",
                "path": "tests/api", 
                "pattern": "*_rebuilt.py",
                "description": "Phase 3: Real HTTP endpoint behavior and request/response validation",
                "expected_improvements": {"execution_time": 0.8, "memory_usage": 0.5}
            },
            {
                "name": "infrastructure",
                "path": "tests/infrastructure",
                "pattern": "*_rebuilt.py", 
                "description": "Phase 4: Real infrastructure behavior and performance characteristics",
                "expected_improvements": {"execution_time": 0.8, "memory_usage": 0.9}
            }
        ]
        
        phase_results = []
        
        for phase in phases:
            print(f"\n📊 Validating {phase['description']}")
            print("-" * 50)
            
            phase_metrics = await self.validate_phase(phase)
            phase_results.append(phase_metrics)
            
            # Print immediate results
            self.print_phase_summary(phase_metrics)
        
        # Execute comprehensive integration tests
        print(f"\n🔗 Running Cross-Phase Integration Tests")
        print("-" * 50)
        integration_results = await self.validate_integration()
        
        # Generate final report
        report = self.generate_comprehensive_report(phase_results, integration_results)
        
        return report
    
    async def validate_phase(self, phase_config: Dict[str, Any]) -> PhaseMetrics:
        """Validate a single phase and measure improvements."""
        phase_name = phase_config["name"]
        test_path = self.base_path / phase_config["path"]
        
        # Determine test pattern
        if "pattern" in phase_config:
            test_pattern = f"{test_path}/{phase_config['pattern']}"
        else:
            test_pattern = str(test_path)
        
        # Execute tests with performance tracking
        start_time = time.time()
        start_memory = self.process.memory_info().rss / 1024 / 1024
        
        try:
            # Run pytest with coverage and performance tracking
            cmd = [
                sys.executable, "-m", "pytest",
                test_pattern,
                "-v",
                "--tb=short",
                "--disable-warnings" if not self.args.verbose else "",
                f"--maxfail={self.args.maxfail}",
            ]
            
            # Add coverage if requested
            if self.args.coverage:
                cmd.extend([
                    f"--cov=faultmaven",
                    f"--cov-report=term-missing"
                ])
            
            # Filter empty strings
            cmd = [c for c in cmd if c]
            
            print(f"Executing: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self.base_path),
                timeout=300  # 5 minute timeout per phase
            )
            
            end_time = time.time()
            end_memory = self.process.memory_info().rss / 1024 / 1024
            
            # Parse pytest output
            test_count, success_rate, failures = self.parse_pytest_output(result.stdout, result.stderr)
            
            return PhaseMetrics(
                name=phase_name,
                test_count=test_count,
                execution_time=end_time - start_time,
                memory_usage_mb=end_memory - start_memory,
                success_rate=success_rate,
                failures=failures,
                improvements=self.calculate_improvements(
                    phase_config.get("expected_improvements", {}),
                    end_time - start_time,
                    end_memory - start_memory
                )
            )
            
        except subprocess.TimeoutExpired:
            return PhaseMetrics(
                name=phase_name,
                test_count=0,
                execution_time=300.0,
                memory_usage_mb=0.0,
                success_rate=0.0,
                failures=["Test execution timeout"],
                improvements={}
            )
        except Exception as e:
            return PhaseMetrics(
                name=phase_name,
                test_count=0,
                execution_time=0.0,
                memory_usage_mb=0.0,
                success_rate=0.0,
                failures=[f"Execution error: {str(e)}"],
                improvements={}
            )
    
    def parse_pytest_output(self, stdout: str, stderr: str) -> tuple[int, float, List[str]]:
        """Parse pytest output to extract metrics."""
        lines = stdout.split('\n') + stderr.split('\n')
        
        test_count = 0
        passed = 0
        failures = []
        
        for line in lines:
            # Look for test result summary
            if "passed" in line and "failed" in line:
                # Parse line like "10 passed, 2 failed in 5.2s"
                parts = line.split()
                for i, part in enumerate(parts):
                    if part.isdigit():
                        if i + 1 < len(parts):
                            if parts[i + 1] == "passed":
                                passed = int(part)
                            elif parts[i + 1] == "failed":
                                test_count = passed + int(part)
                                break
            elif "passed in" in line and "failed" not in line:
                # Parse line like "25 passed in 3.2s"
                parts = line.split()
                for i, part in enumerate(parts):
                    if part.isdigit() and i + 1 < len(parts) and parts[i + 1] == "passed":
                        passed = test_count = int(part)
                        break
            elif "FAILED" in line:
                failures.append(line.strip())
        
        success_rate = (passed / test_count) if test_count > 0 else 0.0
        return test_count, success_rate, failures
    
    def calculate_improvements(self, expected: Dict[str, float], actual_time: float, actual_memory: float) -> Dict[str, Any]:
        """Calculate performance improvements vs expected baselines."""
        # These are estimated baselines from legacy over-mocked tests
        legacy_baselines = {
            "execution_time": 40.0,  # Legacy tests took >40 seconds total
            "memory_usage": 500.0    # Legacy tests used >500MB memory
        }
        
        improvements = {}
        
        if actual_time > 0:
            time_improvement = max(0, (legacy_baselines["execution_time"] - actual_time) / legacy_baselines["execution_time"])
            improvements["execution_time"] = {
                "actual_seconds": actual_time,
                "baseline_seconds": legacy_baselines["execution_time"],
                "improvement_percentage": time_improvement * 100,
                "meets_80_percent_target": time_improvement >= 0.8
            }
        
        if actual_memory > 0:
            memory_improvement = max(0, (legacy_baselines["memory_usage"] - actual_memory) / legacy_baselines["memory_usage"]) 
            improvements["memory_usage"] = {
                "actual_mb": actual_memory,
                "baseline_mb": legacy_baselines["memory_usage"],
                "improvement_percentage": memory_improvement * 100,
                "meets_efficiency_target": actual_memory < 100
            }
        
        return improvements
    
    async def validate_integration(self) -> Dict[str, Any]:
        """Run comprehensive cross-phase integration tests."""
        start_time = time.time()
        
        try:
            # Run the comprehensive validation test
            cmd = [
                sys.executable, "-m", "pytest",
                "tests/test_comprehensive_validation.py",
                "-v", 
                "--tb=short",
                f"--maxfail={self.args.maxfail}"
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self.base_path),
                timeout=300
            )
            
            end_time = time.time()
            
            # Parse results
            test_count, success_rate, failures = self.parse_pytest_output(result.stdout, result.stderr)
            
            return {
                "test_count": test_count,
                "execution_time": end_time - start_time,
                "success_rate": success_rate,
                "failures": failures,
                "cross_phase_coordination": success_rate > 0.9,
                "end_to_end_workflows": test_count > 0,
                "performance_targets_met": (end_time - start_time) < 10.0
            }
            
        except Exception as e:
            return {
                "test_count": 0,
                "execution_time": 0.0,
                "success_rate": 0.0,
                "failures": [f"Integration test error: {str(e)}"],
                "cross_phase_coordination": False,
                "end_to_end_workflows": False,
                "performance_targets_met": False
            }
    
    def generate_comprehensive_report(self, phase_results: List[PhaseMetrics], integration_results: Dict[str, Any]) -> ComprehensiveValidationReport:
        """Generate comprehensive validation report."""
        total_test_count = sum(p.test_count for p in phase_results) + integration_results.get("test_count", 0)
        total_execution_time = sum(p.execution_time for p in phase_results) + integration_results.get("execution_time", 0.0)
        total_memory_usage = sum(p.memory_usage_mb for p in phase_results)
        
        # Calculate overall success rate
        total_passed = sum(p.test_count * p.success_rate for p in phase_results)
        total_passed += integration_results.get("test_count", 0) * integration_results.get("success_rate", 0.0)
        overall_success_rate = total_passed / total_test_count if total_test_count > 0 else 0.0
        
        # Determine overall status
        status = "PASS"
        if overall_success_rate < 0.98:
            status = "FAIL"
        elif total_execution_time > 10.0:
            status = "PERFORMANCE_WARNING"
        elif total_memory_usage > 100.0:
            status = "MEMORY_WARNING"
        
        # Calculate performance improvements
        performance_improvements = {
            "execution_time": {
                "target": "80%+ improvement",
                "actual": f"{((40.0 - total_execution_time) / 40.0 * 100):.1f}% improvement",
                "meets_target": total_execution_time < 8.0  # 80% of 40 seconds
            },
            "memory_usage": {
                "target": "<100MB total",
                "actual": f"{total_memory_usage:.1f}MB",
                "meets_target": total_memory_usage < 100.0
            },
            "test_reliability": {
                "target": ">98% pass rate", 
                "actual": f"{overall_success_rate * 100:.1f}% pass rate",
                "meets_target": overall_success_rate > 0.98
            }
        }
        
        # Generate recommendations
        recommendations = []
        if overall_success_rate < 0.98:
            recommendations.append("Investigate test failures to improve reliability")
        if total_execution_time > 10.0:
            recommendations.append("Optimize slow tests to meet performance targets")
        if total_memory_usage > 100.0:
            recommendations.append("Reduce memory usage in test infrastructure")
        if not integration_results.get("cross_phase_coordination", False):
            recommendations.append("Improve cross-phase integration testing")
        
        if not recommendations:
            recommendations.append("All targets met - maintain current architecture")
        
        return ComprehensiveValidationReport(
            validation_timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            overall_status=status,
            total_test_count=total_test_count,
            total_execution_time=total_execution_time,
            total_memory_usage=total_memory_usage,
            overall_success_rate=overall_success_rate,
            phase_metrics=phase_results,
            performance_improvements=performance_improvements,
            integration_validation=integration_results,
            recommendations=recommendations
        )
    
    def print_phase_summary(self, metrics: PhaseMetrics):
        """Print summary for a single phase."""
        status = "✅ PASS" if metrics.success_rate > 0.98 else "❌ FAIL"
        
        print(f"  {status} {metrics.name}")
        print(f"    Tests: {metrics.test_count}")
        print(f"    Time: {metrics.execution_time:.2f}s")
        print(f"    Memory: {metrics.memory_usage_mb:.1f}MB")
        print(f"    Success Rate: {metrics.success_rate * 100:.1f}%")
        
        if metrics.improvements:
            exec_improvement = metrics.improvements.get("execution_time", {})
            if exec_improvement:
                improvement_pct = exec_improvement.get("improvement_percentage", 0)
                target_met = "✅" if exec_improvement.get("meets_80_percent_target", False) else "⚠️"
                print(f"    Performance: {improvement_pct:.1f}% improvement {target_met}")
        
        if metrics.failures:
            print(f"    Failures: {len(metrics.failures)}")
            if self.args.verbose:
                for failure in metrics.failures[:3]:  # Show first 3 failures
                    print(f"      - {failure}")
    
    def print_comprehensive_report(self, report: ComprehensiveValidationReport):
        """Print the comprehensive validation report."""
        print("\n" + "=" * 60)
        print("🎯 COMPREHENSIVE VALIDATION REPORT")
        print("=" * 60)
        
        # Overall Status
        status_emoji = {
            "PASS": "✅",
            "FAIL": "❌", 
            "PERFORMANCE_WARNING": "⚠️",
            "MEMORY_WARNING": "⚠️"
        }
        
        print(f"\nOverall Status: {status_emoji.get(report.overall_status, '?')} {report.overall_status}")
        print(f"Validation Time: {report.validation_timestamp}")
        print(f"Total Tests: {report.total_test_count}")
        print(f"Success Rate: {report.overall_success_rate * 100:.1f}%")
        print(f"Total Time: {report.total_execution_time:.2f}s")
        print(f"Total Memory: {report.total_memory_usage:.1f}MB")
        
        # Performance Improvements
        print(f"\n📈 Performance Improvements:")
        for metric, data in report.performance_improvements.items():
            target_met = "✅" if data["meets_target"] else "❌"
            print(f"  {target_met} {metric}: {data['actual']} (Target: {data['target']})")
        
        # Phase Summary
        print(f"\n📊 Phase Summary:")
        for phase in report.phase_metrics:
            status = "✅" if phase.success_rate > 0.98 else "❌"
            print(f"  {status} {phase.name}: {phase.test_count} tests, {phase.execution_time:.1f}s, {phase.success_rate * 100:.1f}%")
        
        # Integration Results
        integration = report.integration_validation
        integration_status = "✅" if integration.get("success_rate", 0) > 0.9 else "❌"
        print(f"  {integration_status} integration: {integration.get('test_count', 0)} tests, {integration.get('execution_time', 0):.1f}s")
        
        # Recommendations
        print(f"\n💡 Recommendations:")
        for rec in report.recommendations:
            print(f"  - {rec}")
        
        print(f"\n" + "=" * 60)
    
    def save_report(self, report: ComprehensiveValidationReport, filename: Optional[str] = None):
        """Save validation report to JSON file."""
        if filename is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"comprehensive_validation_report_{timestamp}.json"
        
        report_path = self.base_path / filename
        
        with open(report_path, 'w') as f:
            json.dump(asdict(report), f, indent=2, default=str)
        
        print(f"📄 Report saved to: {report_path}")


async def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(description="Comprehensive 4-Phase Test Validation")
    parser.add_argument("--coverage", action="store_true", help="Include coverage reporting")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--maxfail", type=int, default=5, help="Stop after N failures")
    parser.add_argument("--save-report", type=str, help="Save report to specific file")
    parser.add_argument("--phase", choices=["logging", "service", "api", "infrastructure", "integration"], 
                       help="Run only specific phase")
    
    args = parser.parse_args()
    
    validator = ComprehensiveValidator(args)
    
    try:
        report = await validator.validate_all_phases()
        validator.print_comprehensive_report(report)
        
        if args.save_report or report.overall_status != "PASS":
            validator.save_report(report, args.save_report)
        
        # Exit with appropriate code
        if report.overall_status == "PASS":
            print("\n🎉 Comprehensive validation completed successfully!")
            return 0
        else:
            print(f"\n⚠️  Comprehensive validation completed with status: {report.overall_status}")
            return 1
            
    except KeyboardInterrupt:
        print("\n⏹️  Validation interrupted by user")
        return 130
    except Exception as e:
        print(f"\n💥 Validation failed with error: {e}")
        return 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n⏹️  Interrupted")
        sys.exit(130)