import json
import functools

from emannotationschemas.models import make_annotation_model, Base
from emannotationschemas.base import flatten_dict
from emannotationschemas import get_schema
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import numpy as np

class MaterializeAnnotationException(Exception):
    pass


class RootIDNotFoundException(MaterializeAnnotationException):
    pass


class AnnotationParseFailure(MaterializeAnnotationException):
    pass


def lookup_sv_and_cg_bsp(cg, cv, item, pixel_ratios = (1.0, 1.0, 1.0)):
    """ function to fill in the supervoxel_id and root_id

    :param cg: pychunkedgraph.ChunkedGraph
        chunkedgraph instance to use to lookup supervoxel and root_ids
    :param item: dict
        deserialized boundspatialpoint to process
    :parm pixel_ratios: tuple
        ratios to multiple position coordinates to get cg.cv segmentation coordinates
    :return: None
        will edit item in place
    """
    try:
        voxel = np.array(item['position'])*np.array(pixel_ratios)
        sv_id = cv[voxel[0], voxel[1], voxel[2]]
    except:
        msg = "failed to lookup sv_id of voxel {}", voxel
        raise AnnotationParseFailure(msg)

    try:
        root_id = cg.get_root(sv_id)
    except:
        msg = "failed to lookup root_id of sv_id {}", sv_id
        raise AnnotationParseFailure(msg)

    item['supervoxel_id'] = sv_id
    item['root_id'] = root_id

# DEPRECATED
# def materialize_bsp(sv_id_to_root_id_dict, item):
#     """ Function to fill in root_id of BoundSpatialPoint fields
#     using the dictionary provided. Will alter item in place.
#     Meant to be passed to mm.Schema as a context variable ('bsp_fn')
#     :param sv_id_to_root_id_dict: dict
#         dictionary to look up root ids from supervoxel id
#     :param item: dict
#         deserialized boundspatialpoint to process
#     :return: None
#         will edit item in place
#     """

#     try:
#         item['root_id'] = int(sv_id_to_root_id_dict[item['supervoxel_id']])
#     except KeyError:
#         msg = "cannot process {}, root_id not found in {}"
#         raise RootIDNotFoundException(msg.format(item, sv_id_to_root_id_dict))


class MaterializationManager(object):
    def __init__(self, dataset_name, schema_name,
                 table_name, version = 'v1',
                 sqlalchemy_database_uri=None):
        self._dataset_name = dataset_name
        self._schema_name = schema_name
        self._table_name = table_name
        self._sqlalchemy_database_uri = sqlalchemy_database_uri

        self._annotation_model = make_annotation_model(dataset_name,
                                                       schema_name,
                                                       table_name,
                                                       version=version)
        if sqlalchemy_database_uri is not None:
            self._sqlalchemy_engine = create_engine(sqlalchemy_database_uri,
                                                    echo=True)
            Base.metadata.create_all(self.sqlalchemy_engine)

            self._sqlalchemy_session = sessionmaker(bind=self.sqlalchemy_engine)
        else:
            self._sqlalchemy_engine = None
            self._sqlalchemy_session = None

        self._schema_init = get_schema(self.schema_name)

    @property
    def schema_name(self):
        return self._schema_name

    @property
    def table_name(self):
        return self._table_name

    @property
    def dataset_name(self):
        return self._dataset_name

    @property
    def sqlalchemy_database_uri(self):
        return self._sqlalchemy_database_uri

    @property
    def sqlalchemy_engine(self):
        return self._sqlalchemy_engine

    @property
    def sqlalchemy_session(self):
        return self._sqlalchemy_session

    @property
    def annotation_model(self):
        return self._annotation_model

    @property
    def is_sql(self):
        return not self.sqlalchemy_database_uri is None

    @property
    def schema_init(self):
        return self._schema_init

    def get_serialized_info(self):
        """ Puts all initialization parameters into a dict

        :return: dict
        """
        info = {"dataset_name": self.dataset_name,
                "schema_name": self.schema_name,
                "table_name": self.table_name,
                "sqlalchemy_database_uri": self.sqlalchemy_database_uri}

        return info

    def _drop_table(self):
        """ Deletes the table in the database """
        table_name = "%s_%s" % (self.dataset_name, self.schema_name)
        Base.metadata.tables[table_name].drop(self.sqlalchemy_engine)

    def get_schema(self, cg, cv, pixel_ratios = (1.0, 1.0, 1.0)):
        """ Loads schema with appropriate context

        :param cg: pychunkedgraph.ChunkedGraph
            chunkedgraph instance to lookup root_ids from atomic_ids
        :param cv: cloudvolume.CloudVolume
            cloudvolue instanceto look_up atomic_ids
        :param pixel_ratios: tuple 
            ratio of position units to units of cv
        :return:
        """
        context = dict()
        context['bsp_fn'] = functools.partial(lookup_sv_and_cg_bsp,
                                              cg, cv, pixel_ratios=pixel_ratios)
        context['postgis'] = self.is_sql
        return self.schema_init(context=context)

    def deserialize_single_annotation(self, blob, cg, cv, pixel_ratios=(1.0, 1.0, 1.0)):
        """ Materializes single annotation object

        :param blob: binary
        :param sv_id_to_root_id_dict: dict
        :return: dict
        """
        print(self.schema_name)
        schema = self.get_schema(cg, cv, pixel_ratios=pixel_ratios)
        data = schema.load(json.loads(blob)).data

        return flatten_dict(data)

    def add_annotation_to_sql_database(self, deserialized_annotation):
        """ Transforms annotation object into postgis format and commits to the
            database

        :param deserialized_annotation: dict
        """
        assert self.is_sql

        # remove the type field because we don't want it as a column
        deserialized_annotation.pop('type', None)

        # # create a new model instance with data
        annotation = self.annotation_model(**deserialized_annotation)

        # create a new db session
        this_session = self.sqlalchemy_session()

        # add this annotation object to database
        this_session.add(annotation)

        # commit this transaction to database
        this_session.commit()
