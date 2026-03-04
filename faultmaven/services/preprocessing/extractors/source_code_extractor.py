"""
SOURCE_CODE Extractor

Analyzes source code files using AST parsing and pattern matching to extract
key information (functions, classes, imports, error handling). No LLM calls.

Uses tree-sitter for multi-language parsing when available, falling back to
regex-based pattern matching.
"""

import ast
import re
from typing import Any, Dict, List, Optional, Tuple

from faultmaven.services.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    has_content,
    truncate_output,
)

try:
    import tree_sitter as _ts
    import tree_sitter_c as _tsc
    import tree_sitter_go as _tsgo
    import tree_sitter_java as _tsjava
    import tree_sitter_javascript as _tsjs
    import tree_sitter_python as _tspy
    import tree_sitter_rust as _tsrust
    import tree_sitter_typescript as _tsts

    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False


class SourceCodeExtractor:
    """AST-based code analysis (0 LLM calls)"""

    # Output limits
    MAX_OUTPUT_CHARS = 8000  # ~2K tokens
    MAX_FUNCTIONS = 20
    MAX_CLASSES = 15

    @property
    def strategy_name(self) -> str:
        return "ast_parse"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> str:
        """
        Extract key information from source code

        Strategy:
        1. Detect language (Python, JavaScript, TypeScript, Java, Go, etc.)
        2. Parse AST if possible (Python)
        3. Extract key elements:
           - Function/method definitions with signatures
           - Class definitions with inheritance
           - Import statements
           - Error handling (try/catch/except)
           - TODOs and FIXMEs
        4. Format as structured summary
        """
        if not has_content(content):
            return EMPTY_CONTENT_RESPONSE

        # Try Python AST parsing first
        python_result = self._parse_python_ast(content)
        if python_result:
            return python_result

        # Fall back to pattern-based extraction
        return self._pattern_based_extraction(content)

    def _parse_python_ast(self, content: str) -> Optional[str]:
        """Parse Python code using AST"""
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return None  # Not valid Python

        # Extract information
        imports = self._extract_imports(tree)
        classes = self._extract_classes(tree)
        functions = self._extract_functions(tree)
        error_handling = self._extract_error_handling(tree)
        todos = self._extract_todos_from_ast(tree, content)

        return self._format_python_output(
            imports, classes, functions, error_handling, todos
        )

    def _extract_imports(self, tree: ast.AST) -> List[str]:
        """Extract import statements"""
        imports = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = ", ".join(alias.name for alias in node.names)
                imports.append(f"from {module} import {names}")

        return imports[:30]  # Limit

    def _extract_classes(self, tree: ast.AST) -> List[Dict[str, Any]]:
        """Extract class definitions"""
        classes = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = [self._get_name(base) for base in node.bases]
                methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]

                classes.append(
                    {
                        "name": node.name,
                        "bases": bases,
                        "methods": methods[:10],  # Limit methods per class
                        "lineno": node.lineno,
                    }
                )

                if len(classes) >= self.MAX_CLASSES:
                    break

        return classes

    def _extract_functions(self, tree: ast.AST) -> List[Dict[str, Any]]:
        """Extract function definitions (module-level only, not methods)"""
        functions = []

        for node in tree.body if hasattr(tree, "body") else []:
            if isinstance(node, ast.FunctionDef):
                args = [arg.arg for arg in node.args.args]
                returns = self._get_name(node.returns) if node.returns else None

                functions.append(
                    {
                        "name": node.name,
                        "args": args,
                        "returns": returns,
                        "lineno": node.lineno,
                        "is_async": isinstance(node, ast.AsyncFunctionDef),
                    }
                )

                if len(functions) >= self.MAX_FUNCTIONS:
                    break

        return functions

    def _extract_error_handling(self, tree: ast.AST) -> List[Dict[str, Any]]:
        """Extract try/except blocks"""
        error_handlers = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                exceptions = []
                for handler in node.handlers:
                    if handler.type:
                        exc_name = self._get_name(handler.type)
                        exceptions.append(exc_name)

                error_handlers.append(
                    {
                        "lineno": node.lineno,
                        "exceptions": exceptions if exceptions else ["Exception"],
                    }
                )

                if len(error_handlers) >= 10:
                    break

        return error_handlers

    def _extract_todos_from_ast(
        self, tree: ast.AST, content: str
    ) -> List[Tuple[int, str]]:
        """Extract TODO/FIXME comments from source"""
        todos = []
        lines = content.split("\n")

        for i, line in enumerate(lines, 1):
            if re.search(r"#\s*(TODO|FIXME|XXX|HACK):", line, re.IGNORECASE):
                todos.append((i, line.strip()))

                if len(todos) >= 10:
                    break

        return todos

    def _get_name(self, node: Optional[ast.AST]) -> str:
        """Get name from AST node"""
        if node is None:
            return "None"
        elif isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._get_name(node.value)}.{node.attr}"
        elif isinstance(node, ast.Subscript):
            return f"{self._get_name(node.value)}[...]"
        else:
            return ast.unparse(node) if hasattr(ast, "unparse") else "Unknown"

    def _format_python_output(
        self,
        imports: List[str],
        classes: List[Dict[str, Any]],
        functions: List[Dict[str, Any]],
        error_handling: List[Dict[str, Any]],
        todos: List[Tuple[int, str]],
    ) -> str:
        """Format Python analysis output"""
        lines = ["=== PYTHON CODE ANALYSIS ===\n"]

        # Imports
        if imports:
            lines.append(f"## Imports ({len(imports)})")
            for imp in imports[:15]:
                lines.append(f"  {imp}")
            if len(imports) > 15:
                lines.append(f"  ... and {len(imports) - 15} more")
            lines.append("")

        # Classes
        if classes:
            lines.append(f"## Classes ({len(classes)})")
            for cls in classes:
                bases_str = f" ({', '.join(cls['bases'])})" if cls["bases"] else ""
                lines.append(f"  • {cls['name']}{bases_str} (line {cls['lineno']})")
                if cls["methods"]:
                    methods_str = ", ".join(cls["methods"][:5])
                    if len(cls["methods"]) > 5:
                        methods_str += f" ... +{len(cls['methods']) - 5} more"
                    lines.append(f"    Methods: {methods_str}")
            lines.append("")

        # Functions
        if functions:
            lines.append(f"## Functions ({len(functions)})")
            for func in functions:
                async_prefix = "async " if func["is_async"] else ""
                args_str = ", ".join(func["args"])
                returns_str = f" -> {func['returns']}" if func["returns"] else ""
                lines.append(
                    f"  • {async_prefix}{func['name']}({args_str}){returns_str} (line {func['lineno']})"
                )
            lines.append("")

        # Error Handling
        if error_handling:
            lines.append(f"## Error Handling ({len(error_handling)})")
            for eh in error_handling[:5]:
                exceptions = ", ".join(eh["exceptions"])
                lines.append(f"  • try/except at line {eh['lineno']}: {exceptions}")
            if len(error_handling) > 5:
                lines.append(f"  ... and {len(error_handling) - 5} more")
            lines.append("")

        # TODOs
        if todos:
            lines.append(f"## TODOs/FIXMEs ({len(todos)})")
            for lineno, todo in todos:
                lines.append(f"  • Line {lineno}: {todo}")
            lines.append("")

        output = "\n".join(lines)

        return truncate_output(output)

    def _pattern_based_extraction(self, content: str) -> str:
        """
        Extraction for non-Python languages.

        Uses tree-sitter when available for accurate AST-based extraction,
        falling back to regex patterns otherwise.
        """
        if TREE_SITTER_AVAILABLE:
            result = self._tree_sitter_extraction(content)
            if result:
                return result

        # Fallback to regex-based extraction
        return self._regex_based_extraction(content)

    # --- tree-sitter integration ---

    _ts_parsers: Dict[str, Any] = {}  # Lazy-loaded parsers

    def _get_ts_parser(self, lang_name: str) -> Optional[Any]:
        """Get or lazily create a tree-sitter parser for the given language."""
        if not TREE_SITTER_AVAILABLE:
            return None
        if lang_name in self._ts_parsers:
            return self._ts_parsers[lang_name]

        lang_factories = {
            "javascript": lambda: _ts.Language(_tsjs.language()),
            "typescript": lambda: _ts.Language(_tsts.language_typescript()),
            "java": lambda: _ts.Language(_tsjava.language()),
            "go": lambda: _ts.Language(_tsgo.language()),
            "rust": lambda: _ts.Language(_tsrust.language()),
            "c": lambda: _ts.Language(_tsc.language()),
            "python": lambda: _ts.Language(_tspy.language()),
        }

        factory = lang_factories.get(lang_name)
        if not factory:
            return None
        try:
            lang = factory()
            parser = _ts.Parser(lang)
            self._ts_parsers[lang_name] = parser
            return parser
        except Exception:
            return None

    def _tree_sitter_extraction(self, content: str) -> Optional[str]:
        """Try tree-sitter extraction across supported languages."""
        source = content.encode("utf-8", errors="replace")

        # Detect language by trying each parser — use error count heuristic
        lang_name, tree = self._detect_language_tree_sitter(source)
        if not lang_name or not tree:
            return None

        display_names = {
            "javascript": "JavaScript",
            "typescript": "TypeScript",
            "java": "Java",
            "go": "Go",
            "rust": "Rust",
            "c": "C/C++",
        }

        root = tree.root_node
        info = self._extract_with_tree_sitter(root, lang_name, source)

        lines = ["=== SOURCE CODE ANALYSIS (tree-sitter) ===\n"]
        lines.append(f"Detected Language: {display_names.get(lang_name, lang_name)}\n")

        if info["imports"]:
            lines.append(f"## Imports/Includes ({len(info['imports'])})")
            for imp in info["imports"][:15]:
                lines.append(f"  {imp}")
            if len(info["imports"]) > 15:
                lines.append(f"  ... and {len(info['imports']) - 15} more")
            lines.append("")

        if info["classes"]:
            lines.append(f"## Classes/Structs ({len(info['classes'])})")
            for cls in info["classes"][:10]:
                lines.append(f"  • {cls}")
            lines.append("")

        if info["functions"]:
            lines.append(f"## Functions ({len(info['functions'])})")
            for func in info["functions"][:15]:
                lines.append(f"  • {func}")
            lines.append("")

        if info["error_handling"]:
            lines.append(f"## Error Handling ({len(info['error_handling'])})")
            for eh in info["error_handling"][:10]:
                lines.append(f"  • {eh}")
            lines.append("")

        if info["todos"]:
            lines.append(f"## TODOs/FIXMEs ({len(info['todos'])})")
            for todo in info["todos"][:10]:
                lines.append(f"  • {todo}")
            lines.append("")

        output = "\n".join(lines)
        return truncate_output(output)

    def _detect_language_tree_sitter(
        self, source: bytes
    ) -> Tuple[Optional[str], Optional[Any]]:
        """Detect language by parsing with each grammar, choosing the one with fewest errors."""
        # Try languages in order of likelihood (skip Python — handled by AST path)
        candidates = ["javascript", "typescript", "java", "go", "rust", "c"]
        best_lang = None
        best_tree = None
        best_error_count = float("inf")

        for lang_name in candidates:
            parser = self._get_ts_parser(lang_name)
            if not parser:
                continue
            try:
                tree = parser.parse(source)
                errors = self._count_errors(tree.root_node)
                # Accept if zero errors, or track best
                if errors == 0:
                    return lang_name, tree
                if errors < best_error_count:
                    best_error_count = errors
                    best_lang = lang_name
                    best_tree = tree
            except Exception:
                continue

        # Accept best if it has fewer errors than 30% of nodes
        if best_tree and best_lang:
            total = self._count_nodes(best_tree.root_node)
            if total > 0 and best_error_count / total < 0.3:
                return best_lang, best_tree

        return None, None

    def _count_errors(self, node: Any) -> int:
        """Count ERROR nodes in the tree."""
        count = 1 if node.type == "ERROR" else 0
        for child in node.children:
            count += self._count_errors(child)
        return count

    def _count_nodes(self, node: Any) -> int:
        """Count total nodes in the tree."""
        count = 1
        for child in node.children:
            count += self._count_nodes(child)
        return count

    def _collect_by_type(self, node: Any, type_name: str) -> List[Any]:
        """Collect all descendant nodes matching a type."""
        results = []
        if node.type == type_name:
            results.append(node)
        for child in node.children:
            results.extend(self._collect_by_type(child, type_name))
        return results

    def _extract_ts_function_name(self, node: Any) -> Optional[str]:
        """Extract function name from a tree-sitter node.

        Handles C/C++ where name is nested inside declarator→declarator.
        """
        # Direct name field (most languages)
        name_node = node.child_by_field_name("name")
        if name_node:
            return name_node.text.decode()

        # C/C++: function_definition → declarator (function_declarator) → declarator (identifier)
        declarator = node.child_by_field_name("declarator")
        if declarator:
            inner = declarator.child_by_field_name("declarator")
            if inner and inner.type == "identifier":
                return inner.text.decode()

        return None

    def _extract_with_tree_sitter(
        self, root: Any, lang_name: str, source: bytes
    ) -> Dict[str, List[str]]:
        """Extract imports, classes, functions, error handling, TODOs from tree."""
        # Language-specific node type mappings
        import_types = {
            "javascript": ["import_statement"],
            "typescript": ["import_statement"],
            "java": ["import_declaration"],
            "go": ["import_spec"],
            "rust": ["use_declaration"],
            "c": ["preproc_include"],
        }
        class_types = {
            "javascript": ["class_declaration"],
            "typescript": ["class_declaration", "interface_declaration"],
            "java": ["class_declaration", "interface_declaration"],
            "go": ["type_declaration"],
            "rust": ["struct_item", "enum_item"],
            "c": ["struct_specifier"],
        }
        function_types = {
            "javascript": ["function_declaration", "method_definition"],
            "typescript": ["function_declaration", "method_definition"],
            "java": ["method_declaration"],
            "go": ["function_declaration", "method_declaration"],
            "rust": ["function_item"],
            "c": ["function_definition"],
        }
        error_types = {
            "javascript": ["try_statement"],
            "typescript": ["try_statement"],
            "java": ["try_statement"],
            "go": [],  # Go uses if err != nil — handled separately
            "rust": [],  # Rust uses ? and Result — handled separately
            "c": [],
        }

        result: Dict[str, List[str]] = {
            "imports": [],
            "classes": [],
            "functions": [],
            "error_handling": [],
            "todos": [],
        }

        # Imports
        for type_name in import_types.get(lang_name, []):
            for node in self._collect_by_type(root, type_name):
                text = node.text.decode("utf-8", errors="replace").strip()
                result["imports"].append(text)

        # Classes/Structs
        for type_name in class_types.get(lang_name, []):
            for node in self._collect_by_type(root, type_name):
                name_node = node.child_by_field_name("name")
                name = (
                    name_node.text.decode()
                    if name_node
                    else (
                        node.text.decode().split()[1]
                        if len(node.text.decode().split()) > 1
                        else "?"
                    )
                )
                result["classes"].append(name)

        # Functions
        for type_name in function_types.get(lang_name, []):
            for node in self._collect_by_type(root, type_name):
                name = self._extract_ts_function_name(node)
                if name:
                    result["functions"].append(name)

        # Error handling
        for type_name in error_types.get(lang_name, []):
            for node in self._collect_by_type(root, type_name):
                result["error_handling"].append(
                    f"try/catch at line {node.start_point.row + 1}"
                )

        # Go error handling: count `if err != nil` patterns
        if lang_name == "go":
            if_nodes = self._collect_by_type(root, "if_statement")
            err_checks = 0
            for node in if_nodes:
                text = node.text.decode("utf-8", errors="replace")
                if "err" in text and "nil" in text:
                    err_checks += 1
            if err_checks > 0:
                result["error_handling"].append(f"{err_checks} error checks found")

        # Rust error handling: count ? operators and .unwrap() calls
        if lang_name == "rust":
            full_text = source.decode("utf-8", errors="replace")
            q_count = full_text.count("?")
            unwrap_count = len(re.findall(r"\.unwrap\(\)", full_text))
            expect_count = len(re.findall(r"\.expect\(", full_text))
            total = q_count + unwrap_count + expect_count
            if total > 0:
                result["error_handling"].append(
                    f"{total} error handling patterns found"
                )

        # TODOs/FIXMEs from comments
        for node in self._collect_by_type(root, "comment"):
            text = node.text.decode("utf-8", errors="replace")
            if re.search(r"(TODO|FIXME|XXX|HACK):", text, re.IGNORECASE):
                result["todos"].append(
                    f"Line {node.start_point.row + 1}: {text.strip()}"
                )

        return result

    # --- Regex fallback (original pattern-based extraction) ---

    def _regex_based_extraction(self, content: str) -> str:
        """Regex-based extraction for non-Python languages (fallback)."""
        lines = ["=== SOURCE CODE ANALYSIS (Pattern-based) ===\n"]

        # Detect language
        language = self._detect_language(content)
        lines.append(f"Detected Language: {language}\n")

        # Extract patterns
        imports = self._extract_imports_pattern(content, language)
        functions = self._extract_functions_pattern(content, language)
        classes = self._extract_classes_pattern(content, language)
        error_handling = self._extract_error_handling_pattern(content, language)
        todos = self._extract_todos_pattern(content)

        if imports:
            lines.append(f"## Imports/Includes ({len(imports)})")
            for imp in imports[:15]:
                lines.append(f"  {imp}")
            lines.append("")

        if classes:
            lines.append(f"## Classes/Structs ({len(classes)})")
            for cls in classes[:10]:
                lines.append(f"  • {cls}")
            lines.append("")

        if functions:
            lines.append(f"## Functions ({len(functions)})")
            for func in functions[:15]:
                lines.append(f"  • {func}")
            lines.append("")

        if error_handling:
            lines.append(f"## Error Handling ({len(error_handling)})")
            for eh in error_handling[:10]:
                lines.append(f"  • {eh}")
            lines.append("")

        if todos:
            lines.append(f"## TODOs/FIXMEs ({len(todos)})")
            for todo in todos[:10]:
                lines.append(f"  • {todo}")
            lines.append("")

        output = "\n".join(lines)

        return truncate_output(output)

    def _detect_language(self, content: str) -> str:
        """Detect programming language from content patterns"""
        if re.search(r"\b(def|import|from\s+\w+\s+import)\b", content):
            return "Python"
        elif re.search(r"\b(function|const|let|var|=>)\b", content):
            return "JavaScript/TypeScript"
        elif re.search(r"\b(public|private|class)\s+\w+\s*{", content):
            return "Java"
        elif re.search(r"\bfunc\s+\w+\s*\(", content):
            return "Go"
        elif re.search(r"\bfn\s+\w+\s*\(", content):
            return "Rust"
        elif re.search(r'#include\s*[<"]', content):
            return "C/C++"
        else:
            return "Unknown"

    def _extract_imports_pattern(self, content: str, language: str) -> List[str]:
        """Extract import/include statements using patterns"""
        imports = []

        if language in ["JavaScript/TypeScript"]:
            pattern = r'^import\s+.*?from\s+[\'"].*?[\'"]'
            imports = re.findall(pattern, content, re.MULTILINE)
        elif language == "Java":
            pattern = r"^import\s+[\w\.]+;"
            imports = re.findall(pattern, content, re.MULTILINE)
        elif language == "Go":
            pattern = r'^\s*"[\w/\.]+"'
            imports = re.findall(pattern, content, re.MULTILINE)
        elif language in ["C/C++"]:
            pattern = r'^#include\s*[<"].*?[>"]'
            imports = re.findall(pattern, content, re.MULTILINE)

        return [imp.strip() for imp in imports[:30]]

    def _extract_functions_pattern(self, content: str, language: str) -> List[str]:
        """Extract function definitions using patterns"""
        functions = []

        if language in ["JavaScript/TypeScript"]:
            pattern = r"\b(?:function|const|let|var)\s+(\w+)\s*(?:=\s*)?(?:async\s+)?\([^)]*\)"
            functions = re.findall(pattern, content)
        elif language == "Java":
            pattern = (
                r"\b(?:public|private|protected)\s+(?:static\s+)?[\w<>]+\s+(\w+)\s*\("
            )
            functions = re.findall(pattern, content)
        elif language == "Go":
            pattern = r"\bfunc\s+(\w+)\s*\("
            functions = re.findall(pattern, content)
        elif language == "Rust":
            pattern = r"\bfn\s+(\w+)\s*\("
            functions = re.findall(pattern, content)

        return functions[: self.MAX_FUNCTIONS]

    def _extract_classes_pattern(self, content: str, language: str) -> List[str]:
        """Extract class definitions using patterns"""
        classes = []

        if language in ["JavaScript/TypeScript", "Java"]:
            pattern = r"\bclass\s+(\w+)(?:\s+extends\s+(\w+))?"
            matches = re.findall(pattern, content)
            classes = [
                f"{cls[0]} extends {cls[1]}" if cls[1] else cls[0] for cls in matches
            ]
        elif language == "Rust":
            pattern = r"\bstruct\s+(\w+)"
            classes = re.findall(pattern, content)

        return classes[: self.MAX_CLASSES]

    def _extract_error_handling_pattern(self, content: str, language: str) -> List[str]:
        """Extract error handling using patterns"""
        error_handlers = []

        if language in ["JavaScript/TypeScript", "Java"]:
            pattern = r"\btry\s*{.*?}\s*catch\s*\((\w+)\s+(\w+)\)"
            matches = re.findall(pattern, content, re.DOTALL)
            error_handlers = [
                f"catch ({exc_type} {exc_name})" for exc_type, exc_name in matches
            ]
        elif language == "Go":
            pattern = r"if\s+err\s*!=\s*nil"
            count = len(re.findall(pattern, content))
            if count > 0:
                error_handlers = [f"{count} error checks found"]
        elif language == "Rust":
            pattern = r"\?|\.unwrap\(\)|\.expect\("
            count = len(re.findall(pattern, content))
            if count > 0:
                error_handlers = [f"{count} error handling patterns found"]

        return error_handlers[:10]

    def _extract_todos_pattern(self, content: str) -> List[str]:
        """Extract TODO/FIXME comments"""
        todos = []
        lines = content.split("\n")

        for i, line in enumerate(lines, 1):
            if re.search(r"(//|#|/\*)\s*(TODO|FIXME|XXX|HACK):", line, re.IGNORECASE):
                todos.append(f"Line {i}: {line.strip()}")

                if len(todos) >= 10:
                    break

        return todos
