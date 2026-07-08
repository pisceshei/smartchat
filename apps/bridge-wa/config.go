package main

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Config is the process-wide configuration, all from env vars. Sensible
// defaults let the service boot inside the compose network with just
// BRIDGE_API_TOKEN set.
type Config struct {
	// Shared bearer token required on the management HTTP endpoints via the
	// X-Bridge-Auth header. Empty => auth disabled (dev only; logged loudly).
	APIToken string
	// TCP listen address for the HTTP server.
	Listen string
	// Directory that holds the whatsmeow SQLite store (sessions survive
	// restarts because this is a mounted volume in compose).
	StoreDir string
	// Full path to the SQLite file. Defaults to {StoreDir}/bridge.db.
	DBPath string
	// Absolute base URL at which THIS bridge is reachable from the SmartChat
	// API container, used to build inbound-media fetch URLs
	// ({PublicURL}/media/{token}). On the compose network the service name
	// resolves, so the default is http://bridge-wa:8100.
	PublicURL string
	// Base URL of the SmartChat file store, used as a fallback to resolve an
	// outbound MediaBlock.file_id to bytes when the payload carries no explicit
	// url ({FilesBase}/api/v1/files/{file_id}). Empty => outbound media without
	// a url degrades to its caption text.
	FilesBase string
	// How long a cached inbound-media blob is served before eviction.
	MediaTTL time.Duration
	// Inbound media larger than this is not downloaded; the block degrades to a
	// text placeholder so we never buffer huge blobs in memory.
	MediaMaxBytes int64
	// Heartbeat / status-reconcile interval per device.
	Heartbeat time.Duration
	// whatsmeow log level: DEBUG/INFO/WARN/ERROR.
	LogLevel string
}

func loadConfig() Config {
	storeDir := envStr("BRIDGE_STORE_DIR", "/data")
	dbPath := envStr("BRIDGE_DB_PATH", "")
	if dbPath == "" {
		dbPath = filepath.Join(storeDir, "bridge.db")
	}
	return Config{
		APIToken:      envStr("BRIDGE_API_TOKEN", ""),
		Listen:        envStr("BRIDGE_LISTEN", ":8100"),
		StoreDir:      storeDir,
		DBPath:        dbPath,
		PublicURL:     strings.TrimRight(envStr("BRIDGE_PUBLIC_URL", "http://bridge-wa:8100"), "/"),
		FilesBase:     strings.TrimRight(envStr("SMARTCHAT_FILES_BASE", ""), "/"),
		MediaTTL:      envDuration("BRIDGE_MEDIA_TTL", 15*time.Minute),
		MediaMaxBytes: envInt64("BRIDGE_MEDIA_MAX_BYTES", 200*1024*1024),
		Heartbeat:     envDuration("BRIDGE_HEARTBEAT", 20*time.Second),
		LogLevel:      envStr("BRIDGE_LOG_LEVEL", "INFO"),
	}
}

func envStr(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func envInt64(key string, def int64) int64 {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			return n
		}
	}
	return def
}

func envDuration(key string, def time.Duration) time.Duration {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
