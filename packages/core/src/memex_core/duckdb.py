"""DuckDB integration for analytical queries over PostgreSQL."""

import duckdb
import logging
from typing import Optional
from memex_common.config import MemexConfig, PostgresMetaStoreConfig

logger = logging.getLogger(__name__)


class DuckDBManager:
    """Manages an in-memory DuckDB instance attached to PostgreSQL."""

    _instance: Optional['DuckDBManager'] = None

    def __init__(self, config: MemexConfig):
        self.config = config
        self.conn = None
        self._setup()

    @classmethod
    def get_instance(cls, config: Optional[MemexConfig] = None) -> 'DuckDBManager':
        if cls._instance is None:
            if config is None:
                raise ValueError('Config required for first initialization')
            cls._instance = cls(config)
        return cls._instance

    def _setup(self):
        """Initialize DuckDB and attach Postgres."""
        if not isinstance(self.config.server.meta_store, PostgresMetaStoreConfig):
            logger.warning('Meta store is not Postgres. Skipping DuckDB attachment.')
            return

        pg_config = self.config.server.meta_store.instance

        # Connection string for DuckDB's postgres scanner
        # format: dbname=db user=user password=pass host=host port=port
        conn_str = (
            f'dbname={pg_config.database} '
            f'user={pg_config.user} '
            f'password={pg_config.password.get_secret_value()} '
            f'host={pg_config.host} '
            f'port={pg_config.port}'
        )

        try:
            self.conn = duckdb.connect()  # In-memory
            if self.conn:
                self.conn.install_extension('postgres')
                self.conn.load_extension('postgres')

                # Attach as 'memex' or 'postgres'
                self.conn.execute(f"ATTACH '{conn_str}' AS postgres (TYPE POSTGRES)")
                logger.info('Successfully attached Postgres to DuckDB')

        except Exception as e:
            logger.error(f'Failed to attach Postgres to DuckDB: {e}')
            self.conn = None

    def query(self, sql: str, params: Optional[list] = None) -> list:
        """Execute a query against DuckDB."""
        if not self.conn:
            raise RuntimeError('DuckDB not initialized or connection failed')

        if params:
            return self.conn.execute(sql, params).fetchall()
        return self.conn.execute(sql).fetchall()

    def query_df(self, sql: str, params: Optional[list] = None):
        """Execute a query and return a Pandas/Polars DataFrame."""
        if not self.conn:
            raise RuntimeError('DuckDB not initialized')

        if params:
            return self.conn.execute(sql, params).df()
        return self.conn.execute(sql).df()
