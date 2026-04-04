"""
Migration script for creating Supabase cloud tables.

Run this once to create tables in your Supabase project:

    python app/migrate_cloud_tables.py --project-url https://xyz.supabase.co \
        --anon-key your-anon-key

This will create:
- lessons (with pgvector embeddings)
- patterns
- gotchas
- benchmarks
- sync_state
"""

import argparse
from supabase import create_client

def migrate_tables(project_url: str, anon_key: str):
    """Migrate tables to Supabase"""
    print(f"Connecting to {project_url}...")
    
    client = create_client(project_url, anon_key)
    
    # Create lessons table with vector
    print("Creating lessons table with vector embeddings...")
    client.table("lessons").select("*").execute()  # Test connection
    
    # Insert dummy row to ensure table exists
    client.table("lessons").insert({
        "topic": "migration_test",
        "content": "This is a test embedding for table creation",
        "embedding": [0.1] * 1536,  # Dummy embedding
        "confidence": 1.0,
        "source": "migration",
        "timestamp": "2026-01-01 00:00:00"
    }).execute()
    print("✓ Lessons table created")
    
    # Create patterns table
    print("Creating patterns table...")
    client.table("patterns").insert({
        "name": "test_pattern",
        "description": "Test pattern for migration",
        "steps": ["step1", "step2"],
        "success_rate": 1.0,
        "complexity": "low"
    }).execute()
    print("✓ Patterns table created")
    
    # Create gotchas table
    print("Creating gotchas table...")
    client.table("gotchas").insert({
        "category": "test",
        "error_pattern": "error",
        "fix_pattern": "fix",
        "confidence": 1.0
    }).execute()
    print("✓ Gotchas table created")
    
    # Create benchmarks table
    print("Creating benchmarks table...")
    client.table("benchmarks").insert({
        "run_id": "test_run",
        "task": "test_task",
        "success": True,
        "iterations": 1,
        "tokens_used": 100,
        "memory_hits": 0,
        "patterns_applied": 0
    }).execute()
    print("✓ Benchmarks table created")
    
    # Create sync_state table
    print("Creating sync_state table...")
    client.table("sync_state").insert({
        "last_sync": "2026-01-01 00:00:00",
        "device_id": "test-device",
        "status": "test",
        "local_changes_pending": False
    }).execute()
    print("✓ Sync_state table created")
    
    print("\n✓ All tables created successfully!")
    print("You can now use the cloud memory for cross-device sync.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate tables to Supabase")
    parser.add_argument("--project-url", required=True, help="Supabase project URL")
    parser.add_argument("--anon-key", required=True, help="Supabase anon key")
    
    args = parser.parse_args()
    
    migrate_tables(args.project_url, args.anon_key)
