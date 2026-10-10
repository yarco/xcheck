"""Progress metadata without exposing a running peer's answer or tool output."""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .files import atomic_write, is_alive, now, workspace


class Activity:
    def __init__(self, directory, kind):
        self.path = directory / 'activity.json'
        self.kind = kind
        self.data = dict(events=0, completed_tools=0)

    def observe(self, obj):
        update = (obj.get('params') or {}).get('update') or {}
        if 'jsonrpc' in obj:  # ACP: name the update, call or response
            event = update.get('sessionUpdate') or obj.get('method') or ('error' if 'error' in obj else 'result')
        else:  # a native stream names its own event type
            event = obj.get('type')
        at = now()
        self.data.update(events=self.data['events'] + 1, last_event=event, observed_at=at)
        if self.kind == 'opencode':
            session = (obj.get('params') or {}).get('sessionId') or (obj.get('result') or {}).get('sessionId')
            if isinstance(session, str):
                self.data['session_id'] = session
        tool = None
        item = obj.get('item') or {}
        if event == 'tool_call_update' and update.get('status') == 'completed':
            tool = update.get('kind') or 'tool'
        elif event == 'item.completed' and item.get('type') not in (None, 'agent_message', 'reasoning'):
            tool = item['type']  # Codex: command_execution, mcp_tool_call, web_search, ...
        elif event == 'user' and any(isinstance(b, dict) and b.get('type') == 'tool_result' and not b.get('is_error')
                                     for b in (obj.get('message') or {}).get('content') or []):
            tool = 'tool_result'  # Claude
        if tool:
            self.data['completed_tools'] += 1
            self.data['last_completed_tool'] = dict(kind=tool, observed_at=at)
        atomic_write(self.path, json.dumps(self.data))


def snapshot(run, name):
    directory = run.lane_dir(name)
    live = is_alive(directory / 'alive') if not (directory / 'exit.json').exists() else False
    result = dict(lane=name, live=live, explicit_cap=run.cap, explicit_idle=run.idle or None)
    for filename, key in [('activity.json', 'stream'), ('phase.json', 'phase')]:
        try:
            result[key] = json.loads((directory / filename).read_text())
        except (OSError, ValueError):
            pass
    session = result.get('stream', {}).get('session_id')
    if run.lane(name).kind == 'opencode' and session:
        result['native'] = opencode_native(run, name, session)
    result['note'] = 'Native state can persist after exit. Liveness, retry events and an open tool alone do not prove forward progress.'
    return result


def compact(row):
    phase = row.get('phase', {}).get('phase', 'unknown')
    live = {True: 'yes', False: 'no', None: 'unknown'}[row['live']]
    stream = row.get('stream', {})
    parts = [row['lane'], f'phase={phase}', f'live={live}',
             f"events={stream.get('events', 0)}", f"tools={stream.get('completed_tools', 0)}"]
    if stream.get('last_event'):
        parts.append(f"last={stream['last_event']}")
    native = row.get('native', {})
    if native.get('last_tool'):
        tool = native['last_tool']
        parts.append(f"native_tool={tool.get('kind')}:{tool.get('status')}")
    if native.get('last_reasoning'):
        parts.append(f"reasoning_chars={native['last_reasoning']['characters']}")
    return ' '.join(parts)


def opencode_native(run, name, session):
    path = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'opencode/opencode.db'
    try:
        with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=1)) as db:
            owner = db.execute('SELECT directory, time_created FROM session WHERE id=?', (session,)).fetchone()
            if not owner or Path(owner[0]).resolve() != workspace(name).resolve() or owner[1] < run.epoch * 1000:
                return dict(unavailable='native session ownership could not be verified')
            rows = db.execute('SELECT data, time_updated FROM part WHERE session_id=? ORDER BY time_updated DESC LIMIT 20', (session,)).fetchall()
        result = {}
        for raw, updated in rows:
            part = json.loads(raw)
            kind = part.get('type')
            if kind == 'tool' and 'last_tool' not in result:
                state = part.get('state') or {}
                result['last_tool'] = dict(kind=part.get('tool'), status=state.get('status'), updated_at=now(updated / 1000), time=state.get('time'))
            elif kind == 'reasoning' and 'last_reasoning' not in result:
                result['last_reasoning'] = dict(characters=len(part.get('text', '')), updated_at=now(updated / 1000), time=part.get('time'))
        return result
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, KeyError):
        return dict(unavailable='native progress store unavailable or its schema changed')
