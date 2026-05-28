from __future__ import annotations

from tunapi.context import RunContext


class TestRunContextPush:
    def test_attrs(self):
        ctx = RunContext(project="proj", branch="main")
        assert ctx.project == "proj"
        assert ctx.branch == "main"

    def test_no_branch(self):
        ctx = RunContext(project="proj", branch=None)
        assert ctx.branch is None
