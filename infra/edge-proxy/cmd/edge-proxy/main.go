// SPDX-License-Identifier: Apache-2.0
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
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/edgekeys"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/keybroker"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/proxy"
	"github.com/jain-aanchal/ai-tally/infra/edge-proxy/internal/telemetry"
)

// telemetryReportInterval is how often the sink summarizes shed records. A minute is frequent
// enough to notice a broken pipeline in the first alert window and quiet enough that a healthy
// proxy (which logs nothing, since the report is silent when nothing was shed) stays quiet.
const telemetryReportInterval = time.Minute

func main() {
	cfg, err := config.FromEnv(os.Getenv)
	if err != nil {
		log.Fatalf("edge-proxy: config error: %v", err)
	}

	// A config can parse cleanly and still ship nothing (telemetry with no usable credential). Say so
	// loudly at boot rather than logging a destination the proxy will never successfully write to.
	for _, w := range cfg.Warnings() {
		log.Printf("edge-proxy: WARNING: %s", w)
	}

	var opts []proxy.Option

	// Key-broker mode (CTO-43): load the customer's KMS export and mint short-lived tokens so the
	// provider key never reaches their application code.
	if cfg.Mode == config.ModeBroker {
		broker, err := keybroker.LoadStaticBroker(cfg.BrokerFile, cfg.BrokerTTL)
		if err != nil {
			log.Fatalf("edge-proxy: key broker: %v", err)
		}
		opts = append(opts, proxy.WithBroker(broker))
		log.Printf("edge-proxy: key-broker mode (ttl=%s)", cfg.BrokerTTL)
	}

	// Telemetry shipping: a self-hosted proxy emits the same metadata-only records as the cloud
	// proxy, labeled by deployment. Records go to the gateway's POST /v1/batches as single-span
	// batches, authenticated per record by the presented tenant key (Initiative 2 sec 6.3 / sec 8).
	// Empty URL keeps the CTO-39 NopSink (no telemetry).
	var sink *telemetry.HTTPSink
	if cfg.TelemetryURL != "" {
		dep := telemetry.DeploymentCloud
		if cfg.SelfHosted {
			dep = telemetry.DeploymentSelfHost
		}
		sink = telemetry.NewHTTPSink(telemetry.Options{
			URL:         cfg.TelemetryURL,
			Deployment:  dep,
			IngestToken: cfg.IngestToken,
			// The envelope tenant for records the edge-key cache did not resolve. Without it a
			// gateway running with auth disabled has no tenant to attribute the batch to and refuses
			// every one of them.
			TenantId: cfg.TenantId,
			// Shed records are the only evidence of a telemetry pipeline that is failing without
			// erroring (no credential, every span rejected per item). Report them on a cadence so the
			// operator finds out from the proxy's own log instead of from a missing dashboard.
			ReportInterval: telemetryReportInterval,
		})
		opts = append(opts, proxy.WithSink(sink))
		// The tenant claim is logged because it decides where proxied spend lands and it is a UUID,
		// not a secret. "unresolved-only" is not a fallback: it means the envelope claims no tenant.
		tenantClaim := cfg.TenantId
		if tenantClaim == "" {
			tenantClaim = "none (the ingest credential's tenant decides)"
		}
		log.Printf("edge-proxy: telemetry -> %s (deployment=%s, fallback tenant=%s)",
			cfg.TelemetryURL, dep, tenantClaim)
	}

	// Provider-protocol mode (CTO-167): the proxy reads scalar model/usage metadata off responses.
	// Empty is pure pass-through (CTO-39), the byte-identical default.
	if cfg.Provider != "" {
		log.Printf("edge-proxy: provider protocol = %s (response metadata extraction on)", cfg.Provider)
	}

	// Root context for background workers (the edge-key refresher); cancelled on shutdown.
	rootCtx, rootCancel := context.WithCancel(context.Background())
	defer rootCancel()

	// Multi-provider routing (Initiative 2 sec 6.1): log the route table when configured.
	if len(cfg.Routes) > 0 {
		log.Printf("edge-proxy: multi-provider routing (mode=%s, %d routes)", cfg.RouteMode, len(cfg.Routes))
		for _, rt := range cfg.Routes {
			log.Printf("edge-proxy:   route %s -> %s (provider=%q)", rt.Match, rt.Upstream, rt.Provider)
		}
	}

	// Fast key-to-tenant resolution (Initiative 2 sec 6.2): build the in-memory key cache and start
	// its delta-sync refresher. Empty KeysURL keeps the CTO-39 core (no resolution, no 403).
	if cfg.KeysURL != "" {
		cache := edgekeys.New(edgekeys.Options{
			URL:          cfg.KeysURL,
			ServiceToken: cfg.ServiceToken,
			Interval:     cfg.KeysRefreshInterval,
		})
		// Block on the first sync (bounded retry) before serving so a transient boot-time feed error
		// does not leave the cache empty and reject all traffic. With RequireTenant set an empty cache
		// fails every request closed, so a persistent failure is fatal rather than silently 403-ing
		// everything; in open mode we log and proceed (Run keeps retrying), since an empty cache there
		// only means requests carry no TenantId.
		if err := cache.InitialSync(rootCtx, 5, 2*time.Second); err != nil {
			if cfg.RequireTenant {
				log.Fatalf("edge-proxy: edge key cache initial sync failed with RequireTenant set: %v", err)
			}
			log.Printf("edge-proxy: edge key cache initial sync failed, continuing in open mode: %v", err)
		}
		go cache.Run(rootCtx)
		opts = append(opts, proxy.WithKeyResolver(cache))
		log.Printf("edge-proxy: edge key cache <- %s (refresh=%s)", cfg.KeysURL, cfg.KeysRefreshInterval)
	}

	p := proxy.New(cfg, opts...)

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
	if sink != nil {
		// Flush buffered telemetry, then report anything that was shed: a proxy that shipped nothing
		// at all must say so before it exits.
		sink.Close()
	}
	log.Printf("edge-proxy: stopped")
}
