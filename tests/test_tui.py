import pytest
from textual.widgets import DataTable

from ctfws.models.host import HostCreate
from ctfws.tui.app import WorkspaceTUI


@pytest.mark.asyncio
async def test_dashboard_mounts_hosts_and_networks(workspace) -> None:
    workspace.add_host(HostCreate(name="web01", ip="10.10.10.20"))
    application = WorkspaceTUI(workspace.paths)

    async with application.run_test() as pilot:
        await pilot.pause()
        hosts = application.query_one("#hosts", DataTable)
        networks = application.query_one("#networks", DataTable)

        assert hosts.row_count == 1
        assert networks.row_count == 0
