# Copyright 2018 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import itertools
import logging
from abc import ABC, ABCMeta
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
from typing import Dict, Iterable, List, Optional, Type, TypeVar

from pants.base.exiter import PANTS_FAILED_EXIT_CODE, PANTS_SUCCEEDED_EXIT_CODE
from pants.core.util_rules.filter_empty_sources import (
    FieldSetsWithSources,
    FieldSetsWithSourcesRequest,
)
from pants.engine import desktop
from pants.engine.addresses import Address
from pants.engine.collection import Collection
from pants.engine.console import Console
from pants.engine.fs import Digest, DirectoryToMaterialize, Workspace
from pants.engine.goal import Goal, GoalSubsystem
from pants.engine.interactive_runner import InteractiveProcessRequest, InteractiveRunner
from pants.engine.process import FallibleProcessResult
from pants.engine.rules import goal_rule, rule
from pants.engine.selectors import Get, MultiGet
from pants.engine.target import (
    FieldSetWithOrigin,
    Sources,
    TargetsToValidFieldSets,
    TargetsToValidFieldSetsRequest,
)
from pants.engine.unions import UnionMembership, union

# TODO(#6004): use proper Logging singleton, rather than static logger.
logger = logging.getLogger(__name__)


class Status(Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


@dataclass(frozen=True)
class TestResult:
    status: Status
    stdout: str
    stderr: str
    coverage_data: Optional["CoverageData"]
    xml_results: Optional[Digest]

    # Prevent this class from being detected by pytest as a test class.
    __test__ = False

    @staticmethod
    def from_fallible_process_result(
        process_result: FallibleProcessResult,
        *,
        coverage_data: Optional["CoverageData"] = None,
        xml_results: Optional[Digest] = None,
    ) -> "TestResult":
        return TestResult(
            status=Status.SUCCESS if process_result.exit_code == 0 else Status.FAILURE,
            stdout=process_result.stdout.decode(),
            stderr=process_result.stderr.decode(),
            coverage_data=coverage_data,
            xml_results=xml_results,
        )


@dataclass(frozen=True)
class TestDebugRequest:
    ipr: InteractiveProcessRequest

    # Prevent this class from being detected by pytest as a test class.
    __test__ = False


@union
class TestFieldSet(FieldSetWithOrigin, metaclass=ABCMeta):
    """The fields necessary to run tests on a target."""

    sources: Sources

    __test__ = False


# NB: This is only used for the sake of coordinator_of_tests. Consider inlining that rule so that
# we can remove this wrapper type.
@dataclass(frozen=True)
class WrappedTestFieldSet:
    field_set: TestFieldSet


@dataclass(frozen=True)
class AddressAndTestResult:
    address: Address
    test_result: TestResult


class CoverageData(ABC):
    """Base class for inputs to a coverage report.

    Subclasses should add whichever fields they require - snapshots of coverage output, XML files,
    etc.
    """


_CD = TypeVar("_CD", bound=CoverageData)


@union
class CoverageDataCollection(Collection[_CD]):
    element_type: Type[_CD]


class CoverageReport(ABC):
    """Represents a code coverage report that can be materialized to the terminal or disk."""

    def materialize(self, console: Console, workspace: Workspace) -> Optional[PurePath]:
        """Materialize this code coverage report to the terminal or disk.

        :param console: A handle to the terminal.
        :param workspace: A handle to local disk.
        :return: If a report was materialized to disk, the path of the file in the report one might
                 open first to start examining the report.
        """
        ...


@dataclass(frozen=True)
class ConsoleCoverageReport(CoverageReport):
    """Materializes a code coverage report to the terminal."""

    report: str

    def materialize(self, console: Console, workspace: Workspace) -> None:
        console.print_stderr(f"\n{self.report}")
        return None


@dataclass(frozen=True)
class FilesystemCoverageReport(CoverageReport):
    """Materializes a code coverage report to disk."""

    result_digest: Digest
    directory_to_materialize_to: PurePath
    report_file: Optional[PurePath]

    def materialize(self, console: Console, workspace: Workspace) -> Optional[PurePath]:
        workspace.materialize_directory(
            DirectoryToMaterialize(
                self.result_digest, path_prefix=str(self.directory_to_materialize_to),
            )
        )
        console.print_stderr(f"\nWrote coverage report to `{self.directory_to_materialize_to}`")
        return self.report_file


class TestOptions(GoalSubsystem):
    """Runs tests."""

    name = "test"

    required_union_implementations = (TestFieldSet,)

    # Prevent this class from being detected by pytest as a test class.
    __test__ = False

    @classmethod
    def register_options(cls, register) -> None:
        super().register_options(register)
        register(
            "--debug",
            type=bool,
            default=False,
            help=(
                "Run a single test target in an interactive process. This is necessary, for "
                "example, when you add breakpoints to your code."
            ),
        )
        register(
            "--use-coverage",
            type=bool,
            default=False,
            help="Generate a coverage report if the test runner supports it.",
        )
        register(
            "--open-coverage",
            type=bool,
            default=False,
            help=(
                "If a coverage report file is generated, open it on the local system if the "
                "system supports this."
            ),
        )


class Test(Goal):
    subsystem_cls = TestOptions

    __test__ = False


@goal_rule
async def run_tests(
    console: Console,
    options: TestOptions,
    interactive_runner: InteractiveRunner,
    workspace: Workspace,
    union_membership: UnionMembership,
) -> Test:
    if options.values.debug:
        targets_to_valid_field_sets = await Get[TargetsToValidFieldSets](
            TargetsToValidFieldSetsRequest(
                TestFieldSet,
                goal_description="`test --debug`",
                error_if_no_valid_targets=True,
                expect_single_field_set=True,
            )
        )
        field_set = targets_to_valid_field_sets.field_sets[0]
        request = await Get[TestDebugRequest](TestFieldSet, field_set)
        debug_result = interactive_runner.run_local_interactive_process(request.ipr)
        return Test(debug_result.process_exit_code)

    targets_to_valid_field_sets = await Get[TargetsToValidFieldSets](
        TargetsToValidFieldSetsRequest(
            TestFieldSet,
            goal_description=f"the `{options.name}` goal",
            error_if_no_valid_targets=False,
        )
    )
    field_sets_with_sources = await Get[FieldSetsWithSources](
        FieldSetsWithSourcesRequest(targets_to_valid_field_sets.field_sets)
    )

    results = await MultiGet(
        Get[AddressAndTestResult](WrappedTestFieldSet(field_set))
        for field_set in field_sets_with_sources
    )

    exit_code = PANTS_SUCCEEDED_EXIT_CODE
    for result in results:
        if result.test_result.status == Status.FAILURE:
            exit_code = PANTS_FAILED_EXIT_CODE
        has_output = result.test_result.stdout or result.test_result.stderr
        if has_output:
            status = (
                console.green("✓")
                if result.test_result.status == Status.SUCCESS
                else console.red("𐄂")
            )
            console.print_stderr(f"{status} {result.address}")
        if result.test_result.stdout:
            console.print_stderr(result.test_result.stdout)
        if result.test_result.stderr:
            console.print_stderr(result.test_result.stderr)
        if has_output and result != results[-1]:
            console.print_stderr("")

    # Print summary
    if len(results) > 1:
        console.print_stderr("")
        for result in results:
            console.print_stderr(
                f"{result.address.reference():80}.....{result.test_result.status.value:>10}"
            )

    for result in results:
        xml_results = result.test_result.xml_results
        if not xml_results:
            continue
        workspace.materialize_directory(DirectoryToMaterialize(xml_results))

    if options.values.use_coverage:
        all_coverage_data: Iterable[CoverageData] = [
            result.test_result.coverage_data
            for result in results
            if result.test_result.coverage_data is not None
        ]

        coverage_types_to_collection_types: Dict[
            Type[CoverageData], Type[CoverageDataCollection]
        ] = {
            collection_cls.element_type: collection_cls
            for collection_cls in union_membership.get(CoverageDataCollection)
        }
        coverage_collections: List[CoverageDataCollection] = []
        for data_cls, data in itertools.groupby(all_coverage_data, lambda data: type(data)):
            collection_cls = coverage_types_to_collection_types[data_cls]
            coverage_collections.append(collection_cls(data))

        coverage_reports = await MultiGet(
            Get[CoverageReport](CoverageDataCollection, coverage_collection)
            for coverage_collection in coverage_collections
        )

        coverage_report_files = []
        for report in coverage_reports:
            report_file = report.materialize(console, workspace)
            if report_file is not None:
                coverage_report_files.append(report_file)

        if coverage_report_files and options.values.open_coverage:
            desktop.ui_open(console, interactive_runner, coverage_report_files)

    return Test(exit_code)


@rule
async def coordinator_of_tests(wrapped_field_set: WrappedTestFieldSet) -> AddressAndTestResult:
    field_set = wrapped_field_set.field_set

    # TODO(#6004): when streaming to live TTY, rely on V2 UI for this information. When not a
    # live TTY, periodically dump heavy hitters to stderr. See
    # https://github.com/pantsbuild/pants/issues/6004#issuecomment-492699898.
    logger.info(f"Starting tests: {field_set.address.reference()}")
    result = await Get[TestResult](TestFieldSet, field_set)
    logger.info(
        f"Tests {'succeeded' if result.status == Status.SUCCESS else 'failed'}: "
        f"{field_set.address.reference()}"
    )
    return AddressAndTestResult(field_set.address, result)


def rules():
    return [coordinator_of_tests, run_tests]
