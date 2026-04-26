"""
Tests for SourceCodeExtractor.

Covers:
- R5.1: tree-sitter multi-language parsing
- Python AST path (unchanged behavior)
- Regex fallback when tree-sitter unavailable
"""

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
