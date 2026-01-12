# Datetime Standard for FaultMaven

**Status:** ✅ ENFORCED
**Last Updated:** 2025-10-24

---

## TL;DR

**ALWAYS use timezone-aware datetimes in UTC:**

```python
from datetime import datetime, timezone

# ✅ CORRECT
timestamp = datetime.now(timezone.utc)

# ❌ WRONG - Never use these
timestamp = datetime.now()      # Naive datetime
timestamp = datetime.utcnow()   # Deprecated in Python 3.12+
```

---

## The Standard

### 1. **ALWAYS Use Timezone-Aware Datetimes**

All datetime objects in FaultMaven **MUST** be timezone-aware and in UTC.

**Why?**
- Prevents timezone-aware/naive datetime comparison errors
- Ensures consistent behavior across deployments
- Follows Python 3.12+ best practices (utcnow() is deprecated)
- Enables proper ISO 8601 serialization

### 2. **Creating Datetimes**

**For current timestamp:**
```python
from datetime import datetime, timezone

now = datetime.now(timezone.utc)
```

**In Pydantic models:**
```python
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class MyModel(BaseModel):
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Creation timestamp"
    )
```

### 3. **Serialization to JSON/API**

**Use the utility function:**
```python
from faultmaven.utils.serialization import to_json_compatible

# Converts datetime to ISO 8601 string with timezone
json_timestamp = to_json_compatible(datetime.now(timezone.utc))
# Result: "2025-10-24T12:30:00+00:00"
```

**Pydantic model serialization:**
```python
from pydantic import BaseModel
from datetime import datetime

class MyModel(BaseModel):
    created_at: datetime

    class Config:
        json_encoders = {datetime: lambda v: to_json_compatible(v)}
```

### 4. **Parsing Datetimes from Strings**

**Use the utility function:**
```python
from faultmaven.models import parse_utc_timestamp

# Handles multiple formats:
# - "2025-10-24T12:30:00+00:00" (timezone-aware)
# - "2025-10-24T12:30:00Z" (Zulu time)
# - "2025-10-24T12:30:00" (naive, assumes UTC)

dt = parse_utc_timestamp("2025-10-24T12:30:00+00:00")
# Result: datetime(2025, 10, 24, 12, 30, 0, tzinfo=timezone.utc)
```

---

## Utilities

### Available Helper Functions

| Function | Location | Purpose |
|----------|----------|---------|
| `datetime.now(timezone.utc)` | `datetime` | Create current UTC timestamp |
| `to_json_compatible()` | `faultmaven.utils.serialization` | Serialize datetime to ISO 8601 |
| `utc_timestamp()` | `faultmaven.models.common` | Generate UTC timestamp string |
| `parse_utc_timestamp()` | `faultmaven.models.common` | Parse ISO string to datetime |

### to_json_compatible() Behavior

```python
from faultmaven.utils.serialization import to_json_compatible
from datetime import datetime, timezone

# Timezone-aware → ISO with offset
dt = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
to_json_compatible(dt)  # "2025-01-01T12:00:00+00:00"

# Timezone-naive → ISO with 'Z' (assumes UTC)
dt = datetime(2025, 1, 1, 12, 0, 0)
to_json_compatible(dt)  # "2025-01-01T12:00:00Z"
```

### parse_utc_timestamp() Flexibility

```python
from faultmaven.models import parse_utc_timestamp

# All of these work:
parse_utc_timestamp("2025-10-24T12:30:00+00:00")  # Standard
parse_utc_timestamp("2025-10-24T12:30:00Z")       # Zulu time
parse_utc_timestamp("2025-10-24T12:30:00")        # Naive (assumes UTC)

# All return: timezone-aware datetime in UTC
```

---

## Common Patterns

### Pattern 1: Model Timestamps

```python
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from faultmaven.utils.serialization import to_json_compatible

class Case(BaseModel):
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Case creation time"
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last update time"
    )

    class Config:
        json_encoders = {datetime: lambda v: to_json_compatible(v)}
```

### Pattern 2: Updating Timestamps

```python
from datetime import datetime, timezone

# Update timestamp
case.updated_at = datetime.now(timezone.utc)
```

### Pattern 3: Time Comparisons

```python
from datetime import datetime, timezone, timedelta

# Calculate time differences
now = datetime.now(timezone.utc)
age = now - case.created_at  # Works because both are timezone-aware

# Check if recent
is_recent = age < timedelta(hours=1)
```

### Pattern 4: Database Storage

```python
from datetime import datetime, timezone

# Store
timestamp = datetime.now(timezone.utc)
await db.execute("INSERT INTO table (created_at) VALUES (?)", (timestamp,))

# Retrieve (may need parsing depending on DB driver)
from faultmaven.models import parse_utc_timestamp
row = await db.fetchone("SELECT created_at FROM table WHERE id=?", (id,))
created_at = parse_utc_timestamp(row['created_at'])
```

---

## What NOT to Do

### ❌ NEVER Use Naive Datetimes

```python
# ❌ WRONG - timezone-naive
now = datetime.now()

# ❌ WRONG - deprecated in Python 3.12+
now = datetime.utcnow()

# ✅ CORRECT
now = datetime.now(timezone.utc)
```

### ❌ NEVER Mix Aware and Naive

```python
# ❌ WRONG - will raise TypeError
naive = datetime.now()
aware = datetime.now(timezone.utc)
diff = aware - naive  # TypeError: can't subtract offset-naive and offset-aware

# ✅ CORRECT - both timezone-aware
aware1 = datetime.now(timezone.utc)
aware2 = datetime.now(timezone.utc)
diff = aware2 - aware1  # Works
```

### ❌ NEVER Forget the Import

```python
# ❌ WRONG - NameError: name 'timezone' is not defined
from datetime import datetime
now = datetime.now(timezone.utc)

# ✅ CORRECT
from datetime import datetime, timezone
now = datetime.now(timezone.utc)
```

---

## Verification

### Code Review Checklist

When reviewing code with datetime usage:

- [ ] All `datetime.now()` calls include `timezone.utc`
- [ ] No `datetime.utcnow()` usage (deprecated)
- [ ] Imports include both `datetime` and `timezone`
- [ ] Pydantic models use `default_factory=lambda: datetime.now(timezone.utc)`
- [ ] JSON serialization uses `to_json_compatible()` or proper Config
- [ ] Parsing uses `parse_utc_timestamp()` for consistency

### Automated Check

Run this to verify codebase compliance:

```bash
# Check for naive datetime.now()
grep -r "datetime.now()" faultmaven --include="*.py" | grep -v "timezone.utc"

# Check for deprecated utcnow()
grep -r "datetime.utcnow()" faultmaven --include="*.py"

# Both should return no results
```

---

## Why This Matters

### The Problem This Solves

**Before (inconsistent):**
```python
# File A
created = datetime.now()  # Naive

# File B
updated = datetime.now(timezone.utc)  # Aware

# Later...
age = updated - created  # TypeError: can't subtract!
```

**After (consistent):**
```python
# File A
created = datetime.now(timezone.utc)  # Aware

# File B
updated = datetime.now(timezone.utc)  # Aware

# Later...
age = updated - created  # Works perfectly!
```

### Real-World Impact

1. **API Responses**: Consistent ISO 8601 timestamps with timezone info
2. **Database Queries**: Proper timezone handling in WHERE clauses
3. **Time Comparisons**: No TypeError when calculating time differences
4. **Debugging**: Clear timezone context in all timestamps
5. **International**: Works correctly regardless of server timezone

---

## Current Status

**As of 2025-10-24:**

✅ **100% Compliant**
- All datetime usage follows the standard
- No naive datetime.now() found
- No deprecated utcnow() found
- All timezone imports present where needed

**Enforcement:**
- Fixed in container.py (2025-10-24)
- Standard documented
- Automated checks available

---

## References

- **Serialization Utility**: `faultmaven/utils/serialization.py`
- **Parsing Utility**: `faultmaven/models/common.py`
- **Python Docs**: [datetime.timezone](https://docs.python.org/3/library/datetime.html#timezone-objects)
- **PEP 615**: [IANA Time Zone Database](https://peps.python.org/pep-0615/)

---

**This is the authoritative datetime standard for FaultMaven. All datetime usage must comply with this document.**
