"""
Database connection module for SafeRoute backend.

Provides async database engine and session management using SQLAlchemy
with a factory pattern for both PostgreSQL and PostGIS databases.
"""

import asyncio
import os
from enum import Enum
from typing import Dict, Optional, Tuple
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


class DatabaseType(Enum):
    """Enum for database types."""

    POSTGRES = "postgres"
    POSTGIS = "postgis"


class DatabaseConfig:
    """Configuration for a database connection."""

    def __init__(
        self,
        db_type: DatabaseType,
        host: str = "127.0.0.1",
        port: int = 5432,
        user: str = "saferoute",
        password: str = "",
        database: str = "saferoute",
        echo: bool = False,
        sslmode: Optional[str] = None,
        database_url: Optional[str] = None,
    ):
        """
        Initialize database configuration.

        Args:
            db_type: Type of database (POSTGRES or POSTGIS)
            host: Database host
            port: Database port
            user: Database user
            password: Database password
            database: Database name
            echo: Whether to echo SQL queries (for debugging)
            sslmode: SSL mode (e.g., "disable", "require", "prefer")
            database_url: Full database URL (if provided, takes precedence over individual params)
        """
        self.db_type = db_type
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.echo = echo
        self.sslmode = sslmode
        self.database_url = database_url

    def get_url(self) -> str:
        """
        Construct database URL from configuration.

        Returns:
            Database URL string for SQLAlchemy
        """
        # If a full database URL is provided, use it directly
        if self.database_url:
            return self.database_url

        password_encoded = quote_plus(self.password) if self.password else ""
        url = f"postgresql+asyncpg://{self.user}:{password_encoded}@{self.host}:{self.port}/{self.database}"

        # Add SSL mode if specified
        if self.sslmode:
            url += f"?sslmode={self.sslmode}"

        return url


class DatabaseConnection:
    """Encapsulates a single database connection with engine and session maker."""

    def __init__(self, config: DatabaseConfig):
        """
        Initialize database connection.

        Args:
            config: Database configuration
        """
        self.config = config
        self.engine: AsyncEngine = create_async_engine(
            config.get_url(),
            echo=config.echo,
            future=True,
        )
        self.session_maker = sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def check_health(self, timeout: float = 1.0) -> Tuple[bool, Optional[str]]:
        """
        Check database health.

        Args:
            timeout: Timeout in seconds for the health check

        Returns:
            Tuple of (success: bool, error_message: Optional[str])
        """
        try:

            async def _run() -> None:
                async with self.engine.connect() as conn:
                    if self.config.db_type == DatabaseType.POSTGIS:
                        # PostGIS-specific health check
                        await conn.execute(text("SELECT PostGIS_Version()"))
                    else:
                        # Standard PostgreSQL health check
                        await conn.execute(text("SELECT 1"))

            await asyncio.wait_for(_run(), timeout=timeout)
            return True, None
        except asyncio.TimeoutError:
            return False, f"{self.config.db_type.value} connection timeout"
        except Exception as e:
            return False, str(e).split("\n")[0][:200]

    async def get_session(self):
        """
        Get an async database session.

        Yields:
            AsyncSession: SQLAlchemy async session instance
        """
        async with self.session_maker() as session:
            yield session

    async def close(self):
        """Close the database engine and all connections."""
        await self.engine.dispose()


class DatabaseFactory:
    """
    Factory class for managing multiple database connections.

    This factory encapsulates database initialization, connection management,
    and provides a unified interface for both PostgreSQL and PostGIS databases.
    """

    def __init__(self):
        """Initialize the database factory."""
        self._connections: Dict[DatabaseType, DatabaseConnection] = {}
        self._initialized = False

    def _create_postgres_config(self) -> DatabaseConfig:
        """
        Create PostgreSQL database configuration from environment variables.

        Priority:
        1. DATABASE_URL (if set, used directly)
        2. Individual environment variables (POSTGRES_HOST, etc.) + SSL mode

        Returns:
            DatabaseConfig for PostgreSQL
        """
        # Check for full database URL first (highest priority)
        database_url = os.getenv("DATABASE_URL")
        if database_url:
            # Convert postgresql:// to postgresql+asyncpg:// if needed
            if database_url.startswith("postgresql://"):
                database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            elif database_url.startswith("postgres://"):
                database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)

            return DatabaseConfig(
                db_type=DatabaseType.POSTGRES,
                database_url=database_url,
                echo=os.getenv("POSTGRES_ECHO", "false").lower() == "true",
            )

        # Otherwise, use individual environment variables
        # Check for SSL mode: DATABASE_SSLMODE or POSTGRES_SSLMODE
        sslmode = os.getenv("DATABASE_SSLMODE") or os.getenv("POSTGRES_SSLMODE")

        return DatabaseConfig(
            db_type=DatabaseType.POSTGRES,
            host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.getenv("POSTGRES_USER", "saferoute"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
            database=os.getenv("POSTGRES_DATABASE", "saferoute"),
            echo=os.getenv("POSTGRES_ECHO", "false").lower() == "true",
            sslmode=sslmode,
        )

    def _create_postgis_config(self) -> DatabaseConfig:
        """
        Create PostGIS database configuration from environment variables.

        Priority:
        1. POSTGIS_DATABASE_URL (if set, used directly)
        2. Individual environment variables (POSTGIS_HOST, etc.) + SSL mode

        Returns:
            DatabaseConfig for PostGIS
        """
        # Check for full database URL first (highest priority)
        database_url = os.getenv("POSTGIS_DATABASE_URL")
        if database_url:
            # Convert postgresql:// to postgresql+asyncpg:// if needed
            if database_url.startswith("postgresql://"):
                database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            elif database_url.startswith("postgres://"):
                database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)

            return DatabaseConfig(
                db_type=DatabaseType.POSTGIS,
                database_url=database_url,
                echo=os.getenv("POSTGIS_ECHO", "false").lower() == "true",
            )

        # Otherwise, use individual environment variables
        # Check for SSL mode: DATABASE_SSLMODE or POSTGIS_SSLMODE
        sslmode = os.getenv("DATABASE_SSLMODE") or os.getenv("POSTGIS_SSLMODE")

        return DatabaseConfig(
            db_type=DatabaseType.POSTGIS,
            host=os.getenv("POSTGIS_HOST", "127.0.0.1"),
            port=int(os.getenv("POSTGIS_PORT", "5433")),
            user=os.getenv("POSTGIS_USER", "saferoute"),
            password=os.getenv("POSTGIS_PASSWORD", ""),
            database=os.getenv("POSTGIS_DATABASE", "saferoute_geo"),
            echo=os.getenv("POSTGIS_ECHO", "false").lower() == "true",
            sslmode=sslmode,
        )

    def initialize(self, databases: list[DatabaseType] = None):
        """
        Initialize database connections.

        Args:
            databases: List of database types to initialize. If None, initializes POSTGRES only.
        """
        if self._initialized:
            return

        if databases is None:
            databases = [DatabaseType.POSTGRES]

        for db_type in databases:
            if db_type == DatabaseType.POSTGRES:
                config = self._create_postgres_config()
            elif db_type == DatabaseType.POSTGIS:
                config = self._create_postgis_config()
            else:
                raise ValueError(f"Unknown database type: {db_type}")

            self._connections[db_type] = DatabaseConnection(config)

        self._initialized = True

    def get_connection(self, db_type: DatabaseType) -> DatabaseConnection:
        """
        Get a database connection by type.

        Args:
            db_type: Type of database to get connection for

        Returns:
            DatabaseConnection instance

        Raises:
            ValueError: If database type is not initialized
        """
        if not self._initialized:
            raise ValueError("DatabaseFactory not initialized. Call initialize() first.")

        if db_type not in self._connections:
            raise ValueError(f"Database type {db_type} not initialized")

        return self._connections[db_type]

    async def check_health(
        self, db_type: DatabaseType, timeout: float = 1.0
    ) -> Tuple[bool, Optional[str]]:
        """
        Check health of a specific database.

        Args:
            db_type: Type of database to check
            timeout: Timeout in seconds for the health check

        Returns:
            Tuple of (success: bool, error_message: Optional[str])
        """
        try:
            connection = self.get_connection(db_type)
            return await connection.check_health(timeout)
        except ValueError as e:
            return False, str(e)

    async def close_all(self):
        """Close all database connections."""
        for connection in self._connections.values():
            await connection.close()
        self._connections.clear()
        self._initialized = False

    def get_session_dependency(self, db_type: DatabaseType):
        """
        Get a FastAPI dependency for database sessions.

        Args:
            db_type: Type of database to get session for

        Returns:
            Async generator function for FastAPI dependency injection
        """

        async def _get_session():
            connection = self.get_connection(db_type)
            async with connection.session_maker() as session:
                yield session

        return _get_session


# Global database factory instance
_db_factory: Optional[DatabaseFactory] = None


def get_database_factory() -> DatabaseFactory:
    """
    Get or create the global database factory instance.

    Returns:
        DatabaseFactory instance
    """
    global _db_factory
    if _db_factory is None:
        _db_factory = DatabaseFactory()
    return _db_factory


def initialize_databases(databases: list[DatabaseType] = None):
    """
    Initialize databases using the global factory.

    Args:
        databases: List of database types to initialize. If None, initializes POSTGRES only.
    """
    factory = get_database_factory()
    factory.initialize(databases)
