#!/usr/bin/env python3
"""
Comprehensive validation script for OpenRouteService integration.
Tests all endpoints and validates the implementation.
"""

import asyncio
import json
import os
import sys

import httpx

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

BASE_URL = "http://localhost:20002"


def print_section(title: str):
    """Print a formatted section header."""
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_result(test_name: str, passed: bool, details: str = ""):
    """Print test result."""
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"{status} - {test_name}")
    if details:
        print(f"      {details}")


async def test_health_endpoint():
    """Test /health endpoint."""
    print_section("1. Testing /health Endpoint")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}/health", timeout=5.0)

            if response.status_code == 200:
                data = response.json()
                print_result("Health endpoint returns 200", True)
                print(f"      Response: {json.dumps(data, indent=2)}")

                # Check for ORS status
                if "openrouteservice" in data:
                    ors_status = data["openrouteservice"]
                    print_result(
                        "OpenRouteService status in response",
                        True,
                        f"Status: {ors_status}",
                    )
                    return ors_status == "enabled"
                else:
                    print_result(
                        "OpenRouteService status in response",
                        False,
                        "Missing 'openrouteservice' field",
                    )
                    return False
            else:
                print_result(
                    "Health endpoint returns 200", False, f"Got {response.status_code}"
                )
                print(f"      Response: {response.text}")
                return False

    except httpx.ConnectError:
        print_result(
            "Health endpoint accessible",
            False,
            "Service not running on localhost:20002",
        )
        print(
            "      Please start the service: uvicorn services.routing_service.main:app --host 0.0.0.0 --port 20002"
        )
        return False
    except Exception as e:
        print_result("Health endpoint test", False, f"Error: {e}")
        return False


async def test_route_endpoint():
    """Test /route endpoint."""
    print_section("2. Testing /route Endpoint")

    test_cases = [
        {
            "name": "Valid route request",
            "params": "start=53.342,-6.256&end=53.345,-6.262&profile=driving-car",
            "expected_status": 200,
        },
        {
            "name": "Invalid coordinate format",
            "params": "start=invalid&end=53.345,-6.262",
            "expected_status": 400,
        },
        {
            "name": "Invalid latitude",
            "params": "start=100,0&end=53.345,-6.262",
            "expected_status": 400,
        },
    ]

    results = []

    for test_case in test_cases:
        try:
            async with httpx.AsyncClient() as client:
                url = f"{BASE_URL}/route?{test_case['params']}"
                response = await client.get(url, timeout=10.0)

                passed = response.status_code == test_case["expected_status"]
                print_result(
                    test_case["name"], passed, f"Status: {response.status_code}"
                )

                if response.status_code == 200:
                    data = response.json()

                    # Validate GeoJSON structure
                    is_valid_geojson = (
                        data.get("type") == "FeatureCollection"
                        and isinstance(data.get("features"), list)
                        and len(data.get("features", [])) > 0
                    )
                    print_result("Valid GeoJSON FeatureCollection", is_valid_geojson)

                    if data.get("features"):
                        feature = data["features"][0]
                        has_linestring = (
                            feature.get("geometry", {}).get("type") == "LineString"
                            and len(feature.get("geometry", {}).get("coordinates", []))
                            > 0
                        )
                        print_result("LineString geometry present", has_linestring)
                        print(
                            f"      Coordinates count: {len(feature.get('geometry', {}).get('coordinates', []))}"
                        )

                        if has_linestring:
                            print(
                                f"      Example coordinates: {feature['geometry']['coordinates'][:2]}"
                            )

                    results.append(is_valid_geojson)
                else:
                    print(f"      Response: {response.text[:200]}")
                    results.append(passed)

        except httpx.ConnectError:
            print_result(test_case["name"], False, "Service not running")
            results.append(False)
        except Exception as e:
            print_result(test_case["name"], False, f"Error: {e}")
            results.append(False)

    return all(results)


async def test_isochrone_endpoint():
    """Test /isochrone endpoint."""
    print_section("3. Testing /isochrone Endpoint")

    test_cases = [
        {
            "name": "Valid isochrone request",
            "params": "location=53.342,-6.256&range=600&profile=driving-car",
            "expected_status": 200,
        },
        {
            "name": "Multiple ranges",
            "params": "location=53.342,-6.256&range=600,1200,1800&range_type=time",
            "expected_status": 200,
        },
        {
            "name": "Invalid coordinate format",
            "params": "location=invalid",
            "expected_status": 400,
        },
        {
            "name": "Invalid range format",
            "params": "location=53.342,-6.256&range=invalid",
            "expected_status": 400,
        },
    ]

    results = []

    for test_case in test_cases:
        try:
            async with httpx.AsyncClient() as client:
                url = f"{BASE_URL}/isochrone?{test_case['params']}"
                response = await client.get(url, timeout=10.0)

                passed = response.status_code == test_case["expected_status"]
                print_result(
                    test_case["name"], passed, f"Status: {response.status_code}"
                )

                if response.status_code == 200:
                    data = response.json()

                    # Validate GeoJSON structure
                    is_valid_geojson = (
                        data.get("type") == "FeatureCollection"
                        and isinstance(data.get("features"), list)
                        and len(data.get("features", [])) > 0
                    )
                    print_result("Valid GeoJSON FeatureCollection", is_valid_geojson)

                    if data.get("features"):
                        feature = data["features"][0]
                        has_polygon = feature.get("geometry", {}).get("type") in [
                            "Polygon",
                            "MultiPolygon",
                        ]
                        print_result("Polygon geometry present", has_polygon)
                        print(f"      Features count: {len(data.get('features', []))}")

                        if has_polygon:
                            props = feature.get("properties", {})
                            print(f"      Properties: {json.dumps(props, indent=6)}")

                    results.append(is_valid_geojson)
                else:
                    print(f"      Response: {response.text[:200]}")
                    results.append(passed)

        except httpx.ConnectError:
            print_result(test_case["name"], False, "Service not running")
            results.append(False)
        except Exception as e:
            print_result(test_case["name"], False, f"Error: {e}")
            results.append(False)

    return all(results)


async def main():
    """Run all validation tests."""
    print("\n" + "=" * 60)
    print("  OpenRouteService Integration Validation")
    print("=" * 60)

    # Check if service is running
    print("\n⚠️  Note: This script assumes the service is running on localhost:20002")
    print(
        "   Start it with: uvicorn services.routing_service.main:app --host 0.0.0.0 --port 20002"
    )
    print("   Make sure ORS_API_KEY is set in environment or .env file")

    results = {
        "health": await test_health_endpoint(),
        "route": await test_route_endpoint(),
        "isochrone": await test_isochrone_endpoint(),
    }

    print_section("Validation Summary")
    for test_name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} - {test_name}")

    all_passed = all(results.values())
    print(f"\n{'✅ All tests passed!' if all_passed else '❌ Some tests failed'}")

    return 0 if all_passed else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
