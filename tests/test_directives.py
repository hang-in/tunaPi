from __future__ import annotations

from pathlib import Path

from tunapi.directives import (
    parse_directives,
    ParsedDirectives,
    parse_context_line,
    format_context_line,
)
from tunapi.config import ProjectsConfig, ProjectConfig
from tunapi.context import RunContext


def _empty_projects() -> ProjectsConfig:
    return ProjectsConfig(projects={})


class TestParseDirectives:
    def test_empty(self):
        result = parse_directives(
            "", engine_ids=("claude",), projects=_empty_projects()
        )
        assert isinstance(result, ParsedDirectives)
        assert result.prompt == ""

    def test_no_directives(self):
        result = parse_directives(
            "hello world", engine_ids=("claude",), projects=_empty_projects()
        )
        assert result.prompt == "hello world"

    def test_engine_directive(self):
        result = parse_directives(
            "/codex do something",
            engine_ids=("claude", "codex"),
            projects=_empty_projects(),
        )
        assert result.engine == "codex"
        assert "do something" in result.prompt

    def test_branch_directive(self):
        result = parse_directives(
            "/claude @main do stuff",
            engine_ids=("claude",),
            projects=_empty_projects(),
        )
        assert result.engine == "claude"
        assert result.branch == "main"

    def test_whitespace_only(self):
        result = parse_directives(
            "   \n  ", engine_ids=("claude",), projects=_empty_projects()
        )
        assert result.prompt == "   \n  "


class TestParseContextLine:
    def test_empty(self):
        result = parse_context_line("", projects=_empty_projects())
        assert result is None

    def test_none(self):
        result = parse_context_line(None, projects=_empty_projects())
        assert result is None

    def test_no_ctx_prefix(self):
        result = parse_context_line("hello world", projects=_empty_projects())
        assert result is None

    def test_valid_ctx(self):
        projects = ProjectsConfig(
            projects={
                "myproj": ProjectConfig(
                    alias="myproj", path=Path("/p"), worktrees_dir=Path(".wt")
                )
            }
        )
        result = parse_context_line("ctx: myproj", projects=projects)
        assert result is not None
        assert result.project == "myproj"

    def test_ctx_with_branch(self):
        projects = ProjectsConfig(
            projects={
                "myproj": ProjectConfig(
                    alias="myproj", path=Path("/p"), worktrees_dir=Path(".wt")
                )
            }
        )
        result = parse_context_line("ctx: myproj @main", projects=projects)
        assert result is not None
        assert result.branch == "main"

    def test_ctx_backtick_wrapped(self):
        projects = ProjectsConfig(
            projects={
                "myproj": ProjectConfig(
                    alias="myproj", path=Path("/p"), worktrees_dir=Path(".wt")
                )
            }
        )
        result = parse_context_line("`ctx: myproj`", projects=projects)
        assert result is not None


class TestFormatContextLine:
    def test_none_context(self):
        result = format_context_line(None, projects=_empty_projects())
        assert result is None

    def test_no_project(self):
        result = format_context_line(
            RunContext(project=None, branch=None),
            projects=_empty_projects(),
        )
        assert result is None

    def test_with_project(self):
        projects = ProjectsConfig(
            projects={
                "myproj": ProjectConfig(
                    alias="myproj", path=Path("/p"), worktrees_dir=Path(".wt")
                )
            }
        )
        result = format_context_line(
            RunContext(project="myproj", branch=None),
            projects=projects,
        )
        assert result is not None
        assert "myproj" in result

    def test_with_branch(self):
        projects = ProjectsConfig(
            projects={
                "myproj": ProjectConfig(
                    alias="myproj", path=Path("/p"), worktrees_dir=Path(".wt")
                )
            }
        )
        result = format_context_line(
            RunContext(project="myproj", branch="main"),
            projects=projects,
        )
        assert "myproj" in result
        assert "main" in result
