// Command edge-proxy runs ai-tally's transparent OpenAI reverse proxy (CTO-39).
//
// Customers set OPENAI_BASE_URL to this proxy's address and add an X-Tenant-Key header; requests
// are forwarded to the real provider unmodified. See internal/proxy for the design invariants.
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/config"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/proxy"
)

func main() {
	cfg, err := config.FromEnv(os.Getenv)
	if err != nil {
		log.Fatalf("edge-proxy: config error: %v", err)
	}

	p := proxy.New(cfg)

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	// Everything else is proxied. /healthz is the one path the proxy owns; it's a liveness probe,
	// not a real provider route, so there's no collision with the OpenAI API surface.
	mux.Handle("/", p)

	srv := &http.Server{
		Addr:    cfg.ListenAddr,
		Handler: mux,
		// No WriteTimeout: streaming completions can legitimately run for minutes. ReadHeader and
		// Idle timeouts still protect against slowloris-style stalls.
		ReadHeaderTimeout: 15 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	go func() {
		log.Printf("edge-proxy: forwarding %s -> %s", cfg.ListenAddr, cfg.Upstream)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("edge-proxy: serve error: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("edge-proxy: graceful shutdown failed: %v", err)
	}
	log.Printf("edge-proxy: stopped")
}
