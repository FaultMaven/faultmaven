# Phase Handlers Tests - OBSOLETE

## Status: OBSOLETE (2026-01-08)

These tests reference `faultmaven.services.agentic.phase_handlers.*` which was removed during module extraction.

## Files Renamed

All `test_*.py` files have been renamed to `OBSOLETE_test_*.py` to exclude them from pytest collection:

- OBSOLETE_test_blast_radius_handler.py
- OBSOLETE_test_document_handler.py
- OBSOLETE_test_hypothesis_handler.py
- OBSOLETE_test_intake_handler.py
- OBSOLETE_test_solution_handler.py
- OBSOLETE_test_timeline_handler.py
- OBSOLETE_test_validation_handler.py

## Next Steps

These tests should either be:
1. **Deleted** - if the functionality no longer exists
2. **Rewritten** - if equivalent functionality exists in the new module structure (likely in `faultmaven.modules.agent.*`)
3. **Archived** - moved to a separate archive directory for reference

## Related

- Module extraction: auth, agent, case, knowledge modules
- Phase handlers moved/deleted during architectural refactoring
