package main

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"os"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"

	// Pure-Go SQLite (no CGO). It registers the "sqlite" database/sql driver;
	// we pass whatsmeow the "sqlite3" DIALECT (SQL syntax selector) while the
	// underlying driver is modernc's "sqlite" — decoupled via NewWithDB.
	_ "modernc.org/sqlite"
)

// Manager owns the whatsmeow store container, the persistent device registry
// (bridge_devices table in the same SQLite file), and the live *Device map.
type Manager struct {
	cfg       Config
	db        *sql.DB
	container *sqlstore.Container
	log       waLog.Logger
	waLog     waLog.Logger
	callback  *callbackClient
	media     *mediaCache
	outHTTP   *http.Client
	ctx       context.Context
	cancel    context.CancelFunc

	mu      sync.Mutex
	devices map[string]*Device
}

func newManager(cfg Config) (*Manager, error) {
	if err := os.MkdirAll(cfg.StoreDir, 0o755); err != nil {
		return nil, fmt.Errorf("create store dir: %w", err)
	}
	dsn := fmt.Sprintf(
		"file:%s?_pragma=busy_timeout(10000)&_pragma=foreign_keys(1)&_pragma=journal_mode(WAL)",
		cfg.DBPath,
	)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	// Single connection => fully serialized access, no "database is locked".
	db.SetMaxOpenConns(1)

	logLevel := cfg.LogLevel
	dbLog := waLog.Stdout("Store", logLevel, true)
	container := sqlstore.NewWithDB(db, "sqlite3", dbLog)

	ctx, cancel := context.WithCancel(context.Background())
	if err := container.Upgrade(ctx); err != nil {
		cancel()
		return nil, fmt.Errorf("store upgrade: %w", err)
	}

	m := &Manager{
		cfg:       cfg,
		db:        db,
		container: container,
		log:       waLog.Stdout("Bridge", logLevel, true),
		waLog:     waLog.Stdout("WA", logLevel, true),
		callback:  newCallbackClient(),
		media:     newMediaCache(cfg.MediaTTL),
		outHTTP:   &http.Client{Timeout: 30 * time.Second},
		ctx:       ctx,
		cancel:    cancel,
		devices:   make(map[string]*Device),
	}
	if err := m.ensureSchema(); err != nil {
		cancel()
		return nil, err
	}
	return m, nil
}

func (m *Manager) ensureSchema() error {
	_, err := m.db.ExecContext(m.ctx, `
CREATE TABLE IF NOT EXISTS bridge_devices (
  device_id       TEXT PRIMARY KEY,
  jid             TEXT NOT NULL DEFAULT '',
  callback_url    TEXT NOT NULL DEFAULT '',
  callback_secret TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL DEFAULT ''
)`)
	if err != nil {
		return fmt.Errorf("ensure schema: %w", err)
	}
	return nil
}

// restore rebuilds live devices from the registry on boot.
func (m *Manager) restore() {
	type row struct{ id, jid, cbURL, cbSecret, status string }
	rows, err := m.db.QueryContext(m.ctx,
		`SELECT device_id, jid, callback_url, callback_secret, status FROM bridge_devices`)
	if err != nil {
		m.log.Errorf("restore query: %v", err)
		return
	}
	var records []row
	for rows.Next() {
		var r row
		if err := rows.Scan(&r.id, &r.jid, &r.cbURL, &r.cbSecret, &r.status); err != nil {
			m.log.Errorf("restore scan: %v", err)
			continue
		}
		records = append(records, r)
	}
	_ = rows.Close()

	for _, r := range records {
		// terminal states stay inert: report the stored status, never re-pair.
		if r.status == statusLoggedOut || r.status == statusBanned {
			d := newDevice(m, r.id, m.container.NewDevice(), r.cbURL, r.cbSecret)
			d.setStatus(r.status)
			m.mu.Lock()
			m.devices[r.id] = d
			m.mu.Unlock()
			continue
		}
		var st *store.Device
		if r.jid != "" {
			if jid, perr := types.ParseJID(r.jid); perr == nil {
				if existing, gerr := m.container.GetDevice(m.ctx, jid); gerr == nil && existing != nil && existing.ID != nil {
					st = existing
				}
			}
		}
		if st == nil {
			// interrupted pairing (no persisted session yet) — re-arm QR.
			st = m.container.NewDevice()
		}
		d := newDevice(m, r.id, st, r.cbURL, r.cbSecret)
		m.mu.Lock()
		m.devices[r.id] = d
		m.mu.Unlock()
		if err := d.start(); err != nil {
			m.log.Errorf("device %s restore start failed: %v", r.id, err)
		} else {
			m.log.Infof("device %s restored (paired=%v)", r.id, r.jid != "")
		}
	}
}

// Create provisions (or reuses) a device. Returns the current status.
func (m *Manager) Create(id, cbURL, cbSecret string) (string, error) {
	m.mu.Lock()
	if d, ok := m.devices[id]; ok {
		st := d.getStatus()
		if st != statusLoggedOut && st != statusBanned {
			d.updateCallback(cbURL, cbSecret)
			m.mu.Unlock()
			m.saveCallback(id, cbURL, cbSecret)
			return st, nil
		}
		// terminal — explicit re-provision: tear down and re-pair fresh.
		delete(m.devices, id)
		m.mu.Unlock()
		d.destroy(true)
		m.mu.Lock()
	}

	var st *store.Device
	if jid := m.lookupJID(id); jid != "" {
		if parsed, perr := types.ParseJID(jid); perr == nil {
			if existing, gerr := m.container.GetDevice(m.ctx, parsed); gerr == nil && existing != nil && existing.ID != nil {
				st = existing
			}
		}
	}
	if st == nil {
		st = m.container.NewDevice()
	}
	jidStr := ""
	if st.ID != nil {
		jidStr = st.ID.String()
	}
	d := newDevice(m, id, st, cbURL, cbSecret)
	m.devices[id] = d
	m.mu.Unlock()

	m.saveDevice(id, jidStr, cbURL, cbSecret, statusConnecting)
	if err := d.start(); err != nil {
		m.log.Errorf("device %s start failed: %v", id, err)
		return d.getStatus(), err
	}
	return d.getStatus(), nil
}

func (m *Manager) Get(id string) (*Device, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	d, ok := m.devices[id]
	return d, ok
}

func (m *Manager) Remove(id string) bool {
	m.mu.Lock()
	d, ok := m.devices[id]
	if ok {
		delete(m.devices, id)
	}
	m.mu.Unlock()
	if ok {
		d.destroy(true)
	}
	m.deleteRow(id)
	return ok
}

func (m *Manager) shutdown() {
	m.mu.Lock()
	devices := make([]*Device, 0, len(m.devices))
	for _, d := range m.devices {
		devices = append(devices, d)
	}
	m.mu.Unlock()
	for _, d := range devices {
		d.destroy(false) // keep sessions on graceful shutdown
	}
	m.cancel()
	_ = m.db.Close()
}

// ---- registry persistence -------------------------------------------------

func (m *Manager) saveDevice(id, jid, cbURL, cbSecret, status string) {
	_, err := m.db.ExecContext(m.ctx, `
INSERT INTO bridge_devices(device_id, jid, callback_url, callback_secret, status, created_at)
VALUES(?, ?, ?, ?, ?, ?)
ON CONFLICT(device_id) DO UPDATE SET
  jid=excluded.jid,
  callback_url=excluded.callback_url,
  callback_secret=excluded.callback_secret,
  status=excluded.status`,
		id, jid, cbURL, cbSecret, status, time.Now().UTC().Format(time.RFC3339))
	if err != nil {
		m.log.Errorf("saveDevice %s: %v", id, err)
	}
}

func (m *Manager) saveJID(id, jid string) {
	if _, err := m.db.ExecContext(m.ctx,
		`UPDATE bridge_devices SET jid=? WHERE device_id=?`, jid, id); err != nil {
		m.log.Errorf("saveJID %s: %v", id, err)
	}
}

func (m *Manager) saveStatus(id, status, jid string) {
	if _, err := m.db.ExecContext(m.ctx,
		`UPDATE bridge_devices SET status=?, jid=? WHERE device_id=?`, status, jid, id); err != nil {
		m.log.Errorf("saveStatus %s: %v", id, err)
	}
}

func (m *Manager) saveCallback(id, cbURL, cbSecret string) {
	if _, err := m.db.ExecContext(m.ctx,
		`UPDATE bridge_devices SET callback_url=?, callback_secret=? WHERE device_id=?`,
		cbURL, cbSecret, id); err != nil {
		m.log.Errorf("saveCallback %s: %v", id, err)
	}
}

func (m *Manager) lookupJID(id string) string {
	var jid string
	err := m.db.QueryRowContext(m.ctx,
		`SELECT jid FROM bridge_devices WHERE device_id=?`, id).Scan(&jid)
	if err != nil {
		return ""
	}
	return jid
}

func (m *Manager) deleteRow(id string) {
	if _, err := m.db.ExecContext(m.ctx,
		`DELETE FROM bridge_devices WHERE device_id=?`, id); err != nil {
		m.log.Errorf("deleteRow %s: %v", id, err)
	}
}
