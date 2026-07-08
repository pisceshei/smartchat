package main

import (
	"crypto/rand"
	"encoding/hex"
	"io"
	"net/http"
	"sync"
	"time"
)

// mediaCache holds downloaded inbound WhatsApp media in memory keyed by an
// unguessable token, and serves it at GET /media/{token} so the SmartChat
// ingress pipeline can fetch it via BaseAdapter.fetch_media (a plain,
// header-less httpx GET). Entries expire after cfg.MediaTTL; a janitor evicts
// them. This is intentionally simple — personal-account CS volume is low and
// throttled by the Python sender.
type mediaCache struct {
	mu      sync.Mutex
	entries map[string]*mediaEntry
	ttl     time.Duration
}

type mediaEntry struct {
	data     []byte
	mime     string
	filename string
	expires  time.Time
}

func newMediaCache(ttl time.Duration) *mediaCache {
	c := &mediaCache{entries: make(map[string]*mediaEntry), ttl: ttl}
	go c.janitor()
	return c
}

func newToken() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// put stores bytes and returns the token to embed in the media-ref URL.
func (c *mediaCache) put(data []byte, mime, filename string) string {
	tok := newToken()
	c.mu.Lock()
	c.entries[tok] = &mediaEntry{
		data:     data,
		mime:     mime,
		filename: filename,
		expires:  time.Now().Add(c.ttl),
	}
	c.mu.Unlock()
	return tok
}

func (c *mediaCache) get(tok string) (*mediaEntry, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.entries[tok]
	if !ok || time.Now().After(e.expires) {
		return nil, false
	}
	return e, true
}

func (c *mediaCache) janitor() {
	tick := time.NewTicker(time.Minute)
	defer tick.Stop()
	for range tick.C {
		now := time.Now()
		c.mu.Lock()
		for k, e := range c.entries {
			if now.After(e.expires) {
				delete(c.entries, k)
			}
		}
		c.mu.Unlock()
	}
}

// serveHTTP handles GET /media/{token}. No X-Bridge-Auth: the path token is the
// capability (matches BaseAdapter.fetch_media which sends no auth header).
func (c *mediaCache) serveHTTP(w http.ResponseWriter, r *http.Request) {
	tok := r.PathValue("token")
	e, ok := c.get(tok)
	if !ok {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	if e.mime != "" {
		w.Header().Set("Content-Type", e.mime)
	} else {
		w.Header().Set("Content-Type", "application/octet-stream")
	}
	if e.filename != "" {
		w.Header().Set("Content-Disposition", "inline; filename=\""+e.filename+"\"")
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(e.data)
}

// fetchURL downloads an outbound-media source URL (used when a payload media
// block carries an explicit url, or via the SMARTCHAT_FILES_BASE fallback).
func fetchURL(client *http.Client, url string, maxBytes int64) ([]byte, string, error) {
	resp, err := client.Get(url)
	if err != nil {
		return nil, "", err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= 300 {
		return nil, "", &httpStatusError{code: resp.StatusCode, url: url}
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxBytes))
	if err != nil {
		return nil, "", err
	}
	return data, resp.Header.Get("Content-Type"), nil
}

type httpStatusError struct {
	code int
	url  string
}

func (e *httpStatusError) Error() string {
	return "GET " + e.url + " returned " + http.StatusText(e.code)
}
