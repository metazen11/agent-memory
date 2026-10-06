"""Fault injection for durable replay; payloads stay on disk until acknowledged."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def node(script, tmp):
    result = subprocess.run(
        ["node", "-e", script, str(tmp)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def test_offline_then_acknowledgment_and_quarantine(tmp_path):
    result = node(
        """
const s=require('./integrations/codex/spool'); const fs=require('fs'); const path=require('path');
(async()=>{
 const dir=path.join(process.argv[1],'repo/.agent-memory-codex/spool');
 s.save(dir,{session_id:'native',cwd:'/local/repo',tool_name:'test'});
 const offline=await s.drain(s.files(dir),async()=>{throw new Error('offline')},{delayMs:0});
 const retained=s.files(dir).length;
 fs.writeFileSync(path.join(dir,'0-bad.json'),'invalid');
 const calls=[]; const recovered=await s.drain(s.files(dir),async(r,p)=>calls.push([r,p]),{delayMs:0});
 console.log(JSON.stringify({offline,retained,recovered,pending:s.files(dir).length,calls,quarantine:fs.readdirSync(path.join(dir,'../quarantine')).length}));
})();""",
        tmp_path,
    )
    assert result["offline"]["error"] == "unreachable"
    assert result["retained"] == 1
    assert result["recovered"]["drained"] == 1
    assert result["recovered"]["quarantined"] == 1
    assert result["pending"] == 0
    assert result["calls"][0][1]["cwd"] == "/local/repo"
    assert result["calls"][0][1]["ingest_id"]
    assert result["quarantine"] == 2


def test_concurrent_drains_cannot_send_same_file(tmp_path):
    result = node(
        """
const s=require('./integrations/codex/spool');
(async()=>{const dir=process.argv[1];s.save(dir,{session_id:'native'});let sent=0;
const send=async()=>{sent++;await new Promise(r=>setTimeout(r,40));};
const results=await Promise.all([s.drain(s.files(dir),send,{delayMs:0}),s.drain(s.files(dir),send,{delayMs:0})]);
console.log(JSON.stringify({sent,drained:results.reduce((n,r)=>n+r.drained,0)}));})();""",
        tmp_path,
    )
    assert result == {"sent": 1, "drained": 1}


def test_discovery_skips_symlinks_and_copies_share_receipt_identity(tmp_path):
    result = node(
        """
const s=require('./integrations/codex/spool');const fs=require('fs');const path=require('path');
(async()=>{const root=process.argv[1];const dirs=['repo/.agent-memory-codex/spool','repo/.claude/worktrees/child/.agent-memory-codex/spool'];
for(const d of dirs){fs.mkdirSync(path.join(root,d),{recursive:true});fs.writeFileSync(path.join(root,d,'1791049671013-1.json'),JSON.stringify({session_id:'native'}));}
fs.symlinkSync(path.join(root,'repo'),path.join(root,'archive-link'));
const found=s.discover(root);const ids=[];for(const d of found)await s.drain(s.files(d),async(r,p)=>ids.push(p.ingest_id),{delayMs:0});
console.log(JSON.stringify({directories:found.length,ids}));})();""",
        tmp_path,
    )
    assert result["directories"] == 2
    assert result["ids"][0] == result["ids"][1]


@pytest.mark.asyncio
async def test_live_replay_receipts_preserve_repeated_prompt_events(
    client, test_prefix, test_project
):
    payload = {
        "session_id": test_prefix + "-receipts",
        "cwd": test_project,
        "prompt": "yes",
        "ingest_id": test_prefix + "-p1",
        "agent_name": "codex-cli",
    }
    first = await client.post("/api/prompts", json=payload)
    replay = await client.post("/api/prompts", json=payload)
    later = await client.post(
        "/api/prompts", json=payload | {"ingest_id": test_prefix + "-p2"}
    )
    assert first.status_code == replay.status_code == later.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["id"] != later.json()["id"]
    tool = {
        "session_id": payload["session_id"],
        "cwd": test_project,
        "tool_name": test_prefix + "-receipt-tool",
        "ingest_id": test_prefix + "-t1",
        "source_system": "codex-cli",
    }
    for _ in range(2):
        result = await client.post("/api/queue", json=tool)
        assert result.status_code == 200
    query = f"SELECT count(*) FROM mem_tool_calls WHERE tool_name='{tool['tool_name']}'"
    count = subprocess.check_output(
        [str(ROOT / "scripts/psql_wrapper.sh"), "-qAt", "-c", query], text=True
    ).strip()
    assert count == "1"


def test_transient_failure_retains_file_and_dead_lock_is_reclaimed(tmp_path):
    result = node(
        """
const s=require('./integrations/codex/spool');const fs=require('fs');const path=require('path');
(async()=>{const dir=process.argv[1];const file=s.save(dir,{session_id:'native'});
fs.mkdirSync(file+'.lock');fs.writeFileSync(path.join(file+'.lock','owner.json'),JSON.stringify({pid:99999999}));
const transient=await s.drain(s.files(dir),async()=>{throw Object.assign(new Error('rate limited'),{status:429});},{delayMs:0});
const retained=s.files(dir).length;
const permanent=await s.drain(s.files(dir),async()=>{throw Object.assign(new Error('invalid'),{status:422});},{delayMs:0});
console.log(JSON.stringify({transient,retained,permanent,pending:s.files(dir).length}));})();""",
        tmp_path,
    )
    assert result["transient"]["error"] == "http_429"
    assert result["retained"] == 1
    assert result["permanent"]["quarantined"] == 1
    assert result["pending"] == 0
