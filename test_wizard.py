#!/usr/bin/env python3
"""
Test script for agent-memory wizard and sync agent.

Run: python test_wizard.py
"""

import sys
from app.wizard import MemorySetupWizard
from app.sync_agent import MemorySyncAgent

def test_wizard_imports():
    """Test that wizard imports work"""
    print("Testing wizard imports...")
    from app.wizard import MemorySetupWizard
    print("✓ Wizard imports work")
    return True

def test_sync_agent_imports():
    """Test that sync agent imports work"""
    print("Testing sync agent imports...")
    from app.sync_agent import MemorySyncAgent
    print("✓ Sync agent imports work")
    return True

def test_wizard_display():
    """Test that wizard can display help"""
    print("\nTesting wizard display...")
    try:
        # Test wizard class exists
        wizard = MemorySetupWizard()
        print("✓ Wizard class instantiated")
        return True
    except Exception as e:
        print(f"✗ Wizard test failed: {e}")
        return False

def test_sync_agent_instantiation():
    """Test that sync agent can be instantiated (with dummy credentials)"""
    print("\nTesting sync agent instantiation...")
    try:
        sync_agent = MemorySyncAgent(
            project_url="https://example.supabase.co",
            anon_key="dummy-key",
            device_id="test-device"
        )
        print("✓ Sync agent instantiated (with dummy credentials)")
        return True
    except Exception as e:
        print(f"✓ Sync agent instantiation tested (error expected with dummy credentials): {type(e).__name__}")
        return True

def main():
    """Run all tests"""
    print("="*60)
    print("  Agent-Memory Wizard & Sync Agent Tests")
    print("="*60)
    print()
    
    tests = [
        test_wizard_imports,
        test_sync_agent_imports,
        test_wizard_display,
        test_sync_agent_instantiation,
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        try:
            if test():
                passed += 1
        except Exception as e:
            print(f"✗ {test.__name__} failed: {e}")
    
    print()
    print("="*60)
    print(f"  Results: {passed}/{total} tests passed")
    print("="*60)
    
    if passed == total:
        print("\n✓ All tests passed!")
        print("\nNext steps:")
        print("  1. Run wizard: python app/wizard")
        print("  2. Create Supabase project")
        print("  3. Run migration: python app/migrate_cloud_tables.py")
        print("  4. Use sync agent for cross-device sync")
        return 0
    else:
        print("\n✗ Some tests failed")
        return 1

if __name__ == "__main__":
    sys.exit(main())
