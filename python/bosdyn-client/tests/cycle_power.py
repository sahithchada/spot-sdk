"""Simple tutorial inspecting and cycling power on robot."""

import sys

import bosdyn.client
import bosdyn.client.util
from bosdyn.client.power import PowerClient

def main():
    import argparse
    parser = argparse.ArgumentParser()
    bosdyn.client.util.add_common_arguments(parser)
    options = parser.parse_args()

    # Create robot object with a power client.
    sdk = bosdyn.client.create_standard_sdk('PowerClient')
    sdk.load_app_token(options.app_token)
    robot = sdk.create_robot(options.hostname)
    robot.authenticate(options.username, options.password)
    power_client = robot.ensure_client(PowerClient.default_service_name)

    try:
        power_client.power_command(lease=None, request=None)
    except bosdyn.client.LeaseUseError as e:
        print("{}".format(e))

    try:
        power_client.power_command_feedback(1337)
    except bosdyn.client.InvalidRequestError as e:
        print("{}".format(e))


if __name__ == "__main__":
    if not main():
        sys.exit(1)
