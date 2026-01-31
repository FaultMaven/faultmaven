# SQLModel Analysis: Should We Use It?

**Date**: 2026-01-10  
**Context**: Evaluating SQLModel for unifying Pydantic models and database schema  
**Related**: TD-001 Report Storage Migration, CaseRepository implementations

---

## Executive Summary

**Short Answer**: SQLModel is **not recommended** for this codebase at this time. The current raw SQL approach provides better performance, flexibility, and aligns with existing architecture patterns. However, SQLModel could be valuable for **new, simpler modules** or **greenfield features**.

**Recommendation**: **Keep current approach** (Pydantic models + raw SQL) for complex repositories. Consider SQLModel for new simple CRUD modules only.

---

## Current Architecture Analysis

### What You Have Now

**Pattern**: Pydantic models + Raw SQL queries with manual mapping

```python
# Domain Model (Pydantic)
class CaseReport(BaseModel):
    report_id: str
    case_id: str
    report_type: ReportType
    title: str
    content: str
    generated_at: str  # ISO 8601
    updated_at: str    # ISO 8601
    # ... domain logic here

# Repository (Raw SQL)
class PostgreSQLHybridCaseRepository:
    async def add_report(self, report: CaseReport) -> CaseReport:
        query = text("""
            INSERT INTO reports (
                report_id, case_id, report_type, version,
                title, content, generated_at, updated_at
            ) VALUES (
                :report_id, :case_id, :report_type, :version,
                :title, :content, :generated_at::timestamptz, :updated_at::timestamptz
            )
        """)
        await self.db.execute(query, {...})
        
    def _row_to_report(self, row) -> CaseReport:
        # Manual mapping from DB row to Pydantic model
        return CaseReport(...)
```

**Characteristics**:
- ✅ **Complex Queries**: JSONB aggregations, custom WHERE clauses, full-text search
- ✅ **Performance-First**: Hand-tuned SQL with specific indexes
- ✅ **Flexibility**: Easy to optimize queries without ORM overhead
- ✅ **Hybrid Schema**: Mix of normalized tables + JSONB columns
- ❌ **Boilerplate**: Manual row-to-model mapping (`_row_to_report`)
- ❌ **Type Safety**: Less compile-time checking of queries

---

## SQLModel Approach

### What SQLModel Would Look Like

```python
# Single definition (Pydantic + SQLAlchemy ORM)
from sqlmodel import SQLModel, Field, Relationship
from typing import Optional, List

class CaseReport(SQLModel, table=True):
    __tablename__ = "reports"
    
    report_id: str = Field(primary_key=True)
    case_id: str = Field(foreign_key="cases.case_id")
    report_type: str
    version: int = Field(default=1)
    is_current: bool = Field(default=True)
    title: str = Field(max_length=200)
    content: str
    format: str = Field(default="markdown")
    generation_status: str
    generation_time_ms: int
    generated_at: datetime
    updated_at: datetime
    metadata: Optional[dict] = Field(default=None, sa_column=Column(JSONB))
    
    # Relationships
    case: Optional["Case"] = Relationship(back_populates="reports")

# Repository becomes simpler
class PostgreSQLHybridCaseRepository:
    async def add_report(self, report: CaseReport) -> CaseReport:
        self.db.add(report)
        await self.db.commit()
        await self.db.refresh(report)
        return report
    
    async def get_report(self, report_id: str) -> Optional[CaseReport]:
        return await self.db.get(CaseReport, report_id)
```

**Characteristics**:
- ✅ **Single Source of Truth**: One class = schema + model
- ✅ **Less Boilerplate**: No manual mapping needed
- ✅ **Type Safety**: Better IDE support, query validation
- ✅ **Automatic Migrations**: Alembic can generate from models
- ❌ **ORM Overhead**: Less control over generated SQL
- ❌ **Complex Queries**: Harder to express JSONB aggregations
- ❌ **Performance**: May generate suboptimal SQL for complex cases

---

## Detailed Comparison

### 1. **Complex Query Support**

#### Current Approach (Raw SQL)
```python
# Complex JSONB aggregation - works great
query = text("""
    SELECT
        c.*,
        COALESCE(
            json_agg(DISTINCT jsonb_build_object(
                'evidence_id', e.evidence_id,
                'category', e.category,
                ...
            )) FILTER (WHERE e.evidence_id IS NOT NULL),
            '[]'::json
        ) as evidence_data
    FROM cases c
    LEFT JOIN evidence e ON c.case_id = e.case_id
    WHERE c.case_id = :case_id
    GROUP BY c.case_id
""")
```
**Pros**: Full control, exact SQL, optimized  
**Cons**: Manual string construction, no type checking

#### SQLModel Approach
```python
# Would need to fall back to raw SQL anyway
result = await self.db.execute(
    select(Case)
    .join(Evidence)  # Can't easily do JSONB aggregation
    .where(Case.case_id == case_id)
)
# Still need manual JSONB aggregation - can't use ORM here
```
**Reality**: For complex queries, you'd still use `text()` queries, defeating the purpose.

**Verdict**: **Raw SQL wins** for your complex queries.

---

### 2. **Simple CRUD Operations**

#### Current Approach
```python
async def add_report(self, report: CaseReport) -> CaseReport:
    query = text("""
        INSERT INTO reports (...) VALUES (...)
    """)
    await self.db.execute(query, {
        "report_id": report.report_id,
        "case_id": report.case_id,
        ...
    })
    return report

def _row_to_report(self, row) -> CaseReport:
    return CaseReport(
        report_id=row.report_id,
        case_id=row.case_id,
        ...
    )
```
**Lines of Code**: ~30 lines per method (query + mapping)

#### SQLModel Approach
```python
async def add_report(self, report: CaseReport) -> CaseReport:
    self.db.add(report)
    await self.db.commit()
    await self.db.refresh(report)
    return report
```
**Lines of Code**: ~5 lines per method

**Verdict**: **SQLModel wins** for simple CRUD (50-80% less code).

---

### 3. **Hybrid Schema (Normalized + JSONB)**

#### Current Approach
```python
# JSONB columns stored as-is, accessed via PostgreSQL operators
query = text("""
    SELECT * FROM cases
    WHERE (progress->>'symptom_verified')::boolean = TRUE
      AND (inquiry->>'quick_suggestions') @> '["Check logs"]'::jsonb
""")
```
**Pros**: Native PostgreSQL JSONB operators, flexible queries  
**Cons**: String-based query construction

#### SQLModel Approach
```python
# SQLModel can handle JSONB, but queries become verbose
result = await self.db.execute(
    select(Case)
    .where(
        Case.progress['symptom_verified'].astext.cast(Boolean) == True
    )
    .where(
        Case.inquiry['quick_suggestions'].contains(['Check logs'])
    )
)
```
**Reality**: Works, but loses readability. For complex JSONB queries, you'd still use raw SQL.

**Verdict**: **Tie** - both work, but raw SQL is more readable for complex JSONB.

---

### 4. **Type Safety & IDE Support**

#### Current Approach
```python
# No compile-time checking
query = text("SELECT * FROM reports WHERE report_id = :report_id")
result = await self.db.execute(query, {"report_id": report_id})
row = result.fetchone()
report = self._row_to_report(row)  # Manual mapping
```
**Issues**: 
- Typos in column names only found at runtime
- Type mismatches discovered during execution
- No autocomplete for query fields

#### SQLModel Approach
```python
# Compile-time checking, autocomplete
report = await self.db.get(CaseReport, report_id)
report.title  # IDE knows this exists, type is str
```
**Pros**: Type checking, autocomplete, refactoring support  
**Cons**: ORM overhead for complex queries

**Verdict**: **SQLModel wins** for type safety on simple operations.

---

### 5. **Performance**

#### Current Approach
```python
# Exact SQL you write - optimized by you
query = text("""
    SELECT * FROM reports
    WHERE case_id = :case_id
      AND report_type = :report_type
      AND is_current = TRUE
""")
# Uses index: idx_reports_current_unique
```
**Performance**: Excellent - you control the exact query

#### SQLModel Approach
```python
# ORM generates SQL - may be suboptimal
reports = await self.db.execute(
    select(CaseReport)
    .where(CaseReport.case_id == case_id)
    .where(CaseReport.report_type == report_type)
    .where(CaseReport.is_current == True)
)
# Might generate: SELECT * FROM reports WHERE ... (less efficient)
```
**Performance**: Good for simple queries, may degrade for complex ones

**Verdict**: **Raw SQL wins** for performance-critical queries.

---

### 6. **Migration & Schema Management**

#### Current Approach
```python
# Manual SQL migration scripts
# docs/schema/005_add_reports_table.sql
CREATE TABLE reports (
    report_id UUID PRIMARY KEY,
    ...
);
```
**Pros**: Full control, explicit schema  
**Cons**: Manual maintenance, no auto-generation

#### SQLModel Approach
```python
# Alembic can auto-generate migrations from models
alembic revision --autogenerate -m "Add reports table"
```
**Pros**: Auto-generated migrations, schema in code  
**Cons**: May miss edge cases, less explicit

**Verdict**: **SQLModel wins** for schema management (with caveats).

---

## Real-World Assessment

### Your Codebase Complexity

**Current Repository Queries**:
1. **Complex JOINs**: 4-way LEFT JOINs with aggregations
2. **JSONB Operations**: `json_agg`, `jsonb_build_object`, `FILTER` clauses
3. **Full-Text Search**: `to_tsvector`, `ts_rank`, custom relevance
4. **Performance-Critical**: Hand-tuned indexes, specific query patterns
5. **Custom Aggregations**: Building nested JSON structures from relational data

**SQLModel Fit**: ❌ **Poor** - You'd still need raw SQL for most operations.

### Migration Cost

**To Adopt SQLModel**:
1. Refactor all repository implementations (high risk)
2. Convert complex queries to ORM (may lose performance)
3. Keep raw SQL fallbacks for complex cases (defeats purpose)
4. Update all Pydantic models to SQLModel (breaking change)
5. Retest all queries for performance regressions

**Estimated Effort**: 2-3 weeks + risk of performance issues

**ROI**: Low - You'd still use raw SQL for your complex queries.

---

## When SQLModel Makes Sense

### ✅ Good Fit For:

1. **Simple CRUD Modules**
   - Auth module (users, sessions) - straightforward tables
   - Simple lookup tables
   - New features with basic CRUD

2. **Greenfield Features**
   - New modules without existing complex queries
   - Features where performance isn't critical
   - Rapid prototyping

3. **Team Preferences**
   - Team prefers ORM-style development
   - Heavy reliance on auto-generated migrations
   - Less SQL expertise in team

### ❌ Poor Fit For:

1. **Your Current Case Repository**
   - Complex JSONB aggregations
   - Performance-critical queries
   - Hand-tuned SQL with specific indexes
   - Hybrid normalized + JSONB schema

2. **Established Codebase**
   - Already working well
   - Performance requirements met
   - Complex query patterns established

---

## Hybrid Approach (Recommended)

**Best of Both Worlds**: Use SQLModel for new simple modules, keep raw SQL for complex ones.

### Strategy

```python
# Simple modules: Use SQLModel
# modules/auth/infrastructure/user_repository.py
from sqlmodel import SQLModel, Field

class User(SQLModel, table=True):
    user_id: str = Field(primary_key=True)
    email: str
    # ... simple CRUD

# Complex modules: Keep raw SQL
# modules/case/infrastructure/postgresql_hybrid_case_repository.py
# Keep current approach - raw SQL for complex queries
```

### Benefits
- ✅ SQLModel for simple CRUD (less boilerplate)
- ✅ Raw SQL for complex queries (performance, flexibility)
- ✅ Gradual adoption (new modules only)
- ✅ No breaking changes to existing code

---

## Recommendation

### For FaultMaven Codebase: **Don't Migrate to SQLModel**

**Reasons**:
1. **Complex Queries Dominate**: Your Case repository uses complex SQL that SQLModel can't handle well
2. **Performance Matters**: Hand-tuned queries are already optimized
3. **Working Solution**: Current approach is functional and maintainable
4. **High Migration Cost**: Would require refactoring all repositories
5. **Diminishing Returns**: You'd still use raw SQL for the complex parts

### When to Revisit SQLModel

**Consider SQLModel If**:
- ✅ You add new simple modules (e.g., notifications, preferences)
- ✅ Performance requirements relax
- ✅ Team struggles with raw SQL maintenance
- ✅ You want to standardize on one approach for new code

### Alternative: Incremental Improvement

Instead of SQLModel, consider:

1. **Type-Safe Query Builders** (if available for async SQLAlchemy)
   ```python
   # More type-safe than text(), but still raw SQL
   from sqlalchemy import select, func
   query = select(CaseReport).where(CaseReport.case_id == case_id)
   ```

2. **Better Mapping Utilities**
   ```python
   # Reduce boilerplate in _row_to_report
   def map_row_to_model(row, model_class):
       return model_class(**{k: getattr(row, k) for k in row.keys()})
   ```

3. **SQL Query Templates** (if needed)
   ```python
   # Reusable query fragments
   EVIDENCE_AGGREGATION = """
       COALESCE(json_agg(...) FILTER (WHERE ...), '[]'::json) as evidence_data
   """
   ```

---

## Conclusion

**SQLModel is over-engineering for your current needs** because:

1. ❌ **Most of your queries are complex** - You'd still use raw SQL
2. ❌ **Performance is critical** - Hand-tuned queries are better
3. ❌ **High migration cost** - Low ROI for refactoring
4. ❌ **Working solution** - Current approach is maintainable

**However**, SQLModel would be valuable for:
- ✅ New simple modules (auth, notifications)
- ✅ Greenfield features with basic CRUD
- ✅ Future refactoring if complexity reduces

**Recommended Action**: **Keep current approach**. Consider SQLModel only for new simple modules where it adds clear value.

---

## Quick Decision Matrix

| Criteria | Raw SQL (Current) | SQLModel | Winner |
|----------|------------------|----------|--------|
| **Complex Queries** | ✅ Excellent | ❌ Poor (needs raw SQL fallback) | Raw SQL |
| **Simple CRUD** | ⚠️ Verbose | ✅ Concise | SQLModel |
| **Performance** | ✅ Full control | ⚠️ ORM overhead | Raw SQL |
| **Type Safety** | ❌ Runtime errors | ✅ Compile-time | SQLModel |
| **Migration Cost** | ✅ None (already working) | ❌ High (refactor all) | Raw SQL |
| **Schema Management** | ⚠️ Manual SQL | ✅ Auto-migrations | SQLModel |
| **JSONB Support** | ✅ Native PostgreSQL | ⚠️ Verbose ORM syntax | Raw SQL |
| **Team Expertise** | ✅ SQL expertise | ⚠️ ORM learning curve | Depends |

**Overall**: Raw SQL (Current) wins 5-3, with SQLModel better for simple cases.

**Verdict**: **Don't migrate**. Use SQLModel selectively for new simple modules only.

---

## References

- SQLModel Documentation: https://sqlmodel.tiangolo.com/
- FastAPI + SQLModel Guide: https://fastapi.tiangolo.com/tutorial/sql-databases/
- Your Current Implementation: `postgresql_hybrid_case_repository.py`
