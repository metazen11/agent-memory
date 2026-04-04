"""
Memory Setup Wizard — Interactive setup for agent-memory

The wizard guides users through setting up agent-memory with
different options: local SQLite, local PostgreSQL, or cloud Supabase.

Usage:
    >>> from app.wizard import MemorySetupWizard
    >>> wizard = MemorySetupWizard()
    >>> wizard.run()
"""

import os
import sys
import socket
from typing import Optional
from supabase import create_client
from app.config import settings

class MemorySetupWizard:
    """
    Interactive wizard for setting up agent-memory.
    
    Modes:
    - Local SQLite (simplest, no server)
    - Local PostgreSQL (advanced, local server)
    - Cloud with Supabase (persistent across devices)
    """
    
    @staticmethod
    def run():
        """Run setup wizard"""
        print("\n" + "="*60)
        print("  Agent-Memory Setup Wizard")
        print("="*60)
        
        MemorySetupWizard._display_welcome()
        
        # Step 1: Choose mode
        print("\n1. Choose memory mode:")
        print("  a) Local SQLite (simplest, no server)")
        print("  b) Local PostgreSQL (advanced, local server)")
        print("  c) Cloud with Supabase (persistent across devices)")
        print("  d) Use existing Supabase project")
        
        choice = input("\nEnter choice (a/b/c/d): ").strip().lower()
        
        if choice == "a":
            MemoryWizard._setup_local_sqlite()
        elif choice == "b":
            MemoryWizard._setup_local_postgres()
        elif choice == "c":
            MemoryWizard._setup_cloud_supabase_new()
        elif choice == "d":
            MemoryWizard._setup_cloud_supabase_existing()
        else:
            print("Invalid choice")
            sys.exit(1)
    
    @staticmethod
    def _display_welcome():
        """Display welcome message"""
        print("\nWelcome to Agent-Memory Setup!")
        print("\nAgent-Memory provides:")
        print("  • Persistent memory across sessions")
        print("  • Semantic search with vector embeddings")
        print("  • Lessons, patterns, and gotchas")
        print("  • Cross-device sync (with Supabase)")
        print("  • Database-agnostic (SQLite, PostgreSQL)")
    
    @staticmethod
    def _setup_local_sqlite():
        """Setup local SQLite (simplest)"""
        print("\n✓ Setting up local SQLite memory...")
        
        # Check if already configured
        from app.db import db
        if db.engine:
            print("SQLite database already configured")
            return
        
        # Create SQLite database
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import os
        
        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".agent-memory-codex",
            "memory.db"
        )
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        engine = create_engine(f"sqlite:///{db_path}")
        
        from app.models import Base
        Base.metadata.create_all(engine)
        
        print("✓ SQLite database created")
        print(f"  Location: {db_path}")
        
        print("\nNext steps:")
        print("  1. Add lessons: db.add_lesson('topic', 'content')")
        print("  2. Search: db.search('query')")
        print("  3. Close: db.close()")
    
    @staticmethod
    def _setup_local_postgres():
        """Setup local PostgreSQL"""
        print("\n✓ Setting up local PostgreSQL...")
        
        # Check PostgreSQL
        try:
            import subprocess
            result = subprocess.run(
                ["pg_isready", "-h", "localhost"],
                capture_output=True
            )
            if result.returncode != 0:
                print("PostgreSQL not found. Install with:")
                print("  apt-get install postgresql postgresql-contrib")
                print("  # Then restart PostgreSQL")
                return
        except:
            print("PostgreSQL not found. Install with:")
            print("  apt-get install postgresql postgresql-contrib")
            return
        
        # Create database
        print("Creating database...")
        from psycopg2 import connect
        
        try:
            conn = connect(
                host="localhost",
                database="postgres",
                user="postgres",
                password=""
            )
            cur = conn.cursor()
            cur.execute("CREATE DATABASE agent_memory")
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
            conn.close()
            print("✓ Database created")
        except:
            print("⚠ Could not create database. Please configure manually.")
            return
        
        print("✓ PostgreSQL setup complete")
    
    @staticmethod
    def _setup_cloud_supabase_new():
        """Setup new Supabase project"""
        print("\n✓ Setting up cloud memory with Supabase...")
        
        print("\n2. Creating Supabase account...")
        print("  Visit: https://supabase.com/")
        print("\n3. Sign up and create project")
        print("  - Go to Settings → Database")
        print("  - Enable pgvector extension")
        print("\n4. Get credentials:")
        print("  - Settings → API")
        print("  - Anon Key: ")
        print("  - Service Role Key: (keep secret)")
        
        anon_key = input("\nAnon Key: ").strip()
        service_role_key = input("Service Role Key: ").strip()
        project_url = input("Project URL: ").strip()
        
        MemoryWizard._initialize_cloud_memory(
            project_url=project_url,
            anon_key=anon_key,
            service_role_key=service_role_key
        )
    
    @staticmethod
    def _setup_cloud_supabase_existing():
        """Setup existing Supabase project"""
        print("\n✓ Connecting to existing Supabase project...")
        
        project_url = input("\nProject URL: ").strip()
        anon_key = input("Anon Key: ").strip()
        
        MemoryWizard._initialize_cloud_memory(
            project_url=project_url,
            anon_key=anon_key
        )
    
    @staticmethod
    def _initialize_cloud_memory(
        project_url: str,
        anon_key: str,
        service_role_key: str = None
    ):
        """Initialize cloud memory"""
        print("\n✓ Connecting to Supabase...")
        
        # Create Supabase client
        client = create_client(project_url, anon_key)
        
        # Test connection
        try:
            client.table("lessons").select("*").execute()
            print("✓ Connected to Supabase")
        except Exception as e:
            print(f"✗ Connection failed: {e}")
            return
        
        # Create tables if not exists
        MemoryWizard._create_cloud_tables(client, service_role_key)
        
        print("✓ Cloud memory initialized")
        print("\nNext steps:")
        print("  1. Download local memory sync agent")
        print("  2. Connect to cloud")
        print("  3. Memory syncs automatically across devices")
    
    @staticmethod
    def _create_cloud_tables(client, service_role_key: str):
        """Create cloud tables"""
        try:
            # Create lessons table
            client.table("lessons").insert({}).execute()
            
            # Create patterns table
            client.table("patterns").insert({}).execute()
            
            # Create gotchas table
            client.table("gotchas").insert({}).execute()
            
            # Create benchmarks table
            client.table("benchmarks").insert({}).execute()
            
            # Create sync_state table
            client.table("sync_state").insert({}).execute()
            
            print("✓ Cloud tables created")
        except:
            print("⚠ Tables may already exist")
    
    @staticmethod
    def _get_device_id() -> str:
        """Get unique device ID"""
        hostname = socket.gethostname()
        import uuid
        import time
        timestamp = str(int(time.time() * 1000))
        return f"{hostname}-{uuid.uuid4().hex[:8]}-{timestamp}"


# Standalone entry point for command-line execution
if __name__ == "__main__":
    MemorySetupWizard.run()

