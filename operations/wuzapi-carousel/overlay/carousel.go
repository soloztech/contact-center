// SPDX-License-Identifier: MIT
// Experimental overlay for asternic/wuzapi 9487eca9a40f292d19953a44983979c85d91ccce.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"mime"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"go.mau.fi/whatsmeow"
	waBinary "go.mau.fi/whatsmeow/binary"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

const (
	carouselBaselineCommit = "9487eca9a40f292d19953a44983979c85d91ccce"
	carouselMaxHTTPBytes   = 15 * 1024 * 1024
	carouselMaxImageBytes  = 5 * 1024 * 1024
	carouselMaxTotalBytes  = 10 * 1024 * 1024
	carouselMaxDimension   = 4096
	carouselMaxPixels      = 16000000
	carouselTimeout        = 90 * time.Second
)

var (
	carouselMessageID = regexp.MustCompile(`^[0-9A-F]{32}$`)
	carouselRecipient = regexp.MustCompile(`^[1-9][0-9]{4,19}@(s\.whatsapp\.net|lid)$`)
	carouselPhone     = regexp.MustCompile(`^\+[1-9][0-9]{5,14}$`)
)

type carouselButton struct {
	Type        string `json:"type"`
	Title       string `json:"title"`
	ID          string `json:"id,omitempty"`
	URL         string `json:"url,omitempty"`
	PhoneNumber string `json:"phone_number,omitempty"`
}

type carouselCard struct {
	Body    string           `json:"Body"`
	Image   string           `json:"Image"`
	Buttons []carouselButton `json:"Buttons"`
}

type carouselRequest struct {
	Phone string         `json:"Phone"`
	ID    string         `json:"Id"`
	Body  string         `json:"Body"`
	Cards []carouselCard `json:"Cards"`
}

type preparedCarouselCard struct {
	body          string
	image         []byte
	mime          string
	width, height int
	buttons       []*waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton
}

type preparedCarousel struct {
	id, body  string
	recipient types.JID
	cards     []preparedCarouselCard
}

// Only these two network operations are permitted, and both receive the request context.
type carouselTransport interface {
	Upload(context.Context, []byte, whatsmeow.MediaType) (whatsmeow.UploadResponse, error)
	SendMessage(context.Context, types.JID, *waE2E.Message, ...whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error)
}

type carouselFailure struct {
	code   string
	state  string
	status int
}

func (e *carouselFailure) Error() string { return e.code }

// Check duplicate JSON keys before Go's case-insensitive struct decoder can discard
// one of them. The depth bound also keeps malformed unknown values inexpensive.
func carouselJSONValue(decoder *json.Decoder, depth int) error {
	if depth > 8 {
		return errors.New("JSON nesting exceeds limit")
	}
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	delimiter, container := token.(json.Delim)
	if !container {
		return nil
	}
	if delimiter != '{' && delimiter != '[' {
		return errors.New("invalid JSON container")
	}
	seen := map[string]bool{}
	for decoder.More() {
		if delimiter == '{' {
			key, err := decoder.Token()
			if err != nil {
				return err
			}
			name, ok := key.(string)
			if !ok {
				return errors.New("invalid JSON key")
			}
			// Field names are ASCII. Reject Unicode aliases that Go's struct
			// decoder might otherwise fold into the same ASCII field name.
			for _, char := range name {
				if char > 127 {
					return errors.New("invalid JSON key")
				}
			}
			folded := strings.ToLower(name)
			if seen[folded] {
				return errors.New("duplicate JSON key")
			}
			seen[folded] = true
		}
		if err := carouselJSONValue(decoder, depth+1); err != nil {
			return err
		}
	}
	_, err = decoder.Token()
	return err
}

func decodeCarousel(reader io.Reader) (*preparedCarousel, error) {
	body, err := io.ReadAll(io.LimitReader(reader, carouselMaxHTTPBytes+1))
	if err != nil || len(body) > carouselMaxHTTPBytes {
		return nil, errors.New("HTTP payload exceeds limit or cannot be read")
	}
	if !utf8.Valid(body) {
		return nil, errors.New("JSON must be UTF-8")
	}
	keys := json.NewDecoder(bytes.NewReader(body))
	if err := carouselJSONValue(keys, 0); err != nil {
		return nil, errors.New("invalid or duplicate JSON fields")
	}
	if _, err := keys.Token(); err != io.EOF {
		return nil, errors.New("exactly one JSON object is required")
	}
	var request carouselRequest
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil {
		return nil, errors.New("invalid or unknown carousel fields")
	}
	return prepareCarousel(request)
}

func carouselText(value string, maximum int, multiline bool) bool {
	if !utf8.ValidString(value) || strings.TrimSpace(value) == "" || utf8.RuneCountInString(value) > maximum {
		return false
	}
	for _, char := range value {
		if unicode.IsControl(char) && !(multiline && (char == '\n' || char == '\t')) {
			return false
		}
	}
	return true
}

func prepareCarousel(request carouselRequest) (*preparedCarousel, error) {
	if !carouselMessageID.MatchString(request.ID) {
		return nil, errors.New("Id must contain 32 uppercase hexadecimal characters")
	}
	if !carouselRecipient.MatchString(request.Phone) {
		return nil, errors.New("Phone must be a canonical direct PN or LID JID")
	}
	if !carouselText(request.Body, 1024, true) {
		return nil, errors.New("Body must contain 1 to 1024 characters")
	}
	if len(request.Cards) < 2 || len(request.Cards) > 10 {
		return nil, errors.New("Cards must contain 2 to 10 entries")
	}
	recipient, err := types.ParseJID(request.Phone)
	if err != nil {
		return nil, errors.New("invalid recipient")
	}
	prepared := &preparedCarousel{id: request.ID, body: request.Body, recipient: recipient}
	total := 0
	replyIDs := map[string]bool{}
	for index, card := range request.Cards {
		if !carouselText(card.Body, 160, true) {
			return nil, fmt.Errorf("card %d Body must contain 1 to 160 characters", index+1)
		}
		if len(card.Buttons) < 1 || len(card.Buttons) > 3 {
			return nil, fmt.Errorf("card %d requires 1 to 3 buttons", index+1)
		}
		buttons := make([]*waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton, 0, len(card.Buttons))
		for _, button := range card.Buttons {
			if !carouselText(button.Title, 20, false) {
				return nil, fmt.Errorf("card %d button title must contain 1 to 20 characters", index+1)
			}
			params := map[string]string{"display_text": button.Title}
			name := button.Type
			switch button.Type {
			case "reply":
				if !carouselText(button.ID, 200, false) || replyIDs[button.ID] || button.URL != "" || button.PhoneNumber != "" {
					return nil, errors.New("reply IDs must be present and unique across all cards, with no other action fields")
				}
				replyIDs[button.ID] = true
				name, params["id"] = "quick_reply", button.ID
			case "cta_url":
				parsed, err := url.Parse(button.URL)
				if err != nil || !carouselText(button.URL, 2048, false) || strings.ContainsAny(button.URL, "\\ ") || strings.IndexFunc(button.URL, unicode.IsSpace) >= 0 || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil || (parsed.Port() != "" && parsed.Port() != "443") || button.ID != "" || button.PhoneNumber != "" {
					return nil, errors.New("URL action requires an HTTPS URL without credentials or other action fields")
				}
				params["url"], params["merchant_url"] = button.URL, button.URL
			case "cta_call":
				if !carouselPhone.MatchString(button.PhoneNumber) || button.ID != "" || button.URL != "" {
					return nil, errors.New("phone action requires an E.164 number and no other action fields")
				}
				params["phone_number"] = button.PhoneNumber
			default:
				return nil, errors.New("unsupported button action")
			}
			encoded, _ := json.Marshal(params)
			buttons = append(buttons, &waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton{Name: proto.String(name), ButtonParamsJSON: proto.String(string(encoded))})
		}
		mimeType, encoded, found := strings.Cut(card.Image, ";base64,")
		if !found || (mimeType != "data:image/jpeg" && mimeType != "data:image/png") {
			return nil, errors.New("images must be JPEG or PNG base64 data URLs")
		}
		if len(encoded) > base64.StdEncoding.EncodedLen(carouselMaxImageBytes) {
			return nil, errors.New("image exceeds 5 MiB")
		}
		imageBytes, err := base64.StdEncoding.Strict().DecodeString(encoded)
		if err != nil || len(imageBytes) == 0 || len(imageBytes) > carouselMaxImageBytes {
			return nil, errors.New("invalid image encoding or size")
		}
		total += len(imageBytes)
		if total > carouselMaxTotalBytes {
			return nil, errors.New("images exceed 10 MiB aggregate limit")
		}
		config, format, err := image.DecodeConfig(bytes.NewReader(imageBytes))
		if err != nil || "data:image/"+format != mimeType || config.Width <= 0 || config.Height <= 0 || config.Width > carouselMaxDimension || config.Height > carouselMaxDimension || config.Width*config.Height > carouselMaxPixels {
			return nil, errors.New("image contents, MIME type or dimensions are invalid")
		}
		// Decode the complete bounded image, not just its header: truncated data must
		// fail during admission rather than after one of the other cards is uploaded.
		if _, _, err := image.Decode(bytes.NewReader(imageBytes)); err != nil {
			return nil, errors.New("image data is incomplete or corrupt")
		}
		prepared.cards = append(prepared.cards, preparedCarouselCard{body: card.Body, image: imageBytes, mime: strings.TrimPrefix(mimeType, "data:"), width: config.Width, height: config.Height, buttons: buttons})
	}
	return prepared, nil
}

func dispatchCarousel(ctx context.Context, client carouselTransport, prepared *preparedCarousel) (whatsmeow.SendResponse, *waE2E.Message, *carouselFailure) {
	cards := make([]*waE2E.InteractiveMessage, 0, len(prepared.cards))
	for _, card := range prepared.cards {
		if ctx.Err() != nil {
			return whatsmeow.SendResponse{}, nil, &carouselFailure{"request_cancelled", "not_sent", http.StatusGatewayTimeout}
		}
		upload, err := client.Upload(ctx, card.image, whatsmeow.MediaImage)
		digest := sha256.Sum256(card.image)
		if err != nil || upload.URL == "" || upload.DirectPath == "" || len(upload.MediaKey) != 32 || len(upload.FileEncSHA256) != 32 || !bytes.Equal(upload.FileSHA256, digest[:]) || upload.FileLength != uint64(len(card.image)) {
			return whatsmeow.SendResponse{}, nil, &carouselFailure{"image_upload_failed", "not_sent", http.StatusBadGateway}
		}
		imageMessage := &waE2E.ImageMessage{URL: proto.String(upload.URL), DirectPath: proto.String(upload.DirectPath), MediaKey: upload.MediaKey, FileEncSHA256: upload.FileEncSHA256, FileSHA256: upload.FileSHA256, FileLength: proto.Uint64(upload.FileLength), Mimetype: proto.String(card.mime), Width: proto.Uint32(uint32(card.width)), Height: proto.Uint32(uint32(card.height))}
		cards = append(cards, &waE2E.InteractiveMessage{
			Header:             &waE2E.InteractiveMessage_Header{HasMediaAttachment: proto.Bool(true), Media: &waE2E.InteractiveMessage_Header_ImageMessage{ImageMessage: imageMessage}},
			Body:               &waE2E.InteractiveMessage_Body{Text: proto.String(card.body)},
			InteractiveMessage: &waE2E.InteractiveMessage_NativeFlowMessage_{NativeFlowMessage: &waE2E.InteractiveMessage_NativeFlowMessage{Buttons: card.buttons, MessageVersion: proto.Int32(1)}},
		})
	}
	message := &waE2E.Message{InteractiveMessage: &waE2E.InteractiveMessage{
		Body:               &waE2E.InteractiveMessage_Body{Text: proto.String(prepared.body)},
		InteractiveMessage: &waE2E.InteractiveMessage_CarouselMessage_{CarouselMessage: &waE2E.InteractiveMessage_CarouselMessage{Cards: cards, MessageVersion: proto.Int32(1), CarouselCardType: waE2E.InteractiveMessage_CarouselMessage_HSCROLL_CARDS.Enum()}},
	}}
	// Match the explicit native-flow node used by the pinned SendButtons handler.
	// No extra carousel node or view-once wrapper is inferred from the protobuf.
	// Actual client rendering remains an acceptance gate for this experimental path.
	nodes := []waBinary.Node{{Tag: "biz", Content: []waBinary.Node{{Tag: "interactive", Attrs: waBinary.Attrs{"type": "native_flow", "v": "1"}, Content: []waBinary.Node{{Tag: "native_flow", Attrs: waBinary.Attrs{"v": "9", "name": "mixed"}}}}}}}
	if ctx.Err() != nil {
		return whatsmeow.SendResponse{}, nil, &carouselFailure{"request_cancelled", "not_sent", http.StatusGatewayTimeout}
	}
	response, err := client.SendMessage(ctx, prepared.recipient, message, whatsmeow.SendRequestExtra{ID: prepared.id, AdditionalNodes: &nodes, Timeout: 60 * time.Second})
	// Crossing SendMessage is the ambiguity boundary. Never classify its error (or
	// a malformed acknowledgement) as safe to retry, even if the context expired.
	if err != nil || response.ID != prepared.id || response.Timestamp.IsZero() {
		return response, message, &carouselFailure{"delivery_unconfirmed", "unknown", http.StatusBadGateway}
	}
	return response, message, nil
}

func carouselJSONResponse(w http.ResponseWriter, status int, success bool, data map[string]interface{}, failure string) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	body := map[string]interface{}{"code": status, "success": success, "data": data}
	if failure != "" {
		body["error"] = failure
	}
	// A failed response write cannot undo a send and must never trigger another.
	_ = json.NewEncoder(w).Encode(body)
}

func (s *server) SendCarousel() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		mediaType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil || mediaType != "application/json" {
			carouselJSONResponse(w, http.StatusUnsupportedMediaType, false, map[string]interface{}{"delivery_state": "not_sent"}, "application/json is required")
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, carouselMaxHTTPBytes)
		prepared, err := decodeCarousel(r.Body)
		if err != nil {
			carouselJSONResponse(w, http.StatusBadRequest, false, map[string]interface{}{"delivery_state": "not_sent"}, err.Error())
			return
		}
		// Auth is the same middleware as the upstream message routes. Reading the
		// session and crossing the network happen only after complete validation.
		user := r.Context().Value("userinfo").(Values)
		client := clientManager.GetWhatsmeowClient(user.Get("Id"))
		if client == nil || !client.IsConnected() || !client.IsLoggedIn() {
			carouselJSONResponse(w, http.StatusServiceUnavailable, false, map[string]interface{}{"Id": prepared.id, "delivery_state": "not_sent"}, "session_unavailable")
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), carouselTimeout)
		defer cancel()
		response, message, failure := dispatchCarousel(ctx, client, prepared)
		if failure != nil {
			carouselJSONResponse(w, failure.status, false, map[string]interface{}{"Id": prepared.id, "delivery_state": failure.state}, failure.code)
			return
		}
		historyLimit, _ := strconv.Atoi(user.Get("History"))
		s.saveOutgoingMessageToHistory(user.Get("Id"), prepared.recipient.String(), prepared.id, "carousel", prepared.body, "", historyLimit)
		s.publishSentMessageEvent(user.Get("Token"), user.Get("Id"), user.Get("Id"), prepared.recipient, prepared.id, message, response.Timestamp)
		carouselJSONResponse(w, http.StatusOK, true, map[string]interface{}{"Details": "Sent", "Id": prepared.id, "Timestamp": response.Timestamp.Unix(), "delivery_state": "accepted"}, "")
	}
}

func (s *server) GetCarouselCapabilities() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		carouselJSONResponse(w, http.StatusOK, true, map[string]interface{}{
			"schema_version": "1.0", "extension": "carousel_v1", "experimental": true, "baseline_commit": carouselBaselineCommit,
			"carousel": map[string]interface{}{
				"min_cards": 2, "max_cards": 10, "max_body_length": 1024, "max_card_body_length": 160,
				"min_buttons": 1, "max_buttons": 3, "max_button_title_length": 20, "max_reply_id_length": 200,
				"action_types": []string{"reply", "cta_url", "cta_call"}, "max_url_length": 2048, "max_phone_digits": 15,
				"image_mime_types": []string{"image/jpeg", "image/png"}, "max_image_bytes": carouselMaxImageBytes, "max_total_image_bytes": carouselMaxTotalBytes,
				"max_http_bytes": carouselMaxHTTPBytes, "max_image_dimension": carouselMaxDimension, "max_image_pixels": carouselMaxPixels,
				"image_source": "data_url", "recipient_types": []string{"pn", "lid"}, "id_format": "uppercase_hex_32", "automatic_retry": false,
			},
		}, "")
	}
}
