---
name: minimal-testing
description: Write focused tests for core user flows and critical paths with behavior-focused assertions and fast execution. Use this skill when creating or modifying test files, writing unit tests, integration tests, or test cases for any feature.
---

# Testing Writing Guidelines

**Core Philosophy**: Write minimal, focused tests for critical paths. Defer comprehensive testing until explicitly requested.

## Minimal Testing

**Do NOT write tests for:**
- Every intermediate step or code change
- Exhuastive input validataion
- Rare user scenarios
- Non-critical utility functions
- Secondary workflows or helper functions
- Edge cases unless business-critical
- Error states unless explicitly required
- Validation logic unless core to feature

**DO write tests for:**
- Primary user workflows (happy path)
- Critical business logic
- Core feature functionality
- Features at logical completion points

## Behavior Over Implementation

Test what the code does, not how it does it.

**Good - Tests behavior:**
```python
def test_discount_calculation_applies_percentage():
    result = calculate_discount(price=100, rate=0.2)
    assert result == 80
```

**Bad - Tests implementation:**
```python
def test_discount_uses_multiplication():
    # Don't test internal calculation method
    assert discount._multiply_called == True
```

## Common Mistakes to Avoid

**Over-testing during development:**
- Writing tests for every function
- Testing all edge cases upfront
- Testing implementation details
- Slowing down feature development

**Under-testing critical paths:**
- Skipping main user flow tests
- Not testing business logic
- Ignoring core feature validation

**Brittle tests:**
- Testing internal implementation
- Coupling tests to code structure
- Not mocking external dependencies

## Quick Reference

| Scenario                        | Action                         |
| ------------------------------- | ------------------------------ |
| Implementing new feature        | Test critical path only        |
| User requests edge case testing | Add those specific tests       |
| Non-critical utility function   | Skip testing unless requested  |
| Core business calculation       | Always test                    |
| Error handling                  | Defer unless business-critical |
| Integration point               | Test at completion             |
