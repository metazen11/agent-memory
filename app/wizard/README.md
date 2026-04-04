# Memory Setup Wizard

The interactive wizard for setting up Agent-Memory with different storage options.

## Quick Start

Run the wizard:

```bash
cd /Users/mz/Dropbox/_CODING/agentMemory
python app/wizard
```

## Setup Options

### 1. Local SQLite (Simplest)
- **No server required**
- **Single device only**
- **File-based storage**
- **Best for: Development, testing, single device**

```python
from agentMemory.db import db
db.add_lesson("lesson1", "content")
```

### 2. Local PostgreSQL (Advanced)
- **Local server**
- **Better performance**
- **Single device**
- **Best for: Production, large datasets**

```python
from agentMemory.db import db
# Connect to PostgreSQL
db = PostgreSQLMemory(host="localhost")
db.add_lesson("lesson1", "content")
```

### 3. Cloud Supabase (Cross-Device) ⭐ RECOMMENDED
- **Persistent across devices**
- **Automatic sync**
- **Vector search**
- **Best for: Multi-device, collaborative work**

```bash
# Run wizard to setup
python app/wizard
```

Then use:

```python
from agentMemory.sync_agent import MemorySyncAgent

# Create sync agent
sync_agent = MemorySyncAgent(
    project_url="https://your-project.supabase.co",
    anon_key="your-anon-key",
    device_id="my-device-123"
)

# Sync to cloud
sync_agent.sync_to_cloud()

# Use memory
memory = MemorySyncAgent(project_url, anon_key)
memory.add_lesson("lesson1", "content")
```

## Cross-Device Sync

With Supabase, your memory automatically syncs across all devices:

```
Device A (Mac)     Device B (Linux)     Device C (Windows)
   ↓                     ↓                     ↓
┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐
│ Local SQLite      │ │ Local SQLite      │ │ Local SQLite      │
│ (SQLite memory.db)│ │ (SQLite memory.db)│ │ (SQLite memory.db)│
└────────┬──────────┘ └────────┬──────────┘ └────────┬──────────┘
         ↓                     ↓                     ↓
    ┌─────────────────────────────────────────────────┐
    │              Supabase Cloud Storage              │
    │  (Shared lessons, patterns, gotchas)            │
    └─────────────────────────────────────────────────┘
```

## Configuration

Create `.env` file:

```bash
SUPABASE_PROJECT_URL=https://xyz.supabase.co
SUPABASE_ANON_KEY=your-anon-key
```

Or pass parameters directly:

```python
sync_agent = MemorySyncAgent(
    project_url="https://xyz.supabase.co",
    anon_key="your-anon-key",
    device_id="my-device"
)
```

## Usage Examples

### Add lesson
```python
memory.add_lesson("always run tests", "Always run tests after modifications")
```

### Search
```python
memory.search("test")
```

### Add pattern
```python
memory.add_pattern(
    name="fix-auth",
    description="Fix authentication",
    steps=["read file", "edit code", "run tests"]
)
```

### Get benchmarks
```python
memory.get_benchmarks()
```

## Sync Loop

For automatic sync:

```python
sync_agent.run_sync_loop()  # Syncs every 5 minutes
```

## Troubleshooting

### Connection failed
```
Error: Could not connect to Supabase
```

**Solution:**
1. Check URL is correct
2. Verify anon key
3. Restart Supabase project

### Tables not found
```
Error: Table doesn't exist
```

**Solution:**
Run migration:
```bash
python app/migrate_cloud_tables.py \
  --project-url https://xyz.supabase.co \
  --anon-key your-anon-key
```

## License

MIT License - See LICENSE file
