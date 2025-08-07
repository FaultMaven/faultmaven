# Architecture Tests

This directory contains architecture-specific tests and validation scripts.

## Purpose

The `tests/architecture/` directory holds tests that validate:

- Clean architecture boundaries and layer separation
- Interface compliance and implementation correctness
- Dependency injection patterns
- Architectural constraints and design principles

## Structure

- **Boundary Tests**: Validate layer separation (API → Service → Core → Infrastructure)
- **Interface Tests**: Ensure components implement required interfaces
- **Dependency Tests**: Validate dependency injection and container behavior
- **Integration Tests**: Test architectural integration patterns

## Key Principles Tested

1. **Separation of Concerns**: Each layer has distinct responsibilities
2. **Dependency Inversion**: High-level modules don't depend on low-level details
3. **Interface Segregation**: Components depend on abstractions, not concretions
4. **Single Responsibility**: Each component has one reason to change
5. **Open/Closed**: Open for extension, closed for modification

## Test Categories

- `unit/` - Individual component architecture tests
- `integration/` - Cross-layer architecture validation
- `compliance/` - Interface and contract compliance tests
- `boundaries/` - Layer boundary and import validation tests

This ensures that architectural decisions are tested and validated automatically.