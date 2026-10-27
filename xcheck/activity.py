"""Progress metadata without exposing a running peer's answer or tool output."""

import json
import math
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
        elif event == 'agent_event':
            native = obj.get('event') or {}
            self.data['last_event'] = native.get('type') or event
            if native.get('type') == 'content_end' and native.get('contentType') == 'tool' and not native.get('error'):
                tool = native.get('toolName') or 'tool'
        if tool:
            self.data['completed_tools'] += 1
            self.data['last_completed_tool'] = dict(kind=tool, observed_at=at)
        atomic_write(self.path, json.dumps(self.data))


def snapshot(run, name):
    directory = run.lane_dir(name)
    live = is_alive(directory / 'alive') if not (directory / 'exit.json').exists() else False
    result = dict(lane=name, live=live, explicit_cap=run.cap, explicit_idle=run.idle or None)
    try:  # a settled lane reports its outcome, not the last phase it reached
        result['status'] = json.loads((directory / 'exit.json').read_text())['status']
    except (OSError, ValueError, KeyError, TypeError):
        pass
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
    state = f"status={row['status']}" if row.get('status') else f'phase={phase}'
    parts = [row['lane'], state, f'live={live}',
             f"events={stream.get('events', 0)}", f"tools={stream.get('completed_tools', 0)}"]
    if stream.get('last_event'):
        parts.append(f"last={stream['last_event']}")
    native = row.get('native', {})
    if native.get('unavailable'):
        parts.append('native=unavailable')
    if native.get('skipped_messages_or_parts'):
        parts.append('native=partial')
    if native.get('last_tool') and native['last_tool'].get('kind'):
        tool = native['last_tool']
        parts.append(f"native_tool={tool.get('kind')}:{tool.get('status')}")
    if native.get('last_reasoning'):
        parts.append(f"reasoning_chars={native['last_reasoning']['characters']}")
    return ' '.join(parts)


def _native_times(value):
    if not isinstance(value, dict):
        return None
    return {key: value[key] for key in ('created', 'ran', 'completed')
            if isinstance(value.get(key), (int, float)) and not isinstance(value[key], bool)
            and math.isfinite(value[key])}


def _opencode_messages(run, name, session):
    """Read only a session created for this run's exact lane workspace."""
    path = run.store(name) or Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'opencode/opencode.db'
    if run.lane(name).model.split('/', 1)[0] in ('kilo', 'anyrouter'):
        path = run.lane_dir(name) / 'opencode.db'
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        owner = db.execute('SELECT directory, time_created FROM session_v2 WHERE id=?', (session,)).fetchone()
        if not owner or Path(owner[0]).resolve() != workspace(run.lane(name)).resolve() or owner[1] < run.epoch * 1000:
            raise ValueError('native session ownership could not be verified')
        return db.execute("SELECT data, time_updated FROM session_message WHERE session_id=? AND type='assistant' ORDER BY time_updated DESC, seq DESC LIMIT 20", (session,)).fetchall()


def opencode_failure(run, name):
    """Supplement a settled ACP auth failure, which V2 strips of its cause.

    Never export the native response body, headers or credential data. This
    is deliberately separate from progress snapshots, which stay text-free.
    An unavailable/mismatched store leaves the authoritative ACP error intact.
    """
    try:
        session = json.loads((run.lane_dir(name) / 'activity.json').read_text())['session_id']
        if not isinstance(session, str):
            return None
        rows = _opencode_messages(run, name, session)
        message = json.loads(rows[0][0]) if rows else {}
        model = message.get('model') or {}
        expected = run.lane(name).model
        expected = expected if '/' in expected else f'opencode/{expected}'
        if f"{model.get('providerID')}/{model.get('id')}" != expected:
            return None
        error = message.get('error') or {}
        if (error.get('type') not in ('provider.auth', 'provider.quota') and error.get('status') != 429) or not isinstance(error.get('message'), str):
            return None
        result = dict(kind='native_error', type=error.get('type', 'provider.rate_limit'), message=' '.join(error['message'].split())[:500])
        if isinstance(error.get('status'), int) and not isinstance(error['status'], bool):
            result['status'] = error['status']
        return result
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, KeyError):
        return None


def opencode_native(run, name, session):
    try:
        rows = _opencode_messages(run, name, session)
        result, skipped = {}, 0
        for raw, updated in rows:
            try:
                message = json.loads(raw)
                if not isinstance(message, dict) or not isinstance(message.get('content'), list):
                    raise ValueError("unknown native message shape")
                for part in reversed(message['content']):
                    if not isinstance(part, dict):
                        skipped += 1
                        continue
                    kind = part.get('type')
                    if kind == 'tool' and 'last_tool' not in result:
                        state, tool = part.get('state'), part.get('name')
                        if (not isinstance(state, dict) or not isinstance(tool, str) or not tool
                                or state.get('status') not in ('streaming', 'running', 'completed', 'error')):
                            skipped += 1
                            continue
                        result['last_tool'] = dict(kind=tool, status=state.get('status'), updated_at=now(updated / 1000), time=_native_times(part.get('time')))
                    elif kind == 'reasoning' and 'last_reasoning' not in result:
                        text = part.get('text')
                        if not isinstance(text, str):
                            skipped += 1
                            continue
                        result['last_reasoning'] = dict(characters=len(text), updated_at=now(updated / 1000), time=_native_times(part.get('time')))
            except (OSError, ValueError, TypeError, OverflowError):
                skipped += 1
        if skipped:
            result['skipped_messages_or_parts'] = skipped

        return result
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError, KeyError):
        return dict(unavailable='native progress store unavailable or its schema changed')
