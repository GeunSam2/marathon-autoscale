import logging
import os
import json
import sys
import time
import math
import argparse

import requests
import jwt

from autoscaler.marathonclient import MarathonClient

from autoscaler.modes import scalesqs
from autoscaler.modes import scalecpu

class Autoscaler():
    """Marathon auto scaler
    upon initialization, it reads a list of command line parameters or env
    variables. Then it logs in to DCOS and starts querying metrics relevant
    to the scaling objective (cpu,mem). Scaling can happen by cpu, mem,
    cpu and mem, cpu or mem. The checks are performed on a configurable
    interval.
    """

    ERR_THRESHOLD = 10 # Maximum number of attempts to decode a response
    LOGGING_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    DCOS_CA = 'dcos-ca.crt'
    MARATHON_APPS_URI = '/service/marathon/v2/apps'

    # Dictionary defines the different scaling modes available
    SCALING_MODES = {
        'sqs': scalesqs.ScaleBySQS,
        'cpu': scalecpu.ScaleCPU
    }

    def __init__(self):
        """Initialize the object with data from the command line or environment
        variables. Log in into DCOS if username / password are provided.
        Set up logging according to the verbosity requested.
        """

        # Start logging
        if self.verbose:
            level = logging.DEBUG
        else:
            level = logging.INFO

        logging.basicConfig(
            level=level,
            format=self.LOGGING_FORMAT
        )

        self.log = logging.getLogger("marathon-autoscaler")

        self.app_instances = 0
        self.scale_up = 0
        self.cool_down = 0

        args = self.parse_arguments()

        self.dcos_master = args.dcos_master
        self.trigger_mode = args.trigger_mode
        self.autoscale_multiplier = float(args.autoscale_multiplier)
        self.max_instances = float(args.max_instances)
        self.marathon_app = args.marathon_app
        self.min_instances = float(args.min_instances)
        self.cool_down_factor = float(args.cool_down_factor)
        self.scale_up_factor = float(args.scale_up_factor)
        self.interval = args.interval
        self.verbose = args.verbose or os.environ.get("AS_VERBOSE")

        # Initialize marathon client for auth requests
        self.marathon_client = MarathonClient(self.dcos_master)

        # Set auth header
        # TODO: does this make sense
        self.marathon_client.authenticate()

    def timer(self):
        """Simple timer function"""
        self.log.debug("Successfully completed a cycle, sleeping for %s seconds",
                       self.interval)
        time.sleep(self.interval)

    def get_app_details(self):
        """Retrieve metadata about marathon_app
        Returns:
            Dictionary of task_id mapped to mesos slave_id
        """
        app_task_dict = {}

        response = self.marathon_client.dcos_rest(
            "get",
            self.MARATHON_APPS_URI + self.marathon_app
        )

        if response['app']['tasks']:
            self.app_instances = response['app']['instances']
            self.log.debug("Marathon app %s has %s deployed instances",
                           self.marathon_app, self.app_instances)
            for i in response['app']['tasks']:
                taskid = i['id']
                hostid = i['host']
                slave_id = i['slaveId']
                self.log.debug(
                    "Task %s is running on host %s with slaveId %s",
                    taskid,
                    hostid,
                    slave_id
                )
                app_task_dict[str(taskid)] = str(slave_id)
        else:
            self.log.error('No task data in marathon for app %s', self.marathon_app)

        return app_task_dict

    def get_all_apps(self):
        """Query marathon for a list of its apps
        Returns:
            a list of all marathon apps
        """
        apps = []

        response = self.marathon_client.dcos_rest(
            "get",
            self.MARATHON_APPS_URI
        )

        if response['apps']:
            for i in response['apps']:
                appid = i['id']
                apps.append(appid)
            self.log.debug("Found the following marathon apps %s", apps)
        else:
            self.log.error("No Apps found on Marathon")
            sys.exit(1)

        return apps

    def autoscale(self, min, max, metric):

        if min <= metric <= max:
            self.log.info("%s within thresholds" % self.trigger_mode)
            self.scale_up = 0
            self.cool_down = 0
        elif (metric > max) and (self.scale_up >= self.scale_up_factor):
            self.log.info("Auto-scale triggered based on %s exceeding threshold" % self.trigger_mode)
            self.scale_app(True)
            self.scale_up = 0
        elif (metric < min) and (self.cool_down >= self.cool_down_factor):
            self.log.info("Auto-scale triggered based on %s below the threshold" % self.trigger_mode)
            self.scale_app(False)
            self.cool_down = 0
        elif metric > max:
            self.scale_up += 1
            self.cool_down = 0
            self.log.info("%s above thresholds, but waiting for scaling factor (%s) "
                          "to be exceeded in order to trigger auto scale. " %
                          (self.trigger_mode, self.scale_up))
        elif metric < min:
            self.cool_down += 1
            self.scale_up = 0
            self.log.info("%s below thresholds, but waiting for cool down factor (%s) "
                          "to be exceeded in order to trigger auto scale. " %
                          (self.trigger_mode, self.cool_down))
        else:
            self.log.info("%s not exceeding threshold" % self.trigger_mode)

    def scale_app(self, is_up):
        """Scale marathon_app up or down
        Args:
            is_up(bool): Scale up if True, scale down if False
        """
        if is_up:
            target_instances = math.ceil(self.app_instances * self.autoscale_multiplier)
            if target_instances > self.max_instances:
                self.log.info("Reached the set maximum of instances %s", self.max_instances)
                target_instances = self.max_instances
        else:
            target_instances = math.floor(self.app_instances / self.autoscale_multiplier)
            if target_instances < self.min_instances:
                self.log.info("Reached the set minimum of instances %s", self.min_instances)
                target_instances = self.min_instances

        self.log.debug("scale_app: app_instances %s target_instances %s",
                       self.app_instances, target_instances)
        if self.app_instances != target_instances:
            data = {'instances': target_instances}
            json_data = json.dumps(data)
            response = self.marathon_client.dcos_rest(
                "put",
                '/service/marathon/v2/apps/' + self.marathon_app,
                data=json_data
            )
            self.log.debug("scale_app response: %s", response)

    def parse_arguments(self):
        """Set up an argument parser
        Override values of command line arguments with environment variables.
        """
        parser = argparse.ArgumentParser(description='Marathon autoscaler app.')
        parser.set_defaults()
        parser.add_argument('--dcos-master',
                            help=('The DNS hostname or IP of your Marathon'
                                  ' Instance'),
                            **self.env_or_req('AS_DCOS_MASTER'))
        parser.add_argument('--trigger_mode',
                            help=('Which metric(s) to trigger Autoscale '
                                  '(and, or, cpu, mem, sqs)'),
                            **self.env_or_req('AS_TRIGGER_MODE'))
        parser.add_argument('--autoscale_multiplier',
                            help=('Autoscale multiplier for triggered '
                                  'Autoscale (ie 2)'),
                            **self.env_or_req('AS_AUTOSCALE_MULTIPLIER'), type=float)
        parser.add_argument('--max_instances',
                            help=('The Max instances that should ever exist'
                                  ' for this application (ie. 20)'),
                            **self.env_or_req('AS_MAX_INSTANCES'), type=int)
        parser.add_argument('--marathon-app',
                            help=('Marathon Application Name to Configure '
                                  'Autoscale for from the Marathon UI'),
                            **self.env_or_req('AS_MARATHON_APP'))
        parser.add_argument('--min_instances',
                            help='Minimum number of instances to maintain',
                            **self.env_or_req('AS_MIN_INSTANCES'), type=int)
        parser.add_argument('--cool_down_factor',
                            help='Number of cycles to avoid scaling again',
                            **self.env_or_req('AS_COOL_DOWN_FACTOR'), type=int)
        parser.add_argument('--scale_up_factor',
                            help='Number of cycles to avoid scaling again',
                            **self.env_or_req('AS_SCALE_UP_FACTOR'), type=int)
        parser.add_argument('--interval',
                            help=('Time in seconds to wait between '
                                  'checks (ie. 20)'),
                            **self.env_or_req('AS_INTERVAL'), type=int)
        parser.add_argument('-v', '--verbose', action="store_true",
                            help='Display DEBUG messages')

        try:
            args = parser.parse_args()
            return args
        except argparse.ArgumentError as arg_err:
            sys.stderr.write(arg_err)
            parser.print_help()
            sys.exit(1)

    @staticmethod
    def env_or_req(key):
        """Environment variable substitute
        Args:
            key (str): Name of environment variable to look for
        Returns:
            string to be included in parameter parsing configuration
        """
        if os.environ.get(key):
            result = {'default': os.environ.get(key)}
        else:
            result = {'required': True}
        return result

    def run(self, scalemode):
        """Main function
        Runs the query - compute - act cycle
        """
        self.cool_down = 0
        self.scale_up = 0

        while True:

            # Get all of the marathon apps
            marathon_apps = self.get_all_apps()
            self.log.debug("The following apps exist in marathon %s", marathon_apps)

            # test for apps existence in Marathon.
            if self.marathon_app not in marathon_apps:
                self.log.error("Could not find %s in list of apps.", self.marathon_app)
                self.timer()
                continue

            # Get a dictionary of app taskId and hostId for the marathon app
            app_task_dict = self.get_app_details()

            # verify if app has any Marathon task data.
            if not app_task_dict:
                self.timer()
                continue

            self.log.debug("Tasks for %s : %s", self.marathon_app, app_task_dict)

            scalemode = self.SCALING_MODES.get(self.trigger_mode, None)
            if scalemode is None:
                self.log.error("Scale mode is not found.")
                sys.exit(1)

            # Get the mode dimension and actual metric
            min = scalemode.get_min()
            max = scalemode.get_max()
            metric = scalemode.get_metric()

            if metric == -1.0:
                self.timer()
                continue

            # Evaluate whether to auto-scale
            self.autoscale(min, max, metric)
            self.timer()


if __name__ == "__main__":
    AutoScaler = Autoscaler()
    AutoScaler.run()
