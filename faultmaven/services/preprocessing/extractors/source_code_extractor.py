"""
SOURCE_CODE Extractor

Analyzes source code files using AST parsing and pattern matching to extract
key information (functions, classes, imports, error handling). No LLM calls.
"""

import re
import ast
from typing import List, Dict, Any, Optional, Tuple


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

        return self._format_python_output(imports, classes, functions, error_handling, todos)

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

                classes.append({
                    'name': node.name,
                    'bases': bases,
                    'methods': methods[:10],  # Limit methods per class
                    'lineno': node.lineno
                })

                if len(classes) >= self.MAX_CLASSES:
                    break

        return classes

    def _extract_functions(self, tree: ast.AST) -> List[Dict[str, Any]]:
        """Extract function definitions (module-level only, not methods)"""
        functions = []

        for node in tree.body if hasattr(tree, 'body') else []:
            if isinstance(node, ast.FunctionDef):
                args = [arg.arg for arg in node.args.args]
                returns = self._get_name(node.returns) if node.returns else None

                functions.append({
                    'name': node.name,
                    'args': args,
                    'returns': returns,
                    'lineno': node.lineno,
                    'is_async': isinstance(node, ast.AsyncFunctionDef)
                })

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

                error_handlers.append({
                    'lineno': node.lineno,
                    'exceptions': exceptions if exceptions else ['Exception']
                })

                if len(error_handlers) >= 10:
                    break

        return error_handlers

    def _extract_todos_from_ast(self, tree: ast.AST, content: str) -> List[Tuple[int, str]]:
        """Extract TODO/FIXME comments from source"""
        todos = []
        lines = content.split('\n')

        for i, line in enumerate(lines, 1):
            if re.search(r'#\s*(TODO|FIXME|XXX|HACK):', line, re.IGNORECASE):
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
            return ast.unparse(node) if hasattr(ast, 'unparse') else "Unknown"

    def _format_python_output(
        self,
        imports: List[str],
        classes: List[Dict[str, Any]],
        functions: List[Dict[str, Any]],
        error_handling: List[Dict[str, Any]],
        todos: List[Tuple[int, str]]
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
                bases_str = f" ({', '.join(cls['bases'])})" if cls['bases'] else ""
                lines.append(f"  • {cls['name']}{bases_str} (line {cls['lineno']})")
                if cls['methods']:
                    methods_str = ', '.join(cls['methods'][:5])
                    if len(cls['methods']) > 5:
                        methods_str += f" ... +{len(cls['methods']) - 5} more"
                    lines.append(f"    Methods: {methods_str}")
            lines.append("")

        # Functions
        if functions:
            lines.append(f"## Functions ({len(functions)})")
            for func in functions:
                async_prefix = "async " if func['is_async'] else ""
                args_str = ', '.join(func['args'])
                returns_str = f" -> {func['returns']}" if func['returns'] else ""
                lines.append(f"  • {async_prefix}{func['name']}({args_str}){returns_str} (line {func['lineno']})")
            lines.append("")

        # Error Handling
        if error_handling:
            lines.append(f"## Error Handling ({len(error_handling)})")
            for eh in error_handling[:5]:
                exceptions = ', '.join(eh['exceptions'])
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

        output = '\n'.join(lines)

        if len(output) > self.MAX_OUTPUT_CHARS:
            return output[:self.MAX_OUTPUT_CHARS] + "\n... [Truncated for length]"

        return output

    def _pattern_based_extraction(self, content: str) -> str:
        """
        Pattern-based extraction for non-Python languages

        Supports: JavaScript, TypeScript, Java, Go, Rust, C/C++
        """
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

        output = '\n'.join(lines)

        if len(output) > self.MAX_OUTPUT_CHARS:
            return output[:self.MAX_OUTPUT_CHARS] + "\n... [Truncated for length]"

        return output

    def _detect_language(self, content: str) -> str:
        """Detect programming language from content patterns"""
        if re.search(r'\b(def|import|from\s+\w+\s+import)\b', content):
            return "Python"
        elif re.search(r'\b(function|const|let|var|=>)\b', content):
            return "JavaScript/TypeScript"
        elif re.search(r'\b(public|private|class)\s+\w+\s*{', content):
            return "Java"
        elif re.search(r'\bfunc\s+\w+\s*\(', content):
            return "Go"
        elif re.search(r'\bfn\s+\w+\s*\(', content):
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
            pattern = r'^import\s+[\w\.]+;'
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
            pattern = r'\b(?:function|const|let|var)\s+(\w+)\s*(?:=\s*)?(?:async\s+)?\([^)]*\)'
            functions = re.findall(pattern, content)
        elif language == "Java":
            pattern = r'\b(?:public|private|protected)\s+(?:static\s+)?[\w<>]+\s+(\w+)\s*\('
            functions = re.findall(pattern, content)
        elif language == "Go":
            pattern = r'\bfunc\s+(\w+)\s*\('
            functions = re.findall(pattern, content)
        elif language == "Rust":
            pattern = r'\bfn\s+(\w+)\s*\('
            functions = re.findall(pattern, content)

        return functions[:self.MAX_FUNCTIONS]

    def _extract_classes_pattern(self, content: str, language: str) -> List[str]:
        """Extract class definitions using patterns"""
        classes = []

        if language in ["JavaScript/TypeScript", "Java"]:
            pattern = r'\bclass\s+(\w+)(?:\s+extends\s+(\w+))?'
            matches = re.findall(pattern, content)
            classes = [f"{cls[0]} extends {cls[1]}" if cls[1] else cls[0] for cls in matches]
        elif language == "Rust":
            pattern = r'\bstruct\s+(\w+)'
            classes = re.findall(pattern, content)

        return classes[:self.MAX_CLASSES]

    def _extract_error_handling_pattern(self, content: str, language: str) -> List[str]:
        """Extract error handling using patterns"""
        error_handlers = []

        if language in ["JavaScript/TypeScript", "Java"]:
            pattern = r'\btry\s*{.*?}\s*catch\s*\((\w+)\s+(\w+)\)'
            matches = re.findall(pattern, content, re.DOTALL)
            error_handlers = [f"catch ({exc_type} {exc_name})" for exc_type, exc_name in matches]
        elif language == "Go":
            pattern = r'if\s+err\s*!=\s*nil'
            count = len(re.findall(pattern, content))
            if count > 0:
                error_handlers = [f"{count} error checks found"]
        elif language == "Rust":
            pattern = r'\?|\.unwrap\(\)|\.expect\('
            count = len(re.findall(pattern, content))
            if count > 0:
                error_handlers = [f"{count} error handling patterns found"]

        return error_handlers[:10]

    def _extract_todos_pattern(self, content: str) -> List[str]:
        """Extract TODO/FIXME comments"""
        todos = []
        lines = content.split('\n')

        for i, line in enumerate(lines, 1):
            if re.search(r'(//|#|/\*)\s*(TODO|FIXME|XXX|HACK):', line, re.IGNORECASE):
                todos.append(f"Line {i}: {line.strip()}")

                if len(todos) >= 10:
                    break

        return todos
