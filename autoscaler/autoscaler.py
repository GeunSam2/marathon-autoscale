import logging
import os
import json
import sys
import time

import requests
import jwt

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

    def __init__(self):
        """Initialize the object with data from the command line or environment
        variables. Log in into DCOS if username / password are provided.
        Set up logging according to the verbosity requested.
        """
        self.app_instances = 0
        self.trigger_var = 0
        self.cool_down = 0
        self.dcos_headers = {}

        self.parse_arguments()

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

        # Set auth header
        self.authenticate()

    def authenticate(self):
        """Using a userid/pass or a service account secret,
        get or renew JWT auth token
        Returns:
            Sets dcos_headers to be used for authentication
        """

        # Get the cert authority
        if not os.path.isfile(self.DCOS_CA):

            response = requests.get(
                self.dcos_master + '/ca/dcos-ca.crt',
                verify=False
            )
            with open(self.DCOS_CA, "wb") as crt_file:
                crt_file.write(response.content)

        # Authenticate using using username and password
        if ('AS_USERID' in os.environ.keys()) and ('AS_PASSWORD' in os.environ.keys()):

            auth_data = json.dumps(
                {
                    'uid': os.environ.get('AS_USERID'),
                    'password': os.environ.get('AS_PASSWORD')
                }
            )

        # Authenticate using a service account
        elif ('AS_SECRET' in os.environ.keys()) and ('AS_USERID' in os.environ.keys()):

            # Get the private key from the auto-scaler secret
            saas = json.loads(os.environ.get('AS_SECRET'))

            # Create a JWT token
            jwt_token = jwt.encode(
                {'uid': os.environ.get('AS_USERID')}, saas['private_key'], algorithm='RS256'
            )
            auth_data = json.dumps(
                {
                    "uid": os.environ.get('AS_USERID'),
                    "token": jwt_token.decode('utf-8')
                }
            )

        # No authentication
        else:
            self.dcos_headers = {'Content-type': 'application/json'}
            return

        # Create or renew auth token for the service account
        response = requests.post(
            self.dcos_master + "/acs/api/v1/auth/login",
            headers={"Content-type": "application/json"},
            data=auth_data,
            verify=self.DCOS_CA
        )

        result = response.json()

        if 'token' not in result:
            sys.stderr.write("Unable to authenticate or renew JWT token: %s", result)
            sys.exit(1)

        self.dcos_headers = {
            'Authorization': 'token=' + result['token'],
            'Content-type': 'application/json'
        }

    def dcos_rest(self, method, path, data=None):
        """Common querying procedure that handles 401 errors
        Args:
            path (str): URI path after the mesos master address
        Returns:
            JSON requests.response.content result of the query
        """
        err_num = 0
        done = False

        while not done:
            try:

                if data is None:
                    response = requests.get(
                        self.dcos_master + path,
                        headers=self.dcos_headers,
                        verify=False
                    )
                else:
                    response = requests.get(
                        self.dcos_master + path,
                        headers=self.dcos_headers,
                        data=data,
                        verify=False
                    )

                self.log.debug("%s %s %s", method, path, response.status_code)
                done = True

                if response.status_code != 200:
                    if response.status_code == 401:
                        self.log.info("Token expired. Re-authenticating to DC/OS")
                        self.authenticate()
                        done = False
                        continue
                    else:
                        response.raise_for_status()

                content = response.content.strip()
                if not content:
                    content = "{}"

                result = json.loads(content)
                return result

            except requests.exceptions.HTTPError as http_err:
                done = False
                self.log.error("HTTP Error: %s", http_err)
                self.timer()
            except json.JSONDecodeError as dec_err:
                done = False
                err_num += 1
                self.log.error("Non JSON result returned: %s", dec_err)
                if err_num > self.ERR_THRESHOLD:
                    self.log.error("FATAL: Threshold of JSON parsing errors "
                                   "exceeded. Shutting down.")
                    sys.exit(1)
                self.timer()

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

        response = self.dcos_rest(
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

        response = self.dcos_rest("get", self.MARATHON_APPS_URI)

        if response['apps']:
            for i in response['apps']:
                appid = i['id']
                apps.append(appid)
            self.log.debug("Found the following marathon apps %s", apps)
        else:
            self.log.error("No Apps found on Marathon")
            sys.exit(1)

        return apps