from __future__ import annotations

import abc
import collections
import logging
import re
import time
from datetime import datetime
import pytz
import uuid
from queue import PriorityQueue
from typing import OrderedDict, Dict, Type, Set, Union, Optional, List
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader
from kubeflow.training import PyTorchJobClient
from kubeflow.training.constants.constants import PYTORCHJOB_GROUP, PYTORCHJOB_VERSION, PYTORCHJOB_PLURAL
from kubernetes import client
from kubernetes.client import V1ConfigMap, V1ObjectMeta

from fltk.core.distributed.dist_node import DistNode
from fltk.util.cluster.client import construct_job, ClusterManager
from fltk.util.task import get_job_arrival_class, DistributedArrivalTask, FederatedArrivalTask, ArrivalTask
from fltk.util.task.arrival_task import HistoricalArrivalTask, _ArrivalTask
from fltk.util.task.generator import ArrivalGenerator

if TYPE_CHECKING:
    from fltk.util.config import DistributedConfig


# Setup required variables for Jinja templates.
EXPERIMENT_DIR = 'experiments'
__ENV = Environment(loader=FileSystemLoader(EXPERIMENT_DIR))


def _generate_experiment_path_name(task: ArrivalTask, u_id: Union[uuid.UUID, str], config: DistributedConfig):
    """
    Helper function to generate experiment name for logging without conflicts
    @param task: Arrival task for Task related information.
    @type task: ArrivalTask
    @param u_id: Unique identifier string corresponding to the experiment.
    @type u_id: str
    @param config: Distributed configuration for logging directory configuration.
    @type config: DistributedConfig
    @return: String representation of the logging path for a specific experiment.
    @rtype: str
    """
    log_dir = config.execution_config.log_path
    experiment_path = config.execution_config.experiment_prefix
    experiment_name = f"{task.dataset}_{task.network}_{u_id}_{task.replication}"
    full_path = f"{log_dir}/{experiment_path}/{experiment_name}"
    return full_path


def render_template(task: ArrivalTask, tpe: str, replication: int, experiment_path: str) -> str:
    """
    Helper function to render jinja templates with necessary arguments for experiment (types). These templates are
    used for generating ConfigMaps used by the Pods that perform the learning experiments.
    @param task: Arrival description of Experiment/Deployment.
    @type task: ArrivalTask
    @param tpe: Indicator to distinct between 'learner' and 'parameter server'/'federator'
    @type tpe: str
    @param replication: Count for the replication of an experiment.
    @type replication: int
    @param experiment_path: Path where the experiment folder resides.
    @type experiment_path: str
    @return: Rendered template containing the content of a ConfigMap for a learner of `tpe` for the provided task.
    @rtype: str
    """
    if isinstance(task, FederatedArrivalTask):
        template = __ENV.get_template('node.jinja.yaml')
    elif isinstance(task, DistributedArrivalTask):
        template = __ENV.get_template('dist_node.jinja.yaml')
    else:
        raise Exception(f"Cannot handle type of task: {task}")
    filled_template = template.render(task=task, tpe=tpe, replication=replication, experiment_path=experiment_path)
    return filled_template


def _prepare_experiment_maps(task: ArrivalTask, config: DistributedConfig, u_id: uuid.UUID, replication: int = 1) -> \
        (OrderedDict[str, V1ConfigMap], OrderedDict[str, str]):
    """
    Helper private function to create ConfigMap descriptions for a deployment of learners.
    @param task: Task description object.
    @type task: ArrivalTask
    @param config:
    @type config:
    @param u_id:
    @type u_id:
    @param replication:
    @type replication:
    @return:
    @rtype:
    """
    type_dict = collections.OrderedDict()
    name_dict = collections.OrderedDict()
    for tpe in task.type_map.keys():
        name = str(f'{tpe}-{u_id}-{replication}').lower()
        meta = V1ObjectMeta(name=name,
                            labels={'app.kubernetes.io/name': f"fltk.node.config.{tpe}"})
        exp_path = _generate_experiment_path_name(task, u_id, config)
        filled_template = render_template(task=task, tpe=tpe, replication=replication, experiment_path=exp_path)
        type_dict[tpe] = V1ConfigMap(data={'node.config.yaml': filled_template}, metadata=meta)
        name_dict[tpe] = name
    return type_dict, name_dict


def _generate_task(arrival) -> ArrivalTask:
    """
    Function to generate a task from an Arrival.
    @param arrival: Arrival to create a (runnable) Task from.
    @type arrival: Arrival
    @return: Mapped ArrivalTask for the given task.
    @rtype: ArrivalTask
    """
    unique_identifier: uuid.UUID = uuid.uuid4()
    task_type: Type[ArrivalTask] = get_job_arrival_class(arrival.task.experiment_type)
    task = task_type.build(arrival=arrival,
                           u_id=unique_identifier,
                           replication=arrival.task.replication)
    return task


class Orchestrator(DistNode, abc.ABC):
    """
    Central component of the Federated Learning System: The Orchestrator.

    The Orchestrator is in charge of the following tasks:
    - Running experiments
        - Creating and/or managing tasks
        - Keep track of progress (pending/started/failed/completed)
    - Keep track of timing

    Note that the Orchestrator does not function like a Federator, in the sense that it keeps a central model, performs
    aggregations and keeps track of Clients. For this, the KubeFlow PyTorch-Operator is used to deploy a train task as
    a V1PyTorchJob, which automatically generates the required setup in the cluster. In addition, this allows more Jobs
    to be scheduled, than that there are resources, as such, letting the Kubernetes Scheduler let decide when to run
    which containers where.
    """
    _alive = False
    # Priority queue, requires an orderable object, otherwise a Tuple[int, Any] can be used to insert.
    pending_tasks: "PriorityQueue[ArrivalTask]" = PriorityQueue()
    deployed_tasks: Set[_ArrivalTask] = set()
    completed_tasks: Set[_ArrivalTask] = set()
    SLEEP_TIME = 5

    def __init__(self, cluster_mgr: ClusterManager, arv_gen: ArrivalGenerator, config: DistributedConfig):
        self._logger = logging.getLogger('Orchestrator')
        self._logger.debug("Loading in-cluster configuration")
        self._cluster_mgr = cluster_mgr
        self._arrival_generator = arv_gen
        self._config = config
        self._logger.info(f"Running in parallel_execution mode: {self._config.cluster_config.orchestrator.parallel_execution}")

        # API to interact with the cluster.
        self._client = PyTorchJobClient()
        self._v1 = client.CoreV1Api()

    def stop(self) -> None:
        """
        Stop the Orchestrator.
        @return: None
        @rtype: None
        """
        self._logger.info("Received stop signal for the Orchestrator.")
        self._alive = False

        self._cluster_mgr.stop()

    @abc.abstractmethod
    def run(self, clear: bool = False, experiment_replication: int = -1) -> None:
        """
        Main loop of the Orchestrator for simulated arrivals. By default, previous deployments are not stopped (i.e.
        PytorchTrainingJobs) on the cluster, which may interfere with utilization statistics of your cluster.
        Make sure to check if you want previous results to be removed.
        @param clear: Boolean indicating whether a previous deployment needs to be cleaned up (i.e. lingering jobs that
        were deployed by the previous run).
        @type clear: bool
        @param experiment_replication: Replication index (integer) to allow for the logging to experiment specific
        directories for experiments.
        @type experiment_replication: int
        @return: None
        @rtype: None
        """

    def _clear_jobs(self):
        """
        Function to clear existing jobs in the environment (i.e. old experiments/tests). This will will, currently,
        not remove configuration map objects. A later version will allow for removing these autmatically as well.
        @return: None
        @rtype: None
        """
        namespace = self._config.cluster_config.namespace
        self._logger.info(f'Clearing old jobs in current namespace: {namespace}')

        for job in self._client.get(namespace=self._config.cluster_config.namespace)['items']:
            job_name = job['metadata']['name']
            self._logger.info(f'Deleting: {job_name}')
            try:
                self._client.custom_api.delete_namespaced_custom_object(
                        PYTORCHJOB_GROUP,
                        PYTORCHJOB_VERSION,
                        namespace,
                        PYTORCHJOB_PLURAL,
                        job_name)
            except Exception as excp:
                self._logger.warning(f'Could not delete: {job_name}. Reason: {excp}')

    def _create_config_maps(self, config_maps: Dict[str, V1ConfigMap]) -> None:
        """
        Private helper function to generate V1ConfigMap resources that are to be attached to the different trainers.
        This allows for dynamic deployment with generated configuration files.
        """
        for _, config_map in config_maps.items():
            self._v1.create_namespaced_config_map(self._config.cluster_config.namespace,
                                                  config_map)

    def wait_for_jobs_to_complete(self, others: Optional[List[str]] = None):
        """
        Function to wait for all tasks to complete. This allows to wait for all the resources to free-up after running
        an experiment. Thereby allowing for running multiple experiments on a single cluster, without letting
        experiments interfere with each other.
        """
        if others:
            uuid_regex = re.compile("[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

            ids = {uuid_regex.search(task).group() for task in others if uuid_regex.search(task) is not None}
            historical_tasks = map(HistoricalArrivalTask, ids)
            self.deployed_tasks.update(historical_tasks)
        while len(self.deployed_tasks) > 0:
            task_to_move = set()
            for task in self.deployed_tasks:
                try:
                    job_status = self._client.get_job_status(name=f"trainjob-{task.id}",
                                                             namespace='test')
                except Exception as e:
                    logging.debug(msg=f"Could not retrieve job_status for {task.id}")
                    job_status = None

                if job_status and job_status in {'Completed', 'Failed', 'Succeeded'}:
                    logging.info(f"{task.id} was completed with status: {job_status}, moving to completed")
                    task_to_move.add(task)
                else:
                    logging.info(f"Waiting for {task.id} to complete, {self.pending_tasks.qsize()} pending, {self._arrival_generator.arrivals.qsize()} arrivals")
            self.completed_tasks.update(task_to_move)
            self.deployed_tasks.difference_update(task_to_move)
            time.sleep(self.SLEEP_TIME)


class SimulatedOrchestrator(Orchestrator):
    """
    Orchestrator implementation for Simulated arrivals. Currently, only inter-arrival times following a Poisson process
    are supported.
    """

    def __init__(self, cluster_mgr: ClusterManager, arrival_generator: ArrivalGenerator, config: DistributedConfig):
        super().__init__(cluster_mgr, arrival_generator, config)

    def run(self, clear: bool = False, experiment_replication: int = -1) -> None:
        self._alive = True
        start_time = time.time()
        if clear:
            self._clear_jobs()
        while self._alive and time.time() - start_time < self._config.get_duration():
            # 1. Check arrivals
            # If new arrivals, store them in arrival list
            while not self._arrival_generator.arrivals.empty():
                arrival = self._arrival_generator.arrivals.get()
                task = _generate_task(arrival)
                current_time = datetime.now(pytz.timezone("Europe/Amsterdam"))
                self._logger.info("****************** NEW ARRIVAL ******************")
                self._logger.info(f"Arrival of: {task} at {current_time}")
                self._logger.info("*************************************************")
                self.pending_tasks.put(task)

            # Deploy all pending tasks without logic
            while not self.pending_tasks.empty():
                curr_task: ArrivalTask = self.pending_tasks.get()
                self._logger.info(f"Scheduling arrival of Arrival: {curr_task.id}")
                # Create persistent logging information. A these will not be deleted by the Orchestrator, as such, they
                # allow you to retrieve information of experiments after removing the PytorchJob after completion.
                config_dict, configmap_name_dict = _prepare_experiment_maps(curr_task,
                                                                            config=self._config,
                                                                            u_id=curr_task.id,
                                                                            replication=experiment_replication)
                self._create_config_maps(config_dict)

                job_to_start = construct_job(self._config, curr_task, configmap_name_dict)
                self._logger.info(f"Deploying on cluster: {curr_task.id}")
                self._client.create(job_to_start, namespace=self._config.cluster_config.namespace)
                self.deployed_tasks.add(curr_task)

                # TODO: Extend this logic in your real project, this is only meant for demo purposes
                # For now we exit the thread after scheduling a single task.
                if not self._config.cluster_config.orchestrator.parallel_execution:
                    self.wait_for_jobs_to_complete()

            self._logger.info("Still alive...")
            # Prevent high cpu utilization by sleeping between checks.
            time.sleep(self.SLEEP_TIME)
        self.stop()
        if self._config.cluster_config.orchestrator.parallel_execution:
            self.wait_for_jobs_to_complete()
        self._logger.info('Experiment completed.')


class BatchOrchestrator(Orchestrator):
    """
    Orchestrator implementation to allow for running all experiments that were defined in one go.
    """

    def __init__(self, cluster_mgr: ClusterManager, arrival_generator: ArrivalGenerator, config: DistributedConfig):
        super().__init__(cluster_mgr, arrival_generator, config)

    def run(self, clear: bool = False,
            experiment_replication: int = 1,
            wait_historical: bool = True) -> None:
        """
        Main loop of the Orchestrator for processing a configuration as a batch, i.e. deploy all-at-once (batch)
        without any scheduling or simulation applied. This will make use of Kubeflow Training-operators to ensure that
        pods are created with sufficient resources (depending on resources available on your cluster).
        @param clear: Boolean indicating whether a previous deployment needs to be cleaned up (i.e. lingering jobs that
        were deployed by the previous run).
        @type clear: bool
        @return: None
        @rtype: None
        """
        self._logger.info(f"Starting experiment Orchestrator: {experiment_replication}")
        self._alive = True
        try:
            if wait_historical:
                curr_jobs = self._client.get(namespace="test")
                jobs = [job['metadata']['name'] for job in curr_jobs['items']]
                self.wait_for_jobs_to_complete(others=jobs)
            start_time = time.time()

            if clear:
                self._clear_jobs()
        except Exception as e:
            self._logger.warning(f"Failed during house keeping: {e}")

        duration = self._config.get_duration()
        # In case client does not generate experiment in-time

        # TODO: Add test suite for batch orchestrator
        while self._arrival_generator.arrivals.qsize() == 0:
            self._logger.info("Waiting for first arrival!")
            time.sleep(self.SLEEP_TIME)
        # 1. Check arrivals
        # If new arrivals, store them in arrival PriorityQueue
        while not self._arrival_generator.arrivals.empty():
            arrival = self._arrival_generator.arrivals.get()
            task = _generate_task(arrival)
            self._logger.debug(f"Arrival of: {task}, priority: {task.priority}")
            self.pending_tasks.put(task)

        # 2. Schedule all tasks that arrived previously
        while not self.pending_tasks.empty():
            # Do blocking request to priority queue
            curr_task: ArrivalTask = self.pending_tasks.get()
            self._logger.info(f"Scheduling arrival of Arrival: {curr_task.id}, priority: {curr_task.priority}")

            # Create persistent logging information. A these will not be deleted by the Orchestrator, as such
            # allow you to retrieve information of experiments even after removing the PytorchJob after completion.
            config_dict, configmap_name_dict = _prepare_experiment_maps(curr_task,
                                                                        config=self._config,
                                                                        u_id=curr_task.id,
                                                                        replication=experiment_replication)
            self._create_config_maps(config_dict)

            job_to_start = construct_job(self._config, curr_task, configmap_name_dict)
            self._logger.info(f"Deploying on cluster: {curr_task.id}")
            self._client.create(job_to_start, namespace=self._config.cluster_config.namespace)
            self.deployed_tasks.add(curr_task)
            # Either wait to complete, or continue. Note that the orchestrator currently does not support scaling
            # experiments up or down.

            if not self._config.cluster_config.orchestrator.parallel_execution:
                self.wait_for_jobs_to_complete()
            # time.sleep(15)

        if self._config.cluster_config.orchestrator.parallel_execution:
            self.wait_for_jobs_to_complete()
        logging.info('Experiment completed.')
        # Stop experiment
        self.stop()


class BatchOrchestrator(Orchestrator):
    """
    Orchestrator implementation to allow for running all experiments that were defined in one go.
    """

    def __init__(self, cluster_mgr: ClusterManager, arrival_generator: ArrivalGenerator, config: DistributedConfig):
        super().__init__(cluster_mgr, arrival_generator, config)

    def run(self, clear: bool = False,
            experiment_replication: int = 1,
            wait_historical: bool = True) -> None:
        """
        Main loop of the Orchestrator for processing a configuration as a batch, i.e. deploy all-at-once (batch)
        without any scheduling or simulation applied. This will make use of Kubeflow Training-operators to ensure that
        pods are created with sufficient resources (depending on resources available on your cluster).
        @param clear: Boolean indicating whether a previous deployment needs to be cleaned up (i.e. lingering jobs that
        were deployed by the previous run).
        @type clear: bool
        @return: None
        @rtype: None
        """
        self._logger.info(f"Starting experiment Orchestrator: {experiment_replication}")
        self._alive = True
        try:
            if wait_historical:
                curr_jobs = self._client.get(namespace="test")
                jobs = [job['metadata']['name'] for job in curr_jobs['items']]
                self.wait_for_jobs_to_complete(others=jobs)
            start_time = time.time()

            if clear:
                self._clear_jobs()
        except Exception as e:
            self._logger.warning(f"Failed during house keeping: {e}")

        duration = self._config.get_duration()
        # In case client does not generate experiment in-time

        # TODO: Add test suite for batch orchestrator
        while self._arrival_generator.arrivals.qsize() == 0:
            self._logger.info("Waiting for first arrival!")
            time.sleep(self.SLEEP_TIME)
        # 1. Check arrivals
        # If new arrivals, store them in arrival PriorityQueue
        while not self._arrival_generator.arrivals.empty():
            arrival = self._arrival_generator.arrivals.get()
            task = _generate_task(arrival)
            self._logger.debug(f"Arrival of: {task}, priority: {task.priority}")
            self.pending_tasks.put(task)

        # 2. Schedule all tasks that arrived previously
        i = 1
        while not self.pending_tasks.empty():
            # Do blocking request to priority queue
            curr_task: ArrivalTask = self.pending_tasks.get()
            self._logger.info(f"Scheduling arrival of Arrival: {curr_task.id}, priority: {curr_task.priority}")

            # Create persistent logging information. A these will not be deleted by the Orchestrator, as such
            # allow you to retrieve information of experiments even after removing the PytorchJob after completion.
            config_dict, configmap_name_dict = _prepare_experiment_maps(curr_task,
                                                                        config=self._config,
                                                                        u_id=curr_task.id,
                                                                        replication=experiment_replication)
            self._create_config_maps(config_dict)

            job_to_start = construct_job(self._config, curr_task, configmap_name_dict)
            self._logger.info(f"Deploying on cluster: {curr_task.id}")
            self._client.create(job_to_start, namespace=self._config.cluster_config.namespace)
            self.deployed_tasks.add(curr_task)
            # Either wait to complete, or continue. Note that the orchestrator currently does not support scaling
            # experiments up or down.

            if not self._config.cluster_config.orchestrator.parallel_execution:
                self.wait_for_jobs_to_complete()
            i += 1
            # time.sleep(15)

        if self._config.cluster_config.orchestrator.parallel_execution:
            self.wait_for_jobs_to_complete()
        logging.info('Experiment completed.')
        # Stop experiment
        self.stop()

class OptimizedSimulatedOrchestrator(Orchestrator):
    """
    Orchestrator implementation for Simulated arrivals. Currently, only inter-arrival times following a Poisson process
    are supported.
    """

    def __init__(self, cluster_mgr: ClusterManager, arrival_generator: ArrivalGenerator, config: DistributedConfig):
        super().__init__(cluster_mgr, arrival_generator, config)

    def _optimize_epochs(self, task: ArrivalTask) -> int:
        """
        Optimizes the number of epochs to be run for a given task, based on the current state of the cluster.
        @param task: Task to optimize epochs for.
        @type task: ArrivalTask
        @return: Number of epochs to run for this task.
        @rtype: int
        """
        # Values obtained from regression analysis
        INTERCEPT = -3.1629
        SLOPE = 27.0135
        BASE_EPOCHS = 10

        def estimated_time(task1: ArrivalTask) -> float:
            return INTERCEPT + SLOPE * task1.hyper_parameters.default.total_epochs

        def estimate_epochs(time: float) -> int:
            return int((time - INTERCEPT) / SLOPE)

        current_queue_time = 0
        for pending_task in self.pending_tasks.queue:
            current_queue_time += estimated_time(pending_task)

        allocated_time = self.total_time - current_queue_time
        task.set_epochs(min(max(estimate_epochs(allocated_time), 1), BASE_EPOCHS))
        return task

    def run(self, clear: bool = False, experiment_replication: int = -1) -> None:
        self._alive = True
        start_time = time.time()
        if clear:
            self._clear_jobs()
        while self._alive and time.time() - start_time < self._config.get_duration():
            # 1. Check arrivals
            # If new arrivals, store them in arrival list
            while not self._arrival_generator.arrivals.empty():
                arrival = self._arrival_generator.arrivals.get()
                task = _generate_task(arrival)
                current_time = datetime.now(pytz.timezone("Europe/Amsterdam"))
                self._logger.info("****************** NEW ARRIVAL ******************")
                self._logger.info(f"Arrival of: {task} at {current_time}")
                self._logger.info("*************************************************")

                task = self._optimize_epochs(task)

                self.pending_tasks.put(task)

            # Deploy all pending tasks without logic
            while not self.pending_tasks.empty():
                curr_task: ArrivalTask = self.pending_tasks.get()
                self._logger.info(f"Scheduling arrival of Arrival: {curr_task.id}")
                # Create persistent logging information. A these will not be deleted by the Orchestrator, as such, they
                # allow you to retrieve information of experiments after removing the PytorchJob after completion.
                config_dict, configmap_name_dict = _prepare_experiment_maps(curr_task,
                                                                            config=self._config,
                                                                            u_id=curr_task.id,
                                                                            replication=experiment_replication)
                self._create_config_maps(config_dict)

                job_to_start = construct_job(self._config, curr_task, configmap_name_dict)
                self._logger.info(f"Deploying on cluster: {curr_task.id}")
                self._client.create(job_to_start, namespace=self._config.cluster_config.namespace)
                self.deployed_tasks.add(curr_task)

                # TODO: Extend this logic in your real project, this is only meant for demo purposes
                # For now we exit the thread after scheduling a single task.
                if not self._config.cluster_config.orchestrator.parallel_execution:
                    self.wait_for_jobs_to_complete()

            self._logger.info("Still alive...")
            # Prevent high cpu utilization by sleeping between checks.
            time.sleep(self.SLEEP_TIME)
        self.stop()
        if self._config.cluster_config.orchestrator.parallel_execution:
            self.wait_for_jobs_to_complete()
        self._logger.info('Experiment completed.')
