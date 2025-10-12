# Documentation Reorganization - Execution Checklist

**Date**: 2025-10-11  
**Estimated Time**: 2.5-3.5 hours

---

## ✅ Quick Summary

**Goal**: Clean project structure with all docs in `docs/`, temporary files in `_temp/`

**Files Affected**:
- ✅ **Keep & Organize**: ~12 permanent docs
- 🗑️ **Move to _temp/**: ~15 temporary/obsolete files
- ⏱️ **Delete _temp/** after 1-2 weeks

---

## Execution Steps

### ☐ Step 1: Create Directories (5 minutes)
```bash
cd /home/swhouse/projects/FaultMaven

# New permanent directories
mkdir -p docs/getting-started
mkdir -p docs/architecture/diagrams
mkdir -p docs/architecture/decisions
mkdir -p docs/architecture/legacy

# Temporary directories (for review)
mkdir -p _temp/root-level-docs
mkdir -p _temp/loose-docs
mkdir -p _temp/duplicates
```

---

### ☐ Step 2: Clean Project Root (10 minutes)

#### Move PERMANENT docs from root to organized locations:
```bash
# Architecture diagrams
mv architecture-diagram.md docs/architecture/diagrams/system-architecture.md
mv faultmaven/ARCHITECTURE_DIAGRAM.md docs/architecture/diagrams/system-architecture-code.md
mv faultmaven/ARCHITECTURE_DIAGRAM.mmd docs/architecture/diagrams/system-architecture.mmd
```

#### Move TEMPORARY/OBSOLETE docs from root to _temp/:
```bash
# Implementation status (temporary)
mv PHASE_0_AUDIT_REPORT.md _temp/root-level-docs/
mv PHASE_0_ENHANCEMENTS_SUMMARY.md _temp/root-level-docs/
mv IMPLEMENTATION_COMPLETE.md _temp/root-level-docs/
mv IMPLEMENTATION_PLAN.md _temp/root-level-docs/
mv IMPLEMENTATION_README.md _temp/root-level-docs/
mv DOCTOR_PATIENT_IMPLEMENTATION_SUMMARY.md _temp/root-level-docs/
mv FRONTEND_DATA_UPLOAD_IMPLEMENTATION_REQUEST.md _temp/root-level-docs/

# AI notes and legacy (temporary)
mv CLAUDE.md _temp/root-level-docs/
mv MICROSERVICES_ARCHITECTURE.md _temp/root-level-docs/
mv TECHNICAL_SPECIFICATIONS.md _temp/root-level-docs/
```

**Verify**: `ls -1 *.md` should show only `README.md`

---

### ☐ Step 3: Organize Loose docs/ Files (10 minutes)

```bash
cd docs/

# PERMANENT - Move to organized locations
mv ARCHITECTURE_DECISION_GUIDE.md architecture/decisions/architecture-decision-guide.md
mv KNOWLEDGE_BASE_SYSTEM.md specifications/knowledge-base-system.md
mv how-to-add-providers.md development/
mv opik-setup.md infrastructure/
mv SCHEMA_ALIGNMENT.md api/schema-alignment.md
mv LOGGING_POLICY.md logging/logging-policy.md
mv USER_GUIDE.md getting-started/user-guide.md

# TEMPORARY - Move to _temp/
mv FLAGS_AND_CONFIG.md _temp/loose-docs/
mv TECHNICAL_DEBT.md _temp/loose-docs/
mv FUTURE_ENHANCEMENTS.md _temp/loose-docs/
```

---

### ☐ Step 4: Remove Docs from Code/Test Directories (5 minutes)

```bash
cd /home/swhouse/projects/FaultMaven

# Move test docs to proper location
mv tests/ARCHITECTURE_TESTING_GUIDE.md docs/testing/architecture-testing-guide.md
mv tests/NEW_TEST_PATTERNS.md docs/testing/new-test-patterns.md
```

**Verify**:
- `ls faultmaven/*.md` should return nothing
- `ls tests/*.md` should return nothing

---

### ☐ Step 5: Handle Duplicate Files (5 minutes)

```bash
cd docs/

# Keep only authoritative version (system-requirements-specification.md v2.0)
# Move duplicates to _temp/
mv FAULTMAVEN_SYSTEM_REQUIREMENTS.md _temp/duplicates/
mv faultmaven_system_requirements_v2.md _temp/duplicates/
```

---

### ☐ Step 6: Create Index Files (30 minutes)

Create README.md in new directories:

```bash
# docs/README.md - Master index
cat > docs/README.md << 'EOF'
# FaultMaven Documentation

Master index for all FaultMaven documentation.

## Quick Navigation

- 🚀 [Getting Started](./getting-started/) - Installation, quickstart, user guide
- 🏗️ [Architecture](./architecture/architecture-overview.md) - System architecture and design
- 📋 [Specifications](./specifications/) - Requirements and technical specifications
- 🔌 [API Documentation](./api/) - API contracts and integration guides
- 💻 [Development](./development/) - Developer guides and setup
- 🏗️ [Infrastructure](./infrastructure/) - Infrastructure setup and configuration
- 🧪 [Testing](./testing/) - Testing strategies and guides
- 📚 [Guides](./guides/) - How-to guides and tutorials

See [Architecture Overview](./architecture/architecture-overview.md) for complete documentation map.
EOF

# docs/getting-started/README.md
cat > docs/getting-started/README.md << 'EOF'
# Getting Started with FaultMaven

Quick start guides for new users and developers.

- [User Guide](./user-guide.md) - End-user documentation
EOF

# docs/architecture/diagrams/README.md
cat > docs/architecture/diagrams/README.md << 'EOF'
# FaultMaven Architecture Diagrams

Visual representations of FaultMaven's architecture.

- [System Architecture](./system-architecture.md) - High-level system design
- [System Architecture Mermaid](./system-architecture.mmd) - Mermaid diagram source
EOF

# docs/architecture/decisions/README.md
cat > docs/architecture/decisions/README.md << 'EOF'
# Architecture Decision Records (ADRs)

Documents significant architecture decisions and their rationale.

- [Architecture Decision Guide](./architecture-decision-guide.md) - Decision-making framework
EOF
```

---

### ☐ Step 7: Update Cross-References (1-2 hours)

**Search and replace** old paths in documentation:

1. Update `architecture-overview.md` if needed (already uses relative paths, should be fine)
2. Search for broken links:
   ```bash
   cd docs/
   grep -r "architecture-diagram.md" .
   grep -r "../architecture-diagram" .
   grep -r "CLAUDE.md" .
   ```
3. Fix any broken references found

---

### ☐ Step 8: Update Root README.md (10 minutes)

Update the main project README to point to new doc structure:

```markdown
## Documentation

📚 **Complete documentation is in [`docs/`](./docs/)**

Quick Links:
- 🚀 [Getting Started](./docs/getting-started/)
- 🏗️ [Architecture Overview](./docs/architecture/architecture-overview.md)
- 📋 [System Requirements](./docs/specifications/system-requirements-specification.md)
- 🔌 [API Documentation](./docs/api/)
- 💻 [Development Guide](./docs/development/)
- 🧪 [Testing Guide](./docs/testing/)
```

---

### ☐ Step 9: Test and Validate (30 minutes)

```bash
# 1. Check for broken links in markdown files
cd /home/swhouse/projects/FaultMaven/docs
find . -name "*.md" -type f | while read file; do
  echo "Checking $file..."
  grep -o '\[.*\](.*\.md)' "$file" || true
done

# 2. Verify no docs left in wrong places
ls ../*.md | grep -v README.md  # Should be empty
ls ../faultmaven/*.md           # Should be empty
ls ../tests/*.md                # Should be empty

# 3. Verify _temp/ structure
ls -la ../_temp/root-level-docs/  # Should have ~10 files
ls -la ../_temp/loose-docs/       # Should have ~3 files
ls -la ../_temp/duplicates/       # Should have ~2 files

# 4. Test navigation from docs/README.md
# Open in browser/markdown viewer and click through links
```

---

### ☐ Step 10: Commit Changes (5 minutes)

```bash
cd /home/swhouse/projects/FaultMaven

# Review changes
git status

# Add all changes
git add .

# Commit with descriptive message
git commit -m "docs: reorganize documentation structure

- Move all docs to docs/ directory
- Create getting-started/, diagrams/, decisions/, legacy/ subdirectories
- Move 15 temporary/obsolete files to _temp/ for later cleanup
- Consolidate duplicate system requirements
- Remove documentation from faultmaven/ and tests/ directories
- Create index README.md files for navigation
- Update root README.md with new doc structure

After 1-2 weeks, review _temp/ and delete if not needed.
"
```

---

### ☐ Step 11: Review _temp/ (1-2 weeks later)

```bash
# After documentation reorganization has been in use:

cd /home/swhouse/projects/FaultMaven

# 1. Review files one more time
ls _temp/root-level-docs/
ls _temp/loose-docs/
ls _temp/duplicates/

# 2. If confident nothing needed, delete
rm -rf _temp/

# 3. Commit the cleanup
git add .
git commit -m "docs: remove temporary files after review period"
```

---

## Validation Checklist

After Steps 1-10:

- [ ] Project root has only README.md and LICENSE (+ config files)
- [ ] No .md files in `faultmaven/` directory
- [ ] No .md files in `tests/` directory  
- [ ] All permanent docs organized in `docs/` subdirectories
- [ ] `_temp/` contains ~15 files for review
- [ ] All new directories have README.md index files
- [ ] Root README.md updated with new doc links
- [ ] No broken links in major documents
- [ ] Git commit successful

After Step 11 (1-2 weeks later):

- [ ] Reviewed all files in `_temp/`
- [ ] Confirmed nothing needed from `_temp/`
- [ ] Deleted `_temp/` directory
- [ ] Git commit successful

---

## Quick Stats

**Before**:
- Root-level docs: 10+
- Docs in code directories: 2
- Loose files in docs/: 10+
- **Total to reorganize**: ~27 files

**After**:
- Root-level docs: 0 (only README + LICENSE)
- Docs in code directories: 0
- Loose files in docs/: 0
- **Organized in subdirectories**: ~12 files
- **Moved to _temp/** (for later deletion): ~15 files

**Net Result**: Cleaner structure, easier navigation, professional appearance

---

**Status**: Ready to execute  
**Next**: Run Steps 1-10 (2.5-3.5 hours)

---

**End of Checklist**





