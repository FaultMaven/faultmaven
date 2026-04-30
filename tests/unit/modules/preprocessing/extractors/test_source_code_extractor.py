"""
Tests for SourceCodeExtractor.

Covers:
- R5.1: tree-sitter multi-language parsing
- Python AST path (unchanged behavior)
- Regex fallback when tree-sitter unavailable
- ISS-041: argparse CLI argument enumeration (short+long flag pairs)
"""

import os
from unittest.mock import patch

import pytest

from faultmaven.modules.preprocessing.extractors.source_code_extractor import (
    TREE_SITTER_AVAILABLE,
    SourceCodeExtractor,
)


class TestSourceCodeExtractor:
    @pytest.fixture
    def extractor(self):
        return SourceCodeExtractor()

    def test_properties(self, extractor):
        assert extractor.strategy_name == "ast_parse"
        assert extractor.llm_calls_used == 0

    # --- Python (AST path — not affected by tree-sitter) ---

    def test_python_ast_path(self, extractor):
        """Python code should use the native AST parser."""
        code = '''
import os
from typing import List

class Foo:
    def bar(self):
        pass

def baz(x: int) -> str:
    """Convert x to string."""
    return str(x)
'''
        result = extractor.extract(code)
        assert "PYTHON CODE ANALYSIS" in result.file_extract
        assert "import os" in result.file_extract
        assert "Foo" in result.file_extract
        assert "baz" in result.file_extract

    def test_python_error_handling(self, extractor):
        """Python try/except blocks extracted."""
        code = """
def risky():
    try:
        do_work()
    except ValueError as e:
        handle(e)
    except (TypeError, KeyError):
        fallback()
"""
        result = extractor.extract(code)
        assert "Error Handling" in result.file_extract
        assert "ValueError" in result.file_extract

    # --- Import counting (regression for ISS-021) ---

    def test_python_import_heading_total_count(self, extractor):
        """Import heading must report the accurate total (top-level + conditional).

        Regression: ISS-021 — extractor previously truncated the per-line
        listing to 15 with a low-signal "... and N more" tail. The total
        count in the heading is the load-bearing signal for counting
        questions and must always reflect every Import/ImportFrom node.
        """
        # 6 top-level + 4 conditional + 2 in try/except = 12 import nodes total
        code = """
import argparse
import os
from foo.runner import Runner
from foo.util import helper
try:
    import simplejson as json
except ImportError:
    import json

from bar.module import Thing
from baz.module import Other

def use_things():
    pass

if __name__ == "__main__":
    if condition_a:
        from pkg.a import A
    if condition_b:
        from pkg.b import B
    if condition_c:
        from pkg.c import C
    if condition_d:
        from pkg.d import D
"""
        result = extractor.extract(code)

        # Heading reflects accurate total
        assert (
            "## Imports (12 total" in result.file_extract
        ), f"Heading must show total=12. Got:\n{result.file_extract}"
        # Heading reflects top-level vs conditional breakdown
        assert "6 top-level" in result.file_extract
        assert "6 conditional" in result.file_extract  # 2 try/except + 4 if-blocks

    def test_python_imports_no_truncation_for_realistic_file(self, extractor):
        """A file with ~17 imports renders all of them (not truncated to 15).

        Mirrors the structure of NAB run.py (the failing benchmark case):
        a few top-level imports, a try/except import fallback, and
        many conditional imports inside ``if __name__ == "__main__":``.
        """
        # 4 top-level + 2 try/except + 11 conditional in __main__ = 17 imports
        code = """
import argparse
import os
try:
    import simplejson as json
except ImportError:
    import json

from foo.runner import Runner
from foo.util import detectorNameToClass, checkInputs


def main():
    pass


if __name__ == "__main__":
    if "a" in detectors:
        from foo.detectors.a import ADetector
    if "b" in detectors:
        from foo.detectors.b import BDetector
    if "c" in detectors:
        from foo.detectors.c import CDetector
    if "d" in detectors:
        from foo.detectors.d import DDetector
    if "e" in detectors:
        from foo.detectors.e import EDetector
    if "f" in detectors:
        from foo.detectors.f import FDetector
    if "g" in detectors:
        from foo.detectors.g import GDetector
    if "h" in detectors:
        from foo.detectors.h import HDetector
    if "i" in detectors:
        from foo.detectors.i import IDetector
    if "j" in detectors:
        from foo.detectors.j import JDetector
    if "k" in detectors:
        from foo.detectors.k import KDetector
"""
        result = extractor.extract(code)

        # Total heading is correct
        assert "## Imports (17 total" in result.file_extract
        # All 11 conditional detector imports must be visible in the listing —
        # no "... and N more" truncation for the typical file size.
        for letter in "abcdefghijk":
            assert (
                f"{letter.upper()}Detector" in result.file_extract
            ), f"{letter.upper()}Detector missing from output:\n{result.file_extract}"
        assert (
            "... and" not in result.file_extract
        ), "17-import file should not be truncated"

    def test_python_conditional_imports_marked(self, extractor):
        """Conditional (non-top-level) imports get a [conditional] marker."""
        code = """
import os

if some_flag:
    import platform
"""
        result = extractor.extract(code)
        # Top-level: no marker
        assert "  import os\n" in result.file_extract + "\n"
        # Conditional: marker present
        assert "import platform [conditional]" in result.file_extract

    # --- ISS-041: argparse CLI argument enumeration ---

    def test_argparse_synthetic_mixed_forms(self, extractor):
        """Synthetic argparse code: long-only, short+long, default, store_true.

        Regression for ISS-041 — argparse scanner must enumerate calls
        whose first positional is the long form AND calls that pair a
        short flag with a long form. Both must surface in the output,
        with the short flag preserved as an alias.
        """
        code = """
import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose",
                        action="store_true",
                        help="Enable verbose logging")
    parser.add_argument("-o", "--output",
                        default="out.txt",
                        help="Output file path")
    parser.add_argument("--count",
                        type=int,
                        default=10,
                        help="Number of items")
    parser.add_argument("-d", "--debug",
                        action="store_true",
                        help="Enable debug mode")
    args = parser.parse_args()
"""
        result = extractor.extract(code)

        # file_meta records the count
        assert (
            result.file_meta.get("cli_arguments") == 4
        ), f"file_meta.cli_arguments must be 4. Got: {result.file_meta}"

        # All 4 long-form names rendered
        for name in ("--verbose", "--output", "--count", "--debug"):
            assert name in result.file_extract, (
                f"{name} missing from CLI arguments listing:\n" f"{result.file_extract}"
            )

        # Short flags preserved as aliases for paired arguments
        assert "-o" in result.file_extract
        assert "-d" in result.file_extract

        # Section heading mentions the total
        assert "## CLI Arguments (4" in result.file_extract

    def test_argparse_real_nab_run_py(self, extractor):
        """Against the real NAB run.py fixture, all 12 argparse args enumerate.

        Regression for ISS-041 — the LLM previously saw only 9 of 12
        because the scanner skipped the calls that paired a short flag
        with a long-form name (-p/--profilesFile, -t/--thresholdsFile,
        -n/--numCPUs).

        Skipped gracefully when fm-data-exam fixture absent (e.g., CI
        without the sibling repo checkout).
        """
        fixture_path = "/home/swhouse/product/fm-data-exam/test-data/NAB/run.py"
        if not os.path.exists(fixture_path):
            pytest.skip(f"NAB run.py fixture not available at {fixture_path}")

        with open(fixture_path) as f:
            content = f.read()
        result = extractor.extract(content)

        # 12 argparse calls total in NAB run.py
        assert (
            result.file_meta.get("cli_arguments") == 12
        ), f"NAB run.py has 12 add_argument calls. Got: {result.file_meta}"

        # The three previously dropped (paired short+long) must be present
        # with their short-form aliases preserved.
        extract = result.file_extract
        assert "--profilesFile" in extract and "-p" in extract
        assert "--thresholdsFile" in extract and "-t" in extract
        assert "--numCPUs" in extract and "-n" in extract

        # Plus all the long-only and the existing short+long ones
        for long_name in (
            "--detect",
            "--optimize",
            "--score",
            "--normalize",
            "--skipConfirmation",
            "--dataDir",
            "--resultsDir",
            "--windowsFile",
            "--detectors",
        ):
            assert long_name in extract, f"{long_name} missing from output"

    def test_argparse_no_calls_no_section(self, extractor):
        """Files without argparse calls don't render an empty section."""
        code = """
import os

def main():
    return os.getcwd()
"""
        result = extractor.extract(code)
        assert "CLI Arguments" not in result.file_extract
        # cli_arguments either absent or 0 — both acceptable
        assert result.file_meta.get("cli_arguments", 0) == 0

    def test_argparse_short_only_form(self, extractor):
        """A bare positional arg (no leading dashes) is treated as positional name."""
        code = """
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("filename", help="Input file")
parser.add_argument("--flag", action="store_true")
"""
        result = extractor.extract(code)
        # Both should be enumerated
        assert result.file_meta.get("cli_arguments") == 2
        assert "filename" in result.file_extract
        assert "--flag" in result.file_extract

    # --- JavaScript (tree-sitter path) ---

    @pytest.mark.skipif(not TREE_SITTER_AVAILABLE, reason="tree-sitter not installed")
    def test_javascript_functions_classes(self, extractor):
        """JS classes and functions extracted via tree-sitter."""
        code = """
import React from 'react';

class Dashboard extends React.Component {
    render() { return null; }
}

function fetchData(url) {
    return fetch(url);
}

// TODO: Add error handling
"""
        result = extractor.extract(code)
        assert "tree-sitter" in result.file_extract
        assert "JavaScript" in result.file_extract
        assert "Dashboard" in result.file_extract
        assert "fetchData" in result.file_extract
        assert "TODO" in result.file_extract

    @pytest.mark.skipif(not TREE_SITTER_AVAILABLE, reason="tree-sitter not installed")
    def test_javascript_imports(self, extractor):
        """JS imports extracted."""
        code = """
import React from 'react';
import { useState, useEffect } from 'react';

function App() { return null; }
"""
        result = extractor.extract(code)
        assert "Imports" in result.file_extract
        assert "react" in result.file_extract

    @pytest.mark.skipif(not TREE_SITTER_AVAILABLE, reason="tree-sitter not installed")
    def test_javascript_try_catch(self, extractor):
        """JS try/catch extracted."""
        code = """
function doWork() {
    try {
        riskyOperation();
    } catch (e) {
        console.error(e);
    }
}
"""
        result = extractor.extract(code)
        assert "Error Handling" in result.file_extract
        assert "try/catch" in result.file_extract

    # --- TypeScript ---

    @pytest.mark.skipif(not TREE_SITTER_AVAILABLE, reason="tree-sitter not installed")
    def test_typescript_interfaces(self, extractor):
        """TS interfaces and classes extracted."""
        code = """
import { Component } from 'react';

interface User {
    id: number;
    name: string;
}

class UserService {
    async getUser(id: number): Promise<User> {
        return fetch('/api/users/' + id).then(r => r.json());
    }
}

function processItems(items: User[]): void {
    items.forEach(item => console.log(item.name));
}
"""
        result = extractor.extract(code)
        assert "TypeScript" in result.file_extract
        assert "User" in result.file_extract
        assert "UserService" in result.file_extract
        assert "processItems" in result.file_extract

    # --- Java ---

    @pytest.mark.skipif(not TREE_SITTER_AVAILABLE, reason="tree-sitter not installed")
    def test_java_class_methods(self, extractor):
        """Java class and methods extracted."""
        code = """
import java.util.List;
import java.io.IOException;

public class MyService {
    public void process(String input) throws IOException {
        try {
            doWork(input);
        } catch (Exception e) {
            throw new IOException(e);
        }
    }

    private int calculate(int x) {
        return x * 2;
    }
}
"""
        result = extractor.extract(code)
        assert "Java" in result.file_extract
        assert "MyService" in result.file_extract
        assert "process" in result.file_extract
        assert "calculate" in result.file_extract
        assert "Error Handling" in result.file_extract

    # --- Go ---

    @pytest.mark.skipif(not TREE_SITTER_AVAILABLE, reason="tree-sitter not installed")
    def test_go_funcs_and_structs(self, extractor):
        """Go structs, funcs, and error checks extracted."""
        code = """
package main

import (
    "fmt"
    "net/http"
)

type Server struct {
    Port int
    Host string
}

func (s *Server) Start() error {
    if err := s.init(); err != nil {
        return fmt.Errorf("init failed: %w", err)
    }
    return nil
}

func handleRequest(w http.ResponseWriter, r *http.Request) {
    fmt.Fprintf(w, "Hello")
}

// TODO: Add graceful shutdown
"""
        result = extractor.extract(code)
        assert "Go" in result.file_extract
        assert "Server" in result.file_extract
        assert "handleRequest" in result.file_extract or "Start" in result.file_extract
        assert (
            "error checks" in result.file_extract.lower()
            or "Error Handling" in result.file_extract
        )
        assert "TODO" in result.file_extract

    # --- Rust ---

    @pytest.mark.skipif(not TREE_SITTER_AVAILABLE, reason="tree-sitter not installed")
    def test_rust_structs_and_fns(self, extractor):
        """Rust structs, functions, and use declarations extracted."""
        code = """
use std::io;
use std::collections::HashMap;

struct Config {
    port: u16,
    host: String,
}

fn process(input: &str) -> Result<String, io::Error> {
    Ok(input.to_string())
}

impl Config {
    fn new() -> Self {
        Config { port: 8080, host: String::from("localhost") }
    }
}
"""
        result = extractor.extract(code)
        assert "Rust" in result.file_extract
        assert "Config" in result.file_extract
        assert "process" in result.file_extract or "new" in result.file_extract
        assert "use std" in result.file_extract or "Imports" in result.file_extract

    # --- C ---

    @pytest.mark.skipif(not TREE_SITTER_AVAILABLE, reason="tree-sitter not installed")
    def test_c_includes_and_functions(self, extractor):
        """C includes, structs, and functions extracted."""
        code = """
#include <stdio.h>
#include <stdlib.h>

struct Config {
    int port;
    char* host;
};

int process(const char* input) {
    return strlen(input);
}

// TODO: Add error handling
"""
        result = extractor.extract(code)
        assert "C/C++" in result.file_extract
        assert "Config" in result.file_extract
        assert "process" in result.file_extract
        assert "#include" in result.file_extract or "Imports" in result.file_extract

    # --- Fallback ---

    def test_regex_fallback_when_tree_sitter_disabled(self, extractor):
        """When tree-sitter is mocked as unavailable, regex fallback used."""
        js_code = """
const React = require('react');

function greet(name) {
    return 'Hello ' + name;
}

// TODO: Fix this
"""
        with patch(
            "faultmaven.modules.preprocessing.extractors.source_code_extractor.TREE_SITTER_AVAILABLE",
            False,
        ):
            result = extractor.extract(js_code)
            assert "Pattern-based" in result.file_extract
            assert "JavaScript" in result.file_extract
            assert "greet" in result.file_extract
