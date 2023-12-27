from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
import importlib.metadata
import json
import sys

from attrs import asdict, evolve, field, frozen
from rpds import HashTrieMap
from url import URL
import structlog.stdlib

from bowtie._commands import (
    CaseErrored,
    CaseResult,
    CaseSkipped,
    Seq,
    TestCase,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path
    from typing import Any, Self, TextIO

    from bowtie._commands import AnyCaseResult, AnyTestResult, Command, Test
    from bowtie._core import Implementation


class EmptyReport(Exception):
    pass


class _InvalidBowtieReport(Exception):
    pass


_DIALECT_URI_TO_SHORTNAME = {
    URL.parse("https://json-schema.org/draft/2020-12/schema"): "Draft 2020-12",
    URL.parse("https://json-schema.org/draft/2019-09/schema"): "Draft 2019-09",
    URL.parse("http://json-schema.org/draft-07/schema#"): "Draft 7",
    URL.parse("http://json-schema.org/draft-06/schema#"): "Draft 6",
    URL.parse("http://json-schema.org/draft-04/schema#"): "Draft 4",
    URL.parse("http://json-schema.org/draft-03/schema#"): "Draft 3",
}


def writer(file: TextIO = sys.stdout) -> Callable[..., Any]:
    return lambda **result: file.write(f"{json.dumps(result)}\n")  # type: ignore[reportUnknownArgumentType]


@frozen
class Reporter:
    _write: Callable[..., Any] = field(default=writer(), alias="write")
    _log: structlog.stdlib.BoundLogger = field(
        factory=structlog.stdlib.get_logger,
    )

    def unsupported_dialect(
        self,
        implementation: Implementation,
        dialect: URL,
    ):
        self._log.warn(
            "Unsupported dialect, skipping implementation.",
            logger_name=implementation.name,
            dialect=dialect,
        )

    def unacknowledged_dialect(
        self,
        implementation: str,
        dialect: URL,
        response: Any,
    ):
        self._log.warn(
            (
                "Implicit dialect not acknowledged. "
                "Proceeding, but implementation may not have configured "
                "itself to handle schemas without $schema."
            ),
            logger_name=implementation,
            dialect=dialect,
            response=response,
        )

    def ready(self, run_metadata: RunMetadata):
        self._write(**run_metadata.serializable())

    def will_speak(self, dialect: URL):
        self._log.debug("Will speak", dialect=dialect)

    def finished(self, count: int, did_fail_fast: bool):
        if not count:
            self._log.error("No test cases ran.")
        else:
            self._log.info("Finished", count=count)
        self._write(did_fail_fast=did_fail_fast)

    def no_such_image(self, name: str):
        self._log.error("Not a known Bowtie implementation.", logger_name=name)

    def startup_failed(self, name: str, stderr: str):
        self._log.exception(
            "Startup failed!",
            logger_name=name,
            **{"stderr": stderr} if stderr else {},
        )

    def dialect_error(self, implementation: Implementation, stderr: str):
        self._log.error(
            "Tried to start sending test cases, but got an error.",
            logger_name=implementation.name,
            stderr=stderr,
        )

    def no_implementations(self):
        self._log.error("No implementations started successfully!")

    def invalid_response(
        self,
        cmd: Command[Any],
        response: bytes,
        implementation: Implementation,
        error: Exception,
    ):
        self._log.exception(
            "Invalid response",
            logger_name=implementation.name,
            exc_info=error,
            request=cmd,
            response=response,
        )

    def case_started(self, seq: Seq, case: TestCase):
        return CaseReporter.case_started(
            case=case,
            seq=seq,
            write=self._write,
            log=self._log.bind(
                seq=seq,
                case=case.description,
                schema=case.schema,
            ),
        )


@frozen
class CaseReporter:
    _write: Callable[..., Any] = field(alias="write")
    _log: structlog.stdlib.BoundLogger = field(alias="log")

    @classmethod
    def case_started(
        cls,
        log: structlog.stdlib.BoundLogger,
        write: Callable[..., None],
        case: TestCase,
        seq: Seq,
    ) -> CaseReporter:
        self = cls(log=log, write=write)
        self._write(case=case.serializable(), seq=seq)
        return self

    def got_results(self, results: CaseResult):
        for result in results.results:
            if result.errored:
                self._log.error(
                    "",
                    logger_name=results.implementation,
                    **result.context,  # type: ignore[reportGeneralTypeIssues]
                )
        self._write(**asdict(results))

    def skipped(self, skipped: CaseSkipped):
        self._write(**skipped.serializable())

    def no_response(self, implementation: str):
        self._log.error("No response", logger_name=implementation)

    def case_errored(self, results: CaseErrored):
        implementation, context = results.implementation, results.context
        message = "" if results.caught else "uncaught error"
        self._log.error(message, logger_name=implementation, **context)
        self._write(**results.serializable())


@frozen
class Count:
    failed_tests: int = 0
    errored_tests: int = 0
    skipped_tests: int = 0

    @property
    def unsuccessful_tests(self):
        """
        Any test which was not a successful result, including skips.
        """
        return self.errored_tests + self.failed_tests + self.skipped_tests


@frozen
class Summary:
    _results: HashTrieMap[Seq, HashTrieMap[str, Any]] = field(
        default=HashTrieMap(),
        repr=False,
    )
    by_implementation: HashTrieMap[str, Count] = field(
        default=HashTrieMap(),
        repr=False,
    )

    def __getitem__(self, seq: Seq):
        return self._results[seq]

    # Assembly

    def with_result(self, result: AnyCaseResult):
        seq = result.seq
        results = self._results.get(seq, HashTrieMap())
        implementation = result.implementation
        if implementation in results:
            raise _InvalidBowtieReport(f"Duplicate result for case ID {seq}!")

        count = self.by_implementation.get(implementation, Count())

        return evolve(
            self,
            by_implementation=self.by_implementation.insert(
                implementation,
                result.be_counted(count),
            ),
            results=self._results.insert(
                seq,
                results.insert(implementation, result),
            ),
        )


@frozen
class RunMetadata:
    dialect: URL
    _implementations: Mapping[str, Mapping[str, Any]] = field(
        repr=lambda value: f"({len(value)} implementations)",
        alias="implementations",
    )
    bowtie_version: str = importlib.metadata.version("bowtie-json-schema")
    metadata: Mapping[str, Any] = field(factory=dict, repr=False)
    started: datetime = field(factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_dict(cls, dialect: str, **kwargs: Any) -> RunMetadata:
        started = kwargs.pop("started")
        if started is not None:
            kwargs["started"] = datetime.fromisoformat(started)
        return cls(dialect=URL.parse(dialect), **kwargs)

    @classmethod
    def from_implementations(
        cls,
        implementations: Iterable[Implementation],
        **kwargs: Any,
    ) -> RunMetadata:
        return cls(
            implementations={
                implementation.name: dict(
                    implementation.metadata or {},
                    image=implementation.name,
                )
                for implementation in implementations
            },
            **kwargs,
        )

    @property
    def dialect_shortname(self):
        return _DIALECT_URI_TO_SHORTNAME.get(self.dialect, self.dialect)

    @property
    def implementations(self):
        return self._implementations.values()

    def serializable(self):
        as_dict = {k.lstrip("_"): v for k, v in asdict(self).items()}
        as_dict.update(
            dialect=str(as_dict.pop("dialect")),
            started=as_dict.pop("started").isoformat(),
        )
        return as_dict


@frozen
class Report:
    _cases: HashTrieMap[Seq, TestCase] = field(alias="cases")
    metadata: RunMetadata
    summary: Summary
    did_fail_fast: bool

    @classmethod
    def from_input(cls, input: Iterable[Mapping[str, Any]]) -> Self:
        iterator = iter(input)
        header = next(iterator, None)
        if header is None:
            raise EmptyReport()
        metadata = RunMetadata.from_dict(**header)

        cases: HashTrieMap[Seq, TestCase] = HashTrieMap()

        summary, did_fail_fast = Summary(), False
        for data in iterator:
            match data:
                case {"seq": Seq(seq), "case": case}:
                    if seq in cases:
                        raise _InvalidBowtieReport(f"Duplicate case: {seq}")

                    case["dialect"] = metadata.dialect
                    case = TestCase.from_dict(**case)
                    cases = cases.insert(seq, case)
                case data if "caught" in data:
                    error = CaseErrored(**data)
                    summary = summary.with_result(error)
                case {"skipped": True, **skip}:
                    skip = CaseSkipped(**skip)
                    summary = summary.with_result(skip)
                case {"did_fail_fast": did_fail_fast}:
                    break
                case _:
                    result = CaseResult.from_dict(data)
                    summary = summary.with_result(result)
        return cls(
            cases=cases,
            metadata=metadata,
            summary=summary,
            did_fail_fast=did_fail_fast,
        )

    @classmethod
    def from_serialized(cls, serialized: Iterable[str]) -> Self:
        return cls.from_input(json.loads(line) for line in serialized)

    @classmethod
    def empty(cls, dialect: URL):
        """
        'The' empty report.
        """
        return cls(
            cases=HashTrieMap(),
            metadata=RunMetadata(dialect=dialect, implementations={}),
            summary=Summary(),
            did_fail_fast=False,
        )

    @property
    def dialect(self):
        return self.metadata.dialect

    @property
    def is_empty(self):
        return not self._cases

    @property
    def total_tests(self):
        return sum(len(case.tests) for case in self._cases.values())

    @property
    def implementations(self):
        return [each["image"] for each in self.metadata.implementations]

    def cases_with_results(
        self,
    ) -> Iterable[
        tuple[
            TestCase,
            Iterable[tuple[Test, Mapping[str, AnyTestResult]]],
        ]
    ]:
        for seq, case in sorted(self._cases.items()):
            case_results = self.summary[seq]
            test_results: list[tuple[Test, Mapping[str, AnyTestResult]]] = []
            for i, test in enumerate(case.tests):
                by_implementation = {
                    each: case_results[each].results[i]
                    for each in self.implementations
                    if each in case_results
                }
                test_results.append((test, by_implementation))
            yield case, test_results

    def generate_badges(self, target_dir: Path):
        label = _DIALECT_URI_TO_SHORTNAME[self.dialect]
        total = self.total_tests
        for impl in self.metadata.implementations:
            dialect_versions = [URL.parse(each) for each in impl["dialects"]]
            if self.dialect not in dialect_versions:
                continue
            supported_drafts = ", ".join(
                _DIALECT_URI_TO_SHORTNAME[each].removeprefix("Draft ")
                for each in reversed(dialect_versions)
            )
            name = impl["name"]
            lang = impl["language"]
            counts = self.summary.by_implementation[impl["image"]]
            passed = (
                total
                - counts.failed_tests
                - counts.errored_tests
                - counts.skipped_tests
            )
            pct = (passed / total) * 100
            r, g, b = 100 - int(pct), int(pct), 0
            badge_per_draft = {
                "schemaVersion": 1,
                "label": label,
                "message": "%d%% Passing" % int(pct),
                "color": f"{r:02x}{g:02x}{b:02x}",
            }
            comp_dir = target_dir / f"{lang}-{name}" / "compliance"
            comp_dir.mkdir(parents=True, exist_ok=True)
            badge_path_per_draft = comp_dir / f"{label.replace(' ', '_')}.json"
            badge_path_per_draft.write_text(json.dumps(badge_per_draft))
            badge_supp_draft = {
                "schemaVersion": 1,
                "label": "JSON Schema Versions",
                "message": supported_drafts,
                "color": "lightgreen",
            }
            supp_dir = target_dir / f"{lang}-{name}"
            badge_path_supp_drafts = supp_dir / "supported_versions.json"
            badge_path_supp_drafts.write_text(json.dumps(badge_supp_draft))
