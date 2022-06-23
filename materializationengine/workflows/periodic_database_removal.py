"""
Periodically clean up expired materialized databases.
"""
import itertools
from datetime import datetime

from celery.utils.log import get_task_logger
from materializationengine.celery_init import celery
from materializationengine.database import create_session
from materializationengine.info_client import get_aligned_volumes, get_datastack_info
from dynamicannotationdb.models import AnalysisVersion
from materializationengine.utils import get_config_param
from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url

celery_logger = get_task_logger(__name__)


def get_aligned_volumes_databases():
    aligned_volumes = get_aligned_volumes()
    SQL_URI_CONFIG = get_config_param("SQLALCHEMY_DATABASE_URI")
    sql_base_uri = SQL_URI_CONFIG.rpartition("/")[0]

    engine = create_engine(sql_base_uri)
    with engine.connect() as connection:
        result = connection.execute("SELECT datname FROM pg_database;")
        databases = [database[0] for database in result]
    aligned_volume_databases = list(set(aligned_volumes).intersection(databases))
    return aligned_volume_databases


@celery.task(name="workflow:remove_expired_databases")
def remove_expired_databases(delete_threshold: int = 5) -> str:
    """
    Remove expired database from time this method is called.
    """
    aligned_volume_databases = get_aligned_volumes_databases()
    datastacks = get_config_param("DATASTACKS")
    current_time = datetime.utcnow()
    remove_db_cron_info = []

    for datastack in datastacks:
        datastack_info = get_datastack_info(datastack)
        aligned_volume = datastack_info["aligned_volume"]["name"]
        if aligned_volume in aligned_volume_databases:
            SQL_URI_CONFIG = get_config_param("SQLALCHEMY_DATABASE_URI")
            sql_base_uri = SQL_URI_CONFIG.rpartition("/")[0]
            sql_uri = make_url(f"{sql_base_uri}/{aligned_volume}")
            session, engine = create_session(sql_uri)
            session.expire_on_commit = False
            # get number of expired dbs that are ready for deletion
            try:
                expired_results = (
                    session.query(AnalysisVersion)
                    .filter(AnalysisVersion.expires_on <= current_time)
                    .order_by(AnalysisVersion.time_stamp)
                    .all()
                )
                expired_versions = [str(expired_db) for expired_db in expired_results]

            except Exception as sql_error:
                celery_logger.error(f"Error: {sql_error}")
                continue

            # get databases that exist currently, filter by materializied dbs
            result = engine.execute("SELECT datname FROM pg_database;").fetchall()
            database_list = list(itertools.chain.from_iterable(result))
            databases = [
                database for database in database_list if database.startswith(datastack)
            ]

            # get databases to delete that are currently present (ordered by timestamp)
            databases_to_delete = [
                database for database in expired_versions if database in databases
            ]

            dropped_dbs = []

            if len(databases) > delete_threshold:
                with engine.connect() as conn:
                    conn.execution_options(isolation_level="AUTOCOMMIT")
                    for database in databases_to_delete:
                        if len(databases) - (len(dropped_dbs) + 1) == 1:
                            celery_logger.info(
                                f"Only one materialized database remaining: {database}, removal stopped."
                            )
                            break
                        if (len(databases) - len(dropped_dbs)) > delete_threshold:
                            try:
                                sql = (
                                    "SELECT 1 FROM pg_database WHERE datname='%s'"
                                    % database
                                )
                                result_proxy = conn.execute(sql)
                                result = result_proxy.scalar()
                                celery_logger.info(
                                    f"Database to be dropped: {database} exists: {result}"
                                )
                                if result:
                                    drop_connections = f"""
                                    SELECT 
                                        pg_terminate_backend(pid) 
                                    FROM 
                                        pg_stat_activity
                                    WHERE 
                                        datname = '{database}'
                                    AND pid <> pg_backend_pid()
                                    """

                                    conn.execute(drop_connections)
                                    celery_logger.info(
                                        f"Dropped connections to: {database}"
                                    )
                                    sql = f"DROP DATABASE {database}"
                                    result_proxy = conn.execute(sql)
                                    celery_logger.info(f"Database: {database} removed")

                                    # strip version from database string
                                    database_version = database.rsplit("__mat")[-1]

                                    expired_database = (
                                        session.query(AnalysisVersion)
                                        .filter(
                                            AnalysisVersion.version == database_version
                                        )
                                        .one()
                                    )
                                    expired_database.valid = False
                                    session.commit()
                                    celery_logger.info(
                                        f"Database '{expired_database}' dropped"
                                    )
                                    dropped_dbs.append(database)
                            except Exception as e:
                                celery_logger.error(
                                    f"ERROR: {e}: {database} does not exist"
                                )
            remove_db_cron_info.append(dropped_dbs)
            session.close()
    return remove_db_cron_info
