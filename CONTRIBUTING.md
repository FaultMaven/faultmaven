# Contributing to FaultMaven

Thank you for your interest in contributing to FaultMaven! 

For detailed contribution guidelines, please see our [Contributing Documentation](docs/CONTRIBUTING.md).

## Quick Start

1. Fork and clone the repository
2. Set up your development environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   pip install -r requirements-test.txt
   ```
3. Run tests to ensure everything works:
   ```bash
   pytest
   ```
4. Make your changes and submit a pull request

## Code Quality

Before submitting a pull request, please ensure:
- All tests pass: `pytest`
- Code is formatted: `black faultmaven/ tests/`
- Imports are sorted: `isort faultmaven/ tests/`
- No linting errors: `flake8 faultmaven/`

For more details, see [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).
