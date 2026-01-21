#!/usr/bin/env python3
"""
SafeRoute Backend - Service Orchestrator.

Starts all microservices in the services folder.
"""
import argparse
import multiprocessing
import os
import sys
import time

import uvicorn

# Service configuration: service_name -> (module_path, port)
SERVICES = {
    "user_management": ("services.user_management.main", 20000),
    "notification": ("services.notification.main", 20001),
    "routing_service": ("services.routing_service.main", 20002),
    "safety_scoring": ("services.safety_scoring.main", 20003),
    "feedback": ("services.feedback.main", 20004),
    "data_cleaner": ("services.data_cleaner.main", 20005),
    "sos": ("services.sos.main", 20006),
}

# Docs service (service discovery)
DOCS_SERVICE = ("docs.main", 8080)


def run_service(module_path, port, service_name):
    """
    Run a single service using uvicorn.

    Args:
        module_path: Python module path to the FastAPI app (e.g., 'services.user_management.main')
        port: Port number to run the service on
        service_name: Human-readable name of the service for logging

    Raises:
        SystemExit: If the service fails to start
    """
    print(f"Starting {service_name} on port {port}...")
    try:
        uvicorn.run(
            f"{module_path}:app",
            host="0.0.0.0",
            port=port,
            reload=False,  # Disable reload in multiprocess mode
            log_level="info",
        )
    except Exception as e:
        print(f"Error starting {service_name}: {e}")
        sys.exit(1)


def start_all_services():
    """
    Start all services in separate processes.

    Starts all microservices defined in SERVICES dictionary and the docs service.
    Handles graceful shutdown on KeyboardInterrupt.
    """
    processes = []
    service_names = []

    print("=" * 60)
    print("SafeRoute Backend - Starting All Services")
    print("=" * 60)
    print()

    # Start all microservices
    for service_name, (module_path, port) in SERVICES.items():
        process = multiprocessing.Process(
            target=run_service,
            args=(module_path, port, service_name),
            name=f"service-{service_name}",
        )
        process.start()
        processes.append(process)
        service_names.append(service_name)
        time.sleep(0.5)  # Small delay between starts

    # Start docs service (service discovery)
    docs_module_path, docs_port = DOCS_SERVICE
    docs_process = multiprocessing.Process(
        target=run_service,
        args=(docs_module_path, docs_port, "service_discovery"),
        name="service-discovery",
    )
    docs_process.start()
    processes.append(docs_process)
    service_names.append("service_discovery")
    time.sleep(0.5)  # Delay to allow service initialization

    print()
    print("=" * 60)
    print("All services started!")
    print("=" * 60)
    print("\nService URLs:")
    for service_name, (_, port) in SERVICES.items():
        print(f"  • {service_name:20s} → http://127.0.0.1:{port}/docs")
    docs_module_path, docs_port = DOCS_SERVICE
    print(f"  • {'service_discovery':20s} → http://127.0.0.1:{docs_port}/")
    print("\nPress Ctrl+C to stop all services...\n")

    # Wait for all processes or handle interrupt
    try:
        # Wait for all processes
        for process in processes:
            process.join()
    except KeyboardInterrupt:
        print("\n\nShutting down all services...")
        for process, service_name in zip(processes, service_names):
            if process.is_alive():
                print(f"  Stopping {service_name}...")
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    print(f"  Force killing {service_name}...")
                    process.kill()
        print("All services stopped.")


def start_single_service(service_name):
    """
    Start a single service.

    Args:
        service_name: Name of the service to start (must be in SERVICES dict)

    Raises:
        SystemExit: If service_name is not found in SERVICES
    """
    if service_name not in SERVICES:
        print(f"Unknown service: {service_name}")
        print(f"\nAvailable services: {', '.join(SERVICES.keys())}")
        sys.exit(1)

    module_path, port = SERVICES[service_name]
    print(f"Starting {service_name} on port {port}...")
    print(f"Docs: http://127.0.0.1:{port}/docs")
    print("\nPress Ctrl+C to stop...\n")

    run_service(module_path, port, service_name)


def main():
    """
    Main entry point for the service orchestrator.

    Parses command-line arguments and either:
    - Lists available services (--list)
    - Starts a single service (--service <name>)
    - Starts all services (default)
    """
    parser = argparse.ArgumentParser(add_help=False)

    parser.add_argument(
        "--service",
        "-s",
        type=str,
        help="Start a single service by name",
    )

    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List all available services",
    )

    args = parser.parse_args()

    if args.list:
        print("Available services:")
        for service_name, (_, port) in SERVICES.items():
            print(f"  • {service_name:20s} (port {port})")
        return

    if args.service:
        start_single_service(args.service)
    else:
        start_all_services()


if __name__ == "__main__":
    # Set environment variable for local development
    os.environ["LOCAL_DEV"] = "true"
    main()
