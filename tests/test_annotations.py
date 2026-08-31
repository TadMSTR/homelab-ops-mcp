"""Tool annotations, asserted on the MCP wire response rather than the source.

Reading the decorators back would only prove the decorators are there. These
tests go through a real client so they check what a consumer actually receives,
including the camelCase field names the protocol uses.
"""

import asyncio

import pytest
from fastmcp import Client

from homelab_ops_mcp.server import mcp

# The full expected set, per tool. destructiveHint and idempotentHint are
# meaningful only when readOnlyHint is false, so the read-only tools carry
# neither rather than carrying a value that would be ignored.
EXPECTED = {
    "read_file": {"readOnlyHint": True, "openWorldHint": False},
    "read_directory": {"readOnlyHint": True, "openWorldHint": False},
    "read_multiple_files": {"readOnlyHint": True, "openWorldHint": False},
    "list_processes": {"readOnlyHint": True, "openWorldHint": False},
    "write_file": {
        "readOnlyHint": False,
        "idempotentHint": True,
        "destructiveHint": True,
        "openWorldHint": False,
    },
    "edit_file": {
        "readOnlyHint": False,
        "idempotentHint": False,
        "destructiveHint": True,
        "openWorldHint": False,
    },
    "run_command": {
        "readOnlyHint": False,
        "idempotentHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
    },
}


def _listed():
    async def go():
        async with Client(mcp) as c:
            return await c.list_tools()

    return {t.name: t for t in asyncio.run(go())}


@pytest.fixture(scope="module")
def tools():
    return _listed()


def test_every_tool_is_annotated(tools):
    """A new tool added without annotations fails here rather than shipping bare."""
    assert set(tools) == set(EXPECTED)
    unannotated = [name for name, t in tools.items() if t.annotations is None]
    assert unannotated == []


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_annotation_values(tools, name):
    got = tools[name].annotations.model_dump(by_alias=True, exclude_none=True)
    got.pop("title", None)
    assert got == EXPECTED[name]


def test_run_command_is_the_only_open_world(tools):
    """It runs arbitrary shell, so curl and git push are inside its envelope.

    The official filesystem server marks everything openWorldHint: false; that
    server has no exec tool, so copying the blanket value would be wrong here.
    """
    open_world = {
        name
        for name, t in tools.items()
        if t.annotations.model_dump(by_alias=True).get("openWorldHint")
    }
    assert open_world == {"run_command"}


def test_read_only_tools_are_exactly_the_non_mutating_ones(tools):
    read_only = {
        name
        for name, t in tools.items()
        if t.annotations.model_dump(by_alias=True).get("readOnlyHint")
    }
    assert read_only == {
        "read_file",
        "read_multiple_files",
        "read_directory",
        "list_processes",
    }


def test_every_writing_tool_is_marked_destructive(tools):
    for name, t in tools.items():
        dumped = t.annotations.model_dump(by_alias=True)
        if not dumped.get("readOnlyHint"):
            assert dumped.get("destructiveHint") is True, name


def test_read_only_tools_omit_the_write_hints(tools):
    """Per the spec those two hints have no meaning when readOnlyHint is true."""
    for name in ("read_file", "read_multiple_files", "read_directory", "list_processes"):
        dumped = tools[name].annotations.model_dump(by_alias=True, exclude_none=True)
        assert "destructiveHint" not in dumped
        assert "idempotentHint" not in dumped


def test_write_file_is_idempotent_and_edit_file_is_not(tools):
    """Writing the same content twice is a no-op; a second edit finds no match."""
    w = tools["write_file"].annotations.model_dump(by_alias=True)
    e = tools["edit_file"].annotations.model_dump(by_alias=True)
    assert w["idempotentHint"] is True
    assert e["idempotentHint"] is False


SNAKE_KEYS = {
    "read_only_hint",
    "destructive_hint",
    "idempotent_hint",
    "open_world_hint",
}


def test_no_snake_case_keys_reach_the_wire(tools):
    """The spec's field names are camelCase, and a snake_case key is not a rename.

    In mcp 1.27.1 the model's fields are literally named ``readOnlyHint`` with no
    aliases and ``extra="allow"``, so a snake_case keyword argument is accepted
    silently as an unrelated extra: the real hint stays None and the response
    carries a junk key no client reads. Newer mcp renames the fields and adds
    aliases, so the same code is correct there — which is exactly what makes
    this worth pinning, since the two versions disagree without erroring and CI
    does not necessarily install what production runs.
    """
    for name, tool in tools.items():
        dumped = tool.annotations.model_dump(by_alias=True, exclude_none=True)
        leaked = SNAKE_KEYS & set(dumped)
        assert leaked == set(), f"{name} emitted snake_case annotation keys: {leaked}"


def test_hint_values_are_actually_set_not_just_present(tools):
    """Guards the same failure from the other side.

    An annotation object can exist and dump to something while every hint the
    code set is None — that is precisely how the mcp 1.27.1 mismatch presents.
    """
    for name, expected in EXPECTED.items():
        dumped = tools[name].annotations.model_dump(by_alias=True, exclude_none=True)
        for key, value in expected.items():
            assert key in dumped, f"{name} is missing {key}"
            assert dumped[key] is value, f"{name}.{key} is {dumped[key]!r}, expected {value!r}"
