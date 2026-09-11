from pathlib import Path

from ctfws.models.evidence import EvidenceCreate
from ctfws.models.host import HostCreate
from ctfws.reports.generator import ReportService


def test_reports_include_workspace_state(workspace, tmp_path: Path) -> None:
    host = workspace.add_host(HostCreate(name="web01", ip="10.10.10.20"))
    workspace.evidence.create(
        EvidenceCreate(
            host_id=host.id,
            type="output",
            description="ip addr capture",
            path="evidence/ip.txt",
        )
    )

    service = ReportService(workspace)
    markdown = service.markdown()
    paths = service.write(tmp_path / "reports", {"md", "html"})

    assert "# Pivot-Lab" in markdown
    assert "web01" in markdown
    assert "ip addr capture" in markdown
    assert (tmp_path / "reports" / "report.md") in paths
    assert (tmp_path / "reports" / "report.html") in paths
