# -*- coding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os

from airflow import configuration as conf
from airflow.configuration import AirflowConfigException
from airflow.utils.file import mkdirs


class FileTaskHandler(logging.Handler):
    """
    FileTaskHandler is a python log handler that handles and reads
    task instance logs. It creates and delegates log handling
    to `logging.FileHandler` after receiving task instance context.
    It reads logs from task instance's host machine.
    """

    def __init__(self, base_log_folder, filename_template):
        """
        :param base_log_folder: Base log folder to place logs.
        :param filename_template: template filename string
        """
        super(FileTaskHandler, self).__init__()
        self.handler = None
        self.local_base = base_log_folder
        self.filename_template = filename_template

    def set_context(self, ti):
        """
        Provide task_instance context to airflow task handler.
        :param ti: task instance object
        """
        local_loc = self._init_file(ti)
        self.handler = logging.FileHandler(local_loc)
        self.handler.setFormatter(self.formatter)
        self.handler.setLevel(self.level)

    def emit(self, record):
        if self.handler is not None:
            self.handler.emit(record)

    def flush(self):
        if self.handler is not None:
            self.handler.flush()

    def close(self):
        if self.handler is not None:
            self.handler.close()

    def _read(self, ti, try_number):
        """
        Template method that contains custom logic of reading
        logs given the try_number.
        :param ti: task instance record
        :param try_number: current try_number to read log from
        :return: log message as a string
        """
        # Task instance here might be different from task instance when
        # initializing the handler. Thus explicitly getting log location
        # is needed to get correct log path.
        log_relative_path = self.filename_template.format(
            dag_id=ti.dag_id, task_id=ti.task_id,
            execution_date=ti.execution_date.isoformat(), try_number=try_number + 1)
        loc = os.path.join(self.local_base, log_relative_path)
        log = ""

        if os.path.exists(loc):
            try:
                with open(loc) as f:
                    log += "*** Reading local log.\n" + "".join(f.readlines())
            except Exception as e:
                log = "*** Failed to load local log file: {}. {}\n".format(loc, str(e))
        else:
            url = os.path.join("http://{ti.hostname}:{worker_log_server_port}/log",
                               log_relative_path).format(
                ti=ti,
                worker_log_server_port=conf.get('celery', 'WORKER_LOG_SERVER_PORT'))
            log += "*** Log file isn't local.\n"
            log += "*** Fetching here: {url}\n".format(**locals())
            try:
                import requests
                timeout = None  # No timeout
                try:
                    timeout = conf.getint('webserver', 'log_fetch_timeout_sec')
                except (AirflowConfigException, ValueError):
                    pass

                response = requests.get(url, timeout=timeout)
                response.raise_for_status()
                log += '\n' + response.text
            except Exception as e:
                log += "*** Failed to fetch log file from worker. {}\n".format(str(e))

        return log

    def read(self, task_instance, try_number=None):
        """
        Read logs of given task instance from local machine.
        :param task_instance: task instance object
        :param try_number: task instance try_number to read logs from. If None
                           it returns all logs separated by try_number
        :return: a list of logs
        """
        # Task instance increments its try number when it starts to run.
        # So the log for a particular task try will only show up when
        # try number gets incremented in DB, i.e logs produced the time
        # after cli run and before try_number + 1 in DB will not be displayed.
        next_try = task_instance.try_number

        if try_number is None:
            try_numbers = list(range(next_try))
        elif try_number < 0:
            logs = ['Error fetching the logs. Try number {} is invalid.'.format(try_number)]
            return logs
        else:
            try_numbers = [try_number]

        logs = [''] * len(try_numbers)
        for i, try_number in enumerate(try_numbers):
            logs[i] += self._read(task_instance, try_number)

        return logs

    def _init_file(self, ti):
        """
        Create log directory and give it correct permissions.
        :param ti: task instance object
        :return relative log path of the given task instance
        """
        # To handle log writing when tasks are impersonated, the log files need to
        # be writable by the user that runs the Airflow command and the user
        # that is impersonated. This is mainly to handle corner cases with the
        # SubDagOperator. When the SubDagOperator is run, all of the operators
        # run under the impersonated user and create appropriate log files
        # as the impersonated user. However, if the user manually runs tasks
        # of the SubDagOperator through the UI, then the log files are created
        # by the user that runs the Airflow command. For example, the Airflow
        # run command may be run by the `airflow_sudoable` user, but the Airflow
        # tasks may be run by the `airflow` user. If the log files are not
        # writable by both users, then it's possible that re-running a task
        # via the UI (or vice versa) results in a permission error as the task
        # tries to write to a log file created by the other user.
        relative_path = self.filename_template.format(
            dag_id=ti.dag_id, task_id=ti.task_id,
            execution_date=ti.execution_date.isoformat(), try_number=ti.try_number + 1)
        full_path = os.path.join(self.local_base, relative_path)
        directory = os.path.dirname(full_path)
        # Create the log file and give it group writable permissions
        # TODO(aoen): Make log dirs and logs globally readable for now since the SubDag
        # operator is not compatible with impersonation (e.g. if a Celery executor is used
        # for a SubDag operator and the SubDag operator has a different owner than the
        # parent DAG)
        if not os.path.exists(directory):
            # Create the directory as globally writable using custom mkdirs
            # as os.makedirs doesn't set mode properly.
            mkdirs(directory, 0o775)

        if not os.path.exists(full_path):
            open(full_path, "a").close()
            # TODO: Investigate using 444 instead of 666.
            os.chmod(full_path, 0o666)

        return full_path