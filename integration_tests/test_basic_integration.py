# Copyright (c) Shopkick 2017
# See LICENSE for details.
import os.path
import subprocess

CUR_PATH = os.path.dirname(os.path.abspath(__file__))
BASIC_PROJECT_DIR = CUR_PATH + "/basic_project"


def check_sky_output(args, cwd=BASIC_PROJECT_DIR):
    return subprocess.check_output(["python", "-m", "opensky"] + args, cwd=BASIC_PROJECT_DIR)


def test_show_config():
    output = check_sky_output(["config"])
    assert "'basicserv'" in output
    assert "'midtier_v1'" in output
    assert "'Mahmoud Hashemi'" in output


def test_setup():
    output = check_sky_output(["setup"])
    assert 'creating database basicserv' in output
    assert 'setup complete' in output


def test_selftest():
    output = check_sky_output(["self", "check"])
    assert 'working' in output
