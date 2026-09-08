// SPDX-License-Identifier: MIT
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"image"
	"image/color"
	"image/jpeg"
	"image/png"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/mux"
	"github.com/patrickmn/go-cache"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

func carouselFixture(t *testing.T) carouselRequest {
	t.Helper()
	var data bytes.Buffer
	picture := image.NewRGBA(image.Rect(0, 0, 2, 2))
	picture.Set(0, 0, color.RGBA{R: 255, A: 255})
	if err := png.Encode(&data, picture); err != nil {
		t.Fatal(err)
	}
	uri := "data:image/png;base64," + base64.StdEncoding.EncodeToString(data.Bytes())
	return carouselRequest{Phone: "5511999999999@s.whatsapp.net", ID: strings.Repeat("A", 32), Body: "Teste: opções", Cards: []carouselCard{
		{Body: "Primeiro cartão", Image: uri, Buttons: []carouselButton{{Type: "reply", Title: "Escolher A", ID: "opaque-a"}}},
		{Body: "Segundo cartão", Image: uri, Buttons: []carouselButton{{Type: "reply", Title: "Escolher B", ID: "opaque-b"}}},
	}}
}

func encodeCarouselFixture(t *testing.T, request carouselRequest) []byte {
	t.Helper()
	data, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestCarouselAdmission(t *testing.T) {
	cases := map[string]func(*carouselRequest){
		"missing_id":          func(r *carouselRequest) { r.ID = "" },
		"lowercase_id":        func(r *carouselRequest) { r.ID = strings.Repeat("a", 32) },
		"group":               func(r *carouselRequest) { r.Phone = "123456@g.us" },
		"bare_phone":          func(r *carouselRequest) { r.Phone = "5511999999999" },
		"empty_body":          func(r *carouselRequest) { r.Body = " " },
		"long_body":           func(r *carouselRequest) { r.Body = strings.Repeat("á", 1025) },
		"one_card":            func(r *carouselRequest) { r.Cards = r.Cards[:1] },
		"eleven_cards":        func(r *carouselRequest) { r.Cards = make([]carouselCard, 11) },
		"long_card":           func(r *carouselRequest) { r.Cards[1].Body = strings.Repeat("a", 161) },
		"control":             func(r *carouselRequest) { r.Cards[1].Body = "test\x00" },
		"duplicate_id":        func(r *carouselRequest) { r.Cards[1].Buttons[0].ID = "opaque-a" },
		"missing_buttons":     func(r *carouselRequest) { r.Cards[1].Buttons = nil },
		"four_buttons":        func(r *carouselRequest) { r.Cards[1].Buttons = make([]carouselButton, 4) },
		"unknown_action":      func(r *carouselRequest) { r.Cards[1].Buttons[0].Type = "copy" },
		"long_button":         func(r *carouselRequest) { r.Cards[1].Buttons[0].Title = strings.Repeat("é", 21) },
		"long_reply_id":       func(r *carouselRequest) { r.Cards[1].Buttons[0].ID = strings.Repeat("a", 201) },
		"mixed_action_fields": func(r *carouselRequest) { r.Cards[1].Buttons[0].URL = "https://example.com" },
		"remote_image":        func(r *carouselRequest) { r.Cards[1].Image = "https://example.com/private.png" },
		"invalid_base64":      func(r *carouselRequest) { r.Cards[1].Image += "!" },
		"mime_spoof": func(r *carouselRequest) {
			r.Cards[1].Image = strings.Replace(r.Cards[1].Image, "image/png", "image/jpeg", 1)
		},
		"non_image": func(r *carouselRequest) {
			r.Cards[1].Image = "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte("not an image"))
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			request := carouselFixture(t)
			mutate(&request)
			if _, err := decodeCarousel(bytes.NewReader(encodeCarouselFixture(t, request))); err == nil {
				t.Fatal("invalid input admitted")
			}
		})
	}
	request := carouselFixture(t)
	request.Phone = "192908674207802@lid"
	request.Body = strings.Repeat("á", 1024)
	request.Cards[0].Buttons[0].Title = strings.Repeat("é", 20)
	request.Cards[1].Buttons = []carouselButton{
		{Type: "cta_url", Title: "Abrir", URL: "https://example.com/item?a=1&b=2"},
		{Type: "cta_call", Title: "Ligar", PhoneNumber: "+5511999999999"},
	}
	if _, err := decodeCarousel(bytes.NewReader(encodeCarouselFixture(t, request))); err != nil {
		t.Fatal(err)
	}
}

func TestCarouselStrictJSON(t *testing.T) {
	valid := string(encodeCarouselFixture(t, carouselFixture(t)))
	cases := []string{
		valid + valid, "null", "[]", "{}", "{", valid + " trailing",
		strings.Replace(valid, `"Cards":`, `"unexpected":1,"Cards":`, 1),
		strings.Replace(valid, `"Body":`, `"body":"hidden","Body":`, 1),
		strings.Replace(valid, `"Cards":`, `"Cardſ":[],"Cards":`, 1),
		strings.Replace(valid, `"type":"reply"`, `"type":"reply","Type":"cta_url"`, 1),
		strings.Replace(valid, `"type":"reply"`, `"type":"reply","payload":{}`, 1),
		strings.Replace(valid, `"Body":`, `"too_deep":[[[[[[[[[[[0]]]]]]]]]]],"Body":`, 1),
		valid[:len(valid)-1] + string([]byte{0xff}) + "}",
	}
	for index, data := range cases {
		if _, err := decodeCarousel(strings.NewReader(data)); err == nil {
			t.Fatalf("invalid JSON admitted at %d", index)
		}
	}
	if _, err := decodeCarousel(strings.NewReader(valid + strings.Repeat(" ", carouselMaxHTTPBytes))); err == nil {
		t.Fatal("oversize HTTP admitted")
	}
}

func TestCarouselActionLinks(t *testing.T) {
	for _, link := range []string{"http://example.com", "javascript:alert(1)", "https://user:secret@example.com", "https://example.com:8080", "https://example.com/path with space", "https://example.com/\\path", "https://"} {
		request := carouselFixture(t)
		request.Cards[0].Buttons = []carouselButton{{Type: "cta_url", Title: "Abrir", URL: link}}
		if _, err := prepareCarousel(request); err == nil {
			t.Fatalf("link admitted: %s", link)
		}
	}
	for _, phone := range []string{"5511999999999", "+0", "+5511999999999999999", "+55 11 999999999"} {
		request := carouselFixture(t)
		request.Cards[0].Buttons = []carouselButton{{Type: "cta_call", Title: "Ligar", PhoneNumber: phone}}
		if _, err := prepareCarousel(request); err == nil {
			t.Fatal("invalid phone admitted")
		}
	}
}

func TestCarouselImageLimits(t *testing.T) {
	request := carouselFixture(t)
	_, encoded, _ := strings.Cut(request.Cards[1].Image, ";base64,")
	data, _ := base64.StdEncoding.DecodeString(encoded)
	request.Cards[1].Image = "data:image/png;base64," + base64.StdEncoding.EncodeToString(data[:len(data)/2])
	if _, err := prepareCarousel(request); err == nil {
		t.Fatal("truncated image admitted")
	}
	var oversized bytes.Buffer
	if err := png.Encode(&oversized, image.NewRGBA(image.Rect(0, 0, carouselMaxDimension+1, 1))); err != nil {
		t.Fatal(err)
	}
	request.Cards[1].Image = "data:image/png;base64," + base64.StdEncoding.EncodeToString(oversized.Bytes())
	if _, err := prepareCarousel(request); err == nil {
		t.Fatal("oversize dimension admitted")
	}
	request.Cards[1].Image = "data:image/png;base64," + base64.StdEncoding.EncodeToString(append(data, make([]byte, carouselMaxImageBytes)...))
	if _, err := prepareCarousel(request); err == nil {
		t.Fatal("oversize bytes admitted")
	}
	request = carouselFixture(t)
	large := "data:image/png;base64," + base64.StdEncoding.EncodeToString(append(data, make([]byte, 4*1024*1024-len(data))...))
	for n := 0; n < 3; n++ {
		card := carouselCard{Body: "Imagem", Image: large, Buttons: []carouselButton{{Type: "cta_url", Title: "Abrir", URL: "https://example.com"}}}
		request.Cards = append(request.Cards, card)
	}
	if _, err := prepareCarousel(request); err == nil {
		t.Fatal("aggregate oversize admitted")
	}
	var jpg bytes.Buffer
	if err := jpeg.Encode(&jpg, image.NewRGBA(image.Rect(0, 0, 2, 2)), nil); err != nil {
		t.Fatal(err)
	}
	request = carouselFixture(t)
	request.Cards[1].Image = "data:image/jpeg;base64," + base64.StdEncoding.EncodeToString(jpg.Bytes())
	if _, err := prepareCarousel(request); err != nil {
		t.Fatal(err)
	}
}

type carouselFakeTransport struct {
	uploads, sends int
	failUpload     int
	badUpload      bool
	sendError      bool
	badID          bool
	zeroTimestamp  bool
	cancel         context.CancelFunc
	message        *waE2E.Message
	extra          whatsmeow.SendRequestExtra
}

func (f *carouselFakeTransport) Upload(ctx context.Context, data []byte, kind whatsmeow.MediaType) (whatsmeow.UploadResponse, error) {
	f.uploads++
	if f.uploads == f.failUpload {
		return whatsmeow.UploadResponse{}, errors.New("upload failed with private details")
	}
	if f.cancel != nil && f.uploads == 2 {
		f.cancel()
	}
	digest := sha256.Sum256(data)
	if f.badUpload {
		digest[0] ^= 1
	}
	return whatsmeow.UploadResponse{URL: "https://example.com/upload", DirectPath: "/upload", MediaKey: make([]byte, 32), FileEncSHA256: make([]byte, 32), FileSHA256: digest[:], FileLength: uint64(len(data))}, nil
}

func (f *carouselFakeTransport) SendMessage(ctx context.Context, recipient types.JID, message *waE2E.Message, extra ...whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
	f.sends++
	f.message, f.extra = message, extra[0]
	if f.sendError {
		return whatsmeow.SendResponse{}, errors.New("ambiguous transport with private details")
	}
	id, stamp := extra[0].ID, time.Unix(1700000000, 0)
	if f.badID {
		id = "different"
	}
	if f.zeroTimestamp {
		stamp = time.Time{}
	}
	return whatsmeow.SendResponse{ID: id, Timestamp: stamp}, nil
}

func TestCarouselSingleDispatch(t *testing.T) {
	prepared, err := prepareCarousel(carouselFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	client := &carouselFakeTransport{}
	response, message, failure := dispatchCarousel(context.Background(), client, prepared)
	if failure != nil || client.uploads != 2 || client.sends != 1 || response.ID != prepared.id || client.extra.ID != prepared.id {
		t.Fatalf("wrong send outcome: %v", failure)
	}
	carousel := message.GetInteractiveMessage().GetCarouselMessage()
	if carousel == nil || len(carousel.Cards) != 2 || carousel.GetCarouselCardType() != waE2E.InteractiveMessage_CarouselMessage_HSCROLL_CARDS {
		t.Fatal("missing native carousel")
	}
	for index, card := range carousel.Cards {
		if card.GetBody().GetText() != prepared.cards[index].body || !card.GetHeader().GetHasMediaAttachment() || card.GetHeader().GetImageMessage().GetMimetype() != "image/png" {
			t.Fatal("card projection lost image/text")
		}
		buttons := card.GetNativeFlowMessage().GetButtons()
		var params map[string]string
		if len(buttons) != 1 || buttons[0].GetName() != "quick_reply" || json.Unmarshal([]byte(buttons[0].GetButtonParamsJSON()), &params) != nil || params["id"] == "" {
			t.Fatal("reply identity lost")
		}
	}
	if client.extra.AdditionalNodes == nil || len(*client.extra.AdditionalNodes) != 1 {
		t.Fatal("missing biz node")
	}
	biz := (*client.extra.AdditionalNodes)[0]
	native := biz.GetChildByTag("interactive", "native_flow")
	if biz.Tag != "biz" || native.Attrs["name"] != "mixed" || native.Attrs["v"] != "9" {
		t.Fatal("incorrect native flow metadata")
	}
}

func TestCarouselDispatchFailures(t *testing.T) {
	prepared, err := prepareCarousel(carouselFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name           string
		client         carouselFakeTransport
		uploads, sends int
		state          string
	}{
		{"first_upload", carouselFakeTransport{failUpload: 1}, 1, 0, "not_sent"},
		{"second_upload", carouselFakeTransport{failUpload: 2}, 2, 0, "not_sent"},
		{"corrupt_upload_result", carouselFakeTransport{badUpload: true}, 1, 0, "not_sent"},
		{"send_error", carouselFakeTransport{sendError: true}, 2, 1, "unknown"},
		{"wrong_ack_id", carouselFakeTransport{badID: true}, 2, 1, "unknown"},
		{"missing_ack_timestamp", carouselFakeTransport{zeroTimestamp: true}, 2, 1, "unknown"},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			_, _, failure := dispatchCarousel(context.Background(), &test.client, prepared)
			if failure == nil || failure.state != test.state || test.client.uploads != test.uploads || test.client.sends != test.sends {
				t.Fatalf("incorrect failure boundary: %#v", failure)
			}
			if strings.Contains(failure.Error(), "private") {
				t.Fatal("provider error leaked")
			}
		})
	}
	for _, cancelBefore := range []bool{true, false} {
		ctx, cancel := context.WithCancel(context.Background())
		client := &carouselFakeTransport{cancel: cancel}
		if cancelBefore {
			cancel()
		}
		_, _, failure := dispatchCarousel(ctx, client, prepared)
		cancel()
		if failure == nil || failure.state != "not_sent" || client.sends != 0 || (cancelBefore && client.uploads != 0) {
			t.Fatal("cancelled request dispatched")
		}
	}
}

func TestCarouselRoutes(t *testing.T) {
	s := &server{router: mux.NewRouter()}
	s.routes()
	// Cached synthetic identities exercise the real auth middleware without a DB.
	userinfocache.Set("", Values{map[string]string{}}, cache.NoExpiration)
	userinfocache.Set("carousel-test-token", Values{map[string]string{"Id": "carousel-test-user"}}, cache.NoExpiration)
	t.Cleanup(func() { userinfocache.Delete(""); userinfocache.Delete("carousel-test-token") })
	for _, target := range []struct {
		method, path, token, contentType string
		body                             []byte
		status                           int
	}{
		{"GET", "/chat/capabilities", "", "", nil, 401},
		{"POST", "/chat/send/carousel", "", "application/json", []byte("{}"), 401},
		// Upstream's static fallback returns 404 for unmatched methods.
		{"POST", "/chat/capabilities", "carousel-test-token", "", nil, 404},
		{"GET", "/chat/send/carousel", "carousel-test-token", "", nil, 404},
		{"GET", "/chat/capabilities", "carousel-test-token", "", nil, 200},
		{"POST", "/chat/send/carousel", "carousel-test-token", "text/plain", []byte("{}"), 415},
		{"POST", "/chat/send/carousel", "carousel-test-token", "application/json", []byte("{}"), 400},
		{"POST", "/chat/send/carousel", "carousel-test-token", "application/json", encodeCarouselFixture(t, carouselFixture(t)), 503},
	} {
		r := httptest.NewRequest(target.method, target.path, bytes.NewReader(target.body))
		r.Header.Set("token", target.token)
		r.Header.Set("Content-Type", target.contentType)
		w := httptest.NewRecorder()
		s.router.ServeHTTP(w, r)
		if w.Code != target.status {
			t.Fatalf("%s %s got %d, wanted %d", target.method, target.path, w.Code, target.status)
		}
		if w.Code == http.StatusOK {
			var result struct {
				Success bool `json:"success"`
				Data    struct {
					Experimental bool   `json:"experimental"`
					Baseline     string `json:"baseline_commit"`
				} `json:"data"`
			}
			if json.Unmarshal(w.Body.Bytes(), &result) != nil || !result.Success || !result.Data.Experimental || result.Data.Baseline != carouselBaselineCommit {
				t.Fatal("capabilities misrepresent experimental baseline")
			}
		}
	}
}
