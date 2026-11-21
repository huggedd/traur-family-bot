import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DB_PATH = 'mcl.sqlite3'
TZ = ZoneInfo('Europe/Zagreb')
LIMIT = 160


def week_key():
    now = datetime.now(TZ)
    monday = (now - timedelta(days=now.weekday())).date()
    return monday.isoformat()


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=20000')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def init_db():
    with connect() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS players (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            evidence TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            channel_id INTEGER,
            message_id INTEGER,
            reviewer_id INTEGER,
            requested_points INTEGER,
            granted_points INTEGER,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            decided_at TEXT,
            FOREIGN KEY(guild_id, user_id) REFERENCES players(guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS awards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            submission_id INTEGER NOT NULL UNIQUE,
            reviewer_id INTEGER NOT NULL,
            points INTEGER NOT NULL CHECK(points > 0 AND points <= 160),
            week_key TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(submission_id) REFERENCES submissions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_awards_week ON awards(guild_id,user_id,week_key);
        ''')


def register(guild_id, user_id, nickname):
    with connect() as conn:
        conn.execute('''INSERT INTO players(guild_id,user_id,nickname) VALUES(?,?,?)
            ON CONFLICT(guild_id,user_id) DO UPDATE SET nickname=excluded.nickname''',
            (guild_id, user_id, nickname))


def player(guild_id, user_id):
    with connect() as conn:
        return conn.execute('SELECT * FROM players WHERE guild_id=? AND user_id=?',
                            (guild_id, user_id)).fetchone()


def points(guild_id, user_id):
    with connect() as conn:
        row = conn.execute('''SELECT COALESCE(SUM(points),0) AS total,
            COALESCE(SUM(CASE WHEN week_key=? THEN points ELSE 0 END),0) AS weekly
            FROM awards WHERE guild_id=? AND user_id=?''',
            (week_key(),guild_id,user_id)).fetchone()
        return row['total'], row['weekly']


def create_submission(guild_id, user_id, evidence):
    with connect() as conn:
        cursor = conn.execute('INSERT INTO submissions(guild_id,user_id,evidence) VALUES(?,?,?)',
                              (guild_id,user_id,evidence))
        return cursor.lastrowid


def attach_submission(submission_id, channel_id, message_id):
    with connect() as conn:
        conn.execute('UPDATE submissions SET channel_id=?,message_id=? WHERE id=?',
                     (channel_id,message_id,submission_id))


def submission(submission_id):
    with connect() as conn:
        return conn.execute('SELECT * FROM submissions WHERE id=?',(submission_id,)).fetchone()


def pending_submissions():
    with connect() as conn:
        return conn.execute("SELECT * FROM submissions WHERE status='pending' AND message_id IS NOT NULL").fetchall()


def approve(submission_id, reviewer_id, requested):
    if not 1 <= requested <= LIMIT:
        return 'invalid', 0, 0
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        s = conn.execute('SELECT * FROM submissions WHERE id=?',(submission_id,)).fetchone()
        if not s or s['status'] != 'pending':
            conn.rollback()
            return 'closed', 0, 0
        wk = week_key()
        earned = conn.execute('''SELECT COALESCE(SUM(points),0) FROM awards
            WHERE guild_id=? AND user_id=? AND week_key=?''',
            (s['guild_id'],s['user_id'],wk)).fetchone()[0]
        granted = min(requested, max(0, LIMIT-earned))
        if granted == 0:
            conn.rollback()
            return 'limit', 0, earned
        conn.execute('''INSERT INTO awards(guild_id,user_id,submission_id,reviewer_id,points,week_key)
            VALUES(?,?,?,?,?,?)''',
            (s['guild_id'],s['user_id'],submission_id,reviewer_id,granted,wk))
        conn.execute('''UPDATE submissions SET status='approved',reviewer_id=?,
            requested_points=?,granted_points=?,decided_at=CURRENT_TIMESTAMP WHERE id=?''',
            (reviewer_id,requested,granted,submission_id))
        conn.commit()
        return 'ok', granted, earned+granted
    finally:
        conn.close()


def reject(submission_id, reviewer_id, reason):
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        cursor = conn.execute('''UPDATE submissions SET status='rejected',reviewer_id=?,
            reason=?,decided_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending' ''',
            (reviewer_id,reason,submission_id))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

# v0.2: persistent personal archives and pending uploads.
def init_v2():
    with connect() as conn:
        cols = {r['name'] for r in conn.execute('PRAGMA table_info(players)')}
        if 'personal_channel_id' not in cols:
            conn.execute('ALTER TABLE players ADD COLUMN personal_channel_id INTEGER')
        cols = {r['name'] for r in conn.execute('PRAGMA table_info(submissions)')}
        if 'activity' not in cols:
            conn.execute('ALTER TABLE submissions ADD COLUMN activity TEXT')
        conn.execute('''CREATE TABLE IF NOT EXISTS upload_intents (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            activity TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        )''')

def set_personal_channel(guild_id, user_id, channel_id):
    with connect() as conn:
        conn.execute('UPDATE players SET personal_channel_id=? WHERE guild_id=? AND user_id=?',
                     (channel_id,guild_id,user_id))

def player_for_channel(guild_id, channel_id):
    with connect() as conn:
        return conn.execute('SELECT * FROM players WHERE guild_id=? AND personal_channel_id=?',
                            (guild_id,channel_id)).fetchone()

def begin_upload(guild_id, user_id, activity):
    with connect() as conn:
        conn.execute('''INSERT INTO upload_intents(guild_id,user_id,activity,created_at)
            VALUES(?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id,user_id) DO UPDATE SET activity=excluded.activity,created_at=CURRENT_TIMESTAMP''',
            (guild_id,user_id,activity))

def get_upload(guild_id, user_id):
    with connect() as conn:
        return conn.execute('''SELECT activity FROM upload_intents WHERE guild_id=? AND user_id=?
            AND created_at >= datetime('now','-30 minutes')''',(guild_id,user_id)).fetchone()

def consume_upload(guild_id, user_id):
    with connect() as conn:
        conn.execute('DELETE FROM upload_intents WHERE guild_id=? AND user_id=?',(guild_id,user_id))

def create_activity_submission(guild_id,user_id,evidence,activity):
    with connect() as conn:
        cur=conn.execute('INSERT INTO submissions(guild_id,user_id,evidence,activity) VALUES(?,?,?,?)',
                         (guild_id,user_id,evidence,activity))
        return cur.lastrowid
