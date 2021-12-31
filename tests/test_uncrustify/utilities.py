# Logic for listing and running tests.
#
# * @author  Ben Gardner        October 2009
# * @author  Guy Maurel         October 2015
# * @author  Matthew Woehlke    June 2018
#

import argparse
import concurrent.futures
import os
import queue
import subprocess
import sys
import threading

from .ansicolor import printc
from .config import config, all_tests, FAIL_ATTRS, PASS_ATTRS, SKIP_ATTRS
from .failure import (Failure, MismatchFailure, UnexpectedlyPassingFailure,
                      UnstableFailure)
from .test import FormatTest


# -----------------------------------------------------------------------------
def _add_common_arguments(parser):
    parser.add_argument('-c', '--show-commands', action='store_true',
                        help='show commands')

    parser.add_argument('-v', '--verbose', action='store_true',
                        help='show detailed test information')

    parser.add_argument('-d', '--diff', action='store_true',
                        help='show diff on failure')

    parser.add_argument('-x', '--xdiff', action='store_true',
                        help='show diff on expected failure')

    parser.add_argument('-g', '--debug', action='store_true',
                        help='generate debug files (.log, .unc)')

    parser.add_argument('-e', '--executable', type=str, required=True,
                        metavar='PATH',
                        help='uncrustify executable to test')

    parser.add_argument('--git', type=str, default=config.git_exe,
                        metavar='PATH',
                        help='git executable to use to generate diffs')

    parser.add_argument('--result-dir', type=str, default=os.getcwd(),
                        metavar='DIR',
                        help='location to which results will be written')


# -----------------------------------------------------------------------------
def add_test_arguments(parser):
    _add_common_arguments(parser)

    parser.add_argument("name",                 type=str, metavar='NAME')
    parser.add_argument("--lang",               type=str, required=True)
    parser.add_argument("--input",              type=str, required=True)
    parser.add_argument("--config",             type=str, required=True)
    parser.add_argument("--expected",           type=str, required=True)
    parser.add_argument("--rerun-config",       type=str, metavar='INPUT')
    parser.add_argument("--rerun-expected",     type=str, metavar='CONFIG')
    parser.add_argument("--xfail",              action='store_true')


# -----------------------------------------------------------------------------
def add_source_tests_arguments(parser):
    _add_common_arguments(parser)

    parser.add_argument('-p', '--show-all', action='store_true',
                        help='show passed/skipped tests')


# -----------------------------------------------------------------------------
def add_format_tests_arguments(parser):
    _add_common_arguments(parser)

    parser.add_argument('-p', '--show-all', action='store_true',
                        help='show passed/skipped tests')

    parser.add_argument('-r', '--select', metavar='CASE(S)', type=str,
                        help='select tests to be executed')

    parser.add_argument('tests', metavar='TEST', type=str, nargs='*',
                        default=all_tests,
                        help='test(s) to run (default all)')

    # Arguments for generating the CTest script; users should not use these
    # directly
    parser.add_argument("--write-ctest", type=str, help=argparse.SUPPRESS)
    parser.add_argument("--cmake-config", type=str, help=argparse.SUPPRESS)
    parser.add_argument("--python", type=str, help=argparse.SUPPRESS)


# -----------------------------------------------------------------------------
def parse_args(parser):
    args = parser.parse_args()

    if args.git is not None:
        config.git_exe = args.git

    config.uncrustify_exe = args.executable
    if not os.path.exists(config.uncrustify_exe):
        msg = 'Specified uncrustify executable {!r} does not exist'.format(
            config.uncrustify_exe)
        printc("FAILED: ", msg, **FAIL_ATTRS)
        sys.exit(-1)

    # Do a sanity check on the executable
    try:
        with open(os.devnull, 'w') as bitbucket:
            subprocess.check_call([config.uncrustify_exe, '--help'],
                                  stdout=bitbucket)
    except Exception as exc:
        msg = ('Specified uncrustify executable {!r} ' +
               'does not appear to be usable: {!s}').format(
            config.uncrustify_exe, exc)
        printc("FAILED: ", msg, **FAIL_ATTRS)
        sys.exit(-1)

    return args


# -----------------------------------------------------------------------------
def test_runner(args, tests, results, lock):
    while not tests.empty():
        test = tests.get()
        try:
            test.run(args)
            if args.show_all:
                outcome = 'XFAILED' if test.test_xfail else 'PASSED'
                with lock:
                    printc('{}: '.format(outcome), test.test_name, **PASS_ATTRS)
            results.put('passing')
        except UnstableFailure:
            results.put('unstable')
        except MismatchFailure:
            results.put('mismatch')
        except UnexpectedlyPassingFailure:
            results.put('xpass')
        except Failure:
            results.put('failing')


# -----------------------------------------------------------------------------
def run_tests(tests, args, selector=None):
    tests_queue = queue.Queue()
    results_queue = queue.Queue()
    lock = threading.Lock()

    for test in tests:
        if selector is not None and not selector.test(test.test_name):
            if args.show_all:
                printc("SKIPPED: ", test.test_name, **SKIP_ATTRS)
        else:
            tests_queue.put(test)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        for i in range(4):
            executor.submit(test_runner, args, tests_queue, results_queue, lock)

    counts = {'passing':0, 'failing':0, 'mismatch':0, 'unstable':0, 'xpass':0}
    while not results_queue.empty():
        r = results_queue.get()
        counts[r] += 1

    return counts


# -----------------------------------------------------------------------------
def report(counts):
    total = sum(counts.values())
    print('{passing} / {total} tests passed'.format(total=total, **counts))
    if counts['failing'] > 0:
        printc('{failing} tests failed to execute'.format(**counts),
               **FAIL_ATTRS)
    if counts['mismatch'] > 0:
        printc(
            '{mismatch} tests did not match the expected output'.format(
                **counts),
            **FAIL_ATTRS)
    if counts['unstable'] > 0:
        printc('{unstable} tests were unstable'.format(**counts),
               **FAIL_ATTRS)
    if counts['xpass'] > 0:
        printc('{xpass} tests passed but were expected to fail'
            .format(**counts), **FAIL_ATTRS)


# -----------------------------------------------------------------------------
def read_format_tests(filename, group):
    tests = []

    print("Processing " + filename)
    with open(filename, 'rt') as f:
        for line_number, line in enumerate(f, 1):
            line = line.strip()
            if not len(line):
                continue
            if line.startswith('#'):
                continue

            test = FormatTest()
            test.build_from_declaration(line, group, line_number)
            tests.append(test)

    return tests


# -----------------------------------------------------------------------------
def fixup_ctest_path(path, config):
    if config is None:
        return path

    dirname, basename = os.path.split(path)
    if os.path.basename(dirname).lower() == config.lower():
        dirname, junk = os.path.split(dirname)
        return os.path.join(dirname, '${CTEST_CONFIGURATION_TYPE}', basename)

    return path
