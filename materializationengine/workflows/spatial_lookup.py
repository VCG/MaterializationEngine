import datetime
import functools
import time
from typing import List

import numpy as np
import pandas as pd
from celery.utils.log import get_task_logger
from celery import chain
from cloudvolume.lib import Vec
from geoalchemy2 import Geometry
from sqlalchemy import (
    case,
    func,
    literal,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

from materializationengine.celery_init import celery
from materializationengine.cloudvolume_gateway import cloudvolume_cache
from materializationengine.database import db_manager, dynamic_annotation_cache
from materializationengine.shared_tasks import (
    get_materialization_info,
    workflow_complete,
    add_index,
)
from materializationengine.index_manager import index_cache
from materializationengine.throttle import throttle_celery, get_queue_length
from materializationengine.utils import (
    create_annotation_model,
    create_segmentation_model,
    get_config_param,
    get_geom_from_wkb,
)
from materializationengine.workflows.chunking import ChunkingStrategy
from materializationengine.workflows.ingest_new_annotations import (
    create_missing_segmentation_table,
    get_new_root_ids,
)

from materializationengine.blueprints.upload.checkpoint_manager import (
    RedisCheckpointManager,
)

Base = declarative_base()

celery_logger = get_task_logger(__name__)


@celery.task(
    name="workflow:run_spatial_lookup_workflow",
    bind=True,
    acks_late=True,
    autoretry_for=(Exception,),
    max_retries=1,
    retry_backoff=True,
)
def run_spatial_lookup_workflow(
    self,
    datastack_info: dict,
    table_name: str,
    chunk_scale_factor: int = 1,
    supervoxel_batch_size: int = 50,
    get_root_ids: bool = True,
    upload_to_database: bool = True,
    use_staging_database: bool = False,
    resume_from_checkpoint: bool = True,
):
    """Spatial Lookup Workflow processes a table's points in chunks and inserts supervoxel IDs into the database."""
    task_id = self.request.id
    start_time = time.time()

    # Setup database and checkpoint manager
    staging_database = get_config_param("STAGING_DATABASE_NAME")
    database = (
        staging_database
        if use_staging_database
        else datastack_info["aligned_volume"]["name"]
    )
    checkpoint_manager = RedisCheckpointManager(database)

    # Initialize workflow state
    checkpoint_manager.initialize_workflow(table_name, task_id)

    # Set materialization timestamp
    materialization_time_stamp = datetime.datetime.utcnow()

    # Get table information
    table_info = get_materialization_info(
        datastack_info=datastack_info,
        materialization_time_stamp=materialization_time_stamp,
        table_name=table_name,
        skip_row_count=True,
        database=database,
    )

    # Initialize variables for chunking
    engine = db_manager.get_engine(database)
    completed_chunks = 0

    # Create chunking strategy with appropriate size
    chunking = ChunkingStrategy(
        engine=engine,
        table_name=table_name,
        database=database,
        base_chunk_size=chunk_scale_factor * 1024,
    )

    # Try to restore from checkpoint
    if resume_from_checkpoint:
        checkpoint_data = checkpoint_manager.get_workflow_data(table_name)
        if checkpoint_data:
            completed_chunks = checkpoint_data.completed_chunks or 0
            celery_logger.info(
                f"Resuming from checkpoint: {completed_chunks} chunks already processed"
            )

            # Restore strategy from checkpoint if available
            if hasattr(checkpoint_data, "chunking_strategy"):
                chunking = ChunkingStrategy.from_checkpoint(
                    checkpoint_data, engine, table_name, database
                )

    # Ensure we have a valid strategy - this will determine bounds if needed
    strategy = chunking.select_strategy()

    # Update checkpoint with chunking information
    checkpoint_manager.update_workflow(
        table_name=table_name,
        min_enclosing_bbox=np.array([chunking.min_coords, chunking.max_coords]),
        total_chunks=chunking.total_chunks,
        chunking_strategy=strategy,
        used_chunk_size=chunking.actual_chunk_size,
        completed_chunks=completed_chunks,
        total_row_estimate=chunking.estimated_rows,
        status="processing",
    )

    # Get a chunk generator that starts from the completed chunk index
    if completed_chunks > 0:
        chunk_generator = chunking.skip_to_index(completed_chunks)
    else:
        chunk_generator = chunking.create_chunk_generator()

    # Process each table in the materialization info
    task_count = 0

    for mat_metadata in table_info:
        # Create segmentation table if it doesn't exist
        create_missing_segmentation_table(mat_metadata)

        #  drop existing indices on the table
        index_cache.drop_table_indices(
            mat_metadata["segmentation_table_name"], engine, drop_primary_key=False
        )

        # Submit tasks for each chunk
        chunk_tasks = 0
        for chunk_idx, (min_corner, max_corner) in enumerate(
            chunk_generator(), completed_chunks
        ):
            # Submit a task to process this chunk
            submit_task(
                min_corner=min_corner,
                max_corner=max_corner,
                mat_metadata=mat_metadata,
                get_root_ids=get_root_ids,
                upload_to_database=upload_to_database,
                chunk_idx=chunk_idx,
                total_chunks=chunking.total_chunks,
                database=database,
                table_name=table_name,
                supervoxel_batch_size=supervoxel_batch_size,
            )

            chunk_tasks += 1


            # Throttle if needed
            if mat_metadata.get("throttle_queues"):
                throttle_celery.wait_if_queue_full(queue_name="process")

        task_count += chunk_tasks

        # Final checkpoint update
        if chunk_tasks > 0:
            checkpoint_manager.update_workflow(
                table_name=table_name,
                completed_chunks=completed_chunks + chunk_tasks,
            )

    # Update workflow status on completion
    completion_time = time.time() - start_time
    checkpoint_manager.update_workflow(
        table_name=table_name,
        status="submitted",
        total_time_seconds=completion_time,
    )

    monitor_spatial_lookup_completion.s(
        table_name=table_name,
        database=database,
        total_chunks=chunking.total_chunks,
        queue_name="process",
        table_info=table_info,
    ).apply_async()

    celery_logger.info(
        f"Completed workflow setup in {completion_time:.2f}s, {task_count} tasks submitted"
    )

    return f"Spatial Lookup submitted {task_count} chunks for processing, chunks will be processed in the background"


def submit_task(
    min_corner,
    max_corner,
    mat_metadata,
    get_root_ids,
    upload_to_database,
    chunk_idx,  
    total_chunks,
    database,
    table_name,
    supervoxel_batch_size
):
    """Submit a task to process a single chunk."""
    min_corner_list = (
        min_corner.tolist() if isinstance(min_corner, np.ndarray) else min_corner
    )
    max_corner_list = (
        max_corner.tolist() if isinstance(max_corner, np.ndarray) else max_corner
    )

    task = process_chunk.si(
        min_corner=min_corner_list,
        max_corner=max_corner_list,
        mat_info=mat_metadata,
        get_root_ids=get_root_ids,
        upload_to_database=upload_to_database,
        chunk_info=f"Chunk {chunk_idx+1}/{total_chunks}",
        database=database,
        table_name=table_name,
        report_completion=True,
        supervoxel_batch_size=supervoxel_batch_size,
    )
    task.apply_async()


@celery.task(
    name="process:process_chunk",
    bind=True,
    acks_late=True,
    autoretry_for=(Exception,),
    max_retries=10,
    retry_backoff=True,
    ignore_result=True,
)
def process_chunk(
    self,
    min_corner,
    max_corner,
    mat_info,
    get_root_ids=True,
    upload_to_database=True,
    chunk_info="",
    database=None,
    table_name=None,
    report_completion=False,
    supervoxel_batch_size=50,
):
    """Query points in a bounding box and process supervoxel IDs and root IDS for a single chunk and inserts into the database.

    Args:
        min_corner (_type_): _description_
        max_corner (_type_): _description_
        mat_info (_type_): _description_
        get_root_ids (bool, optional): _description_. Defaults to True.
        upload_to_database (bool, optional): _description_. Defaults to True.
        chunk_info (str, optional): _description_. Defaults to "".
        database (_type_, optional): _description_. Defaults to None.
        table_name (_type_, optional): _description_. Defaults to None.
        report_completion (bool, optional): _description_. Defaults to False.
    """
    task_id = self.request.id
    start_time = time.time()
    celery_logger.debug(
        f"Starting optimized_process_svids [{chunk_info}] (task_id: {task_id})"
    )

    checkpoint_manager = (
        RedisCheckpointManager(database)
        if database and table_name and report_completion
        else None
    )

    try:
        pts_start_time = time.time()
        pts_df = get_pts_from_bbox(np.array(min_corner), np.array(max_corner), mat_info)
        pts_time = time.time() - pts_start_time

        if pts_df is None or pts_df.empty:
            if report_completion and checkpoint_manager:
                checkpoint_manager.increment_completed(
                    table_name=table_name, rows_processed=0
                )
            return None

        points_count = len(pts_df)
        celery_logger.info(f"Found {points_count} points in bounding box")

        svids_start_time = time.time()
        data = get_scatter_points(pts_df, mat_info, batch_size=supervoxel_batch_size)
        if data is None:
            return
        svids_time = time.time() - svids_start_time

        svids_count = len(data["id"])
        celery_logger.info(
            f"Processed {svids_count} supervoxel IDs in {svids_time:.2f}s"
        )

        if get_root_ids and svids_count > 0:
            root_ids_start_time = time.time()
            root_id_data = get_new_root_ids(data, mat_info)
            root_ids_time = time.time() - root_ids_start_time
            celery_logger.info(
                f"Retrieved {len(root_id_data)} root IDs in {root_ids_time:.2f}s"
            )

        if upload_to_database and len(root_id_data) > 0:
            upload_start_time = time.time()
            is_inserted = insert_segmentation_data(root_id_data, mat_info)
            upload_time = time.time() - upload_start_time
            celery_logger.info(
                f"Inserted {len(root_id_data)} rows in {upload_time:.2f}s"
            )

        if report_completion and checkpoint_manager:
            checkpoint_manager.increment_completed(
                table_name=table_name, rows_processed=points_count
            )

        total_time = time.time() - start_time
        celery_logger.debug(
            f"Completed chunk {chunk_info} in {total_time:.2f}s: "
            f"{points_count} points, {svids_count} supervoxels"
        )

        return {
            "status": "success",
            "points_processed": points_count,
            "svids_found": svids_count,
            "processing_time": total_time,
        }

    except Exception as e:
        celery_logger.error(f"Error processing chunk {chunk_info}: {str(e)}")
        self.retry(exc=e, countdown=int(2**self.request.retries))


@celery.task(name="workflow:monitor_spatial_lookup_completion")
def monitor_spatial_lookup_completion(
    table_name: str,
    database: str,
    total_chunks: int,
    queue_name: str = "process",
    table_info: List[dict] = None,
):
    """
    Monitor task completion by checking:
    1. Queue is empty
    2. All chunks processed in checkpoint
    """
    checkpoint_manager = RedisCheckpointManager(database)
    max_wait_time = 3600 * 24 * 3  # 72-hour timeout
    start_time = time.time()
    polling_interval = 360

    while True:
        current_time = time.time()

        # Check queue length
        queue_length = get_queue_length(queue_name)

        workflow_data = checkpoint_manager.get_workflow_data(table_name)

        if workflow_data:
            current_completed = workflow_data.completed_chunks

            # Completion conditions:
            # 1. Queue is empty
            # 2. All chunks processed
            if queue_length == 0 and current_completed >= total_chunks:

                try:

                    rebuild_indices_for_spatial_lookup(table_info, database)

                    # Update workflow status
                    checkpoint_manager.update_workflow(
                        table_name=table_name,
                        status="completed",
                        index_rebuild_complete=True,
                    )

                    celery_logger.info(f"Spatial lookup completed for {table_name}")
                    break
                except Exception as e:
                    celery_logger.error(f"Error in completion process: {e}")
                    checkpoint_manager.update_workflow(
                        table_name=table_name, status="error", last_error=str(e)
                    )
                    break

        # Timeout protection
        if current_time - start_time > max_wait_time:
            celery_logger.error(f"Spatial lookup monitoring timed out for {table_name}")
            checkpoint_manager.update_workflow(
                table_name=table_name, status="error", last_error="Monitoring timed out"
            )
            break

        # Sleep to prevent tight looping
        time.sleep(polling_interval)


def rebuild_indices_for_spatial_lookup(table_info: list, database: str):
    """Rebuild indices for a table after spatial lookup completion."""
    engine = db_manager.get_engine(database)
    mat_metadata = table_info[0]
    segmentation_table_name = mat_metadata["segmentation_table_name"]

    seg_model = create_segmentation_model(mat_metadata)

    # Drop existing indices on the table
    index_cache.drop_table_indices(
        segmentation_table_name, engine, drop_primary_key=True
    )

    seg_indices = index_cache.add_indices_sql_commands(
        table_name=segmentation_table_name, model=seg_model, engine=engine
    )

    if seg_indices:
        add_index_tasks = [add_index.si(database, command) for command in seg_indices]

        # add workflow complete task to the end of the chain
        add_index_tasks.append(
            workflow_complete.si(
                f"Spatial Lookup for {segmentation_table_name} completed"
            )
        )

        # chain the tasks
        chain(add_index_tasks).apply_async()


def get_pts_from_bbox(min_corner, max_corner, mat_info):
    stmt = select(
        [select_all_points_in_bbox(min_corner, max_corner, mat_info)]
    ).compile(compile_kwargs={"literal_binds": True})

    with db_manager.get_engine(mat_info["aligned_volume"]).begin() as connection:
        df = pd.read_sql(stmt, connection)
        # if the dataframe is empty then there are no points in the bounding box
        # so we can skip the rest of the workflow
        if df.empty:
            return None
        df["pt_position"] = df["pt_position"].apply(lambda pt: get_geom_from_wkb(pt))

        return df


def match_point_and_get_value(point, points_map):
    point_tuple = tuple(point)
    return points_map.get(point_tuple, 0)


def normalize_positions(point, scale_factor):
    scaled_point = np.floor(np.array(point) / scale_factor).astype(int)
    return tuple(scaled_point)


def point_to_chunk_position(cv, pt, mip=None):
    """
    Convert a point into the chunk position.

    pt: x,y,z triple
    mip:
      if None, pt is in physical coordinates
      else pt is in the coordinates of the indicated mip level

    Returns: Vec(chunk_x,chunk_y,chunk_z)
    """
    pt = Vec(*pt, dtype=np.float64)

    if mip is not None:
        pt *= cv.resolution(mip)

    pt /= cv.resolution(cv.watershed_mip)

    if cv.chunks_start_at_voxel_offset:
        pt -= cv.voxel_offset(cv.watershed_mip)

    return (pt // cv.graph_chunk_size).astype(np.int32)


def get_scatter_points(pts_df, mat_info, batch_size=500):
    """Process supervoxel ID lookups in smaller batches to improve performance."""
    segmentation_source = mat_info["segmentation_source"]
    coord_resolution = mat_info["coord_resolution"]
    cv = cloudvolume_cache.get_cv(segmentation_source)
    scale_factor = cv.resolution / coord_resolution

    all_points = []
    all_types = []
    all_ids = []
    sv_id_data = {}  # To accumulate supervoxel IDs

    df = pts_df.copy()
    df["pt_position_scaled"] = df["pt_position"].apply(
        lambda x: normalize_positions(x, scale_factor)
    )
    df["chunk_key"] = df.pt_position_scaled.apply(
        lambda x: str(point_to_chunk_position(cv.meta, x, mip=0))
    )

    df = df.sort_values(by="chunk_key")

    total_batches = (len(df) + batch_size - 1) // batch_size
    celery_logger.info(
        f"Processing {len(df)} points in {total_batches} batches of {batch_size}"
    )

    for batch_idx, batch_start in enumerate(range(0, len(df), batch_size)):
        batch_end = min(batch_start + batch_size, len(df))
        batch_df = df.iloc[batch_start:batch_end]

        celery_logger.info(
            f"Processing batch {batch_idx+1}/{total_batches} with {len(batch_df)} points"
        )

        # Get point data
        batch_points = batch_df["pt_position"].tolist()
        batch_types = batch_df["type"].tolist()
        batch_ids = batch_df["id"].tolist()

        # Call scattered_points on this batch
        start_time = time.time()
        batch_sv_data = cv.scattered_points(
            batch_points, coord_resolution=coord_resolution
        )
        elapsed = time.time() - start_time
        celery_logger.info(
            f"Batch {batch_idx+1} scattered_points call took {elapsed:.2f}s"
        )

        # Accumulate results
        all_points.extend(batch_points)
        all_types.extend(batch_types)
        all_ids.extend(batch_ids)
        sv_id_data.update(batch_sv_data)

    result_df = pd.DataFrame(
        {"id": all_ids, "type": all_types, "pt_position": all_points}
    )

    result_df["pt_position_scaled"] = result_df["pt_position"].apply(
        lambda x: normalize_positions(x, scale_factor)
    )
    result_df["svids"] = result_df["pt_position_scaled"].apply(
        lambda x: match_point_and_get_value(x, sv_id_data)
    )

    result_df.drop(columns=["pt_position_scaled"], inplace=True)
    if result_df["type"].str.contains("pt").all():
        result_df["type"] = result_df["type"].apply(lambda x: f"{x}_supervoxel_id")
    else:
        result_df["type"] = result_df["type"].apply(lambda x: f"{x}_pt_supervoxel_id")

    return _safe_pivot_svid_df_to_dict(result_df)


def select_3D_points_in_bbox(
    table_model: str, spatial_column_name: str, min_corner: List, max_corner: List
) -> select:
    """Generate a sqlalchemy statement that selects all points in the bounding box.

    Args:
        table_model (str): Annotation table model
        spatial_column_name (str): Name of the spatial column
        min_corner (List): Min corner of the bounding box
        max_corner (List): Max corner of the bounding box

    Returns:
        select: sqlalchemy statement that selects all points in the bounding box
    """
    start_coord = np.array2string(min_corner).strip("[]")
    end_coord = np.array2string(max_corner).strip("[]")

    # Format raw SQL string
    spatial_column = getattr(table_model, spatial_column_name)
    return select(
        [
            table_model.id.label("id"),
            spatial_column.label("pt_position"),
            literal(spatial_column.name.split("_", 1)[0]).label("type"),
        ]
    ).where(
        spatial_column.intersects_nd(
            func.ST_3DMakeBox(f"POINTZ({start_coord})", f"POINTZ({end_coord})")
        )
    )


def select_all_points_in_bbox(
    min_corner: np.array,
    max_corner: np.array,
    mat_info: dict,
) -> union_all:
    """Iterates through each Point column in the annotation table and creates
    a query of the union of all points in the bounding box.

    Args:
        min_corner (np.array): Min corner of the bounding box
        max_corner (np.array): Max corner of the bounding box
        mat_info (dict): Materialization info for a given table

    Returns:
        union_all: sqlalchemy statement that creates the union of all points
                   for all geometry columns in the bounding box
    """
    db = dynamic_annotation_cache.get_db(mat_info["aligned_volume"])
    table_name = mat_info["annotation_table_name"]
    schema = db.database.get_table_schema(table_name)
    mat_info["schema"] = schema
    AnnotationModel = create_annotation_model(mat_info)
    SegmentationModel = create_segmentation_model(mat_info)

    spatial_columns = []
    for annotation_column in AnnotationModel.__table__.columns:
        if (
            isinstance(annotation_column.type, Geometry)
            and "Z" in annotation_column.type.geometry_type.upper()
        ):
            supervoxel_column_name = (
                f"{annotation_column.name.rsplit('_', 1)[0]}_supervoxel_id"
            )
            # skip lookup for column if not in Segmentation Model
            if getattr(SegmentationModel, supervoxel_column_name, None):
                spatial_columns.append(
                    annotation_column.name
                )  # use column name instead of Column object
            else:
                continue
    selects = [
        select_3D_points_in_bbox(
            AnnotationModel, spatial_column, min_corner, max_corner
        )
        for spatial_column in spatial_columns
    ]
    return union_all(*selects).alias("points_in_bbox")


def convert_array_to_int(value):
    # Check if the value is a NumPy array
    if isinstance(value, np.ndarray):
        # Convert a single-element NumPy array to an integer
        return (
            value[0] if value.size == 1 else 0
        )  # Replace 0 with appropriate default value
    elif isinstance(value, int):
        # If the value is already an integer, return it as is
        return value
    else:
        # Handle other unexpected data types, perhaps with a default value or an error
        return 0


def insert_segmentation_data(
    data: pd.DataFrame,
    mat_info: dict,
):
    """Inserts the segmentation data into the database.

    Args:
        data (pd.DataFrame): Dataframe containing the segmentation data
        mat_info (dict): Materialization info for a given table

    Returns:
        bool: True if the data is inserted, False otherwise
    """

    start_time = time.time()
    database = mat_info["database"]
    table_name = mat_info["annotation_table_name"]
    # pcg_table_name = mat_info["pcg_table_name"]
    db = dynamic_annotation_cache.get_db(database)
    schema = db.database.get_table_schema(table_name)
    mat_info["schema"] = schema
    SegmentationModel = create_segmentation_model(mat_info)
    seg_columns = SegmentationModel.__table__.columns.keys()
    segmentation_dataframe = pd.DataFrame(columns=seg_columns, dtype=object)
    data_df = pd.DataFrame(data, dtype=object)
    supervoxel_id_cols = [
        col for col in data_df.columns if col.endswith("_supervoxel_id")
    ]

    for col in supervoxel_id_cols:
        data_df[col] = data_df[col].apply(convert_array_to_int)

    # find the common columns between the two dataframes
    common_cols = segmentation_dataframe.columns.intersection(data_df.columns)

    # merge the dataframes and fill the missing values with 0, data might get updated in the next chunk lookup
    df = pd.merge(
        segmentation_dataframe[common_cols], data_df[common_cols], how="right"
    )

    # fill the missing values with 0
    df = df.infer_objects().fillna(0)

    # reindex the dataframe to match the order of the columns in the segmentation model
    df = df.reindex(columns=segmentation_dataframe.columns, fill_value=0)

    # convert the dataframe to a list of dictionaries
    data = df.to_dict(orient="records")

    # create the insert statement with on conflict do update clause
    # to update the data if it already exists in the table
    # if the new value is not 0 then update the value, otherwise keep the old (0) value
    stmt = insert(SegmentationModel).values(data)
    do_update_stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            column.name: case(
                [(stmt.excluded[column.name] != 0, stmt.excluded[column.name])],
                else_=column,
            )
            for column in SegmentationModel.__table__.columns
            if column.name != "id"
        },
    )

    # insert the data or update if it already exists
    with db_manager.get_engine(database).begin() as connection:
        connection.execute(do_update_stmt)
    celery_logger.info(f"Insertion time: {time.time() - start_time} seconds")
    return True


def _safe_pivot_svid_df_to_dict(df: pd.DataFrame) -> dict:
    """Custom pivot function to preserve uint64 dtype values."""
    # Check if required columns exist in the DataFrame
    required_columns = ["id", "type", "svids"]
    if any(col not in df.columns for col in required_columns):
        raise ValueError(f"DataFrame must contain columns: {required_columns}")

    # Get the unique column names from the DataFrame
    columns = ["id"] + df["type"].unique().tolist()

    # Initialize an output dict with lists for each column
    output_dict = {col: [] for col in columns}

    # Group the DataFrame by "id" and iterate over each group
    for row_id, group in df.groupby("id"):
        output_dict["id"].append(row_id)

        # Initialize other columns with 0 for the current row_id
        for col in columns[1:]:
            output_dict[col].append(0)

        # Update the values for each type
        for _, row in group.iterrows():
            col_type = row["type"]
            if col_type in output_dict:
                idx = len(output_dict["id"]) - 1
                output_dict[col_type][idx] = row["svids"]

    return output_dict
