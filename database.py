import os
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Railway volumes are mounted outside the deploy directory.
# In Railway RAILWAY_VOLUME_MOUNT_PATH is provided automatically for the
# attached Volume. Locally the DB stays next to database.py.
APP_DIR = Path(__file__).resolve().parent
LEGACY_DB_PATH = APP_DIR / 'mcl.sqlite3'
volume_path = os.getenv('RAILWAY_VOLUME_MOUNT_PATH')
explicit_db_path = os.getenv('MCL_DB_PATH')
is_railway = any(os.getenv(name) for name in (
    'RAILWAY_ENVIRONMENT_ID',
    'RAILWAY_ENVIRONMENT_NAME',
    'RAILWAY_PROJECT_ID',
))
if is_railway and not (volume_path or explicit_db_path):
    raise RuntimeError(
        'Railway Volume not found. Attach a Volume to this service before starting. '
        'Do not store SQLite in the temporary deployment filesystem.'
    )

if explicit_db_path:
    selected_db_path = Path(explicit_db_path).expanduser()
    if not selected_db_path.is_absolute():
        selected_db_path = APP_DIR / selected_db_path
elif volume_path:
    selected_db_path = Path(volume_path) / 'mcl.sqlite3'
else:
    selected_db_path = LEGACY_DB_PATH

DB_PATH = str(selected_db_path.resolve())
TZ = ZoneInfo('Europe/Zagreb')
LIMIT = 160
_database_prepared = False


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3.Connection and always close afterward."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def prepare_database():
    """Create the DB directory and safely copy a legacy DB on first use.

    SQLite's backup API includes committed WAL data. The legacy source is never
    deleted or modified, and an existing destination is never overwritten.
    """
    global _database_prepared
    if _database_prepared:
        return

    destination = Path(DB_PATH)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination != LEGACY_DB_PATH and not destination.exists() and LEGACY_DB_PATH.exists():
        temporary = destination.with_name(f'.{destination.name}.{os.getpid()}.migrating')
        source_uri = f'{LEGACY_DB_PATH.resolve().as_uri()}?mode=ro'
        try:
            source = sqlite3.connect(source_uri, uri=True)
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
                check = target.execute('PRAGMA quick_check').fetchone()[0]
                if check != 'ok':
                    raise sqlite3.DatabaseError(f'backup integrity check failed: {check}')
            finally:
                target.close()
                source.close()
            if destination.exists():
                temporary.unlink(missing_ok=True)
            else:
                temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    _database_prepared = True


def week_key():
    now = datetime.now(TZ)
    monday = (now - timedelta(days=now.weekday())).date()
    return monday.isoformat()


def connect():
    prepare_database()
    conn = sqlite3.connect(DB_PATH, timeout=20, factory=ClosingConnection)
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
        balance, weekly, _ = _point_summary(conn, guild_id, user_id)
        return balance, weekly


def point_summary(guild_id, user_id):
    with connect() as conn:
        return _point_summary(conn, guild_id, user_id)


def _point_summary(conn, guild_id, user_id):
    wk = week_key()
    row = conn.execute('''
        SELECT
            COALESCE((SELECT SUM(points) FROM awards
                WHERE guild_id=? AND user_id=?), 0)
              + COALESCE((SELECT SUM(amount) FROM point_transactions
                WHERE guild_id=? AND user_id=?), 0) AS balance,
            COALESCE((SELECT SUM(points) FROM awards
                WHERE guild_id=? AND user_id=? AND week_key=?), 0)
              + COALESCE((SELECT SUM(amount) FROM point_transactions
                WHERE guild_id=? AND user_id=? AND week_key=? AND amount>0), 0) AS weekly,
            COALESCE((SELECT SUM(points) FROM awards
                WHERE guild_id=? AND user_id=?), 0)
              + COALESCE((SELECT SUM(amount) FROM point_transactions
                WHERE guild_id=? AND user_id=? AND amount>0), 0) AS earned
        ''', (
            guild_id, user_id, guild_id, user_id,
            guild_id, user_id, wk, guild_id, user_id, wk,
            guild_id, user_id, guild_id, user_id,
        )).fetchone()
    return row['balance'], row['weekly'], row['earned']


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


# v0.4: signed point ledger, staff adjustments and shop purchases.
def init_v3():
    with connect() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS shop_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            description TEXT,
            price INTEGER NOT NULL CHECK(price > 0),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(guild_id, name)
        );
        CREATE TABLE IF NOT EXISTS purchase_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            price INTEGER NOT NULL CHECK(price > 0),
            status TEXT NOT NULL DEFAULT 'pending',
            channel_id INTEGER,
            message_id INTEGER,
            reviewer_id INTEGER,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            decided_at TEXT,
            FOREIGN KEY(guild_id, user_id) REFERENCES players(guild_id, user_id),
            FOREIGN KEY(item_id) REFERENCES shop_items(id)
        );
        CREATE TABLE IF NOT EXISTS point_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL CHECK(amount != 0),
            kind TEXT NOT NULL,
            description TEXT NOT NULL,
            reviewer_id INTEGER,
            purchase_id INTEGER UNIQUE,
            week_key TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(guild_id, user_id) REFERENCES players(guild_id, user_id),
            FOREIGN KEY(purchase_id) REFERENCES purchase_requests(id)
        );
        CREATE INDEX IF NOT EXISTS idx_point_transactions_player
            ON point_transactions(guild_id, user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_point_transactions_week
            ON point_transactions(guild_id, user_id, week_key);
        CREATE INDEX IF NOT EXISTS idx_purchase_requests_pending
            ON purchase_requests(status, message_id);
        ''')


def adjust_points(guild_id, user_id, reviewer_id, amount, reason):
    if amount == 0 or abs(amount) > 100000:
        return 'invalid', 0, 0, 0
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM players WHERE guild_id=? AND user_id=?',
                        (guild_id, user_id)).fetchone() is None:
            conn.rollback()
            return 'missing', 0, 0, 0
        balance, weekly, _ = _point_summary(conn, guild_id, user_id)
        actual = amount
        transaction_week = None
        if amount > 0:
            actual = min(amount, max(0, LIMIT-weekly))
            if actual == 0:
                conn.rollback()
                return 'limit', 0, balance, weekly
            transaction_week = week_key()
        elif balance < -amount:
            conn.rollback()
            return 'insufficient', 0, balance, weekly
        conn.execute('''INSERT INTO point_transactions(
            guild_id,user_id,amount,kind,description,reviewer_id,week_key)
            VALUES(?,?,?,?,?,?,?)''', (
                guild_id, user_id, actual,
                'manual_add' if actual > 0 else 'manual_subtract',
                reason, reviewer_id, transaction_week,
            ))
        conn.commit()
        return 'ok', actual, balance+actual, weekly+(actual if actual > 0 else 0)
    finally:
        conn.close()


def grant_points_unlimited(guild_id, user_id, reviewer_id, amount, reason):
    """Owner-only grant that increases balance without affecting the weekly cap."""
    if amount <= 0 or amount > 1000000:
        return 'invalid', 0, 0
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM players WHERE guild_id=? AND user_id=?',
                        (guild_id,user_id)).fetchone() is None:
            conn.rollback()
            return 'missing', 0, 0
        balance, weekly, _ = _point_summary(conn,guild_id,user_id)
        conn.execute('''INSERT INTO point_transactions(
            guild_id,user_id,amount,kind,description,reviewer_id,week_key)
            VALUES(?,?,?,?,?,?,NULL)''',(
                guild_id,user_id,amount,'owner_grant',reason,reviewer_id,
            ))
        conn.commit()
        return 'ok', balance+amount, weekly
    finally:
        conn.close()


def bulk_grant_target_count(guild_id):
    """Number of registered players who will receive an owner bulk grant."""
    with connect() as conn:
        return conn.execute(
            'SELECT COUNT(*) FROM players WHERE guild_id=?',(guild_id,)).fetchone()[0]


def grant_points_all_unlimited(guild_id, reviewer_id, amount, reason):
    """Grant every registered player points without affecting the weekly cap."""
    if amount <= 0 or amount > 1000000:
        return 'invalid', []
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        players=conn.execute(
            'SELECT user_id,personal_channel_id FROM players WHERE guild_id=? ORDER BY user_id',
            (guild_id,)).fetchall()
        if not players:
            conn.rollback()
            return 'empty', []
        conn.executemany('''INSERT INTO point_transactions(
            guild_id,user_id,amount,kind,description,reviewer_id,week_key)
            VALUES(?,?,?,?,?,?,NULL)''',(
                (guild_id,row['user_id'],amount,'owner_grant_all',reason,reviewer_id)
                for row in players
            ))
        conn.commit()
        return 'ok', [(row['user_id'],row['personal_channel_id']) for row in players]
    finally:
        conn.close()


def set_shop_item(guild_id, name, price, description):
    with connect() as conn:
        conn.execute('''INSERT INTO shop_items(guild_id,name,description,price,active)
            VALUES(?,?,?,?,1)
            ON CONFLICT(guild_id,name) DO UPDATE SET
                description=excluded.description,price=excluded.price,active=1,
                updated_at=CURRENT_TIMESTAMP''',
            (guild_id, name, description, price))
        return conn.execute('SELECT * FROM shop_items WHERE guild_id=? AND name=?',
                            (guild_id, name)).fetchone()


def deactivate_shop_item(guild_id, name):
    with connect() as conn:
        cursor = conn.execute('''UPDATE shop_items SET active=0,updated_at=CURRENT_TIMESTAMP
            WHERE guild_id=? AND name=? AND active=1''',(guild_id, name))
        return cursor.rowcount > 0


def shop_items(guild_id):
    with connect() as conn:
        return conn.execute('''SELECT * FROM shop_items WHERE guild_id=? AND active=1
            ORDER BY price,name LIMIT 25''',(guild_id,)).fetchall()


def shop_item(guild_id, item_id):
    with connect() as conn:
        return conn.execute('''SELECT * FROM shop_items
            WHERE guild_id=? AND id=? AND active=1''',(guild_id,item_id)).fetchone()


def create_purchase_request(guild_id, user_id, item_id):
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        item = conn.execute('''SELECT * FROM shop_items
            WHERE guild_id=? AND id=? AND active=1''',(guild_id,item_id)).fetchone()
        if item is None:
            conn.rollback()
            return 'missing', None, 0
        if conn.execute('SELECT 1 FROM players WHERE guild_id=? AND user_id=?',
                        (guild_id,user_id)).fetchone() is None:
            conn.rollback()
            return 'unregistered', None, 0
        duplicate = conn.execute('''SELECT id FROM purchase_requests
            WHERE guild_id=? AND user_id=? AND item_id=? AND status='pending' ''',
            (guild_id,user_id,item_id)).fetchone()
        balance, _, _ = _point_summary(conn,guild_id,user_id)
        if duplicate:
            conn.rollback()
            return 'duplicate', duplicate['id'], balance
        if balance < item['price']:
            conn.rollback()
            return 'insufficient', None, balance
        cursor = conn.execute('''INSERT INTO purchase_requests(
            guild_id,user_id,item_id,item_name,price) VALUES(?,?,?,?,?)''',
            (guild_id,user_id,item['id'],item['name'],item['price']))
        conn.commit()
        return 'ok', cursor.lastrowid, balance
    finally:
        conn.close()


def attach_purchase_request(request_id, channel_id, message_id):
    with connect() as conn:
        conn.execute('''UPDATE purchase_requests SET channel_id=?,message_id=?
            WHERE id=?''',(channel_id,message_id,request_id))


def purchase_request(request_id):
    with connect() as conn:
        return conn.execute('SELECT * FROM purchase_requests WHERE id=?',
                            (request_id,)).fetchone()


def pending_purchase_requests():
    with connect() as conn:
        return conn.execute('''SELECT * FROM purchase_requests
            WHERE status='pending' AND message_id IS NOT NULL''').fetchall()


def approve_purchase(request_id, reviewer_id):
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        request = conn.execute('SELECT * FROM purchase_requests WHERE id=?',
                               (request_id,)).fetchone()
        if not request or request['status'] != 'pending':
            conn.rollback()
            return 'closed', 0
        balance, _, _ = _point_summary(conn,request['guild_id'],request['user_id'])
        if balance < request['price']:
            conn.rollback()
            return 'insufficient', balance
        conn.execute('''INSERT INTO point_transactions(
            guild_id,user_id,amount,kind,description,reviewer_id,purchase_id)
            VALUES(?,?,?,?,?,?,?)''',(
                request['guild_id'],request['user_id'],-request['price'],'purchase',
                f'Покупка: {request["item_name"]}',reviewer_id,request_id,
            ))
        conn.execute('''UPDATE purchase_requests SET status='approved',reviewer_id=?,
            decided_at=CURRENT_TIMESTAMP WHERE id=?''',(reviewer_id,request_id))
        conn.commit()
        return 'ok', balance-request['price']
    finally:
        conn.close()


def reject_purchase(request_id, reviewer_id, reason):
    with connect() as conn:
        cursor = conn.execute('''UPDATE purchase_requests SET status='rejected',
            reviewer_id=?,reason=?,decided_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='pending' ''',(reviewer_id,reason,request_id))
        return cursor.rowcount > 0


def weekly_leaderboard(guild_id, limit=10):
    with connect() as conn:
        return conn.execute('''
            SELECT p.user_id,p.nickname,
                COALESCE((SELECT SUM(a.points) FROM awards a
                    WHERE a.guild_id=p.guild_id AND a.user_id=p.user_id
                      AND a.week_key=?),0)
                + COALESCE((SELECT SUM(t.amount) FROM point_transactions t
                    WHERE t.guild_id=p.guild_id AND t.user_id=p.user_id
                      AND t.week_key=? AND t.amount>0),0) AS score
            FROM players p
            WHERE p.guild_id=?
            ORDER BY score DESC,p.nickname ASC
            LIMIT ?''',(week_key(),week_key(),guild_id,limit)).fetchall()

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
