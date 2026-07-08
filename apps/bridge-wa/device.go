package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

// device status values — also the account_status.status strings emitted to the
// SmartChat API (ingress maps online->active, offline->disconnected; the rest
// pass through, truncated to 16 chars).
const (
	statusAwaitingQR = "awaiting_qr"
	statusConnecting = "connecting"
	statusOnline     = "online"
	statusOffline    = "offline"
	statusLoggedOut  = "logged_out"
	statusBanned     = "banned"
)

// Device wraps one whatsmeow.Client for one SmartChat channel account. A single
// serial worker goroutine drains inbound work (media download + callback POST)
// so the whatsmeow event loop is never blocked and event ordering is preserved.
type Device struct {
	id    string
	mgr   *Manager
	store *store.Device

	mu             sync.Mutex
	client         *whatsmeow.Client
	callbackURL    string
	callbackSecret string
	status         string
	qr             string
	jid            string
	phone          string
	pushname       string
	lastEmitted    string
	closed         bool

	jobs chan func()
	stop chan struct{}
}

func newDevice(mgr *Manager, id string, st *store.Device, callbackURL, callbackSecret string) *Device {
	return &Device{
		id:             id,
		mgr:            mgr,
		store:          st,
		callbackURL:    callbackURL,
		callbackSecret: callbackSecret,
		status:         statusConnecting,
		jobs:           make(chan func(), 512),
		stop:           make(chan struct{}),
	}
}

// start builds the whatsmeow client and either reconnects a restored session or
// begins QR pairing for a fresh device.
func (d *Device) start() error {
	client := whatsmeow.NewClient(d.store, d.mgr.waLog.Sub(shortID(d.id)))
	client.EnableAutoReconnect = true
	// Recover undecryptable inbound: the first message to a freshly-paired
	// device (and any later Signal-session gap) arrives before this device has
	// the sender's session, so whatsmeow logs "Unavailable message" and no
	// events.Message fires. This asks the user's primary phone to resend those
	// messages, which then decrypt and reach the inbox normally.
	client.AutomaticMessageRerequestFromPhone = true
	client.AddEventHandler(d.onEvent)
	d.mu.Lock()
	d.client = client
	d.mu.Unlock()

	go d.worker()
	go d.heartbeat()

	if d.store.ID != nil {
		// restored, already-paired session — just reconnect (auto-login).
		d.refreshIdentity()
		d.setStatus(statusConnecting)
		return client.Connect()
	}

	// fresh device — arm the QR channel BEFORE connecting.
	ch, err := client.GetQRChannel(d.mgr.ctx)
	if err != nil {
		// ErrQRStoreContainsID => a session raced in; fall through to Connect.
		d.mgr.log.Warnf("device %s GetQRChannel: %v", d.id, err)
	} else {
		go d.qrLoop(ch)
	}
	d.setStatus(statusAwaitingQR)
	return client.Connect()
}

func (d *Device) qrLoop(ch <-chan whatsmeow.QRChannelItem) {
	for {
		select {
		case <-d.stop:
			return
		case item, ok := <-ch:
			if !ok {
				return
			}
			switch item.Event {
			case "code":
				d.mu.Lock()
				d.qr = item.Code
				d.status = statusAwaitingQR
				d.mu.Unlock()
				d.emitStatus(false)
			case "success":
				d.mu.Lock()
				d.qr = ""
				d.status = statusConnecting
				d.mu.Unlock()
				// PairSuccess + Connected finalize identity/status.
			default:
				// timeout / err-* : pairing window closed. Operator must
				// re-POST /devices to restart pairing (never auto re-pair).
				d.mgr.log.Warnf("device %s QR event %q: %v", d.id, item.Event, item.Error)
				d.mu.Lock()
				d.qr = ""
				if d.status == statusAwaitingQR {
					d.status = statusOffline
				}
				d.mu.Unlock()
				d.emitStatus(false)
				return
			}
		}
	}
}

func (d *Device) worker() {
	for {
		select {
		case <-d.stop:
			return
		case job := <-d.jobs:
			job()
		}
	}
}

func (d *Device) enqueue(fn func()) {
	select {
	case d.jobs <- fn:
	default:
		// backlog full (flood) — run detached rather than block the event loop.
		go fn()
	}
}

func (d *Device) heartbeat() {
	tick := time.NewTicker(d.mgr.cfg.Heartbeat)
	defer tick.Stop()
	for {
		select {
		case <-d.stop:
			return
		case <-tick.C:
			d.emitStatus(false)
		}
	}
}

// onEvent is whatsmeow's synchronous handler — keep it fast; hand heavy work to
// the serial worker.
func (d *Device) onEvent(evt any) {
	switch e := evt.(type) {
	case *events.Message:
		d.enqueue(func() { d.handleMessage(e) })
	case *events.UndecryptableMessage:
		// whatsmeow couldn't decrypt this (cold Signal session, usually the
		// first message after pairing). With AutomaticMessageRerequestFromPhone
		// the phone resends it and a normal events.Message follows. Log for
		// visibility so a persistent decrypt failure is diagnosable.
		d.mgr.log.Warnf("device %s undecryptable from %s (unavailable=%v type=%v) — rerequesting from phone",
			d.id, e.Info.Sender.String(), e.IsUnavailable, e.UnavailableType)
	case *events.Receipt:
		d.enqueue(func() { d.handleReceipt(e) })
	case *events.Connected:
		d.refreshIdentity()
		d.setStatus(statusOnline)
		d.persistJID()
		d.emitStatus(true)
	case *events.PairSuccess:
		d.refreshIdentity()
		d.setStatus(statusConnecting)
		d.persistJID()
	case *events.Disconnected:
		// transient — whatsmeow auto-reconnects. The heartbeat emits offline
		// only if it stays down until the next tick (avoids flapping).
	case *events.LoggedOut:
		d.enqueue(d.handleLoggedOut)
	case *events.ConnectFailure:
		d.handleConnectFailure(e)
	case *events.TemporaryBan:
		d.mgr.log.Warnf("device %s temporary ban: code=%v expires=%v", d.id, e.Code, e.Expire)
		d.markBanned()
	}
}

func (d *Device) handleMessage(e *events.Message) {
	info := e.Info
	if info.IsFromMe || info.IsGroup {
		return
	}
	if info.Chat.Server == types.BroadcastServer || info.Chat.Server == types.NewsletterServer {
		return
	}
	m := e.Message
	if m == nil {
		return
	}

	var blocks []any
	var refs []mediaRef

	// media blocks first (a WhatsApp media message carries its text as the media
	// caption, never as Conversation, so text + media never both fire here).
	switch {
	case m.GetImageMessage() != nil:
		img := m.GetImageMessage()
		d.appendMedia(&blocks, &refs, mediaSpec{
			dl: img, mediaType: "image", mime: img.GetMimetype(), caption: img.GetCaption(),
			size: int64(img.GetFileLength()), width: int(img.GetWidth()), height: int(img.GetHeight()),
		})
	case m.GetVideoMessage() != nil:
		v := m.GetVideoMessage()
		d.appendMedia(&blocks, &refs, mediaSpec{
			dl: v, mediaType: "video", mime: v.GetMimetype(), caption: v.GetCaption(),
			size: int64(v.GetFileLength()), durationMs: int64(v.GetSeconds()) * 1000,
			width: int(v.GetWidth()), height: int(v.GetHeight()),
		})
	case m.GetAudioMessage() != nil:
		a := m.GetAudioMessage()
		mt := "audio"
		if a.GetPTT() {
			mt = "voice"
		}
		d.appendMedia(&blocks, &refs, mediaSpec{
			dl: a, mediaType: mt, mime: a.GetMimetype(),
			size: int64(a.GetFileLength()), durationMs: int64(a.GetSeconds()) * 1000,
		})
	case m.GetDocumentMessage() != nil:
		doc := m.GetDocumentMessage()
		d.appendMedia(&blocks, &refs, mediaSpec{
			dl: doc, mediaType: "file", mime: doc.GetMimetype(), caption: doc.GetCaption(),
			filename: doc.GetFileName(), size: int64(doc.GetFileLength()),
		})
	case m.GetStickerMessage() != nil:
		s := m.GetStickerMessage()
		d.appendMedia(&blocks, &refs, mediaSpec{
			dl: s, mediaType: "sticker", mime: s.GetMimetype(),
			size: int64(s.GetFileLength()), width: int(s.GetWidth()), height: int(s.GetHeight()),
		})
	default:
		text := m.GetConversation()
		if text == "" && m.GetExtendedTextMessage() != nil {
			text = m.GetExtendedTextMessage().GetText()
		}
		if text != "" {
			blocks = append(blocks, textBlock{Kind: "text", Text: text})
		}
	}

	if len(blocks) == 0 {
		return // unsupported message type (reaction, poll, protocol, …)
	}

	// LID addressing: WhatsApp now identifies some senders by a @lid privacy id
	// instead of their phone JID. SenderAlt carries the real phone JID — use it
	// so the contact's external id / phone are the phone number, not the LID.
	sender := info.Sender
	if info.AddressingMode == types.AddressingModeLID && !info.SenderAlt.IsEmpty() {
		sender = info.SenderAlt
	}

	ev := messageInEvent{
		Kind:              "message_in",
		ExternalMessageID: string(info.ID),
		ExternalUserID:    sender.User,
		Content:           messageContent{Blocks: blocks},
		ExternalTimestamp: info.Timestamp.UTC().Format(time.RFC3339),
		Profile:           profileHint{DisplayName: info.PushName, Phone: "+" + sender.User},
		MediaRefs:         refs,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := d.mgr.callback.post(ctx, d.cbURL(), d.cbSecret(), ev); err != nil {
		d.mgr.log.Warnf("device %s message_in post failed: %v", d.id, err)
	}
}

type mediaSpec struct {
	dl         whatsmeow.DownloadableMessage
	mediaType  string
	mime       string
	caption    string
	filename   string
	size       int64
	durationMs int64
	width      int
	height     int
}

// appendMedia downloads the encrypted WhatsApp media, caches the plaintext for
// GET /media/{token}, and appends a MediaBlock + matching MediaRef. On failure
// or oversize it degrades to a text placeholder so the message still lands.
func (d *Device) appendMedia(blocks *[]any, refs *[]mediaRef, s mediaSpec) {
	if s.size > 0 && d.mgr.cfg.MediaMaxBytes > 0 && s.size > d.mgr.cfg.MediaMaxBytes {
		*blocks = append(*blocks, textBlock{Kind: "text", Text: mediaFallbackText(s)})
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	data, err := d.client.Download(ctx, s.dl)
	if err != nil || len(data) == 0 {
		d.mgr.log.Warnf("device %s media download failed: %v", d.id, err)
		*blocks = append(*blocks, textBlock{Kind: "text", Text: mediaFallbackText(s)})
		return
	}
	filename := s.filename
	if filename == "" {
		filename = defaultFilename(s.mediaType, s.mime)
	}
	tok := d.mgr.media.put(data, s.mime, filename)
	idx := len(*blocks)
	*blocks = append(*blocks, mediaBlock{
		Kind: "media", MediaType: s.mediaType, FileID: newUUID(), Caption: s.caption,
		Mime: s.mime, Size: int64(len(data)), DurationMs: s.durationMs, Width: s.width, Height: s.height,
	})
	*refs = append(*refs, mediaRef{
		BlockIndex: idx,
		Ref: mediaRefInner{
			Kind: "url", URL: d.mgr.cfg.PublicURL + "/media/" + tok, Filename: filename, Mime: s.mime,
		},
	})
}

func (d *Device) handleReceipt(e *events.Receipt) {
	var status string
	switch e.Type {
	case types.ReceiptTypeDelivered:
		status = "delivered"
	case types.ReceiptTypeRead, types.ReceiptTypeReadSelf, types.ReceiptTypePlayed:
		status = "read"
	default:
		return
	}
	if len(e.MessageIDs) == 0 {
		return
	}
	ts := e.Timestamp.UTC().Format(time.RFC3339)
	evs := make([]any, 0, len(e.MessageIDs))
	for _, mid := range e.MessageIDs {
		evs = append(evs, deliveryStatusEvent{
			Kind: "delivery_status", ExternalMessageID: string(mid),
			Status: status, ExternalUserID: e.Sender.User, OccurredAt: ts,
		})
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	if err := d.mgr.callback.post(ctx, d.cbURL(), d.cbSecret(), evs...); err != nil {
		d.mgr.log.Warnf("device %s delivery_status post failed: %v", d.id, err)
	}
}

func (d *Device) handleLoggedOut() {
	d.setStatus(statusLoggedOut)
	d.emitStatus(true)
	if c := d.getClient(); c != nil {
		c.Disconnect()
	}
	// purge the session so a future connect never silently re-pairs; keep the
	// registry row (status logged_out) so /health + /send report it.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := d.store.Delete(ctx); err != nil {
		d.mgr.log.Warnf("device %s store delete on logout: %v", d.id, err)
	}
	d.mgr.saveStatus(d.id, statusLoggedOut, "")
}

func (d *Device) handleConnectFailure(e *events.ConnectFailure) {
	d.mgr.log.Warnf("device %s connect failure: reason=%v msg=%s", d.id, e.Reason, e.Message)
	switch e.Reason {
	case events.ConnectFailureTempBanned:
		d.markBanned()
	case events.ConnectFailureLoggedOut,
		events.ConnectFailureMainDeviceGone,
		events.ConnectFailureUnknownLogout:
		// terminal logout/ban conditions — purge the session, never re-pair.
		d.enqueue(d.handleLoggedOut)
	}
}

func (d *Device) markBanned() {
	d.setStatus(statusBanned)
	d.emitStatus(true)
	if c := d.getClient(); c != nil {
		c.Disconnect()
	}
	d.mgr.saveStatus(d.id, statusBanned, d.jidValue())
}

// emitStatus reconciles the live client state into a status and POSTs an
// account_status event when it changed (or force).
func (d *Device) emitStatus(force bool) {
	d.mu.Lock()
	st := d.status
	if st != statusLoggedOut && st != statusBanned && st != statusAwaitingQR {
		if d.client != nil && d.client.IsLoggedIn() && d.client.IsConnected() {
			st = statusOnline
		} else {
			st = statusOffline
		}
		d.status = st
	}
	detail := map[string]any{}
	if d.jid != "" {
		detail["jid"] = d.jid
	}
	if d.phone != "" {
		detail["phone"] = d.phone
	}
	if d.pushname != "" {
		detail["pushname"] = d.pushname
	}
	changed := force || st != d.lastEmitted
	d.lastEmitted = st
	cbURL, secret := d.callbackURL, d.callbackSecret
	d.mu.Unlock()
	if !changed || cbURL == "" {
		return
	}
	ev := accountStatusEvent{Kind: "account_status", Status: st, Detail: detail}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	if err := d.mgr.callback.post(ctx, cbURL, secret, ev); err != nil {
		d.mgr.log.Warnf("device %s account_status post failed: %v", d.id, err)
	}
}

func (d *Device) refreshIdentity() {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.store.ID != nil {
		id := d.store.ID
		d.jid = id.String()
		d.phone = "+" + id.User
	}
	if d.client != nil {
		if pn := d.client.Store.PushName; pn != "" {
			d.pushname = pn
		}
	}
}

func (d *Device) persistJID() {
	if j := d.jidValue(); j != "" {
		d.mgr.saveJID(d.id, j)
	}
}

// ---- send -----------------------------------------------------------------

type sendRequest struct {
	To      string      `json:"to"`
	Payload sendPayload `json:"payload"`
}

type sendPayload struct {
	Blocks []map[string]any `json:"blocks"`
}

type sendOutcome struct {
	ok        bool
	messageID string
	status    string // set on logged_out/banned/offline
	httpCode  int
	errMsg    string
}

func (d *Device) send(ctx context.Context, req sendRequest) sendOutcome {
	switch d.getStatus() {
	case statusLoggedOut:
		return sendOutcome{httpCode: http.StatusConflict, status: statusLoggedOut, errMsg: "logged out"}
	case statusBanned:
		return sendOutcome{httpCode: http.StatusConflict, status: statusBanned, errMsg: "banned"}
	}
	c := d.getClient()
	if c == nil || !c.IsLoggedIn() {
		return sendOutcome{httpCode: http.StatusServiceUnavailable, status: statusOffline, errMsg: "not connected"}
	}
	jid, err := parseRecipient(req.To)
	if err != nil {
		return sendOutcome{httpCode: http.StatusBadRequest, errMsg: err.Error()}
	}
	msgs := d.buildMessages(ctx, req.Payload.Blocks)
	if len(msgs) == 0 {
		return sendOutcome{httpCode: http.StatusBadRequest, errMsg: "no sendable blocks"}
	}
	var lastID string
	for _, m := range msgs {
		resp, sErr := c.SendMessage(ctx, jid, m)
		if sErr != nil {
			return sendOutcome{httpCode: http.StatusBadGateway, errMsg: sErr.Error()}
		}
		lastID = string(resp.ID)
	}
	return sendOutcome{ok: true, messageID: lastID}
}

func (d *Device) buildMessages(ctx context.Context, blocks []map[string]any) []*waE2E.Message {
	var out []*waE2E.Message
	for _, b := range blocks {
		kind, _ := b["kind"].(string)
		switch kind {
		case "text":
			if t, _ := b["text"].(string); t != "" {
				out = append(out, &waE2E.Message{Conversation: proto.String(t)})
			}
		case "media":
			if m := d.buildMediaMessage(ctx, b); m != nil {
				out = append(out, m)
			} else if cap, _ := b["caption"].(string); cap != "" {
				out = append(out, &waE2E.Message{Conversation: proto.String(cap)})
			}
		default:
			// location/product_card/quick_buttons are degraded to text before
			// they reach whatsapp_app; forward any text that slipped through.
			if t, _ := b["text"].(string); t != "" {
				out = append(out, &waE2E.Message{Conversation: proto.String(t)})
			}
		}
	}
	return out
}

func (d *Device) buildMediaMessage(ctx context.Context, b map[string]any) *waE2E.Message {
	mediaType, _ := b["media_type"].(string)
	mime, _ := b["mime"].(string)
	caption, _ := b["caption"].(string)
	filename, _ := b["filename"].(string)
	data, dmime, ok := d.resolveOutboundMedia(b)
	if !ok {
		return nil
	}
	if mime == "" {
		mime = dmime
	}
	if mime == "" {
		mime = "application/octet-stream"
	}
	if filename == "" {
		filename = defaultFilename(mediaType, mime)
	}
	up, err := d.client.Upload(ctx, data, uploadMediaType(mediaType))
	if err != nil {
		d.mgr.log.Warnf("device %s media upload failed: %v", d.id, err)
		return nil
	}
	fl := uint64(len(data))
	switch mediaType {
	case "image", "sticker":
		img := &waE2E.ImageMessage{
			URL: proto.String(up.URL), DirectPath: proto.String(up.DirectPath), MediaKey: up.MediaKey,
			Mimetype: proto.String(mime), FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256,
			FileLength: proto.Uint64(fl),
		}
		if caption != "" {
			img.Caption = proto.String(caption)
		}
		return &waE2E.Message{ImageMessage: img}
	case "video":
		v := &waE2E.VideoMessage{
			URL: proto.String(up.URL), DirectPath: proto.String(up.DirectPath), MediaKey: up.MediaKey,
			Mimetype: proto.String(mime), FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256,
			FileLength: proto.Uint64(fl),
		}
		if caption != "" {
			v.Caption = proto.String(caption)
		}
		return &waE2E.Message{VideoMessage: v}
	case "audio", "voice":
		a := &waE2E.AudioMessage{
			URL: proto.String(up.URL), DirectPath: proto.String(up.DirectPath), MediaKey: up.MediaKey,
			Mimetype: proto.String(mime), FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256,
			FileLength: proto.Uint64(fl), PTT: proto.Bool(mediaType == "voice"),
		}
		return &waE2E.Message{AudioMessage: a}
	default: // file
		doc := &waE2E.DocumentMessage{
			URL: proto.String(up.URL), DirectPath: proto.String(up.DirectPath), MediaKey: up.MediaKey,
			Mimetype: proto.String(mime), FileEncSHA256: up.FileEncSHA256, FileSHA256: up.FileSHA256,
			FileLength: proto.Uint64(fl), FileName: proto.String(filename),
		}
		if caption != "" {
			doc.Caption = proto.String(caption)
		}
		return &waE2E.Message{DocumentMessage: doc}
	}
}

// resolveOutboundMedia turns an outbound MediaBlock into bytes. It prefers an
// explicit url on the block, then falls back to SMARTCHAT_FILES_BASE +
// /api/v1/files/{file_id}. If neither yields bytes the caller degrades to the
// caption text.
func (d *Device) resolveOutboundMedia(b map[string]any) ([]byte, string, bool) {
	if u, _ := b["url"].(string); u != "" {
		if data, mime, err := fetchURL(d.mgr.outHTTP, u, d.mgr.cfg.MediaMaxBytes); err == nil {
			return data, mime, true
		} else {
			d.mgr.log.Warnf("device %s outbound media url fetch failed: %v", d.id, err)
		}
	}
	if fid, _ := b["file_id"].(string); fid != "" && d.mgr.cfg.FilesBase != "" {
		u := d.mgr.cfg.FilesBase + "/api/v1/files/" + fid
		if data, mime, err := fetchURL(d.mgr.outHTTP, u, d.mgr.cfg.MediaMaxBytes); err == nil {
			return data, mime, true
		} else {
			d.mgr.log.Warnf("device %s outbound media file fetch failed: %v", d.id, err)
		}
	}
	return nil, "", false
}

// ---- lifecycle helpers ----------------------------------------------------

func (d *Device) logout() error {
	c := d.getClient()
	var err error
	if c != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		if c.IsConnected() {
			err = c.Logout(ctx)
		}
		c.Disconnect()
	}
	d.setStatus(statusLoggedOut)
	d.emitStatus(true)
	d.mgr.saveStatus(d.id, statusLoggedOut, "")
	return err
}

// destroy stops all goroutines, disconnects, and (if purge) deletes the
// whatsmeow session so the pairing does not survive.
func (d *Device) destroy(purge bool) {
	d.mu.Lock()
	if d.closed {
		d.mu.Unlock()
		return
	}
	d.closed = true
	c := d.client
	d.mu.Unlock()

	close(d.stop)
	if c != nil {
		c.RemoveEventHandlers()
		c.Disconnect()
	}
	if purge && d.store != nil && d.store.ID != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := d.store.Delete(ctx); err != nil {
			d.mgr.log.Warnf("device %s store delete: %v", d.id, err)
		}
	}
}

func (d *Device) snapshot() (status, qr, jid, phone, pushname string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.status, d.qr, d.jid, d.phone, d.pushname
}

func (d *Device) setStatus(s string) {
	d.mu.Lock()
	d.status = s
	d.mu.Unlock()
}

func (d *Device) getStatus() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.status
}

func (d *Device) getClient() *whatsmeow.Client {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.client
}

func (d *Device) jidValue() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.jid
}

func (d *Device) cbURL() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.callbackURL
}

func (d *Device) cbSecret() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.callbackSecret
}

func (d *Device) updateCallback(url, secret string) {
	d.mu.Lock()
	if url != "" {
		d.callbackURL = url
	}
	if secret != "" {
		d.callbackSecret = secret
	}
	d.mu.Unlock()
}

// ---- small utilities ------------------------------------------------------

func parseRecipient(to string) (types.JID, error) {
	to = strings.TrimSpace(to)
	if to == "" {
		return types.JID{}, errors.New("empty recipient")
	}
	if strings.Contains(to, "@") {
		return types.ParseJID(to)
	}
	digits := stripNonDigits(to)
	if digits == "" {
		return types.JID{}, errors.New("invalid recipient")
	}
	return types.NewJID(digits, types.DefaultUserServer), nil
}

func stripNonDigits(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

func uploadMediaType(mediaType string) whatsmeow.MediaType {
	switch mediaType {
	case "image", "sticker":
		return whatsmeow.MediaImage
	case "video":
		return whatsmeow.MediaVideo
	case "audio", "voice":
		return whatsmeow.MediaAudio
	default:
		return whatsmeow.MediaDocument
	}
}

func mediaFallbackText(s mediaSpec) string {
	if s.caption != "" {
		return s.caption
	}
	return "[" + s.mediaType + "]"
}

func defaultFilename(mediaType, mime string) string {
	ext := extForMime(mime)
	if ext == "" {
		switch mediaType {
		case "image":
			ext = ".jpg"
		case "video":
			ext = ".mp4"
		case "audio", "voice":
			ext = ".ogg"
		case "sticker":
			ext = ".webp"
		default:
			ext = ".bin"
		}
	}
	return mediaType + ext
}

func extForMime(mime string) string {
	mime = strings.ToLower(strings.SplitN(mime, ";", 2)[0])
	switch mime {
	case "image/jpeg":
		return ".jpg"
	case "image/png":
		return ".png"
	case "image/webp":
		return ".webp"
	case "image/gif":
		return ".gif"
	case "video/mp4":
		return ".mp4"
	case "audio/ogg", "audio/ogg; codecs=opus":
		return ".ogg"
	case "audio/mpeg":
		return ".mp3"
	case "application/pdf":
		return ".pdf"
	default:
		return ""
	}
}

func newUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%s-%s-%s-%s-%s",
		hex.EncodeToString(b[0:4]), hex.EncodeToString(b[4:6]), hex.EncodeToString(b[6:8]),
		hex.EncodeToString(b[8:10]), hex.EncodeToString(b[10:16]))
}

func shortID(id string) string {
	if len(id) > 8 {
		return id[:8]
	}
	return id
}
