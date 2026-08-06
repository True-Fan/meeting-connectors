"""Executable architecture rules.

Layering guarded only by intention decays. These tests walk the import graph of
every module under ``src/`` and fail CI with a named rule when a boundary is
crossed — so the property survives contributors who have not read the design docs.

Rules that reference packages not yet built (``services/``, ``avatar/``) pass
trivially today and start biting the moment those packages appear. That is the
point: the guard is installed before the code it guards.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    """Every ``src.*`` module this file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("src"))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src"):
            found.add(node.module)
    return found


def _modules_under(package: str) -> list[tuple[str, set[str]]]:
    """``(module_name, src_imports)`` for every file in a package."""
    root = SRC / Path(*package.split(".")[1:])
    if not root.exists():
        return []
    return [(_module_name(p), _imports(p)) for p in sorted(root.rglob("*.py"))]


def _assert_no_imports_from(package: str, forbidden: str, *, reason: str) -> None:
    violations = [
        f"{module} imports {imported}"
        for module, imports in _modules_under(package)
        for imported in sorted(imports)
        if imported == forbidden or imported.startswith(f"{forbidden}.")
    ]
    assert not violations, f"{package} must not import {forbidden} ({reason}): {violations}"


def test_src_layout_is_present() -> None:
    """Guard against the rules silently passing because paths moved."""
    assert (SRC / "domain").is_dir()
    assert (SRC / "protocols").is_dir()
    assert (SRC / "connectors" / "zoom").is_dir()


def test_domain_depends_on_nothing() -> None:
    """``domain/`` is the root of the dependency graph."""
    violations = [
        f"{module} imports {imported}"
        for module, imports in _modules_under("src.domain")
        for imported in sorted(imports)
        if not imported.startswith("src.domain")
    ]
    assert not violations, f"domain/ must depend on nothing in src: {violations}"


def test_protocols_depend_only_on_domain() -> None:
    """Ports describe domain types and nothing else."""
    violations = [
        f"{module} imports {imported}"
        for module, imports in _modules_under("src.protocols")
        for imported in sorted(imports)
        if not (imported.startswith(("src.domain", "src.protocols")))
    ]
    assert not violations, f"protocols/ may import only domain/: {violations}"


@pytest.mark.parametrize("package", ["src.api", "src.services", "src.avatar", "src.protocols"])
def test_packages_do_not_import_connectors(package: str) -> None:
    """Only ``src.containers`` may name a connector.

    This is the Dependency Inversion rule made structural: if ``services/`` can
    import ``connectors/zoom``, Zoom vocabulary leaks into the pipeline and the
    media path can no longer be tested without RTMS.
    """
    _assert_no_imports_from(
        package, "src.connectors", reason="composition happens in src.containers"
    )


def test_domain_and_protocols_do_not_import_infrastructure() -> None:
    """Domain models must not depend on logging or metrics."""
    for package in ("src.domain", "src.protocols"):
        _assert_no_imports_from(
            package, "src.infrastructure", reason="domain must stay side-effect free"
        )


def _assert_wire_models_contained(wire_module: str, boundary: str, *, label: str) -> None:
    """Assert a wire-model module is imported only from inside ``boundary``."""
    offenders = [
        f"{module} imports {wire_module}"
        for module, imports in _modules_under("src")
        for imported in imports
        if imported == wire_module and not module.startswith(boundary)
    ]
    assert not offenders, f"{label} must not leave {boundary}: {offenders}"


def test_rtms_wire_models_do_not_escape_the_zoom_connector() -> None:
    """RTMS wire types stay behind the anti-corruption boundary.

    The whole point of ``mapping.py`` is that ``msg_type`` and base64 envelopes stop
    before the pipeline.

    **The boundary is the connector, not the ``rtms`` sub-package.** This rule was
    originally written against ``connectors.zoom.rtms`` and has been failing since the
    Zoom webhook router was added: that router legitimately parses ``RtmsStartedEvent``,
    because verifying and decoding a Zoom webhook is exactly the kind of work that
    belongs *inside* the Zoom connector. The rule was stricter than the architecture it
    was written to protect, so it flagged correct code. Widened to the real invariant —
    no RTMS type reaches ``services/``, ``api/``, ``avatar/``, or another connector — and
    kept executable.
    """
    _assert_wire_models_contained(
        "src.connectors.zoom.rtms.models",
        "src.connectors.zoom",
        label="RTMS wire models",
    )


def test_teams_graph_models_do_not_escape_the_teams_connector() -> None:
    """The Teams half of the same rule.

    ``chatInfo``, ``threadId``, and MSI values stop at ``graph/join_url.py`` and
    ``ingest/mapping.py``. If this ever fails, Graph vocabulary has reached shared code
    and the pipeline can no longer be tested without Teams.
    """
    _assert_wire_models_contained(
        "src.connectors.teams.graph.models",
        "src.connectors.teams",
        label="Teams Graph wire models",
    )


def test_teams_sidecar_protocol_does_not_escape_the_teams_connector() -> None:
    """The Teams IPC codec is a connector-private detail.

    Zoom's sidecar protocol and Teams' are independent by design (doc 005 §1). This rule
    is what stops a future contributor from "sharing" one into the other and coupling two
    release cycles that must stay separate.
    """
    _assert_wire_models_contained(
        "src.connectors.teams.sidecar.protocol",
        "src.connectors.teams",
        label="Teams sidecar wire codec",
    )


def test_the_two_connectors_do_not_import_each_other() -> None:
    """Connector independence, made executable.

    The property the repository is organised around: a future connector must be addable
    without touching an existing one, which is only true if none of them depend on
    another. Adding ``google_meet`` adds one line to this list.
    """
    connectors = ("zoom", "teams")
    offenders: list[str] = []

    for connector in connectors:
        others = [f"src.connectors.{other}" for other in connectors if other != connector]
        for module, imports in _modules_under(f"src.connectors.{connector}"):
            offenders.extend(
                f"{module} imports {imported}"
                for imported in sorted(imports)
                if any(imported == o or imported.startswith(f"{o}.") for o in others)
            )

    assert not offenders, f"connectors must be independent of one another: {offenders}"


def test_shared_code_routes_on_the_domain_enum_not_on_a_connector() -> None:
    """The platform may be *named* in shared code; a connector may not be *reached*.

    ``domain.MeetingPlatform`` lives in the domain precisely so ``api/`` and ``services/``
    can route on it — ``api/dto.py`` and ``MeetingService`` both do, legitimately. The line
    that must not be crossed is importing connector code to decide behaviour, which the
    import rules above already forbid.

    This asserts the enum is where it belongs and remains free of platform mechanics, so
    "route on the enum" stays a cheap thing to do and reaching for a connector stays the
    expensive one.
    """
    from src.domain.meeting import MeetingPlatform

    assert _modules_under("src.domain")  # guard against a silent path change
    assert {p.value for p in MeetingPlatform} == {"zoom", "teams"}

    # The enum carries identity only: no urls, no ports, no SDK hints. Anything more and
    # shared code would start making platform decisions from domain data.
    for platform in MeetingPlatform:
        assert isinstance(platform.value, str)
        assert not hasattr(platform, "sdk")
        assert not hasattr(platform, "transport")


def test_no_relative_imports_in_src() -> None:
    """Absolute imports only, so a module's dependencies are readable in place."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(
            f"{_module_name(path)} (level={node.level})"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
        )
    assert not offenders, f"relative imports found: {offenders}"
