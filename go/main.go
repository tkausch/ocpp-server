// Command csms is a minimal OCPP 2.1 central system (CSMS), a Go port of ../csms.py.
//
// It listens on ws://0.0.0.0:9000/<charge_point_id> and answers Boot, Heartbeat,
// Status, Authorize, MeterValues and TransactionEvent. After the third Updated
// TransactionEvent of a transaction it asks the station to stop it.
package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/otfabric/go-ocpp"
	"github.com/otfabric/go-ocpp/ocpp21"
	"github.com/otfabric/go-ocpp/server"
)

const stopAfterUpdates = 3 // remotely stop a transaction after this many Updated events

// csms implements the handlers for the CALLs a charging station sends.
type csms struct {
	ocpp21.UnimplementedCSMSHandler
	log *slog.Logger

	mu      sync.Mutex
	updates map[string]int // transaction id -> Updated events seen
}

func newCSMS(log *slog.Logger) *csms {
	return &csms{log: log, updates: map[string]int{}}
}

func (h *csms) BootNotification(_ context.Context, p ocpp.Peer, req *ocpp21.BootNotificationRequest) (*ocpp21.BootNotificationResponse, error) {
	h.log.Info("Boot", "cp", p.ID(), "station", req.ChargingStation, "reason", req.Reason)
	return &ocpp21.BootNotificationResponse{
		Status:      ocpp21.RegistrationStatusAccepted,
		CurrentTime: time.Now().UTC(),
		Interval:    10,
	}, nil
}

func (h *csms) Heartbeat(_ context.Context, p ocpp.Peer, _ *ocpp21.HeartbeatRequest) (*ocpp21.HeartbeatResponse, error) {
	h.log.Info("Heartbeat", "cp", p.ID())
	return &ocpp21.HeartbeatResponse{CurrentTime: time.Now().UTC()}, nil
}

func (h *csms) StatusNotification(_ context.Context, p ocpp.Peer, req *ocpp21.StatusNotificationRequest) (*ocpp21.StatusNotificationResponse, error) {
	h.log.Info("Status", "cp", p.ID(), "evse", req.EVSEID, "connector", req.ConnectorID, "status", req.ConnectorStatus)
	return &ocpp21.StatusNotificationResponse{}, nil
}

func (h *csms) Authorize(_ context.Context, p ocpp.Peer, req *ocpp21.AuthorizeRequest) (*ocpp21.AuthorizeResponse, error) {
	h.log.Info("Authorize", "cp", p.ID(), "idToken", req.IDToken.IDToken)
	status := ocpp21.AuthorizationStatusAccepted
	if req.IDToken.IDToken == "BAD" {
		status = ocpp21.AuthorizationStatusBlocked
	}
	return &ocpp21.AuthorizeResponse{IDTokenInfo: ocpp21.IDTokenInfoType{Status: status}}, nil
}

func (h *csms) MeterValues(_ context.Context, _ ocpp.Peer, _ *ocpp21.MeterValuesRequest) (*ocpp21.MeterValuesResponse, error) {
	return &ocpp21.MeterValuesResponse{}, nil
}

func (h *csms) TransactionEvent(_ context.Context, p ocpp.Peer, req *ocpp21.TransactionEventRequest) (*ocpp21.TransactionEventResponse, error) {
	tx := req.TransactionInfo.TransactionID
	h.log.Info("TransactionEvent", "cp", p.ID(), "event", req.EventType, "tx", tx, "seq", req.SeqNo, "trigger", req.TriggerReason)

	h.mu.Lock()
	stop := false
	switch req.EventType {
	case ocpp21.TransactionEventUpdated:
		h.updates[tx]++
		stop = h.updates[tx] == stopAfterUpdates
	case ocpp21.TransactionEventEnded:
		delete(h.updates, tx)
	}
	h.mu.Unlock()

	if stop {
		// Do not call the station from inside its own handler: respond first.
		go h.stop(p, tx)
	}
	return &ocpp21.TransactionEventResponse{}, nil
}

func (h *csms) stop(p ocpp.Peer, tx string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	res, err := ocpp21.RequestStopTransaction(ctx, p, &ocpp21.RequestStopTransactionRequest{TransactionID: tx})
	if err != nil {
		h.log.Error("RequestStopTransaction failed", "cp", p.ID(), "err", err)
		return
	}
	h.log.Info("RequestStopTransaction", "cp", p.ID(), "status", res.Status)
}

// remoteStart asks a connected station to start a transaction.
func remoteStart(ctx context.Context, srv *server.Server, cpID, token string, remoteStartID, evseID int) (*ocpp21.RequestStartTransactionResponse, error) {
	s, ok := srv.Station(cpID)
	if !ok {
		return nil, errors.New("station not connected: " + cpID)
	}
	return ocpp21.RequestStartTransaction(ctx, s, &ocpp21.RequestStartTransactionRequest{
		IDToken:       ocpp21.IDTokenType{IDToken: token, Type: "Central"},
		RemoteStartID: remoteStartID,
		EVSEID:        &evseID,
	})
}

func newServer(log *slog.Logger) (*server.Server, error) {
	return server.New(
		server.WithHandler(ocpp.V21, ocpp21.CSMS(newCSMS(log))),
		server.WithoutAuthentication(), // simulator: plain ws://, no basic auth
		server.WithLogger(log),
		server.WithSessionHandler(
			func(s *server.Session) { log.Info("connected", "cp", s.ID(), "version", s.Version()) },
			func(s *server.Session, err error) { log.Info("disconnected", "cp", s.ID(), "err", err) },
		),
	)
}

func main() {
	addr := flag.String("addr", ":9000", "listen address")
	flag.Parse()
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))

	srv, err := newServer(log)
	if err != nil {
		log.Error("server init failed", "err", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
	}()

	log.Info("CSMS listening", "url", "ws://"+*addr+"/<charge_point_id>")
	if err := srv.ListenAndServe(*addr); err != nil && !errors.Is(err, ocpp.ErrClosed) {
		log.Error("serve failed", "err", err)
		os.Exit(1)
	}
}
