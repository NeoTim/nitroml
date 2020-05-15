# Lint as: python3
"""NitroML benchmark pipeline result overview."""

import ast
import datetime
import json
import re
from typing import Dict, Any, List, NamedTuple, Optional

import pandas as pd

from ml_metadata.metadata_store import metadata_store

# Constants
RUN_ID_KEY = 'run_id'
STARTED_AT = 'started_at'
BENCHMARK_FULL_KEY = 'benchmark_fullname'
BENCHMARK_KEY = 'benchmark'
RUN_KEY = 'run'
NUM_RUNS_KEY = 'num_runs'
_DEFAULT_COLUMNS = (STARTED_AT, RUN_ID_KEY, BENCHMARK_KEY, RUN_KEY,
                    NUM_RUNS_KEY)
_DATAFRAME_CONTEXTUAL_COLUMNS = (STARTED_AT, RUN_ID_KEY, BENCHMARK_FULL_KEY,
                                 BENCHMARK_KEY, RUN_KEY, NUM_RUNS_KEY)
_TRAINER = 'my_orchestrator.components.trainer.component.EstimatorTrainer'
_BENCHMARK_RESULT = 'NitroML.BenchmarkResult'
_TRAINER_ID = 'component_id'
_TRAINER_ID_PREFIX = 'EstimatorTrainer'
_HPARAMS = 'hparams'
_NAME = 'name'
_PRODUCER_COMPONENT = 'producer_component'
_STATE = 'state'


class _Result(NamedTuple):
  """Wrapper for properties and property names."""
  properties: Dict[str, Dict[str, Any]]
  property_names: List[str]


def _merge_results(result1: _Result, result2: _Result) -> _Result:
  """Merges two _Result object into one."""
  properties = result1.properties
  for key, props in result2.properties.items():
    if key in properties:
      properties[key].update(props)
    else:
      properties[key] = props
  property_names = result1.property_names + result2.property_names
  return _Result(properties=properties, property_names=property_names)


def _to_pytype(val: str) -> Any:
  """Coverts val to python type."""
  try:
    return json.loads(val.lower())
  except ValueError:
    return val


def _parse_hparams(hp_prop: str) -> Dict[str, Any]:
  """Parses the hparam properties string into hparams dictionary.

  Args:
    hp_prop: hparams properties retrieved from the Executor MLMD component. It
      is a serialized representation of the hparams, e.g. "['batch_size=256']"

  Returns:
    A dictionary containing hparams name and value.
  """
  # Deserialize the hparams. Execution properties are currently serialized in
  # TFX using __str__. See for details:
  # http://google3/third_party/tfx/orchestration/metadata.py?q=function:_update_execution_proto
  # TODO(b/151084437): Move deserialization code to TFX.
  hp_strings = ast.literal_eval(hp_prop)
  hparams = {}
  for hp in hp_strings:
    name, val = hp.split('=')
    hparams[name] = _to_pytype(val)
  return hparams


def _get_hparams(store: metadata_store.MetadataStore) -> _Result:
  """Returns the hparams of the EstimatorTrainer component.

  Args:
    store: MetaDataStore object to connect to MLMD instance.

  Returns:
    A _Result objects with properties containing hparams.
  """
  results = {}
  hparam_names = set()

  trainer_execs = store.get_executions_by_type(_TRAINER)
  for ex in trainer_execs:
    run_id = ex.properties[RUN_ID_KEY].string_value
    hparams = _parse_hparams(ex.properties[_HPARAMS].string_value)
    hparam_names.update(hparams.keys())
    hparams[RUN_ID_KEY] = run_id
    trainer_id = ex.properties[_TRAINER_ID].string_value.replace(
        _TRAINER_ID_PREFIX, '')
    result_key = run_id + trainer_id
    hparams[BENCHMARK_KEY] = trainer_id[1:]  # Removing '.' prefix
    # BeamDagRunner uses iso format timestamp. See for details:
    # http://google3/third_party/tfx/orchestration/beam/beam_dag_runner.py
    try:
      hparams[STARTED_AT] = datetime.datetime.fromtimestamp(int(run_id))
    except ValueError:
      hparams[STARTED_AT] = run_id
    results[result_key] = hparams
  return _Result(properties=results, property_names=sorted(hparam_names))


def _get_artifact_run_id_map(store: metadata_store.MetadataStore,
                             artifact_ids: List[int]) -> Dict[int, str]:
  """Returns a dictionary mapping artifact_id to its MyOrchestrator run_id.

  Args:
    store: MetaDataStore object to connect to MLMD instance.
    artifact_ids: A list of artifact ids to load.

  Returns:
    A dictionary containing artifact_id as a key and MyOrchestrator run_id as value.
  """
  # Get events of artifacts.
  events = store.get_events_by_artifact_ids(artifact_ids)
  exec_to_artifact = {}
  for event in events:
    exec_to_artifact[event.execution_id] = event.artifact_id

  # Get execution of artifacts.
  executions = store.get_executions_by_id(list(exec_to_artifact.keys()))
  artifact_to_run_id = {}
  for execution in executions:
    artifact_to_run_id[exec_to_artifact[
        execution.id]] = execution.properties[RUN_ID_KEY].string_value

  return artifact_to_run_id


def _get_benchmark_results(store: metadata_store.MetadataStore) -> _Result:
  """Returns the benchmark results of the BenchmarkResultPublisher component.

  Args:
    store: MetaDataStore object to connect to MLMD instance.

  Returns:
    A _Result objects with properties containing benchmark results.
  """
  metrics = {}
  property_names = set()
  publisher_artifacts = store.get_artifacts_by_type(_BENCHMARK_RESULT)
  for artifact in publisher_artifacts:
    evals = {}
    for key, val in artifact.custom_properties.items():
      if val.HasField('int_value'):
        evals[key] = val.int_value
      else:
        evals[key] = _to_pytype(val.string_value)
    property_names = property_names.union(evals.keys())
    metrics[artifact.id] = evals

  artifact_to_run_id = _get_artifact_run_id_map(store, list(metrics.keys()))

  properties = {}
  for artifact_id, evals in metrics.items():
    run_id = artifact_to_run_id[artifact_id]
    evals[RUN_ID_KEY] = run_id
    # BeamDagRunner uses iso format timestamp. See for details:
    # http://google3/third_party/tfx/orchestration/beam/beam_dag_runner.py
    try:
      evals[STARTED_AT] = datetime.datetime.fromtimestamp(int(run_id))
    except ValueError:
      evals[STARTED_AT] = run_id
    result_key = run_id + '.' + evals[BENCHMARK_KEY]
    properties[result_key] = evals

  property_names = property_names.difference(
      {_NAME, _PRODUCER_COMPONENT, _STATE, *_DEFAULT_COLUMNS})
  return _Result(properties=properties, property_names=sorted(property_names))


def _make_dataframe(metrics_list: List[Dict[str, Any]],
                    columns: List[str]) -> pd.DataFrame:
  """Makes pandas.DataFrame from metrics_list."""
  df = pd.DataFrame(metrics_list)
  if not df.empty:
    # Reorder columns.
    # Strip benchmark run repetition for aggregation.
    df[BENCHMARK_FULL_KEY] = df[BENCHMARK_KEY]
    df[BENCHMARK_KEY] = df[BENCHMARK_KEY].apply(
        lambda x: re.sub(r'\.run_\d_of_\d$', '', x))

    key_columns = list(_DATAFRAME_CONTEXTUAL_COLUMNS)
    if RUN_KEY not in df:
      key_columns.remove(RUN_KEY)
    if NUM_RUNS_KEY not in df:
      key_columns.remove(NUM_RUNS_KEY)
    df = df[key_columns + columns]

    df = df.set_index([STARTED_AT])

  return df


def _aggregate_results(df: pd.DataFrame,
                       metric_aggregators: Optional[List[Any]],
                       groupby_columns: List[str]):
  """Aggregates metrics in an overview pd.DataFrame."""

  df = df.copy()
  groupby_columns = groupby_columns.copy()
  if RUN_KEY in df:
    df = df.drop([RUN_KEY], axis=1)
  groupby_columns.remove(RUN_KEY)
  groupby_columns.remove(BENCHMARK_FULL_KEY)
  if NUM_RUNS_KEY not in df:
    groupby_columns.remove(NUM_RUNS_KEY)

  # Group by contextual columns and aggregate metrics.
  df = df.groupby(groupby_columns)
  df = df.agg(metric_aggregators)

  # Flatten MultiIndex into a DataFrame.
  df.columns = [' '.join(col).strip() for col in df.columns.values]
  return df.reset_index().set_index('started_at')


def overview(
    store: metadata_store.MetadataStore,
    metric_aggregators: Optional[List[Any]] = None,
) -> pd.DataFrame:
  """Returns a pandas.DataFrame containing hparams and evaluation results.

  This method assumes that `tf.enable_v2_behavior()` was called beforehand.
  It loads results for all evaluation therefore method can be slow.

  TODO(b/151085210): Allow filtering incomplete benchmark runs.

  Assumptions:
    For the given pipeline, MyOrchestrator run_id and component_id of trainer is unique
    and (my_orchestrator_run_id + trainer.component_id-postfix) is equal to
    (my_orchestrator_run_id + artifact.producer_component-postfix).

  Args:
    store: MetaDataStore object for connecting to an MLMD instance.
    metric_aggregators: Iterable of functions and/or function names,
      e.g. [np.sum, 'mean']. Groups individual runs by their contextual features
      (run id, hparams), and aggregates metrics by the given functions. If a
      function, must either work when passed a DataFrame or when passed to
      DataFrame.apply.

  Returns:
    A pandas DataFrame with the loaded hparams and evaluations or an empty one
    if no evaluations and hparams could be found.
  """
  hparams_result = _get_hparams(store)
  metrics_result = _get_benchmark_results(store)
  result = _merge_results(hparams_result, metrics_result)

  # Filter metrics that have empty hparams and evaluation results.
  results_list = [
      result for result in result.properties.values()
      if len(result) > len(_DEFAULT_COLUMNS)
  ]

  df = _make_dataframe(results_list, result.property_names)
  if metric_aggregators:
    return _aggregate_results(
        df,
        metric_aggregators=metric_aggregators,
        groupby_columns=list(_DATAFRAME_CONTEXTUAL_COLUMNS) +
        hparams_result.property_names)
  return df