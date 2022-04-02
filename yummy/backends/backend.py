from datetime import datetime
from typing import Callable, List, Optional, Tuple, Union, Dict, Any
from abc import ABC, abstractmethod
import pandas as pd
import pyarrow
import pytz
from pydantic.typing import Literal

from feast import FileSource, OnDemandFeatureView
from feast.data_source import DataSource
from feast.errors import FeastJoinKeysDuringMaterialization
from feast.feature_view import DUMMY_ENTITY_ID, DUMMY_ENTITY_VAL, FeatureView
from feast.infra.offline_stores.file_source import SavedDatasetFileStorage
from feast.infra.offline_stores.offline_store import (
    OfflineStore,
    RetrievalJob,
    RetrievalMetadata,
)
from feast.infra.offline_stores.offline_utils import (
    DEFAULT_ENTITY_DF_EVENT_TIMESTAMP_COL,
)
from feast.infra.provider import (
    _get_requested_feature_views_to_features_dict,
)
from feast.registry import Registry
from feast.repo_config import FeastConfigBaseModel, RepoConfig
from feast.saved_dataset import SavedDatasetStorage
from feast.usage import log_exceptions_and_usage
from feast.importer import import_class
from enum import Enum
from yummy.sources.source import YummyDataSourceReader

class BackendType(str, Enum):
    dask = "dask"
    ray = "ray"
    spark = "spark"
    polars = "polars"


class BackendConfig(FeastConfigBaseModel):
    ...


class Backend(ABC):
    """
    Backend implements all operations required to process all offline store steps using
    selected engine
    """
    def __init__(self, backend_config: BackendConfig):
        self._backend_config = backend_config

    @abstractmethod
    @property
    def backend_type(self) -> BackendType:
        ...

    @abstractmethod
    @property
    def retrival_job_type(self):
        ...

    @abstractmethod
    def prepare_entity_df(
        self,
        entity_df: Union[pd.DataFrame, Any],
    ) -> Union[pd.DataFrame, Any]:
        """
        Maps entity_df to type required by backend and finds event timestamp column
        """
        ...

    @abstractmethod
    def get_entity_df_event_timestamp_range(
        self,
        entity_df: Union[pd.DataFrame, Any],
    ) -> Tuple[datetime, datetime]:
        """
        Finds min and max datetime in input entity_df data frame
        """
        ...

    @abstractmethod
    def normalize_timezone(
        self,
        entity_df: Union[pd.DataFrame, Any],
    ) -> Union[pd.DataFrame, Any]:
        """
        Normalize timezon of input entity df to UTC
        """
        ...

    @abstractmethod
    def sort_values(
        self,
        entity_df: Union[pd.DataFrame, Any],
        by: str,
    ) -> Union[pd.DataFrame, Any]:
        """
        Sorts entity df by selected column
        """
        ...

    @abstractmethod
    def field_mapping(
        self,
        df_to_join: Union[pd.DataFrame, Any],
        feature_view: FeatureView,
        features: List[str],
        right_entity_key_columns: List[str],
        entity_df_event_timestamp_col: str,
        event_timestamp_column: str,
        full_feature_names: bool,
    ) -> Union[pd.DataFrame, Any]:
        ...

    @abstractmethod
    def merge(
        self,
        entity_df_with_features: Union[pd.DataFrame, Any],
        df_to_join: Union[pd.DataFrame, Any],
        join_keys: List[str],
    ) -> Union[pd.DataFrame, Any]:
        ...

    @abstractmethod
    def normalize_timestamp(
        self,
        df_to_join: Union[pd.DataFrame, Any],
        event_timestamp_column: str,
        created_timestamp_column: str,
    ) -> Union[pd.DataFrame, Any]:
        ...

    @abstractmethod
    def filter_ttl(
        self,
        df_to_join: Union[pd.DataFrame, Any],
        feature_view: FeatureView,
        entity_df_event_timestamp_col: str,
        event_timestamp_column: str,
    ) -> Union[pd.DataFrame, Any]:
        ...

    @abstractmethod
    def filter_time_range(
        self,
        df_to_join: Union[pd.DataFrame, Any],
        event_timestamp_column: str,
        start_date: datetime,
        end_date: datetime,
    ) -> Union[pd.DataFrame, Any]:
        ...

    @abstractmethod
    def drop_duplicates(
        self,
        df_to_join: Union[pd.DataFrame, Any],
        all_join_keys: List[str],
        event_timestamp_column: str,
        created_timestamp_column: Optional[str],
        entity_df_event_timestamp_col: Optional[str] = None,
    ) -> Union[pd.DataFrame, Any]:
        ...

    @abstractmethod
    def drop_columns(
        self,
        df_to_join: Union[pd.DataFrame, Any],
        event_timestamp_column: str,
        created_timestamp_column: str,
    ) -> Union[pd.DataFrame, Any]:
        ...

    @abstractmethod
    def add_static_column(
        self,
        df_to_join: Union[pd.DataFrame, Any],
        column_name: str,
        column_value: str,
    ) -> Union[pd.DataFrame, Any]:
        ...

    @abstractmethod
    def select(
        self,
        df_to_join: Union[pd.DataFrame, Any],
        columns_list: List[str]
    ) -> Union[pd.DataFrame, Any]:
        ...

    def columns_list(
        self,
        entity_df: Union[pd.DataFrame, Any],
    ) -> List[str]:
        """
        Reads columns list
        """
        return entity_df.columns

    def select_dtypes_columns(
        self,
        entity_df: Union[pd.DataFrame, Any],
        include: List[str]
    ) -> List[str]:
        return entity_df.select_dtypes(include=include).columns

    def create_retrival_job(
        self,
        evaluation_function: Callable,
        full_feature_names: bool,
        on_demand_feature_views: Optional[List[OnDemandFeatureView]] = None,
        metadata: Optional[RetrievalMetadata] = None,
    ) -> RetrievalJob:
        self.retrival_job_type()
        return self.retrival_job_type(
            evaluation_function=evaluate_historical_retrieval,
            full_feature_names=full_feature_names,
            on_demand_feature_views=on_demand_feature_views,
            metadata=metadata,
        )

    def read_datasource(
        self,
        data_source,
        features: List[str],
        backend: Backend,
        entity_df: Optional[Union[pd.DataFrame, Any]] = None,
    ) -> Union[pyarrow.Table, pd.DataFrame, Any]:
        """
        Reads data source
        """
        reader: YummyDataSourceReader = data_source.reader_type()
        assert issubclass(reader, YummyDataSourceReader)
        return reader.read_datasource(data_source, features, backend, entity_df)


class BackendFactory:

    @staticmethod
    def create(
        backend_type: BackendType,
        backend_config: BackendConfig)) -> Backend:
        if backend_type == BackendType.dask:
            from yummy.backends.dask import DaskBackend
            return DaskBackend(backend_config)
        elif backend_type == BackendType.ray:
            import ray
            import dask
            ray.init(ignore_reinit_error=True)
            dask.config.set(scheduler=ray_dask_get)
            from yummy.backends.dask import DaskBackend
            return DaskBackend(backend_config)
        elif backend_type == BackendType.spark:
            from yummy.backends.spark import SparkBackend
            return SparkBackend(backend_config)
        elif backend_type == BackendType.polars:
            from yummy.backends.polars import PolarsBackend
            return PolarsBackend(backend_config)

        return PolarsBackend(backend_config)

class YummyOfflineStoreConfig(FeastConfigBaseModel):
    """Offline store config for local (file-based) store"""

    type: Literal["yummy.YummyOfflineStore"] = "yummy.YummyOfflineStore"
    """ Offline store type selector"""

    config: Optional[Dict[str, str]] = None
    """ Configuration """


class YummyOfflineStore(OfflineStore):
    @staticmethod
    @log_exceptions_and_usage(offline_store="yummy")
    def get_historical_features(
        config: RepoConfig,
        feature_views: List[FeatureView],
        feature_refs: List[str],
        entity_df: Union[pd.DataFrame, str],
        registry: Registry,
        project: str,
        full_feature_names: bool = False,
    ) -> RetrievalJob:

        backend_type = config.offline_store.backend
        backend = BackendFactory.create(backend_type, config.offline_store)
        entity_df = backend.prepare_entity_df(entity_df)
        all_columns = backend.columns_list(entity_df_event_timestamp_col)

        entity_df_event_timestamp_col = DEFAULT_ENTITY_DF_EVENT_TIMESTAMP_COL  # local modifiable copy of global variable
        if entity_df_event_timestamp_col not in all_columns:
            datetime_columns = backend.select_dtypes_columns(
                entity_df,
                include=["datetime", "datetimetz"]
            )
            if len(datetime_columns) == 1:
                print(
                    f"Using {datetime_columns[0]} as the event timestamp. To specify a column explicitly, please name it {DEFAULT_ENTITY_DF_EVENT_TIMESTAMP_COL}."
                )
                entity_df_event_timestamp_col = datetime_columns[0]
            else:
                raise ValueError(
                    f"Please provide an entity_df with a column named {DEFAULT_ENTITY_DF_EVENT_TIMESTAMP_COL} representing the time of events."
                )

        (
            feature_views_to_features,
            on_demand_feature_views_to_features,
        ) = _get_requested_feature_views_to_features_dict(
            feature_refs,
            feature_views,
            registry.list_on_demand_feature_views(config.project),
        )

        entity_df_event_timestamp_range = backend.get_entity_df_event_timestamp_range(
            entity_df, entity_df_event_timestamp_col
        )

        # Create lazy function that is only called from the RetrievalJob object
        def evaluate_historical_retrieval():

            entity_df_with_features = backend.normalize_timezone(entity_df)

            entity_df_with_features = backend.sort_values(entity_df_with_features, entity_df_event_timestamp_col)

            join_keys = []
            all_join_keys = []

            # Load feature view data from sources and join them incrementally
            for feature_view, features in feature_views_to_features.items():
                event_timestamp_column = (
                    feature_view.batch_source.event_timestamp_column
                )
                created_timestamp_column = (
                    feature_view.batch_source.created_timestamp_column
                )

                # Build a list of entity columns to join on (from the right table)
                join_keys = []

                for entity_name in feature_view.entities:
                    entity = registry.get_entity(entity_name, project)
                    join_key = feature_view.projection.join_key_map.get(
                        entity.join_key, entity.join_key
                    )
                    join_keys.append(join_key)

                right_entity_key_columns = [
                    event_timestamp_column,
                    created_timestamp_column,
                ] + join_keys
                right_entity_key_columns = [c for c in right_entity_key_columns if c]

                all_join_keys = list(set(all_join_keys + join_keys))

                df_to_join = backend.read_datasource(feature_view.batch_source, features, backend, entity_df_with_features)

                df_to_join, event_timestamp_column = backend.field_mapping(
                    df_to_join,
                    feature_view,
                    features,
                    right_entity_key_columns,
                    entity_df_event_timestamp_col,
                    event_timestamp_column,
                    full_feature_names,
                )

                df_to_join = backend.merge(entity_df_with_features, df_to_join, join_keys)

                df_to_join = backend.normalize_timestamp(
                    df_to_join, event_timestamp_column, created_timestamp_column
                )

                df_to_join = backend.filter_ttl(
                    df_to_join,
                    feature_view,
                    entity_df_event_timestamp_col,
                    event_timestamp_column,
                )

                df_to_join = backend.drop_duplicates(
                    df_to_join,
                    all_join_keys,
                    event_timestamp_column,
                    created_timestamp_column,
                    entity_df_event_timestamp_col,
                )

                entity_df_with_features = backend.drop_columns(
                    df_to_join, event_timestamp_column, created_timestamp_column
                )

                # Ensure that we delete dataframes to free up memory
                del df_to_join

            return entity_df_with_features.persist()

        job = backend.create_retrival_job(
            evaluation_function=evaluate_historical_retrieval,
            full_feature_names=full_feature_names,
            on_demand_feature_views=OnDemandFeatureView.get_requested_odfvs(
                feature_refs, project, registry
            ),
            metadata=RetrievalMetadata(
                features=feature_refs,
                keys=list(set(all_columns) - {entity_df_event_timestamp_col}),
                min_event_timestamp=entity_df_event_timestamp_range[0],
                max_event_timestamp=entity_df_event_timestamp_range[1],
            ),
        )
        return job

    @staticmethod
    @log_exceptions_and_usage(offline_store="yummy")
    def pull_latest_from_table_or_query(
        config: RepoConfig,
        data_source: DataSource,
        join_key_columns: List[str],
        feature_name_columns: List[str],
        event_timestamp_column: str,
        created_timestamp_column: Optional[str],
        start_date: datetime,
        end_date: datetime,
    ) -> RetrievalJob:

        backend_type = config.offline_store.backend
        backend = BackendFactory.create(backend_type, config.offline_store)

        # Create lazy function that is only called from the RetrievalJob object
        def evaluate_offline_job():
            source_df = backend.read_datasource(data_source, feature_name_columns, backend)

            source_df = backend.normalize_timestamp(
                source_df, event_timestamp_column, created_timestamp_column
            )

            all_columns = backend.columns_list(source_df)

            source_columns = set(all_columns)
            if not set(join_key_columns).issubset(source_columns):
                raise FeastJoinKeysDuringMaterialization(
                    data_source.path, set(join_key_columns), source_columns
                )

            ts_columns = (
                [event_timestamp_column, created_timestamp_column]
                if created_timestamp_column
                else [event_timestamp_column]
            )

            source_df = backend.filter_time_range(source_df, event_timestamp_column, start_date, end_date)

            columns_to_extract = set(
                join_key_columns + feature_name_columns + ts_columns
            )
            if join_key_columns:
                source_df = backend.drop_duplicates(source_df, join_key_columns, event_timestamp_column, created_timestamp_column)
            else:
                source_df = backend.add_static_column(source_df, DUMMY_ENTITY_ID, DUMMY_ENTITY_VAL)
                columns_to_extract.add(DUMMY_ENTITY_ID)


            return backend.select(source_df, list(columns_to_extract))

        # When materializing a single feature view, we don't need full feature names. On demand transforms aren't materialized
        return backend.create_retrival_job(
            evaluation_function=evaluate_offline_job, full_feature_names=False,
        )

    @staticmethod
    @log_exceptions_and_usage(offline_store="file")
    def pull_all_from_table_or_query(
        config: RepoConfig,
        data_source: DataSource,
        join_key_columns: List[str],
        feature_name_columns: List[str],
        event_timestamp_column: str,
        start_date: datetime,
        end_date: datetime,
    ) -> RetrievalJob:
        return YummyOfflineStore.pull_latest_from_table_or_query(
            config=config,
            data_source=data_source,
            join_key_columns=join_key_columns
            + [event_timestamp_column],  # avoid deduplication
            feature_name_columns=feature_name_columns,
            event_timestamp_column=event_timestamp_column,
            created_timestamp_column=None,
            start_date=start_date,
            end_date=end_date,
        )

