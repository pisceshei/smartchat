package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// This file defines the EXACT InboundEvent JSON the bridge POSTs to the
// SmartChat API callback_url (POST /hooks/bridge/{webhook_secret}). The shapes
// mirror apps/api/app/channels/base.py (MessageIn / DeliveryStatus /
// AccountStatus) and py_contracts.content (TextBlock / MediaBlock). The
// envelope is {"events": [...]} which the Python side consumes via
// parse_normalized_events. See README.md for annotated samples.

// ---- content blocks (py_contracts.content) --------------------------------

type textBlock struct {
	Kind string `json:"kind"` // always "text"
	Text string `json:"text"`
}

// mediaBlock mirrors py_contracts.content.MediaBlock. file_id is a REQUIRED
// UUID on the Python side; we emit a random placeholder UUID that the ingress
// pipeline rewrites once it downloads (via media_refs) and stores the bytes.
type mediaBlock struct {
	Kind       string `json:"kind"` // always "media"
	MediaType  string `json:"media_type"`
	FileID     string `json:"file_id"`
	Caption    string `json:"caption,omitempty"`
	Mime       string `json:"mime,omitempty"`
	Size       int64  `json:"size,omitempty"`
	DurationMs int64  `json:"duration_ms,omitempty"`
	Width      int    `json:"width,omitempty"`
	Height     int    `json:"height,omitempty"`
}

type messageContent struct {
	Blocks []any `json:"blocks"`
}

// ---- media refs (base.MediaRef) -------------------------------------------

// mediaRefInner is the adapter-specific ref consumed by BaseAdapter.fetch_media
// (kind == "url" => httpx GET of url). We serve the bytes ourselves from
// {PublicURL}/media/{token}; the URL carries an unguessable token so it needs
// no auth header (fetch_media sends none).
type mediaRefInner struct {
	Kind     string `json:"kind"` // always "url"
	URL      string `json:"url"`
	Filename string `json:"filename,omitempty"`
	Mime     string `json:"mime,omitempty"`
}

type mediaRef struct {
	BlockIndex int           `json:"block_index"`
	Ref        mediaRefInner `json:"ref"`
}

// ---- inbound events -------------------------------------------------------

type profileHint struct {
	DisplayName string `json:"display_name,omitempty"`
	Phone       string `json:"phone,omitempty"`
}

type messageInEvent struct {
	Kind              string         `json:"kind"` // "message_in"
	ExternalMessageID string         `json:"external_message_id"`
	ExternalUserID    string         `json:"external_user_id"`
	Content           messageContent `json:"content"`
	ExternalTimestamp string         `json:"external_timestamp,omitempty"`
	Profile           profileHint    `json:"profile"`
	MediaRefs         []mediaRef     `json:"media_refs"`
}

type deliveryStatusEvent struct {
	Kind              string `json:"kind"` // "delivery_status"
	ExternalMessageID string `json:"external_message_id"`
	Status            string `json:"status"` // sent|delivered|read|failed
	ExternalUserID    string `json:"external_user_id,omitempty"`
	OccurredAt        string `json:"occurred_at,omitempty"`
}

type accountStatusEvent struct {
	Kind   string         `json:"kind"` // "account_status"
	Status string         `json:"status"`
	Detail map[string]any `json:"detail,omitempty"`
}

type inboundEnvelope struct {
	Events []any `json:"events"`
}

// signBridge reproduces base.bridge_signature: hex(hmac_sha256(secret, body)).
func signBridge(secret string, body []byte) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	return hex.EncodeToString(mac.Sum(nil))
}

// callbackClient is a small dependency-free poster used to deliver inbound
// events to the SmartChat API. It signs each body and retries transient
// failures a couple of times (the per-device worker is sequential, so a slow
// callback only backs up that one device).
type callbackClient struct {
	http *http.Client
}

func newCallbackClient() *callbackClient {
	return &callbackClient{http: &http.Client{Timeout: 20 * time.Second}}
}

func (c *callbackClient) post(ctx context.Context, url, secret string, events ...any) error {
	if url == "" || len(events) == 0 {
		return nil
	}
	body, err := json.Marshal(inboundEnvelope{Events: events})
	if err != nil {
		return fmt.Errorf("marshal inbound: %w", err)
	}
	sig := signBridge(secret, body)
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * 500 * time.Millisecond):
			}
		}
		req, rErr := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
		if rErr != nil {
			return rErr
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Bridge-Signature", sig)
		resp, dErr := c.http.Do(req)
		if dErr != nil {
			lastErr = dErr
			continue
		}
		_ = resp.Body.Close()
		if resp.StatusCode < 300 {
			return nil
		}
		// 4xx (bad signature / unknown secret) will not fix itself on retry.
		if resp.StatusCode < 500 {
			return fmt.Errorf("callback %s returned %d", url, resp.StatusCode)
		}
		lastErr = fmt.Errorf("callback %s returned %d", url, resp.StatusCode)
	}
	return lastErr
}
