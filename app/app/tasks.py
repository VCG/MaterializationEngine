import time
import random
import numpy as np

from app import materialize
from celery import Task
from celery import group, chord, chain
from app.schemas import MaterializationSchema, AnalysisVersionSchema
from app import materializationmanager
from annotationframeworkclient.annotationengine import AnnotationClient
from annotationframeworkclient.infoservice import InfoServiceClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.sql import func

import datetime as dt 
from pychunkedgraph.backend import chunkedgraph
from emannotationschemas.models import format_version_db_uri, Base
from emannotationschemas.models import AnalysisVersion, AnalysisTable
from emannotationschemas import models as em_models
from emannotationschemas.base import flatten_dict
from emannotationschemas import get_schema
from app.celery_worker import celery
from dataclasses import dataclass
from multiprocessing.dummy import Pool
from celery.concurrency import eventlet
from flask import current_app
# import eventlet
import logging

logging.debug(f"Celery Settings: {current_app.config}")

SQL_URI = current_app.config['MATERIALIZATION_POSTGRES_URI']
BIGTABLE = current_app.config['BIGTABLE_CONFIG']
CG_TABLE = BIGTABLE['instance_id']
DATASET = BIGTABLE['project_id']
CG_INSTANCE_ID = BIGTABLE['instance_id']
AMDB_INSTANCE_ID = BIGTABLE['amdb_instance_id']
CHUNKGRAPH_TABLE_ID = current_app.config['CHUNKGRAPH_TABLE_ID']

engine = create_engine(SQL_URI, pool_recycle=3600, pool_size=20, max_overflow=50)
Session = scoped_session(sessionmaker(bind=engine, autocommit=False, autoflush=False))
session = Session()
Base.metadata.create_all(engine)

BLACKLIST = ["pni_synapses", "pni_synapses_i2",  "is_chandelier"]

@celery.task(name='process:app.tasks.test_add')
def test_celery(x,y):
    return x + y

class SqlAlchemyTask(Task):
    """An abstract Celery Task that ensures that the connection the the
    database is closed on task completion"""

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        if session is not None:
            Session.remove()

def get_missing_tables(dataset_name: str, analysisversion: int) -> list:    
    tables = session.query(AnalysisTable).filter(AnalysisTable.analysisversion == analysisversion).all()
    
    anno_client = AnnotationClient(dataset_name=dataset_name)
    all_tables = anno_client.get_tables()
    missing_tables_info = [t for t in all_tables 
                           if (t['table_name'] not in [t.tablename for t in tables]) 
                           and (t['table_name']) not in BLACKLIST]
    return missing_tables_info

def get_materialization_metadata(dataset_name: str) -> dict: 
    base_version_number = 1
    base_version = session.query(AnalysisVersion).filter(AnalysisVersion.version==base_version_number).first()
    version = base_version
    analysisversion = session.query(AnalysisVersion).filter(AnalysisVersion.version == base_version_number).first()
    version_db_uri = format_version_db_uri(SQL_URI, dataset_name, version)
    base_version_db_uri = format_version_db_uri(SQL_URI, dataset_name,  base_version_number)
    try:
        info_client  = InfoServiceClient(dataset_name=dataset_name)
        data = info_client.get_dataset_info()
        cg_table_id = data['graphene_source'].rpartition('/')[-1]
    except Exception as e:
        logging.error(f"Could not connect to infoservice: {e}") 
    logging.info(f'Creating new version {analysisversion.version} with timestamp {analysisversion.time_stamp}')
    metadata = {'dataset_name': dataset_name,
            'analysisversion': analysisversion,
            'analysisversion_timestamp': analysisversion.timestamp,
            'base_version_timestamp': base_version.timestamp,
            'base_version': base_version.version,
            'version_db_uri': version_db_uri,
            'base_version_db_uri': base_version_db_uri,
            'cg_table_id': cg_table_id,
            }
    return metadata

@celery.task(base=SqlAlchemyTask, name='process:app.tasks.create_database_from_template')   
def create_database_from_template(template_dataset_name: str, new_database_name: str):
    metadata = get_materialization_metadata(template_dataset_name)
    conn = engine.connect()
    conn.execute("commit")
    conn.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND datname = '{metadata['base_db_name']}';")
    conn.execute(f"create database {new_database_name} TEMPLATE {template_dataset_name}")
    logging.info("Connecting....")

    tables = session.query(AnalysisTable).filter(AnalysisTable.analysisversion == metadata['base_version']).all()

    for table in tables:
        if table.schema != em_models.root_model_name.lower():
            new_analysistable = AnalysisTable(schema=table.schema,
                                              tablename=table.tablename,
                                              valid=False,
                                              analysisversion_id=metadata['analysisversion'].id)
            session.add(new_analysistable)
            session.commit()


def get_max_root_id(dataset_name: str) -> int:
    metadata = get_materialization_metadata(dataset_name)
    analysisversion = metadata['analysisversion']
    base_version_engine = create_engine(metadata['base_version_db_uri'])
    BaseVersionSession = sessionmaker(bind=base_version_engine)
    base_version_session = BaseVersionSession()
    root_model = em_models.make_cell_segment_model(metadata['dataset_name'], 
                                                    version=analysisversion)

    prev_max_id = int(base_version_session.query(func.max(root_model.id).label('max_root_id')).first()[0])
    cg = chunkedgraph.ChunkedGraph(table_id=metadata['cg_table_id'])
    max_root_id = materialize.find_max_root_id_before(cg,
                                                      metadata['base_version_timestamp'],
                                                      2*chunkedgraph.LOCK_EXPIRED_TIME_DELTA,
                                                      start_id=np.uint64(prev_max_id),
                                                      delta_id=100)
    max_seg_id = cg.get_segment_id(np.uint64(max_root_id))
    return max_seg_id

# def materialize_root_ids(dataset_name: str, max_seg_id: int):
#     metadata = get_materialization_metadata(dataset_name)
#     new_roots, old_roots = materialize.materialize_root_ids_delta(cg_table_id=metadata['cg_table_id'],
#                                                                   dataset_name=metadata['dataset_name'],
#                                                                   time_stamp=metadata['analysisversion_timestamp'],
#                                                                   time_stamp_base=metadata['base_version_timestamp'],
#                                                                   min_root_id = max_seg_id,
#                                                                   analysisversion=metadata['analysisversion'],
#                                                                   sqlalchemy_database_uri=metadata['version_db_uri'],
#                                                                   cg_instance_id=CG_INSTANCE_ID)
#     return new_roots, old_roots


@celery.task(name='process:app.tasks.materialize_annotations')
def materialize_annotations(dataset_name: str):
    missing_tables_info = get_missing_tables(dataset_name, 1)
    logging.info(missing_tables_info)

    metadata = get_materialization_metadata(dataset_name)
    for table_info in missing_tables_info:
        materialized_info = materialize.materialize_all_annotations(metadata["cg_table_id"],
                                                metadata["dataset_name"],
                                                table_info['schema_name'],
                                                table_info['table_name'],
                                                analysisversion=metadata['analysisversion'],
                                                time_stamp=metadata['analysisversion_timestamp'],
                                                cg_instance_id=CG_INSTANCE_ID,
                                                sqlalchemy_database_uri=metadata['version_db_uri'],
                                                block_size=100)
        # materialize_root_ids.apply_async(materialized_info)
        at = AnalysisTable(schema=table_info['schema_name'],        
                           tablename=table_info['table_name'],
                           valid=True,
                           analysisversion=metadata['analysisversion'])
        session.add(at)
        session.commit()

@celery.task(name='process:app.tasks.materialize_root_ids')
def materialize_root_ids(args):
    root_ids, serialized_mm_info = args
    model = em_models.make_cell_segment_model(serialized_mm_info["dataset_name"],
                                              serialized_mm_info["version"])
    mm = materializationmanager.MaterializationManager(**serialized_mm_info,
                                                       annotation_model=model)

    annos_dict = {}
    annos_list = []
    for root_id in root_ids:
        ann = {"id": int(root_id)}
        if mm.is_sql:
            # mm.add_annotation_to_sql_database(ann)
            annos_list.append(ann)
        else:
            annos_dict[root_id] = ann

    if not mm.is_sql:
        return annos_dict
    else:
        mm.bulk_insert_annotations(annos_list)
        mm.commit_session()




def materialize_changes(dataset_name: str, old_roots):
    metadata = get_materialization_metadata(dataset_name)
    tables = session.query(AnalysisTable).filter(AnalysisTable.analysisversion == metadata['analysisversion']).all()
    version_engine = create_engine(metadata['version_db_uri'])
    VersionSession = sessionmaker(bind=version_engine)
    version_session = VersionSession()
    version_session.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analysis_user;')
    version_session.execute('GRANT SELECT ON ALL TABLES IN SCHEMA public TO analysis_user;')

    for table in tables:
        if table.schema != em_models.root_model_name.lower():
            delta_info = materialize.materialize_annotations_delta(metadata["cg_table_id"],
                                                                   metadata["dataset_name"],
                                                                   table.tablename,
                                                                   table.schema,
                                                                   old_roots,
                                                                   metadata['analysisversion'],
                                                                   metadata['version_db_uri'],
                                                                   cg_instance_id=CG_INSTANCE_ID,)
        materialize_delta_annotation_task.apply_async(delta_info)
    root_model = em_models.make_cell_segment_model(metadata['dataset_name'], version=metadata['analysisversion'].version)
    version_session.query(root_model).filter(root_model.id.in_(old_roots.tolist())).delete(synchronize_session=False)

    version_session.commit()
    
    new_version = session.query(AnalysisVersion).filter(AnalysisVersion.version==metadata['analysisversion'].version).first()
    new_version.valid = True
    session.commit()

@celery.task(name='process:app.tasks.materialize_delta_annotation_task')
def materialize_delta_annotation_task(args):
    """ Helper for materialize_annotations_delta """
    (block, col, time_stamp,  mm_info, cg_info) = args
    cg = chunkedgraph.ChunkedGraph(**cg_info)
    mm = materializationmanager.MaterializationManager(**mm_info)
    annos_list = []
    for id_, sup_id in block:
        new_root = cg.get_root(sup_id, time_stamp=time_stamp)
        annos_list.append({
            'id': id_,
            col: int(new_root)
        })

    try:
        mm.bulk_update_annotations(annos_list)
        mm.commit_session()
    except Exception as e:
        print(e)
        print("Timestamp:", time_stamp)
        print(annos_list)
        raise Exception(e)