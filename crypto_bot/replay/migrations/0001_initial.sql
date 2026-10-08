
CREATE TABLE IF NOT EXISTS replay_schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replay_runs(id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
 started_at TEXT, finished_at TEXT, config TEXT NOT NULL, metadata TEXT NOT NULL,
 progress TEXT NOT NULL DEFAULT '{}', error TEXT, control TEXT NOT NULL DEFAULT 'RUN',
 worker TEXT, heartbeat REAL, checkpoint TEXT);
CREATE TABLE IF NOT EXISTS replay_variants(id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES replay_runs(id),
 number INTEGER NOT NULL, config TEXT NOT NULL, summary TEXT, UNIQUE(run_id,number));
CREATE TABLE IF NOT EXISTS replay_trades(id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, variant INTEGER NOT NULL,
 position_id INTEGER NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(run_id,variant,position_id));
CREATE INDEX IF NOT EXISTS replay_trade_run ON replay_trades(run_id,variant,status);
CREATE TABLE IF NOT EXISTS replay_orders(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,variant INTEGER NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replay_fills(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,variant INTEGER NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replay_equity_curve(run_id TEXT NOT NULL,variant INTEGER NOT NULL,time INTEGER NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(run_id,variant,time));
CREATE TABLE IF NOT EXISTS replay_audit_events(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,variant INTEGER,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS replay_audit_run ON replay_audit_events(run_id,id);
CREATE TABLE IF NOT EXISTS replay_presets(id INTEGER PRIMARY KEY,name TEXT NOT NULL,config TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replay_worker_status(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT,heartbeat REAL,status TEXT,version TEXT,git_commit TEXT,pid INTEGER);
