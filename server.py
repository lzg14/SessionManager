#!/usr/bin/env python3
"""Session Viewer - Backend"""
import json, sqlite3, webbrowser, threading, shutil
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

PORT = 5690
HOME = Path.home()

# Claude Code sessions (check multiple locations)
def find_claude_sessions():
    paths = [
        HOME / ".claude" / "projects",
        Path("D:/claude/projects"),
        Path("C:/claude/projects"),
    ]
    for p in paths:
        if p.exists(): return p
    return None

CLAUDE_SESSIONS = find_claude_sessions()
OPENCODE = HOME / ".local" / "share" / "opencode" / "opencode.db"
PI = HOME / ".pi" / "agent" / "sessions"
import os
DSH_HOME = Path(os.environ.get("DSH_HOME") or (HOME / ".dsh"))
DSH_SESSIONS = DSH_HOME / "sessions"


def _dsh_read_log(path):
    """读取一个 DSH session 日志，返回解码后的文本（支持 zstd 压缩与多帧拼接）"""
    data = path.read_bytes()
    if path.suffix == ".zstd":
        try:
            from compression import zstd
        except ImportError:
            print("[DSH] 需要 Python 3.14+ (compression.zstd) 才能读取 .jsonl.zstd 日志")
            return None
        # 日志是多个独立 zstd 帧拼接（header 一帧 + 每个 append batch 一帧），逐帧解码
        chunks = []
        rest = data
        while rest:
            d = zstd.ZstdDecompressor()
            chunks.append(d.decompress(rest))
            if not d.eof:
                break
            rest = d.unused_data
        return b"".join(chunks).decode("utf-8", "replace")
    return data.decode("utf-8", "replace")


def _dsh_blocks_text(content):
    """从 ContentBlock 列表提取纯文本（text 块；跳过 reasoning/image/tool 等）"""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                parts.append(b["text"])
    return "\n".join(parts)


def parse_dsh():
    """解析 DeepSeek Harness (dsh) 会话：~/.dsh/sessions/<project>/<session>/session.jsonl[.zstd]"""
    sessions = {}
    if not DSH_SESSIONS.exists():
        return sessions
    for proj_dir in DSH_SESSIONS.iterdir():
        if not proj_dir.is_dir():
            continue
        for sess_dir in proj_dir.iterdir():
            if not sess_dir.is_dir():
                continue
            path = None
            for name in ("session.jsonl.zstd", "session.jsonl"):
                cand = sess_dir / name
                if cand.exists():
                    path = cand
                    break
            if path is None:
                continue
            text = None
            try:
                text = _dsh_read_log(path)
            except Exception:
                continue
            if not text:
                continue
            sid = None
            project = None
            start = None
            title = ""
            msgs = []
            try:
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(d, dict):
                        continue
                    t = d.get("type", "")
                    # 第一行是 session header
                    if t == "session":
                        sid = d.get("id") or sess_dir.name
                        cwd = d.get("cwd")
                        if cwd:
                            project = cwd
                        created = d.get("createdAt")
                        if isinstance(created, (int, float)) and created > 0:
                            try:
                                start = datetime.fromtimestamp(created / 1000).isoformat()
                            except Exception:
                                pass
                        continue
                    # 事件行（打包的 chunk 行没有 type/seq，自动跳过）
                    if "seq" not in d:
                        continue
                    if t == "session/title":
                        ti = d.get("data", {}).get("title")
                        if ti:
                            title = ti
                    elif t == "user/message":
                        msg = d.get("data", {}) or {}
                        src = msg.get("source") or {}
                        # 跳过注入的上下文（文件变更通知/AGENTS.md 等），只保留真人消息
                        if src.get("kind") not in (None, "user"):
                            continue
                        body = _dsh_blocks_text(msg.get("content"))
                        if body and body.strip():
                            msgs.append({"role": "user", "content": body[:500], "time": _dsh_time(d)})
                    elif t == "assistant/message":
                        am = (d.get("data", {}) or {}).get("message", {}) or {}
                        body = _dsh_blocks_text(am.get("content"))
                        if body and body.strip():
                            msgs.append({"role": "assistant", "content": body[:500], "time": _dsh_time(d)})
            except Exception:
                continue
            if not msgs:
                continue  # 空白会话不展示
            if not project:
                project = proj_dir.name
            sessions[sid] = {
                "id": sid,
                "tool": "DeepSeek Harness",
                "project": project,
                "title": title,
                "start": start,
                "msgs": msgs,
                "dir": str(sess_dir),
            }
    return sessions


def _dsh_time(event):
    ts = event.get("time")
    if isinstance(ts, (int, float)) and ts > 0:
        try:
            return datetime.fromtimestamp(ts / 1000).isoformat()
        except Exception:
            return None
    return None


def parse_claude():
    sessions = {}
    if not CLAUDE_SESSIONS.exists(): return sessions
    
    # 遍历项目目录
    for proj_dir in CLAUDE_SESSIONS.iterdir():
        if not proj_dir.is_dir(): continue
        proj_name = proj_dir.name.replace('--', '/').replace('-', ':', 1).replace('-', '/')
        
        # 读取该目录下的 .jsonl 文件
        for jsonl_file in proj_dir.glob('*.jsonl'):
            sid = jsonl_file.stem
            msgs = []
            start_time = None
            
            try:
                with open(jsonl_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        try:
                            d = json.loads(line.strip())
                            t = d.get('type', '')
                            
                            # 记录开始时间
                            ts = d.get('timestamp')
                            if ts and not start_time:
                                try:
                                    start_time = datetime.fromisoformat(ts.replace('Z', '+00:00')).isoformat()
                                except: pass
                            
                            # 提取用户和助手消息
                            if t in ('user', 'assistant'):
                                msg = d.get('message', {})
                                role = msg.get('role', t)
                                content = msg.get('content', '')
                                
                                if isinstance(content, list):
                                    for part in content:
                                        if isinstance(part, dict) and part.get('type') == 'text':
                                            text = part.get('text', '')
                                            if text and len(text.strip()) > 0:
                                                msgs.append({'role': role, 'content': text[:500], 'time': ts})
                                            break
                                elif isinstance(content, str) and content.strip():
                                    msgs.append({'role': role, 'content': content[:500], 'time': ts})
                        except: continue
            except: continue
            
            if msgs:  # 只添加有消息的session
                sessions[sid] = {
                    'id': sid,
                    'tool': 'Claude Code',
                    'project': proj_name,
                    'title': '',
                    'start': start_time,
                    'msgs': msgs
                }
    
    return sessions


def parse_opencode():
    sessions = {}
    if not OPENCODE.exists(): return sessions
    try:
        conn = sqlite3.connect(str(OPENCODE))
        cur = conn.cursor()
        cur.execute("SELECT * FROM session LIMIT 2000")
        cols = [d[0] for d in cur.description]
        for vals in cur.fetchall():
            row = dict(zip(cols, vals))
            sid = str(row.get('id', ''))
            if not sid: continue
            ts = row.get('time_created', 0)
            start = datetime.fromtimestamp(ts / 1000).isoformat() if ts and ts > 1000000000000 else None
            title = row.get('title', '')
            if title and title.isascii(): title = ''  # 过滤乱码
            sessions[sid] = {'id': sid, 'tool': 'OpenCode', 'project': row.get('directory', ''), 'title': title, 'start': start, 'msgs': []}
        
        msg_roles = {}
        try:
            cur.execute("SELECT id, data FROM message LIMIT 10000")
            for r in cur.fetchall():
                try: msg_roles[r[0]] = json.loads(r[1]).get('role', 'user')
                except: pass
        except: pass
        
        try:
            cur.execute("SELECT message_id, session_id, data FROM part LIMIT 20000")
            for r in cur.fetchall():
                if r[1] not in sessions: continue
                try:
                    d = json.loads(r[2])
                    if d.get('type') == 'text' and d.get('text'):
                        sessions[r[1]]['msgs'].append({'role': msg_roles.get(r[0], 'user'), 'content': d['text'][:500], 'time': None})
                except: pass
        except: pass
        conn.close()
    except: pass
    return sessions


def parse_pi():
    sessions = {}
    if not PI.exists(): return sessions
    for pdir in PI.iterdir():
        if not pdir.is_dir(): continue
        proj = pdir.name.replace('--', '/')
        if proj.startswith('/'): proj = proj[1:]
        for sf in pdir.glob('*.jsonl'):
            sid = sf.stem
            start = sid.split('_')[0] if '_' in sid else None
            msgs = []
            try:
                with open(sf, 'r', encoding='utf-8') as f:
                    for line in f:
                        try:
                            d = json.loads(line.strip())
                            if d.get('type') == 'message':
                                msg = d.get('message', {})
                                role = msg.get('role', 'user')
                                if role not in ('user', 'assistant'): continue
                                for part in msg.get('content', []):
                                    if isinstance(part, dict) and part.get('type') == 'text' and part.get('text'):
                                        msgs.append({'role': role, 'content': part['text'][:500], 'time': d.get('timestamp')})
                                        break
                                    elif isinstance(part, str):
                                        msgs.append({'role': role, 'content': part[:500], 'time': d.get('timestamp')})
                                        break
                        except: continue
            except: pass
            sessions[sid] = {'id': sid, 'tool': 'Pi', 'project': proj, 'title': '', 'start': start, 'msgs': msgs}
    return sessions


# 缓存
_all_sessions_cache = None

def get_all_sessions_cached():
    global _all_sessions_cache
    if _all_sessions_cache is None:
        print("Loading sessions...")
        all_s = []
        all_s.extend(parse_claude().values())
        all_s.extend(parse_opencode().values())
        all_s.extend(parse_pi().values())
        all_s.extend(parse_dsh().values())
        # 生成标题
        for s in all_s:
            if not s.get('title'):
                for m in s.get('msgs', []):
                    if m.get('role') == 'user' and m.get('content'):
                        s['title'] = m['content'][:40] + ('...' if len(m['content']) > 40 else '')
                        break
        _all_sessions_cache = all_s
        print(f"Loaded {len(all_s)} sessions")
    return _all_sessions_cache

def get_all(tool='all', search=''):
    import copy
    all_s = copy.deepcopy(get_all_sessions_cached())
    # 按工具类型筛选
    if tool and tool != 'all':
        tool_map = {'claude': 'Claude Code', 'opencode': 'OpenCode', 'pi': 'Pi', 'dsh': 'DeepSeek Harness'}
        target = tool_map.get(tool, tool)
        all_s = [s for s in all_s if s.get('tool') == target]
    if search:
        s = search.lower()
        result = []
        for x in all_s:
            if s in (x.get('project') or '').lower():
                x['match'] = ''
                result.append(x)
                continue
            for m in x.get('msgs', []):
                content = (m.get('content') or '').lower()
                if s in content:
                    idx = content.find(s)
                    start = max(0, idx - 30)
                    end = min(len(content), idx + len(s) + 30)
                    x['match'] = ('...' if start > 0 else '') + (m.get('content') or '')[start:end] + ('...' if end < len(content) else '')
                    result.append(x)
                    break
        all_s = result
    all_s.sort(key=lambda x: x.get('start') or '', reverse=True)
    return all_s


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent / 'public'), **kwargs)
    
    def do_GET(self):
        p = urlparse(self.path)
        if p.path == '/api/sessions':
            params = parse_qs(p.query)
            # 先获取全部统计
            all_sessions = get_all('all', params.get('search', [''])[0])
            stats = {
                'total': len(all_sessions),
                'claude': sum(1 for s in all_sessions if s['tool'] == 'Claude Code'),
                'opencode': sum(1 for s in all_sessions if s['tool'] == 'OpenCode'),
                'pi': sum(1 for s in all_sessions if s['tool'] == 'Pi'),
                'dsh': sum(1 for s in all_sessions if s['tool'] == 'DeepSeek Harness'),
            }
            # 再按筛选条件获取列表
            sessions = get_all(params.get('tool', ['all'])[0], params.get('search', [''])[0])
            self.send_json({'sessions': sessions, 'stats': stats})
        elif p.path.startswith('/api/session/'):
            sid = p.path.split('/')[-1]
            all_s = get_all_sessions_cached()
            for s in all_s:
                if s['id'] == sid:
                    self.send_json(s)
                    return
            self.send_json({'error': 'Not found'}, 404)
        elif p.path.startswith('/api/export/'):
            sid = p.path.split('/')[-1]
            session = None
            all_s = get_all_sessions_cached()
            for s in all_s:
                if s['id'] == sid:
                    session = s
                    break
            if session:
                lines = [f"# {session['tool']} - {session.get('project', '')}", f"\n**ID:** `{session['id']}`", f"**Time:** {session.get('start', '')}", "\n---\n"]
                for m in session.get('msgs', []):
                    lines.append(f"### {'User' if m['role'] == 'user' else 'Assistant'}\n\n{m['content']}\n")
                self.send_response(200)
                self.send_header('Content-Type', 'text/markdown; charset=utf-8')
                self.end_headers()
                self.wfile.write('\n'.join(lines).encode('utf-8'))
            else: self.send_json({'error': 'Not found'}, 404)
        else: super().do_GET()
    
    def do_DELETE(self):
        p = urlparse(self.path)
        if p.path.startswith('/api/session/'):
            sid = p.path.split('/')[-1]
            all_s = get_all_sessions_cached()
            for i, s in enumerate(all_s):
                if s['id'] == sid:
                    # 根据tool类型删除对应文件
                    tool = s.get('tool')
                    if tool == 'Claude Code':
                        f = CLAUDE_SESSIONS / s.get('project', '').replace('/', '--').replace(':', '-') / f"{sid}.jsonl"
                        if f.exists(): f.unlink()
                    elif tool == 'OpenCode':
                        # OpenCode用SQLite，删除session和相关message/part
                        conn = sqlite3.connect(str(OPENCODE))
                        cur = conn.cursor()
                        # 先获取该session的所有message id
                        cur.execute("SELECT id FROM message WHERE session_id = ?", (sid,))
                        msg_ids = [r[0] for r in cur.fetchall()]
                        if msg_ids:
                            placeholders = ','.join(['?'] * len(msg_ids))
                            cur.execute(f"DELETE FROM part WHERE message_id IN ({placeholders})", msg_ids)
                        cur.execute("DELETE FROM message WHERE session_id = ?", (sid,))
                        cur.execute("DELETE FROM session WHERE id = ?", (sid,))
                        conn.commit()
                        conn.close()
                    elif tool == 'Pi':
                        proj = s.get('project', '').replace('/', '--')
                        f = PI / proj / f"{sid}.jsonl"
                        if f.exists(): f.unlink()
                    elif tool == 'DeepSeek Harness':
                        # dsh：删除整个 session 目录（含日志与附属 artifacts）
                        d = s.get('dir')
                        if d and Path(d).exists():
                            shutil.rmtree(d, ignore_errors=True)
                    # 从缓存中移除
                    all_s.pop(i)
                    self.send_json({'ok': True})
                    return
            self.send_json({'error': 'Not found'}, 404)
        else:
            self.send_json({'error': 'Not found'}, 404)
    
    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        super().end_headers()
    
    def log_message(self, *args): pass


if __name__ == '__main__':
    print(f"Session Viewer: http://localhost:{PORT}")
    server = HTTPServer(('127.0.0.1', PORT), Handler)
    threading.Timer(0.5, lambda: webbrowser.open(f'http://localhost:{PORT}')).start()
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nBye!"); server.shutdown()
