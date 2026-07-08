// Command bridge-wa is a multi-session WhatsApp (personal-account) bridge built
// on go.mau.fi/whatsmeow. It manages many device sessions in ONE process and
// exposes the SmartChat "device bridge" HTTP contract on :8100. See README.md
// for the wire contract and the exact InboundEvent JSON it emits.
package main

import (
	"context"
	"crypto/hmac"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

type server struct {
	cfg Config
	mgr *Manager
}

func main() {
	cfg := loadConfig()
	if cfg.APIToken == "" {
		log.Println("WARNING: BRIDGE_API_TOKEN is empty — management endpoints are UNAUTHENTICATED (dev only)")
	}
	mgr, err := newManager(cfg)
	if err != nil {
		log.Fatalf("bridge-wa: init failed: %v", err)
	}
	mgr.restore()

	s := &server{cfg: cfg, mgr: mgr}
	srv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           s.routes(),
		ReadHeaderTimeout: 15 * time.Second,
	}

	go func() {
		log.Printf("bridge-wa: listening on %s (store=%s)", cfg.Listen, cfg.DBPath)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("bridge-wa: http server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	log.Println("bridge-wa: shutting down…")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	mgr.shutdown()
}

func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	// management (X-Bridge-Auth gated)
	mux.HandleFunc("POST /devices", s.handleCreate)
	mux.HandleFunc("GET /devices/{id}/qr", s.handleQR)
	mux.HandleFunc("GET /devices/{id}/health", s.handleHealth)
	mux.HandleFunc("POST /devices/{id}/send", s.handleSend)
	mux.HandleFunc("POST /devices/{id}/logout", s.handleLogout)
	mux.HandleFunc("DELETE /devices/{id}", s.handleDelete)
	// media fetch for inbound (token-gated, no auth header)
	mux.HandleFunc("GET /media/{token}", s.mgr.media.serveHTTP)
	// service liveness for the compose healthcheck (no auth)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})
	return mux
}

// ---- auth -----------------------------------------------------------------

func (s *server) authManage(r *http.Request) bool {
	if s.cfg.APIToken == "" {
		return true
	}
	got := r.Header.Get("X-Bridge-Auth")
	return subtle.ConstantTimeCompare([]byte(got), []byte(s.cfg.APIToken)) == 1
}

// authSend accepts either the shared X-Bridge-Auth token OR a valid
// X-Bridge-Signature over the body using the device callback_secret — the
// latter is what the existing Python BridgeAdapter.send signs with.
func (s *server) authSend(r *http.Request, d *Device, body []byte) bool {
	if s.authManage(r) {
		return true
	}
	sig := r.Header.Get("X-Bridge-Signature")
	if sig == "" {
		return false
	}
	expected := signBridge(d.cbSecret(), body)
	return hmac.Equal([]byte(expected), []byte(sig))
}

// ---- handlers -------------------------------------------------------------

type createBody struct {
	DeviceID       string `json:"device_id"`
	CallbackURL    string `json:"callback_url"`
	CallbackSecret string `json:"callback_secret"`
}

func (s *server) handleCreate(w http.ResponseWriter, r *http.Request) {
	if !s.authManage(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}
	var body createBody
	if err := readJSON(r, &body); err != nil || body.DeviceID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "device_id required"})
		return
	}
	status, err := s.mgr.Create(body.DeviceID, body.CallbackURL, body.CallbackSecret)
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"device_id": body.DeviceID, "status": status,
			"warning": err.Error()})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"device_id": body.DeviceID, "status": status})
}

func (s *server) handleQR(w http.ResponseWriter, r *http.Request) {
	if !s.authManage(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}
	d, ok := s.mgr.Get(r.PathValue("id"))
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]any{"error": "device not found"})
		return
	}
	status, qr, _, _, _ := d.snapshot()
	var qrVal any
	if qr != "" {
		qrVal = qr
	}
	writeJSON(w, http.StatusOK, map[string]any{"qr": qrVal, "status": status})
}

func (s *server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if !s.authManage(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}
	d, ok := s.mgr.Get(r.PathValue("id"))
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]any{"error": "device not found"})
		return
	}
	status, _, jid, phone, pushname := d.snapshot()
	out := map[string]any{"status": status}
	if jid != "" {
		out["jid"] = jid
	}
	if phone != "" {
		out["phone"] = phone
	}
	if pushname != "" {
		out["pushname"] = pushname
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *server) handleSend(w http.ResponseWriter, r *http.Request) {
	d, ok := s.mgr.Get(r.PathValue("id"))
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]any{"ok": false, "error": "device not found"})
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 32<<20))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "read body"})
		return
	}
	if !s.authSend(r, d, body) {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}
	var req sendRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid json"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	out := d.send(ctx, req)
	if out.ok {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "message_id": out.messageID})
		return
	}
	resp := map[string]any{"ok": false}
	if out.status != "" {
		resp["status"] = out.status
	}
	if out.errMsg != "" {
		resp["error"] = out.errMsg
	}
	code := out.httpCode
	if code == 0 {
		code = http.StatusBadGateway
	}
	writeJSON(w, code, resp)
}

func (s *server) handleLogout(w http.ResponseWriter, r *http.Request) {
	if !s.authManage(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}
	d, ok := s.mgr.Get(r.PathValue("id"))
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]any{"ok": false, "error": "device not found"})
		return
	}
	if err := d.logout(); err != nil {
		s.mgr.log.Warnf("device %s logout: %v", d.id, err)
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) handleDelete(w http.ResponseWriter, r *http.Request) {
	if !s.authManage(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}
	s.mgr.Remove(r.PathValue("id"))
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// ---- json helpers ---------------------------------------------------------

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func readJSON(r *http.Request, v any) error {
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		return err
	}
	return json.Unmarshal(body, v)
}
