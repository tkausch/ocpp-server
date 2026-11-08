package main

import (
	"context"
	"io"
	"log/slog"
	"net"
	"testing"
	"time"

	"github.com/otfabric/go-ocpp"
	"github.com/otfabric/go-ocpp/client"
	"github.com/otfabric/go-ocpp/ocpp21"
)

// station is a fake charging station that records the stop request it gets.
type station struct {
	ocpp21.UnimplementedChargingStationHandler
	stopped chan string
}

func (s station) RequestStopTransaction(_ context.Context, _ ocpp.Peer, req *ocpp21.RequestStopTransactionRequest) (*ocpp21.RequestStopTransactionResponse, error) {
	s.stopped <- req.TransactionID
	return &ocpp21.RequestStopTransactionResponse{Status: ocpp21.RequestStartStopStatusAccepted}, nil
}

func TestFullSession(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	srv, err := newServer(slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = srv.Serve(ln) }()
	defer func() { _ = srv.Shutdown(ctx) }()

	st := station{stopped: make(chan string, 1)}
	c, err := client.New("ws://"+ln.Addr().String()+"/CP_T",
		client.WithVersions(ocpp.V21),
		client.WithHandler(ocpp.V21, ocpp21.ChargingStation(st)),
		client.WithInsecureTransport(),
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Connect(ctx); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close() }()
	if c.Version() != ocpp.V21 {
		t.Fatalf("negotiated %s, want 2.1", c.Version())
	}

	boot, err := ocpp21.BootNotification(ctx, c, &ocpp21.BootNotificationRequest{
		Reason:          ocpp21.BootReasonPowerUp,
		ChargingStation: ocpp21.ChargingStationType{VendorName: "Sim", Model: "T"},
	})
	if err != nil || boot.Status != ocpp21.RegistrationStatusAccepted || boot.Interval != 10 {
		t.Fatalf("boot: %+v, %v", boot, err)
	}

	if _, err := ocpp21.Heartbeat(ctx, c, &ocpp21.HeartbeatRequest{}); err != nil {
		t.Fatal(err)
	}

	for token, want := range map[string]ocpp21.AuthorizationStatusEnumType{
		"RFID123": ocpp21.AuthorizationStatusAccepted,
		"BAD":     ocpp21.AuthorizationStatusBlocked,
	} {
		auth, err := ocpp21.Authorize(ctx, c, &ocpp21.AuthorizeRequest{IDToken: ocpp21.IDTokenType{IDToken: token, Type: "ISO14443"}})
		if err != nil || auth.IDTokenInfo.Status != want {
			t.Fatalf("authorize %s: %+v, %v (want %s)", token, auth, err, want)
		}
	}

	send := func(ev ocpp21.TransactionEventEnumType, seq int, trig ocpp21.TriggerReasonEnumType) {
		t.Helper()
		_, err := ocpp21.TransactionEvent(ctx, c, &ocpp21.TransactionEventRequest{
			EventType: ev, Timestamp: time.Now().UTC(), TriggerReason: trig, SeqNo: seq,
			TransactionInfo: ocpp21.TransactionType{TransactionID: "tx-1"},
		})
		if err != nil {
			t.Fatalf("%s: %v", ev, err)
		}
	}
	send(ocpp21.TransactionEventStarted, 1, ocpp21.TriggerReasonAuthorized)
	for i := 0; i < stopAfterUpdates; i++ {
		send(ocpp21.TransactionEventUpdated, 2+i, ocpp21.TriggerReasonMeterValuePeriodic)
	}

	select {
	case tx := <-st.stopped:
		if tx != "tx-1" {
			t.Fatalf("stop for %q, want tx-1", tx)
		}
	case <-ctx.Done():
		t.Fatal("server never sent RequestStopTransaction")
	}
	send(ocpp21.TransactionEventEnded, 5, ocpp21.TriggerReasonRemoteStop)
}
