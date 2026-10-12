import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_workflow_schematic_matches_built_system_flow():
    template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert 'class="workflow-map"' in template
    assert 'workflow-schematic-canvas' in template
    assert 'workflow-link-field' in template
    assert 'workflow-schema-node' in template
    assert 'workflow-node-core' in template
    assert 'workflow-schema-legend' in template

    assert len(re.findall(r'class="workflow-schema-node', template)) == 11

    for label in [
        "DASHBOARD UI",
        "FASTAPI API",
        "CLAUDE AGENT LOOP",
        "TOOL DISPATCH",
        "MATON CONNECTORS",
        "LOCAL SERVICES",
        "POSTGRES MEMORY",
        "POSTGRES VECTOR SEARCH",
        "S3 FILE LOCKER",
        "SCHEDULER + JOBS",
        "VOICE STT / TTS",
        "COMMAND CENTER",
    ]:
        assert label in template

    for label in [
        "Chat, voice, command, billing, meetings, files.",
        "Routes requests through /api/chat, /api/search, /api/documents, voice, meetings, bills, and jobs.",
        "run_turn / stream_turn decides, calls tools, and loops until the answer is ready.",
        "build_tools + dispatch exposes Maton and local tools with business scoping.",
        "Gmail, Calendar, Drive, QuickBooks, Notion, Zoom, Jira, WATI.",
        "Conversations persist in Postgres with in-memory fallback.",
        "document_chunks + pgvector; filtered by entity, sensitivity, region.",
        "Optional original bytes storage plus presigned links.",
        "Due tasks and job pollers run the same agent with readonly / draft / full autonomy ceilings.",
        "Write actions stop for confirmation unless configured task autonomy allows them.",
    ]:
        assert label in template

    assert "Pinecone" not in template

    assert ".workflow-schematic-canvas" in css
    assert ".workflow-link-field" in css
    assert ".workflow-schema-node" in css
    assert ".workflow-node-core" in css
    assert ".workflow-schema-legend" in css
    assert ".workflow-node-agent" in css
    assert ".workflow-node-vector" in css
    assert ".workflow-node-scheduler" in css
    assert "@keyframes workflow-link-flow" in css


if __name__ == "__main__":
    test_workflow_schematic_matches_built_system_flow()
    print("PASS test_workflow_schematic_matches_built_system_flow")
