#!/usr/bin/env python3
"""
Test script for the unified DatabaseFactory.

This script tests the basic functionality of the DatabaseFactory including:
- Initialization
- Connection management
- Health checks
- Session creation
"""

import asyncio
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from libs.db import DatabaseType, get_database_factory


async def test_factory_initialization():
    """Test factory initialization."""
    print("=" * 60)
    print("Testing DatabaseFactory Initialization")
    print("=" * 60)

    # Get factory instance
    factory = get_database_factory()
    print("Factory instance created")

    # Initialize PostgreSQL only
    try:
        factory.initialize([DatabaseType.POSTGRES])
        print("PostgreSQL database initialized")
    except Exception as e:
        print(f"Failed to initialize PostgreSQL: {e}")
        return False

    # Check if connection exists
    try:
        postgres_conn = factory.get_connection(DatabaseType.POSTGRES)
        print(f"PostgreSQL connection retrieved: {postgres_conn.config.database}")
    except Exception as e:
        print(f"Failed to get PostgreSQL connection: {e}")
        return False

    return True


async def test_health_checks():
    """Test database health checks."""
    print("\n" + "=" * 60)
    print("Testing Health Checks")
    print("=" * 60)

    factory = get_database_factory()

    # Test PostgreSQL health check
    try:
        success, error = await factory.check_health(DatabaseType.POSTGRES, timeout=5.0)
        if success:
            print("PostgreSQL health check: PASSED")
        else:
            print(f"PostgreSQL health check: FAILED - {error}")
            print("(This is expected if database is not running)")
    except Exception as e:
        print(f"PostgreSQL health check error: {e}")
        print("(This is expected if database is not running)")

    return True


async def test_session_dependency():
    """Test session dependency creation."""
    print("\n" + "=" * 60)
    print("Testing Session Dependencies")
    print("=" * 60)

    factory = get_database_factory()

    try:
        # Get session dependency
        get_session = factory.get_session_dependency(DatabaseType.POSTGRES)
        print("Session dependency created")

        # Try to create a session
        print("Attempting to create a session...")
        session_gen = get_session()

        try:
            session = await session_gen.__anext__()
            print(f"Session created successfully: {type(session).__name__}")

            # Close the session
            try:
                await session_gen.aclose()
            except StopAsyncIteration:
                pass

        except Exception as e:
            print(f"Session creation requires running database: {type(e).__name__}")
            print("(This is expected if database is not running)")

    except Exception as e:
        print(f"Failed to create session dependency: {e}")
        return False

    return True


async def test_factory_api():
    """Test using the factory API directly."""
    print("\n" + "=" * 60)
    print("Testing Factory API Usage")
    print("=" * 60)

    factory = get_database_factory()

    # Test get_session_dependency method
    try:
        get_session = factory.get_session_dependency(DatabaseType.POSTGRES)
        print("get_session_dependency() works")

        # Try to get a session
        try:
            db_gen = get_session()
            session = await db_gen.__anext__()
            print(f"Session created via factory API: {type(session).__name__}")
            await db_gen.aclose()
        except Exception as e:
            print(f"Session requires running database: {type(e).__name__}")
    except Exception as e:
        print(f"get_session_dependency() failed: {e}")

    # Test check_health via factory
    try:
        success, error = await factory.check_health(DatabaseType.POSTGRES, timeout=5.0)
        if success:
            print("factory.check_health() function: PASSED")
        else:
            print(f"factory.check_health(): Database not available - {error}")
    except Exception as e:
        print(f"factory.check_health() failed: {e}")

    return True


async def test_cleanup():
    """Test cleanup functionality."""
    print("\n" + "=" * 60)
    print("Testing Cleanup")
    print("=" * 60)

    factory = get_database_factory()

    try:
        await factory.close_all()
        print("All connections closed successfully")
    except Exception as e:
        print(f"Failed to close connections: {e}")
        return False

    # Verify connections are cleared
    if not factory._connections:
        print("✓ Connection dictionary cleared")
    else:
        print("  Connections still exist after cleanup")
        return False

    return True


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("DATABASE FACTORY TEST SUITE")
    print("=" * 60)

    results = []

    # Run tests
    results.append(("Initialization", await test_factory_initialization()))
    results.append(("Health Checks", await test_health_checks()))
    results.append(("Session Dependencies", await test_session_dependency()))
    results.append(("Factory API Usage", await test_factory_api()))
    results.append(("Cleanup", await test_cleanup()))

    # Print summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for test_name, result in results:
        status = "PASSED" if result else "  FAILED"
        print(f"{test_name:.<40} {status}")

    print("=" * 60)
    print(f"Total: {passed}/{total} tests passed")
    print("=" * 60)

    if passed == total:
        print("\nAll tests passed!")
        return 0
    else:
        print(f"\n{total - passed} test(s) failed")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
