"""
Sync Agent — Cross-device memory synchronization

The sync agent enables persistent agent memory across devices by
automatically syncing with Supabase cloud storage.

Usage:
    >>> from app.sync_agent import MemorySyncAgent
    >>> sync_agent = MemorySyncAgent(
    ...     project_url="https://xyz.supabase.co",
    ...     anon_key="your-anon-key",
    ...     device_id="my-device-123"
    ... )
    >>> sync_agent.sync_to_cloud()
    >>> sync_agent.sync_from_cloud()
"""

import json
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any
from supabase import create_client
from app.config import settings
from app.models import LessonCreate, LessonOut

class MemorySyncAgent:
    """
    Sync agent for cross-device memory persistence.
    
    Automatically syncs memory state between:
    - Local SQLite (device A)
    - Cloud Supabase (shared state)
    - Other local databases (device B, C, etc.)
    
    This enables true persistent memory across devices.
    """
    
    def __init__(
        self,
        project_url: str,
        anon_key: str,
        device_id: Optional[str] = None,
        sync_interval: int = 300  # 5 minutes
    ):
        """
        Initialize sync agent.
        
        Args:
            project_url: Supabase project URL
            anon_key: Supabase anon key for client sync
            device_id: Unique device identifier (auto-generated if not provided)
            sync_interval: Sync interval in seconds (default: 300)
        """
        self.project_url = project_url
        self.anon_key = anon_key
        self.client = create_client(project_url, anon_key)
        self.device_id = device_id or self._generate_device_id()
        self.sync_interval = sync_interval
        
        # Local state
        self.local_memory_path = self._get_sync_state_file()
        self.last_sync = self._load_last_sync()
        
        # Initialize cloud tables
        self._ensure_cloud_tables()
    
    def _generate_device_id(self) -> str:
        """Generate unique device ID"""
        import socket
        hostname = socket.gethostname()
        import uuid
        timestamp = str(int(time.time() * 1000))
        return f"{hostname}-{uuid.uuid4().hex[:8]}-{timestamp}"
    
    def _get_sync_state_file(self) -> Path:
        """Get path to sync state file"""
        # Use .agent-memory-codex directory
        base_dir = Path(__file__).parent.parent / ".agent-memory-codex"
        sync_dir = base_dir / "sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        return sync_dir / f"{self.device_id}.json"
    
    def _load_last_sync(self) -> str:
        """Load last sync timestamp"""
        try:
            state_file = self._get_sync_state_file()
            if state_file.exists():
                with open(state_file, 'r') as f:
                    state = json.load(f)
                    return state.get('last_sync', '')
        except:
            pass
        
        return ''
    
    def _save_last_sync(self, timestamp: str):
        """Save last sync timestamp"""
        state_file = self._get_sync_state_file()
        try:
            state = {
                'last_sync': timestamp,
                'device_id': self.device_id,
                'status': 'synced',
                'local_changes_pending': False
            }
            with open(state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not save sync state: {e}")
    
    def _ensure_cloud_tables(self):
        """Ensure cloud tables exist"""
        try:
            # Check if lessons table exists
            self.client.table('lessons').select('*').execute()
            print("✓ Cloud tables exist")
        except Exception as e:
            print(f"⚠ Creating cloud tables...")
            self._create_cloud_tables()
    
    def _create_cloud_tables(self):
        """Create cloud tables"""
        try:
            # Create lessons table
            self.client.rpc('create_table', {
                'table_name': 'lessons',
                'schema': {
                    'id': {'type': 'uuid', 'default': 'gen_random_uuid()'},
                    'topic': {'type': 'text'},
                    'content': {'type': 'text'},
                    'embedding': {'type': 'vector', 'dimensions': 1536},
                    'confidence': {'type': 'float8'},
                    'source': {'type': 'text'},
                    'timestamp': {'type': 'timestamp', 'default': 'now()'}
                }
            })
            
            # Create patterns table
            self.client.rpc('create_table', {
                'table_name': 'patterns',
                'schema': {
                    'id': {'type': 'uuid', 'default': 'gen_random_uuid()'},
                    'name': {'type': 'text'},
                    'description': {'type': 'text'},
                    'steps': {'type': 'text[]'},
                    'success_rate': {'type': 'float8'},
                    'complexity': {'type': 'text'}
                }
            })
            
            # Create gotchas table
            self.client.rpc('create_table', {
                'table_name': 'gotchas',
                'schema': {
                    'id': {'type': 'uuid', 'default': 'gen_random_uuid()'},
                    'category': {'type': 'text'},
                    'error_pattern': {'type': 'text'},
                    'fix_pattern': {'type': 'text'},
                    'confidence': {'type': 'float8'}
                }
            })
            
            # Create benchmarks table
            self.client.rpc('create_table', {
                'table_name': 'benchmarks',
                'schema': {
                    'id': {'type': 'uuid', 'default': 'gen_random_uuid()'},
                    'run_id': {'type': 'text'},
                    'task': {'type': 'text'},
                    'success': {'type': 'boolean'},
                    'iterations': {'type': 'integer'},
                    'tokens_used': {'type': 'integer'},
                    'memory_hits': {'type': 'integer'},
                    'patterns_applied': {'type': 'integer'},
                    'timestamp': {'type': 'timestamp', 'default': 'now()'}
                }
            })
            
            # Create sync_state table
            self.client.rpc('create_table', {
                'table_name': 'sync_state',
                'schema': {
                    'last_sync': {'type': 'timestamp'},
                    'device_id': {'type': 'text'},
                    'status': {'type': 'text'},
                    'local_changes_pending': {'type': 'boolean'}
                }
            })
            
            print("✓ Cloud tables created")
        except Exception as e:
            print(f"✗ Could not create cloud tables: {e}")
    
    def sync_to_cloud(self):
        """Upload local changes to cloud"""
        print(f"Syncing device {self.device_id} to cloud...")
        
        # Save local state
        local_state = self._get_local_state()
        
        # Upload changes
        self.client.table('lessons').upsert(local_state.get('lessons', []), on_conflict='id').execute()
        self.client.table('patterns').upsert(local_state.get('patterns', []), on_conflict='id').execute()
        self.client.table('gotchas').upsert(local_state.get('gotchas', []), on_conflict='id').execute()
        
        # Update sync state
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        self._save_last_sync(now)
        
        print("✓ Synced to cloud")
    
    def sync_from_cloud(self):
        """Download cloud changes to local"""
        print(f"Syncing cloud to device {self.device_id}...")
        
        # Fetch latest data
        lessons = self.client.table('lessons').select('*').order('timestamp', desc=True).limit(1000).execute()
        patterns = self.client.table('patterns').select('*').execute()
        gotchas = self.client.table('gotchas').select('*').execute()
        
        # Merge with local state
        local_state = self._get_local_state()
        
        # Update local state with cloud data
        local_state['lessons'] = self._merge_data(
            local_state.get('lessons', []), lessons.data
        )
        local_state['patterns'] = self._merge_data(
            local_state.get('patterns', []), patterns.data
        )
        local_state['gotchas'] = self._merge_data(
            local_state.get('gotchas', []), gotchas.data
        )
        
        # Save merged state
        self._save_local_state(local_state)
        
        # Update sync state
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        self._save_last_sync(now)
        
        print("✓ Synced from cloud")
    
    def _get_local_state(self) -> Dict[str, Any]:
        """Get local state from file"""
        state_file = self._get_sync_state_file()
        if state_file.exists():
            with open(state_file, 'r') as f:
                return json.load(f)
        return {
            'lessons': [],
            'patterns': [],
            'gotchas': []
        }
    
    def _save_local_state(self, state: Dict[str, Any]):
        """Save local state to file"""
        state_file = self._get_sync_state_file()
        with open(state_file, 'w') as f:
            json.dump(state, f, indent=2)
    
    def _merge_data(self, local: list, remote: list) -> list:
        """Merge local and remote data, keeping latest"""
        if not remote:
            return local
        
        # Sort by timestamp and get latest
        remote_sorted = sorted(remote, key=lambda x: x.get('timestamp', ''), reverse=True)
        
        # Merge keeping remote data
        merged = {}
        for item in remote_sorted:
            merged[str(item['id'])] = item
        
        # Keep local items not in remote
        for item in local:
            item_id = str(item['id'])
            if item_id not in merged:
                merged[item_id] = item
        
        return list(merged.values())
    
    def run_sync_loop(self):
        """Run automatic sync loop"""
        print(f"Starting sync loop for device {self.device_id}...")
        print(f"Syncing every {self.sync_interval} seconds...")
        
        while True:
            try:
                self.sync_to_cloud()
                time.sleep(self.sync_interval)
            except KeyboardInterrupt:
                print("\nSync loop stopped")
                break
            except Exception as e:
                print(f"Sync error: {e}")
                time.sleep(self.sync_interval)
    
    def close(self):
        """Close connection"""
        self.client.close()

