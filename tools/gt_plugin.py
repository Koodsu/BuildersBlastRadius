"""pytest plugin for tools/groundtruth.py: write the ids of tests that did not pass."""

import json
import os

FAILED = set()
BROKEN_FILES = set()


def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid.split("[", 1)[0])


def pytest_collectreport(report):
    if report.failed:
        BROKEN_FILES.add(report.nodeid.split("::", 1)[0])


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["GT_OUT"], "w") as handle:
        json.dump({"failed": sorted(FAILED), "broken_files": sorted(BROKEN_FILES)}, handle)
