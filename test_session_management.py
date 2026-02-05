#!/usr/bin/env python3
"""
Complete test script for Session Management functionality.

Usage:
    # With Redis running locally
    python3 test_session_management.py

    # With Redis via K8s port-forward
    export REDIS_HOST=127.0.0.1
    export REDIS_PORT=6379
    export REDIS_PASSWORD=LB04@Redis  # if needed
    python3 test_session_management.py
"""

import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from common.auth.session import get_session_manager
from common.redis_client import get_redis_client


def test_redis_connection():
    """Test 1: Redis connection"""
    print("=" * 60)
    print("Test 1: Redis Connection")
    print("=" * 60)

    redis = get_redis_client()
    if not redis.is_connected():
        print("❌ Redis not connected!")
        print("   Make sure Redis is running:")
        print("   - Local: redis-server")
        print("   - K8s: kubectl port-forward -n data svc/redis 6379:6379")
        return False

    print("✅ Redis connected!")

    # Test basic operations
    print("\nTesting basic operations...")
    print(f"  - Set: {redis.set('test_key', 'test_value')}")
    print(f"  - Get: {redis.get('test_key')}")
    print(f"  - Delete: {redis.delete('test_key')}")
    print("✅ Basic operations working!")

    return True


def test_redis_set_operations():
    """Test 2: Redis Set operations"""
    print("\n" + "=" * 60)
    print("Test 2: Redis Set Operations")
    print("=" * 60)

    redis = get_redis_client()
    if not redis.is_connected():
        print("❌ Redis not connected - skipping test")
        return False

    # Test sadd
    added = redis.sadd("test_set", "value1", "value2", "value3")
    print(f"✅ Added {added} members to set")

    # Test smembers
    members = redis.smembers("test_set")
    print(f"✅ Set has {len(members)} members: {sorted(members)}")

    # Test sismember
    is_member = redis.sismember("test_set", "value1")
    print(f"✅ value1 is member: {is_member}")

    # Test srem
    removed = redis.srem("test_set", "value1")
    print(f"✅ Removed {removed} member(s)")

    # Test delete_many
    deleted = redis.delete_many(["test_set"])
    print(f"✅ Batch deleted {deleted} key(s)")

    print("✅ All Set operations passed!")
    return True


def test_session_creation():
    """Test 3: Session creation"""
    print("\n" + "=" * 60)
    print("Test 3: Session Creation")
    print("=" * 60)

    sm = get_session_manager()
    if not sm.redis.is_connected():
        print("❌ Redis not connected - skipping test")
        return False

    # Create session
    sid = sm.create_session(
        sub="auth0|test_user_123",
        device_id="device_abc123",
        device_name="Test Device",
        app_version="1.0.0",
    )
    print(f"✅ Session created: {sid[:30]}...")

    # Verify session exists
    session = sm.get_session(sid)
    if session:
        print("✅ Session retrieved:")
        print(f"   - sub: {session['sub']}")
        print(f"   - device_id: {session['device_id']}")
        print(f"   - status: {session['status']}")
        return sid
    else:
        print("❌ Session not found")
        return None


def test_session_validation(sid):
    """Test 4: Session validation"""
    print("\n" + "=" * 60)
    print("Test 4: Session Validation")
    print("=" * 60)

    if not sid:
        print("⚠️  No session ID - skipping test")
        return False

    sm = get_session_manager()

    # Valid session
    is_valid = sm.is_session_valid(sid, "auth0|test_user_123")
    print(f"✅ Valid session check: {is_valid}")

    # Invalid user
    is_invalid = sm.is_session_valid(sid, "auth0|wrong_user")
    print(f"✅ Invalid user rejected: {not is_invalid}")

    # Check user sessions index
    user_sessions = sm.get_user_sessions("auth0|test_user_123")
    print(f"✅ User has {len(user_sessions)} session(s) in index")

    return True


def test_logout_all():
    """Test 5: Logout all functionality"""
    print("\n" + "=" * 60)
    print("Test 5: Logout All")
    print("=" * 60)

    sm = get_session_manager()
    if not sm.redis.is_connected():
        print("❌ Redis not connected - skipping test")
        return False

    # Create multiple sessions
    print("Creating 3 sessions for same user...")
    sid1 = sm.create_session("auth0|user123", "device1", "Device 1")
    sid2 = sm.create_session("auth0|user123", "device2", "Device 2")
    sid3 = sm.create_session("auth0|user123", "device3", "Device 3")
    print("✅ Created 3 sessions")

    # Verify sessions exist
    sessions = sm.get_user_sessions("auth0|user123")
    print(f"✅ User has {len(sessions)} sessions")

    # Test logout_all
    deleted_count = sm.delete_user_sessions("auth0|user123")
    print(f"✅ Deleted {deleted_count} sessions")

    # Verify all deleted
    sessions_after = sm.get_user_sessions("auth0|user123")
    session1_after = sm.get_session(sid1)
    session2_after = sm.get_session(sid2)
    session3_after = sm.get_session(sid3)

    if (
        len(sessions_after) == 0
        and not session1_after
        and not session2_after
        and not session3_after
    ):
        print("✅ All sessions deleted successfully!")
        return True
    else:
        print("❌ Some sessions still exist")
        return False


def test_complete_lifecycle():
    """Test 6: Complete session lifecycle"""
    print("\n" + "=" * 60)
    print("Test 6: Complete Session Lifecycle")
    print("=" * 60)

    sm = get_session_manager()
    redis = get_redis_client()

    if not redis.is_connected():
        print("❌ Redis not connected - skipping test")
        return False

    # Create session
    sid = sm.create_session("auth0|lifecycle", "device_lifecycle", "Lifecycle Device")
    print(f"✅ Session created: {sid[:30]}...")

    # Check Redis keys
    session_key = f"session:{sid}"
    user_sessions_key = "user_sessions:auth0|lifecycle"
    device_session_key = "device_session:auth0|lifecycle:device_lifecycle"

    print("\nChecking Redis keys...")
    print(f"  ✅ session:<sid> exists: {redis.exists(session_key)}")
    print(f"  ✅ user_sessions:<sub> exists: {redis.exists(user_sessions_key)}")
    print(f"  ✅ device_session:<sub>:<device_id> exists: {redis.exists(device_session_key)}")

    # Check session data
    session_data = redis.get_json(session_key)
    if session_data:
        required_fields = ["sub", "device_id", "created_at", "last_seen_at", "status"]
        print("\nChecking session data structure...")
        for field in required_fields:
            exists = field in session_data
            print(f"  {'✅' if exists else '❌'} {field}: {exists}")

    # Test update_last_seen
    print("\nTesting update_last_seen...")
    updated = sm.update_last_seen(sid)
    print(f"  ✅ Last seen updated: {updated}")

    # Cleanup
    sm.delete_session(sid)
    print("\n✅ Session lifecycle test passed!")
    return True


def main():
    """Run all tests"""
    print("\n" + "=" * 60)
    print("Session Management Test Suite")
    print("=" * 60)

    results = []

    # Test 1: Redis connection
    if not test_redis_connection():
        print("\n❌ Redis connection failed - cannot continue tests")
        print("\nTo fix:")
        print("  1. Start Redis locally: redis-server")
        print("  2. Or use K8s: kubectl port-forward -n data svc/redis 6379:6379")
        return

    # Test 2: Redis Set operations
    results.append(("Set Operations", test_redis_set_operations()))

    # Test 3: Session creation
    sid = test_session_creation()
    results.append(("Session Creation", sid is not None))

    # Test 4: Session validation
    if sid:
        results.append(("Session Validation", test_session_validation(sid)))
        # Cleanup test session
        sm = get_session_manager()
        sm.delete_session(sid)

    # Test 5: Logout all
    results.append(("Logout All", test_logout_all()))

    # Test 6: Complete lifecycle
    results.append(("Complete Lifecycle", test_complete_lifecycle()))

    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status} {test_name}")

    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 All tests passed!")
    else:
        print(f"\n⚠️  {total - passed} test(s) failed")


if __name__ == "__main__":
    main()
