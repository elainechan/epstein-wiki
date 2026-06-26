// scripts/download.go
//
// Downloads all DOJ Epstein disclosure files across all four sections:
//   - EFTA datasets 1-12  (data-set-N-files)
//   - Court Records       (47 case subpages)
//   - FOIA                (4 agency subpages)
//   - Prior DOJ           (4 category subpages)
//
// Architecture:
//   1. Scrape index page → discover all subpage URLs per section
//   2. Scrape each subpage → collect PDF links
//   3. Download PDFs concurrently, checkpoint after every file
//
// Output layout:
//   <out>/
//     efta/
//       data-set-1-files/   .checkpoint.json  *.pdf
//       data-set-2-files/
//       ...
//     court-records/
//       doe-17-v-indyke/    .checkpoint.json  *.pdf
//       ...
//     foia/
//       fbi/                .checkpoint.json  *.pdf
//       ...
//     prior-doj/
//       maxwell-proffer/    .checkpoint.json  *.pdf
//       ...
//     efta.zip  court-records.zip  foia.zip  prior-doj.zip
//
// Build:  go build -o download scripts/download.go
// Run:    ./download -out /path/to/raw
//
// Flags:
//   -out            output root directory          (default: raw)
//   -sections       "efta,court,foia,prior" or "all"  (default: all)
//   -workers        concurrent subpage workers     (default: 12)
//   -file-workers   per-subpage file goroutines    (default: 8)
//   -delay          ms between file requests       (default: 300)
//   -retries        per-file retry attempts        (default: 3)
//   -no-zip         skip zipping completed sections (default: false)
//   -reset          ignore checkpoints, start fresh (default: false)

package main

import (
	"archive/zip"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	indexURL      = "https://www.justice.gov/epstein/doj-disclosures"
	baseURL       = "https://www.justice.gov"
	userAgent     = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
	checkpointFile = ".checkpoint.json"
)

// ── CLI flags ─────────────────────────────────────────────────────────

var (
	flagOut         = flag.String("out", "raw", "Output root directory")
	flagSections    = flag.String("sections", "all", `Sections to download: "all" or comma-separated: "efta,court,foia,prior"`)
	flagWorkers     = flag.Int("workers", 12, "Concurrent subpage workers")
	flagFileWorkers = flag.Int("file-workers", 8, "Per-subpage file-download goroutines")
	flagDelay       = flag.Int("delay", 300, "Polite delay between file requests (ms)")
	flagRetries     = flag.Int("retries", 5, "Per-file retry attempts on transient errors (incl. false-404s)")
	flagPageDelay   = flag.Int("page-delay", 1000, "Polite delay between subpage scrapes (ms)")
	flagNoZip       = flag.Bool("no-zip", false, "Skip zipping completed sections")
	flagReset       = flag.Bool("reset", false, "Ignore existing checkpoints, start fresh")
	flagDebug       = flag.Bool("debug", false, "Dump raw index page HTML to index-debug.html for inspection")
	flagLog         = flag.String("log", "download.log", "Path to log file (appended); set to '' to disable")
	flagSkipIndex   = flag.Bool("skip-index", false, "Skip index page scrape; use hardcoded subpage URLs for the selected sections")
	flagCookie      = flag.String("cookie", "", "Cookie header value to send with every request (paste from browser DevTools after clicking Yes on the age gate)")
)

// ── Section / subpage model ───────────────────────────────────────────

// section is one of the four top-level groupings on the index page.
type section struct {
	key     string // "efta" | "court" | "foia" | "prior"
	label   string // human name for logs
	dir     string // subdirectory under out root
	subURLs []string // subpage URLs to scrape for PDFs
}

// subpage is one entry within a section (e.g. one court case, one dataset).
type subpage struct {
	section string // section key
	label   string // human label for logs
	slug    string // directory name under section dir
	url     string // URL to scrape for PDFs
}

// ── Checkpoint ────────────────────────────────────────────────────────

type checkpoint struct {
	Subpage    string         `json:"subpage"`
	Links      []string       `json:"links"`
	Completed  map[int]string `json:"completed"`
	LastOK     int            `json:"last_ok"`
	LastOKFile string         `json:"last_ok_file"`
	SavedAt    time.Time      `json:"saved_at"`
}

func cpPath(dir string) string { return filepath.Join(dir, checkpointFile) }

func loadCP(dir string) (*checkpoint, bool) {
	data, err := os.ReadFile(cpPath(dir))
	if err != nil {
		return nil, false
	}
	var cp checkpoint
	if err := json.Unmarshal(data, &cp); err != nil {
		return nil, false
	}
	if cp.Completed == nil {
		cp.Completed = map[int]string{}
	}
	return &cp, true
}

func saveCP(dir string, cp *checkpoint) error {
	cp.SavedAt = time.Now()
	// Recompute LastOK
	cp.LastOK = -1
	cp.LastOKFile = ""
	for pos, fname := range cp.Completed {
		if pos > cp.LastOK {
			cp.LastOK = pos
			cp.LastOKFile = fname
		}
	}
	data, err := json.MarshalIndent(cp, "", "  ")
	if err != nil {
		return err
	}
	tmp := cpPath(dir) + ".tmp"
	if err := os.WriteFile(tmp, data, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, cpPath(dir))
}

type cpState struct {
	mu  sync.Mutex
	cp  *checkpoint
	dir string
}

func newCPState(dir string, cp *checkpoint) *cpState { return &cpState{cp: cp, dir: dir} }

func (s *cpState) markDone(pos int, filename string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cp.Completed[pos] = filename
	_ = saveCP(s.dir, s.cp)
}

func (s *cpState) isDone(pos int) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, ok := s.cp.Completed[pos]
	return ok
}

func (s *cpState) flush() {
	s.mu.Lock()
	defer s.mu.Unlock()
	_ = saveCP(s.dir, s.cp)
}

func (s *cpState) summary() (lastPos int, lastFile string, total int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cp.LastOK, s.cp.LastOKFile, len(s.cp.Completed)
}

// ── Error types ───────────────────────────────────────────────────────

type dlError struct {
	filename string
	cause    string
	hint     string
	wrapped  error
}

func (e *dlError) Error() string {
	return fmt.Sprintf("%s — %s\n         hint: %s\n         detail: %v",
		e.filename, e.cause, e.hint, e.wrapped)
}
func (e *dlError) Unwrap() error { return e.wrapped }

func httpErr(filename, rawURL string, status int) *dlError {
	cause, hint := httpCauseHint(status)
	return &dlError{filename: filename, cause: cause, hint: hint,
		wrapped: fmt.Errorf("HTTP %d from %s", status, rawURL)}
}

func httpCauseHint(status int) (cause, hint string) {
	switch status {
	case 403:
		return "access forbidden (HTTP 403)", "try -delay 1000 -workers 4"
	case 404:
		return "not found (HTTP 404)", "URL may no longer exist on justice.gov"
	case 429:
		return "rate limited (HTTP 429)", "rerun with -delay 1000 -workers 4"
	case 500, 502, 503, 504:
		return fmt.Sprintf("server error (HTTP %d)", status), "will retry; if persistent, wait and rerun"
	default:
		return fmt.Sprintf("unexpected HTTP %d", status), "check the URL manually"
	}
}

func netErr(filename string, err error) *dlError {
	s := err.Error()
	var cause, hint string
	switch {
	case strings.Contains(s, "connection refused"):
		cause, hint = "connection refused", "server refused — check URL or try again later"
	case strings.Contains(s, "no such host"):
		cause, hint = "DNS failed", "check your internet connection"
	case strings.Contains(s, "timeout"), strings.Contains(s, "deadline exceeded"):
		cause, hint = "timed out", "will retry; try -delay 500 if persistent"
	case strings.Contains(s, "connection reset"):
		cause, hint = "connection reset", "will retry; try -delay 1000"
	case strings.Contains(s, "EOF"):
		cause, hint = "connection closed mid-transfer", "will retry"
	default:
		cause, hint = "network error", "check your internet connection"
	}
	return &dlError{filename: filename, cause: cause, hint: hint, wrapped: err}
}

func isTransient(err error) bool {
	var de *dlError
	if !errors.As(err, &de) {
		return false
	}
	s := de.wrapped.Error()
	return strings.Contains(s, "429") || strings.Contains(s, "500") ||
		strings.Contains(s, "502") || strings.Contains(s, "503") ||
		strings.Contains(s, "504") || strings.Contains(s, "timeout") ||
		strings.Contains(s, "deadline") || strings.Contains(s, "reset") ||
		strings.Contains(s, "EOF") || strings.Contains(s, "404")
	// NOTE: justice.gov has been observed returning HTTP 404 for files that
	// genuinely exist (confirmed: same URL succeeded in one run, 404'd in
	// the next, succeeded again in a third run). This is almost certainly
	// bot-detection/rate-limiting disguised as a 404 rather than a real
	// missing file. We therefore treat 404 as retryable like any other
	// transient error. A file is only logged to missing.log after ALL
	// retries — including the final-confirmation pass — still return 404.
}

// ── HTTP client ───────────────────────────────────────────────────────

var client = &http.Client{
	Timeout: 90 * time.Second,
	Transport: &http.Transport{
		MaxIdleConns:        200,
		MaxIdleConnsPerHost: 50,
		IdleConnTimeout:     90 * time.Second,
	},
}

func get(rawURL string) (*http.Response, error) {
	req, _ := http.NewRequest("GET", rawURL, nil)
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("Connection", "keep-alive")
	req.Header.Set("Upgrade-Insecure-Requests", "1")
	if *flagCookie != "" {
		req.Header.Set("Cookie", *flagCookie)
	}
	// Deliberately omit Accept-Encoding — Go's http client handles
	// transparent gzip decompression when we don't set this manually.
	return client.Do(req)
}

// ── Scraping ──────────────────────────────────────────────────────────

var (
	hrefRe = regexp.MustCompile(`(?i)href="(/epstein/doj-disclosures/[^"]+)"`)
	pdfRe  = regexp.MustCompile(`(?i)href="([^"]*\.pdf)"`)
)

// scrapeIndex fetches the main page and returns discovered sections.
func scrapeIndex(wantSections map[string]bool) ([]subpage, error) {
	maxAttempts := 4
	var lastErr error

	for attempt := 1; attempt <= maxAttempts; attempt++ {
		if attempt > 1 {
			wait := time.Duration(attempt*attempt) * 5 * time.Second
			fmt.Fprintf(os.Stderr, "  attempt %d/%d failed (%v) — waiting %v before retry ...\n",
				attempt-1, maxAttempts, lastErr, wait)
			// Print countdown every 5s so it doesn't look frozen
			deadline := time.Now().Add(wait)
			for time.Now().Before(deadline) {
				remaining := time.Until(deadline).Round(time.Second)
				fmt.Printf("\r  retrying in %v ...   ", remaining)
				time.Sleep(5 * time.Second)
			}
			fmt.Println()
		}

		fmt.Printf("  fetching %s ...\n", indexURL)
		resp, err := get(indexURL)
		if err != nil {
			lastErr = fmt.Errorf("network error: %w", err)
			continue
		}

		fmt.Printf("  HTTP %d\n", resp.StatusCode)

		switch resp.StatusCode {
		case 200:
			body, err := io.ReadAll(resp.Body)
			resp.Body.Close()
			if err != nil {
				lastErr = fmt.Errorf("read body: %w", err)
				continue
			}
			fmt.Printf("  body size: %d bytes\n", len(body))

			// Debug: dump raw HTML
			if *flagDebug {
				debugPath := "index-debug.html"
				_ = os.WriteFile(debugPath, body, 0644)
				fmt.Printf("  DEBUG  wrote %d bytes to %s\n", len(body), debugPath)
				allHrefs := regexp.MustCompile(`href="([^"]+)"`).FindAllSubmatch(body, -1)
				fmt.Printf("  DEBUG  total href= attributes: %d\n", len(allHrefs))
				epsteinHrefs := 0
				for _, m := range allHrefs {
					if strings.Contains(string(m[1]), "epstein") {
						epsteinHrefs++
						if epsteinHrefs <= 5 {
							fmt.Printf("  DEBUG  epstein href: %s\n", string(m[1]))
						}
					}
				}
				fmt.Printf("  DEBUG  total epstein hrefs: %d\n", epsteinHrefs)
			}

			seen := map[string]bool{}
			var pages []subpage
			for _, m := range hrefRe.FindAllSubmatch(body, -1) {
				path := string(m[1])
				if seen[path] {
					continue
				}
				seen[path] = true
				sp := classifySubpage(path)
				if sp == nil {
					continue
				}
				if !wantSections["all"] && !wantSections[sp.section] {
					continue
				}
				pages = append(pages, *sp)
			}
			if len(pages) == 0 {
				allMatches := hrefRe.FindAllSubmatch(body, -1)
				fmt.Fprintf(os.Stderr, "  DIAG  hrefRe matched %d paths, 0 classified\n", len(allMatches))
				fmt.Fprintf(os.Stderr, "  DIAG  run with -debug to dump full HTML to index-debug.html\n")
				lastErr = fmt.Errorf("page loaded (%d bytes) but 0 subpage links matched — possible bot-gate or structure change", len(body))
				continue // retry — may be a transient bot-detection page
			}
			return pages, nil

		case 403, 429:
			resp.Body.Close()
			lastErr = fmt.Errorf("HTTP %d — justice.gov is rate-limiting or blocking", resp.StatusCode)
			continue

		default:
			resp.Body.Close()
			return nil, fmt.Errorf("HTTP %d — unexpected status", resp.StatusCode)
		}
	}
	return nil, fmt.Errorf("all %d attempts failed: %w\n  hint: use -skip-index with hardcoded URLs to bypass index scraping", maxAttempts, lastErr)
}

// hardcodedSubpages returns the known subpage list without hitting the index page.
// Use with -skip-index when justice.gov blocks the index scrape.
// URLs harvested from the index page on 2026-06-19.
func hardcodedSubpages(wantSections map[string]bool) []subpage {
	type entry struct{ path, section, slug string }
	all := []entry{
		// EFTA datasets 1-12
		{"/epstein/doj-disclosures/data-set-1-files", "efta", "data-set-1-files"},
		{"/epstein/doj-disclosures/data-set-2-files", "efta", "data-set-2-files"},
		{"/epstein/doj-disclosures/data-set-3-files", "efta", "data-set-3-files"},
		{"/epstein/doj-disclosures/data-set-4-files", "efta", "data-set-4-files"},
		{"/epstein/doj-disclosures/data-set-5-files", "efta", "data-set-5-files"},
		{"/epstein/doj-disclosures/data-set-6-files", "efta", "data-set-6-files"},
		{"/epstein/doj-disclosures/data-set-7-files", "efta", "data-set-7-files"},
		{"/epstein/doj-disclosures/data-set-8-files", "efta", "data-set-8-files"},
		{"/epstein/doj-disclosures/data-set-9-files", "efta", "data-set-9-files"},
		{"/epstein/doj-disclosures/data-set-10-files", "efta", "data-set-10-files"},
		{"/epstein/doj-disclosures/data-set-11-files", "efta", "data-set-11-files"},
		{"/epstein/doj-disclosures/data-set-12-files", "efta", "data-set-12-files"},
		// Court Records
		{"/epstein/doj-disclosures/court-records-ca-florida-holdings-llc-publisher-palm-beach-post-v-aronberg-no-50-2019-ca-014681-xxxx-mb", "court", "ca-florida-holdings-llc-publisher-palm-beach-post-v-aronberg-no-50-2019-ca-014681-xxxx-mb"},
		{"/epstein/doj-disclosures/court-records-doe-17-v-indyke-no-119-cv-09610-sdny-2019", "court", "doe-17-v-indyke-no-119-cv-09610-sdny-2019"},
		{"/epstein/doj-disclosures/court-records-doe-1000-v-indyke-no-119-cv-10577-sdny-2019", "court", "doe-1000-v-indyke-no-119-cv-10577-sdny-2019"},
		{"/epstein/doj-disclosures/court-records-doe-no-3-v-epstein-no-908-cv-80232-sd-fla-2008", "court", "doe-no-3-v-epstein-no-908-cv-80232-sd-fla-2008"},
		{"/epstein/doj-disclosures/court-records-doe-no-4-v-epstein-no-908-cv-80380-sd-fla-2008", "court", "doe-no-4-v-epstein-no-908-cv-80380-sd-fla-2008"},
		{"/epstein/doj-disclosures/court-records-doe-no-5-v-epstein-no-908-cv-80381-sd-fla-2008", "court", "doe-no-5-v-epstein-no-908-cv-80381-sd-fla-2008"},
		{"/epstein/doj-disclosures/court-records-doe-no-6-v-epstein-no-908-cv-80994-sd-fla-2008", "court", "doe-no-6-v-epstein-no-908-cv-80994-sd-fla-2008"},
		{"/epstein/doj-disclosures/court-records-doe-no-8-v-epstein-no-909-cv-80802-sd-fla-2009", "court", "doe-no-8-v-epstein-no-909-cv-80802-sd-fla-2009"},
		{"/epstein/doj-disclosures/court-records-doe-no-101-v-epstein-no-909-cv-80591-sd-fla-2009", "court", "doe-no-101-v-epstein-no-909-cv-80591-sd-fla-2009"},
		{"/epstein/doj-disclosures/court-records-doe-no-102-v-epstein-no-909-cv-80656-sd-fla-2009", "court", "doe-no-102-v-epstein-no-909-cv-80656-sd-fla-2009"},
		{"/epstein/doj-disclosures/court-records-doe-no-103-v-epstein-no-910-cv-80309-sd-fla-2010", "court", "doe-no-103-v-epstein-no-910-cv-80309-sd-fla-2010"},
		{"/epstein/doj-disclosures/court-records-doe-v-epstein-no-908-cv-80069-sd-fla-2008", "court", "doe-v-epstein-no-908-cv-80069-sd-fla-2008"},
		{"/epstein/doj-disclosures/court-records-doe-v-epstein-no-908-cv-80119-sd-fla-2008", "court", "doe-v-epstein-no-908-cv-80119-sd-fla-2008"},
		{"/epstein/doj-disclosures/court-records-doe-v-epstein-no-908-cv-80804-sd-fla-2008", "court", "doe-v-epstein-no-908-cv-80804-sd-fla-2008"},
		{"/epstein/doj-disclosures/court-records-doe-v-epstein-no-909-v-80469-sd-fla-2009", "court", "doe-v-epstein-no-909-v-80469-sd-fla-2009"},
		{"/epstein/doj-disclosures/court-records-doe-v-indyke-no-119-cv-08673-sdny-2019", "court", "doe-v-indyke-no-119-cv-08673-sdny-2019"},
		{"/epstein/doj-disclosures/court-records-doe-v-indyke-no-119-cv-11869-sdny-2019", "court", "doe-v-indyke-no-119-cv-11869-sdny-2019"},
		{"/epstein/doj-disclosures/court-records-doe-v-indyke-no-120-cv-00484-sdny-2020", "court", "doe-v-indyke-no-120-cv-00484-sdny-2020"},
		{"/epstein/doj-disclosures/court-records-doe-v-indyke-no-120-cv-02365-sdny-2020", "court", "doe-v-indyke-no-120-cv-02365-sdny-2020"},
		{"/epstein/doj-disclosures/court-records-doe-v-united-states-no-908-cv-80736-sd-fla-2008", "court", "doe-v-united-states-no-908-cv-80736-sd-fla-2008"},
		{"/epstein/doj-disclosures/court-records-epstein-v-no-sc15-2286-fla-sup-ct-2015", "court", "epstein-v-no-sc15-2286-fla-sup-ct-2015"},
		{"/epstein/doj-disclosures/court-records-epstein-v-rothstein-no-50-2009-ca-040800-xxxx-mb-fla-15th-cir-ct-2009", "court", "epstein-v-rothstein-no-50-2009-ca-040800-xxxx-mb-fla-15th-cir-ct-2009"},
		{"/epstein/doj-disclosures/court-records-government-united-states-virgin-islands-v-jpmorgan-chase-bank-na-no-122-cv-10904-sdny-2022", "court", "government-united-states-virgin-islands-v-jpmorgan-chase-bank-na-no-122-cv-10904-sdny-2022"},
		{"/epstein/doj-disclosures/court-records-re-grand-jury-05-02-wpb-07-103-wpb-no-925-mc-80920-sd-fla-2025", "court", "re-grand-jury-05-02-wpb-07-103-wpb-no-925-mc-80920-sd-fla-2025"},
		{"/epstein/doj-disclosures/court-records-jane-doe-43-v-epstein-no-117-cv-00616-sdny-2017", "court", "jane-doe-43-v-epstein-no-117-cv-00616-sdny-2017"},
		{"/epstein/doj-disclosures/court-records-matter-estate-jeffrey-e-epstein-deceased-no-st-21-rv-00005-vi-super-ct-2021", "court", "matter-estate-jeffrey-e-epstein-deceased-no-st-21-rv-00005-vi-super-ct-2021"},
		{"/epstein/doj-disclosures/court-records-maxwell-v-estate-jeffrey-epstein-no-st-20-cv-155-vi-super-ct-2020", "court", "maxwell-v-estate-jeffrey-epstein-no-st-20-cv-155-vi-super-ct-2020"},
		{"/epstein/doj-disclosures/court-records-maxwell-v-united-states-no-24-1073-us-2025-petition-cert", "court", "maxwell-v-united-states-no-24-1073-us-2025-petition-cert"},
		{"/epstein/doj-disclosures/court-records-operating-engineers-construction-industry-and-miscellaneous-pension-fund-v-dimon-no-123-cv-03903", "court", "operating-engineers-construction-industry-and-miscellaneous-pension-fund-v-dimon-no-123-cv-03903"},
		{"/epstein/doj-disclosures/court-records-v-epstein-909-cv-81092-sd-fla-2009", "court", "v-epstein-909-cv-81092-sd-fla-2009"},
		{"/epstein/doj-disclosures/court-records-v-epstein-no-910-cv-80447-sd-fla-2010", "court", "v-epstein-no-910-cv-80447-sd-fla-2010"},
		{"/epstein/doj-disclosures/court-records-v-epstein-no-910-cv-81111-sd-fla-2010", "court", "v-epstein-no-910-cv-81111-sd-fla-2010"},
		{"/epstein/doj-disclosures/court-records-v-indyke-no-119-cv-10474-sdny-2019", "court", "v-indyke-no-119-cv-10474-sdny-2019"},
		{"/epstein/doj-disclosures/court-records-v-indyke-no-119-cv-10475-sdny-2019", "court", "v-indyke-no-119-cv-10475-sdny-2019"},
		{"/epstein/doj-disclosures/court-records-v-indyke-no-119-cv-10476-sdny-2019", "court", "v-indyke-no-119-cv-10476-sdny-2019"},
		{"/epstein/doj-disclosures/court-records-v-indyke-no-119-cv-10479-sdny-2019", "court", "v-indyke-no-119-cv-10479-sdny-2019"},
		{"/epstein/doj-disclosures/court-records-v-indyke-no-119-cv-10788-sdny-2019", "court", "v-indyke-no-119-cv-10788-sdny-2019"},
		{"/epstein/doj-disclosures/court-records-v-maxwell-no-115-cv-07433-sdny-2015", "court", "v-maxwell-no-115-cv-07433-sdny-2015"},
		{"/epstein/doj-disclosures/court-records-v-maxwell-no-117-mc-00025-sdny-2016", "court", "v-maxwell-no-117-mc-00025-sdny-2016"},
		{"/epstein/doj-disclosures/court-records-v-nine-east-71st-street-no-119-cv-07625-sdny-2019", "court", "v-nine-east-71st-street-no-119-cv-07625-sdny-2019"},
		{"/epstein/doj-disclosures/court-records-state-florida-v-epstein-no-50-2006-cf-009454-axxx-mb-fla-15th-cir-ct-2006", "court", "state-florida-v-epstein-no-50-2006-cf-009454-axxx-mb-fla-15th-cir-ct-2006"},
		{"/epstein/doj-disclosures/court-records-state-florida-v-epstein-no-50-2008-cf-009381-axxx-mb-fla-15th-cir-ct-2008", "court", "state-florida-v-epstein-no-50-2008-cf-009381-axxx-mb-fla-15th-cir-ct-2008"},
		{"/epstein/doj-disclosures/court-records-united-states-v-epstein-no-119-cr-00490-sdny-2019", "court", "united-states-v-epstein-no-119-cr-00490-sdny-2019"},
		{"/epstein/doj-disclosures/court-records-united-states-v-epstein-no-19-2221-2d-cir-2019", "court", "united-states-v-epstein-no-19-2221-2d-cir-2019"},
		{"/epstein/doj-disclosures/court-records-united-states-v-maxwell-no-120-cr-00330-sdny-2020", "court", "united-states-v-maxwell-no-120-cr-00330-sdny-2020"},
		{"/epstein/doj-disclosures/court-records-united-states-v-maxwell-no-120-mj-00132-dnh-2020", "court", "united-states-v-maxwell-no-120-mj-00132-dnh-2020"},
		{"/epstein/doj-disclosures/court-records-united-states-v-maxwell-no-20-3061-2d-cir-2020", "court", "united-states-v-maxwell-no-20-3061-2d-cir-2020"},
		{"/epstein/doj-disclosures/court-records-united-states-v-maxwell-no-21-0058-2d-cir-2021", "court", "united-states-v-maxwell-no-21-0058-2d-cir-2021"},
		{"/epstein/doj-disclosures/court-records-united-states-v-maxwell-no-21-0770-2d-cir-2021", "court", "united-states-v-maxwell-no-21-0770-2d-cir-2021"},
		{"/epstein/doj-disclosures/court-records-united-states-v-maxwell-no-22-1426-2d-cir-2022", "court", "united-states-v-maxwell-no-22-1426-2d-cir-2022"},
		{"/epstein/doj-disclosures/court-records-united-states-v-noel-no-119-cr-00830-sdny-2019", "court", "united-states-v-noel-no-119-cr-00830-sdny-2019"},
		// FOIA
		{"/epstein/doj-disclosures/foia-customs-and-border-protection-cbp", "foia", "customs-and-border-protection-cbp"},
		{"/epstein/doj-disclosures/foia-federal-bureau-investigation-fbi", "foia", "federal-bureau-investigation-fbi"},
		{"/epstein/doj-disclosures/foia-federal-bureau-prisons-bop", "foia", "federal-bureau-prisons-bop"},
		{"/epstein/doj-disclosures/foia-florida", "foia", "florida"},
		// Prior DOJ
		{"/epstein/doj-disclosures/first-phase-declassified-epstein-files", "prior", "first-phase-declassified-epstein-files"},
		{"/epstein/doj-disclosures/bop-video-footage", "prior", "bop-video-footage"},
		{"/epstein/doj-disclosures/maxwell-proffer", "prior", "maxwell-proffer"},
		{"/epstein/doj-disclosures/memoranda-and-correspondence", "prior", "memoranda-and-correspondence"},
	}

	var pages []subpage
	for _, e := range all {
		if !wantSections["all"] && !wantSections[e.section] {
			continue
		}
		pages = append(pages, subpage{
			section: e.section,
			slug:    e.slug,
			label:   e.slug,
			url:     baseURL + e.path,
		})
	}
	return pages
}
func classifySubpage(path string) *subpage {
	// Strip the common prefix
	suffix := strings.TrimPrefix(path, "/epstein/doj-disclosures/")
	if suffix == "" || suffix == path {
		return nil
	}

	// Skip the index page itself
	if suffix == "" {
		return nil
	}

	var sec, dir, label string
	switch {
	case strings.HasPrefix(suffix, "data-set-") && strings.HasSuffix(suffix, "-files"):
		sec = "efta"
		dir = "efta"
		// slug: "data-set-1-files" → "data-set-1"
		label = strings.TrimSuffix(suffix, "-files")
		label = strings.ReplaceAll(label, "-", " ")
		label = strings.Title(label)

	case strings.HasPrefix(suffix, "court-records-"):
		sec = "court"
		dir = "court-records"
		label = strings.TrimPrefix(suffix, "court-records-")

	case strings.HasPrefix(suffix, "foia-"):
		sec = "foia"
		dir = "foia"
		label = strings.TrimPrefix(suffix, "foia-")

	case suffix == "first-phase-declassified-epstein-files" ||
		suffix == "bop-video-footage" ||
		suffix == "maxwell-proffer" ||
		suffix == "memoranda-and-correspondence":
		sec = "prior"
		dir = "prior-doj"
		label = suffix

	default:
		return nil
	}

	// slug: last segment of path, trimmed
	slug := suffix
	if strings.HasPrefix(suffix, dir+"-") {
		slug = strings.TrimPrefix(suffix, dir+"-")
	}
	// For EFTA keep the full suffix as slug (data-set-1-files)
	if sec == "efta" {
		slug = suffix
	}

	return &subpage{
		section: sec,
		label:   label,
		slug:    slug,
		url:     baseURL + path,
	}
}

// scrapePDFs fetches a subpage and all its paginated pages, returning all PDF links.
// Pagination pattern: page 0 = bare URL, then ?page=1, ?page=2, ... (Drupal pager).
// We don't rely on detecting a "Next" link in the HTML (fragile across markup
// changes) — instead we keep requesting incrementing pages until a page
// returns zero PDF links, which reliably signals we've gone past the end.
func scrapePDFs(pageURL string, pageDelay int) ([]string, error) {
	seen := map[string]bool{}
	var allLinks []string
	consecutiveEmpty := 0
	const maxConsecutiveEmpty = 2 // require 2 consecutive empty pages to rule out a transient block
	const maxPages = 100          // hard safety cap

	for pageNum := 0; pageNum < maxPages; pageNum++ {
		url := pageURL
		if pageNum > 0 {
			url = fmt.Sprintf("%s?page=%d", pageURL, pageNum)
		}

		links, err := scrapePDFPage(url, pageDelay)
		if err != nil {
			if pageNum == 0 {
				return nil, err // first page failure is fatal
			}
			fmt.Fprintf(os.Stderr, "  WARN  page %d scrape failed (%v) — using %d links from previous pages\n",
				pageNum, err, len(allLinks))
			break
		}

		newLinks := 0
		for _, l := range links {
			if !seen[l] {
				seen[l] = true
				allLinks = append(allLinks, l)
				newLinks++
			}
		}

		if newLinks == 0 {
			consecutiveEmpty++
			if consecutiveEmpty >= maxConsecutiveEmpty {
				break // confirmed past the last page
			}
		} else {
			consecutiveEmpty = 0
			if pageNum > 0 {
				fmt.Printf("         page %d: +%d links (%d total)\n", pageNum, newLinks, len(allLinks))
			}
		}

		if pageDelay > 0 {
			time.Sleep(time.Duration(pageDelay) * time.Millisecond)
		}
	}

	return allLinks, nil
}

// scrapePDFPage fetches one page and returns its PDF links.
func scrapePDFPage(pageURL string, pageDelay int) ([]string, error) {
	maxAttempts := 4
	var lastErr error

	for attempt := 1; attempt <= maxAttempts; attempt++ {
		if attempt > 1 {
			wait := time.Duration(attempt*attempt) * 5 * time.Second
			fmt.Printf("         retrying subpage scrape in %v (attempt %d/%d): %s\n",
				wait, attempt, maxAttempts, pageURL)
			time.Sleep(wait)
		} else if pageDelay > 0 {
			time.Sleep(time.Duration(pageDelay) * time.Millisecond)
		}

		resp, err := get(pageURL)
		if err != nil {
			lastErr = &dlError{filename: pageURL, cause: "could not fetch subpage",
				hint: "check connection or try again", wrapped: err}
			continue
		}

		switch resp.StatusCode {
		case 200:
			body, err := io.ReadAll(resp.Body)
			resp.Body.Close()
			if err != nil {
				lastErr = &dlError{filename: pageURL, cause: "failed to read subpage body",
					hint: "transient network issue", wrapped: err}
				continue
			}

			var links []string
			seenOnPage := map[string]bool{}
			for _, m := range pdfRe.FindAllSubmatch(body, -1) {
				href := string(m[1])
				full := href
				if !strings.HasPrefix(href, "http") {
					base, _ := url.Parse(pageURL)
					ref, _ := url.Parse(href)
					full = base.ResolveReference(ref).String()
				}
				if !seenOnPage[full] {
					seenOnPage[full] = true
					links = append(links, full)
				}
			}
			return links, nil

		case 403, 429:
			resp.Body.Close()
			lastErr = httpErr(pageURL, pageURL, resp.StatusCode)
			continue

		case 404:
			resp.Body.Close()
			// 404 on any page just means we've gone past the last page —
			// treat as empty, not an error
			return nil, nil

		default:
			resp.Body.Close()
			lastErr = httpErr(pageURL, pageURL, resp.StatusCode)
			if resp.StatusCode >= 500 {
				continue
			}
			return nil, lastErr
		}
	}
	return nil, lastErr
}

// missingLog appends a 404'd file URL to a per-subpage missing.log file.
// These are permanent failures (file genuinely gone) — logged separately
// so they don't drown the error stream and can be audited later.
func missingLog(destDir, fileURL string) {
	path := filepath.Join(destDir, "missing.log")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644)
	if err != nil {
		fmt.Fprintf(os.Stderr, "  WARN  could not write missing.log at %s: %v\n", path, err)
		return
	}
	defer f.Close()
	if _, err := fmt.Fprintf(f, "%s\n", fileURL); err != nil {
		fmt.Fprintf(os.Stderr, "  WARN  could not write to missing.log: %v\n", err)
	}
}

// isMissing returns true for permanent HTTP 404 errors.
func isMissing(err error) bool {
	var de *dlError
	return errors.As(err, &de) && strings.Contains(de.wrapped.Error(), "HTTP 404")
}



// ── File download ─────────────────────────────────────────────────────

type fileResult struct {
	pos      int
	status   string
	filename string
	err      error
}

func downloadFile(pos int, rawURL, destDir string, delayMs, maxRetries int) fileResult {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return fileResult{pos, "error", rawURL, &dlError{filename: rawURL,
			cause: "invalid URL", hint: "may be malformed in page HTML", wrapped: err}}
	}
	filename := filepath.Base(parsed.Path)
	if filename == "" || filename == "." {
		filename = fmt.Sprintf("file-%d", pos)
	}
	outPath := filepath.Join(destDir, filename)

	if _, err := os.Stat(outPath); err == nil {
		return fileResult{pos, "skip", filename, nil}
	}

	time.Sleep(time.Duration(delayMs) * time.Millisecond)

	var lastErr error
	for attempt := 1; attempt <= maxRetries; attempt++ {
		lastErr = tryDownload(rawURL, filename, outPath)
		if lastErr == nil {
			return fileResult{pos, "ok", filename, nil}
		}
		if !isTransient(lastErr) {
			break
		}
		if attempt < maxRetries {
			backoff := time.Duration(attempt) * 3 * time.Second
			if strings.Contains(lastErr.Error(), "404") {
				// 404s on this server often mean rate-limited, not missing —
				// give it extra cooldown before retrying
				backoff = time.Duration(attempt) * 6 * time.Second
			}
			fmt.Printf("         retrying %s in %v (attempt %d/%d): %v\n",
				filename, backoff, attempt+1, maxRetries, lastErr)
			time.Sleep(backoff)
		}
	}
	return fileResult{pos, "error", filename, lastErr}
}

func tryDownload(rawURL, filename, outPath string) error {
	resp, err := get(rawURL)
	if err != nil {
		return netErr(filename, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return httpErr(filename, rawURL, resp.StatusCode)
	}

	tmp := outPath + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return &dlError{filename: filename, cause: "cannot create file",
			hint: fmt.Sprintf("check write permissions on %s", filepath.Dir(outPath)), wrapped: err}
	}
	_, copyErr := io.Copy(f, resp.Body)
	closeErr := f.Close()
	if copyErr != nil {
		os.Remove(tmp)
		return &dlError{filename: filename, cause: "write failed",
			hint: "check available disk space", wrapped: copyErr}
	}
	if closeErr != nil {
		os.Remove(tmp)
		return &dlError{filename: filename, cause: "close failed",
			hint: "check available disk space", wrapped: closeErr}
	}
	if err := os.Rename(tmp, outPath); err != nil {
		os.Remove(tmp)
		return &dlError{filename: filename, cause: "rename failed",
			hint: fmt.Sprintf("check permissions on %s", filepath.Dir(outPath)), wrapped: err}
	}
	return nil
}

// ── Subpage worker ────────────────────────────────────────────────────

type subpageResult struct {
	sp         subpage
	found      int
	downloaded int32
	skipped    int32
	errors     int32
}

// processSubpageWithLinks runs downloads for a subpage whose PDF links
// have already been scraped. Uses logWriter for tee'd output.
func processSubpageWithLinks(sp subpage, links []string, outRoot string,
	fileWorkers, delayMs, retries int, reset bool, logWriter io.Writer) subpageResult {

	res := subpageResult{sp: sp, found: len(links)}
	tag := fmt.Sprintf("[%s/%s]", sp.section, sp.slug)
	log := func(format string, args ...any) { fmt.Fprintf(logWriter, format, args...) }
	logErr := func(format string, args ...any) {
		msg := fmt.Sprintf(format, args...)
		fmt.Fprint(os.Stderr, msg)
		if logWriter != os.Stdout {
			fmt.Fprint(logWriter, msg)
		}
	}

	destDir := filepath.Join(outRoot, sectionDir(sp.section), sp.slug)
	if err := os.MkdirAll(destDir, 0755); err != nil {
		logErr("%s ERROR  cannot create dir: %v\n", tag, err)
		return res
	}

	// Load checkpoint
	var cp *checkpoint
	if !reset {
		if existing, ok := loadCP(destDir); ok {
			cp = existing
		}
	}
	if cp == nil {
		cp = &checkpoint{Subpage: sp.url, Links: links, Completed: map[int]string{}}
		_ = saveCP(destDir, cp)
	}

	state := newCPState(destDir, cp)

	var pending []int
	for i := range links {
		if !state.isDone(i) {
			pending = append(pending, i)
		}
	}

	if len(pending) == 0 {
		log("%s All %d files already done\n", tag, len(links))
		res.skipped = int32(len(links))
	} else {
		log("%s Downloading %d/%d files (%d workers) ...\n", tag, len(pending), len(links), fileWorkers)

		sem := make(chan struct{}, fileWorkers)
		results := make(chan fileResult, len(pending))
		var wg sync.WaitGroup

		for _, pos := range pending {
			wg.Add(1)
			go func(p int, u string) {
				defer wg.Done()
				sem <- struct{}{}
				r := downloadFile(p, u, destDir, delayMs, retries)
				<-sem
				results <- r
			}(pos, links[pos])
		}
		go func() { wg.Wait(); close(results) }()

		var missing int32
		for r := range results {
			switch r.status {
			case "ok":
				atomic.AddInt32(&res.downloaded, 1)
				state.markDone(r.pos, r.filename)
				log("%s ✓  [%d/%d] %s\n", tag, r.pos+1, len(links), r.filename)
			case "skip":
				atomic.AddInt32(&res.skipped, 1)
				state.markDone(r.pos, r.filename)
			case "error":
				if isMissing(r.err) {
					// Confirmed 404 after full retry cycle. We do NOT mark
					// this done in the checkpoint — justice.gov has shown
					// false 404s that succeed on a later run, so we leave
					// it eligible for retry on the next invocation. It's
					// logged to missing.log for visibility but not treated
					// as permanently resolved.
					atomic.AddInt32(&missing, 1)
					missingLog(destDir, links[r.pos])
					log("%s –  [%d/%d] %s (404 after %d attempts — logged to missing.log, will retry next run)\n",
						tag, r.pos+1, len(links), r.filename, retries)
				} else {
					atomic.AddInt32(&res.errors, 1)
					logErr("%s ✗  [%d/%d] %v\n", tag, r.pos+1, len(links), r.err)
				}
			}
		}

		if missing > 0 {
			log("%s WARN  %d file(s) returned HTTP 404 — listed in %s/missing.log\n", tag, missing, destDir)
		}
	}

	lastPos, lastFile, completedCount := state.summary()
	if res.errors > 0 || completedCount < len(links) {
		log("%s ── checkpoint saved ──\n", tag)
		log("%s Completed: %d/%d\n", tag, completedCount, len(links))
		if lastFile != "" {
			log("%s Last success: [%d/%d] %s\n", tag, lastPos+1, len(links), lastFile)
		}
		log("%s Remaining: %d — rerun same command to resume\n", tag, len(links)-completedCount)
	}
	state.flush()

	log("%s Done — %d downloaded, %d skipped, %d errors\n",
		tag, res.downloaded, res.skipped, res.errors)
	return res
}



func sectionDir(sec string) string {
	switch sec {
	case "efta":
		return "efta"
	case "court":
		return "court-records"
	case "foia":
		return "foia"
	case "prior":
		return "prior-doj"
	}
	return sec
}

// ── Zip a whole section directory ─────────────────────────────────────

func zipSection(sectionPath, zipName string) (string, int64, error) {
	zipPath := zipName + ".zip"
	zf, err := os.Create(zipPath)
	if err != nil {
		return "", 0, fmt.Errorf("create zip: %w", err)
	}
	defer zf.Close()
	w := zip.NewWriter(zf)

	err = filepath.Walk(sectionPath, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return err
		}
		// Skip checkpoints and temp files
		name := info.Name()
		if name == checkpointFile || strings.HasSuffix(name, ".tmp") {
			return nil
		}
		rel, _ := filepath.Rel(sectionPath, path)
		dst, err := w.Create(rel)
		if err != nil {
			return err
		}
		src, err := os.Open(path)
		if err != nil {
			return err
		}
		defer src.Close()
		_, err = io.Copy(dst, src)
		return err
	})
	if err != nil {
		w.Close()
		os.Remove(zipPath)
		return "", 0, err
	}
	if err := w.Close(); err != nil {
		os.Remove(zipPath)
		return "", 0, err
	}
	info, err := os.Stat(zipPath)
	if err != nil {
		return "", 0, err
	}
	return zipPath, info.Size(), nil
}

// ── Main ──────────────────────────────────────────────────────────────

func main() {
	flag.Parse()

	// Parse -sections
	wantSections := map[string]bool{}
	for _, s := range strings.Split(*flagSections, ",") {
		wantSections[strings.TrimSpace(s)] = true
	}

	if err := os.MkdirAll(*flagOut, 0755); err != nil {
		fmt.Fprintf(os.Stderr, "ERROR  cannot create output dir %q: %v\n", *flagOut, err)
		os.Exit(1)
	}

	// ── Log file setup ────────────────────────────────────────────────
	// All output goes to both stdout and the log file simultaneously.
	var logWriter io.Writer = os.Stdout
	if *flagLog != "" {
		lf, err := os.OpenFile(*flagLog, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644)
		if err != nil {
			fmt.Fprintf(os.Stderr, "WARN  could not open log file %q: %v — logging to stdout only\n", *flagLog, err)
		} else {
			defer lf.Close()
			logWriter = io.MultiWriter(os.Stdout, lf)
			fmt.Fprintf(logWriter, "\n── Run started %s ──────────────────────────────────────────\n", time.Now().Format("2006-01-02 15:04:05"))
		}
	}
	// Redirect all fmt.Printf / fmt.Fprintf(os.Stdout) through logWriter.
	// We do this by replacing stdout at the os level isn't practical in Go,
	// so instead we pass logWriter through to a log helper.
	log := func(format string, args ...any) {
		fmt.Fprintf(logWriter, format, args...)
	}
	logErr := func(format string, args ...any) {
		msg := fmt.Sprintf(format, args...)
		fmt.Fprint(os.Stderr, msg)
		if logWriter != os.Stdout {
			fmt.Fprint(logWriter, msg)
		}
	}

	log("Index page      : %s\n", indexURL)
	log("Output          : %s/\n", *flagOut)
	log("Log file        : %s\n", *flagLog)
	log("Sections        : %s\n", *flagSections)
	log("Workers         : %d subpages concurrent\n", *flagWorkers)
	log("File workers    : %d per subpage\n", *flagFileWorkers)
	log("Page delay      : %dms between subpage scrapes\n", *flagPageDelay)
	log("File delay      : %dms between file downloads\n", *flagDelay)
	log("Retries         : %d\n", *flagRetries)
	log("Zip on complete : %v\n", !*flagNoZip)
	log("Reset           : %v\n\n", *flagReset)
	if *flagCookie != "" {
		log("Cookie          : set (%d bytes)\n\n", len(*flagCookie))
	} else {
		log("Cookie          : not set (may hit age gate — use -cookie if scraping fails)\n\n")
	}

	// Graceful Ctrl-C
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sigCh
		log("\n\nInterrupted — checkpoints saved. Rerun to resume.\n")
		os.Exit(2)
	}()

	// ── Step 1: discover subpages ─────────────────────────────────────
	var subpages []subpage
	if *flagSkipIndex {
		log("Skipping index page scrape — using hardcoded subpage URLs\n")
		subpages = hardcodedSubpages(wantSections)
	} else {
		log("Scraping index page to discover all subpages ...\n")
		var err error
		subpages, err = scrapeIndex(wantSections)
		if err != nil {
			logErr("ERROR  %v\n", err)
			logErr("TIP    rerun with -skip-index to bypass the index page entirely\n")
			os.Exit(1)
		}
	}

	bySec := map[string]int{}
	for _, sp := range subpages {
		bySec[sp.section]++
	}
	for sec, count := range bySec {
		log("  %-12s %d subpages\n", sec, count)
	}
	log("  ─────────────────\n")
	log("  Total         %d subpages to process\n\n", len(subpages))

	// ── Step 2a: scrape all subpages serially first ───────────────────
	// Running subpage scrapes concurrently triggers 403s because we hit
	// justice.gov with too many simultaneous page requests. Instead we
	// scrape all subpages sequentially (with page-delay between each),
	// collect the PDF link lists, then fan out the actual file downloads
	// concurrently. Checkpoints mean we skip re-scraping on resume.
	type subpageWork struct {
		sp    subpage
		links []string // nil = scrape failed, skip downloads
	}

	var work []subpageWork
	for _, sp := range subpages {
		destDir := filepath.Join(*flagOut, sectionDir(sp.section), sp.slug)

		// Check if we have a valid checkpoint with links already
		if !*flagReset {
			if cp, ok := loadCP(destDir); ok && len(cp.Links) > 0 {
				log("[%s/%s] Resuming from checkpoint (%d links cached)\n", sp.section, sp.slug, len(cp.Links))
				work = append(work, subpageWork{sp: sp, links: cp.Links})
				continue
			}
		}

		time.Sleep(time.Duration(*flagPageDelay) * time.Millisecond)
		log("[%s/%s] Scraping subpage ...\n", sp.section, sp.slug)
		links, err := scrapePDFs(sp.url, 0) // delay already applied above
		if err != nil {
			logErr("[%s/%s] ERROR  %v\n", sp.section, sp.slug, err)
			work = append(work, subpageWork{sp: sp, links: nil})
			continue
		}
		if len(links) == 0 {
			log("[%s/%s] No PDFs found (may be video/audio only) — skipping\n", sp.section, sp.slug)
			work = append(work, subpageWork{sp: sp, links: nil})
			continue
		}
		log("[%s/%s] Found %d PDFs\n", sp.section, sp.slug, len(links))

		// Persist link list to checkpoint immediately
		if err := os.MkdirAll(destDir, 0755); err == nil {
			cp := &checkpoint{Subpage: sp.url, Links: links, Completed: map[int]string{}}
			_ = saveCP(destDir, cp)
		}
		work = append(work, subpageWork{sp: sp, links: links})
	}

	log("\nAll subpages scraped. Starting concurrent file downloads ...\n\n")

	// ── Step 2b: fan out downloads concurrently ───────────────────────
	sem := make(chan struct{}, *flagWorkers)
	resCh := make(chan subpageResult, len(work))
	var wg sync.WaitGroup

	for _, w := range work {
		if w.links == nil {
			// Scrape failed — emit empty result
			resCh <- subpageResult{sp: w.sp}
			continue
		}
		wg.Add(1)
		go func(sw subpageWork) {
			defer wg.Done()
			sem <- struct{}{}
			r := processSubpageWithLinks(sw.sp, sw.links, *flagOut, *flagFileWorkers, *flagDelay, *flagRetries, *flagReset, logWriter)
			<-sem
			resCh <- r
		}(w)
	}
	go func() { wg.Wait(); close(resCh) }()

	// Collect results
	type secStats struct {
		found, downloaded, skipped, errors int32
	}
	stats := map[string]*secStats{}
	for _, sp := range subpages {
		if _, ok := stats[sp.section]; !ok {
			stats[sp.section] = &secStats{}
		}
	}

	for r := range resCh {
		s := stats[r.sp.section]
		s.found += int32(r.found)
		s.downloaded += r.downloaded
		s.skipped += r.skipped
		s.errors += r.errors
	}

	// ── Step 3: zip each section ──────────────────────────────────────
	var zips []string
	if !*flagNoZip {
		for sec, st := range stats {
			if st.errors > 0 {
				log("\nSKIP zip [%s] — %d errors remain; rerun to complete\n", sec, st.errors)
				continue
			}
			secPath := filepath.Join(*flagOut, sectionDir(sec))
			zipBase := filepath.Join(*flagOut, sectionDir(sec))
			log("\nZipping %s/ ...\n", sectionDir(sec))
			zipPath, sizeBytes, err := zipSection(secPath, zipBase)
			if err != nil {
				logErr("ERROR  zip [%s]: %v\n", sec, err)
			} else {
				log("✓  %s  (%.1f MB)\n", filepath.Base(zipPath), float64(sizeBytes)/1_048_576)
				zips = append(zips, zipPath)
			}
		}
	}

	// ── Final summary ─────────────────────────────────────────────────
	log("\n══════════════════════════════════════════════════════════════\n")
	log("%-14s %8s %10s %8s %8s\n", "Section", "Found", "Downloaded", "Skipped", "Errors")
	log(strings.Repeat("─", 54) + "\n")

	sectionOrder := []string{"efta", "court", "foia", "prior"}
	var totalFound, totalDL, totalSkip, totalErr int32
	for _, sec := range sectionOrder {
		st, ok := stats[sec]
		if !ok {
			continue
		}
		log("%-14s %8d %10d %8d %8d\n", sec, st.found, st.downloaded, st.skipped, st.errors)
		totalFound += st.found
		totalDL += st.downloaded
		totalSkip += st.skipped
		totalErr += st.errors
	}
	log(strings.Repeat("─", 54) + "\n")
	log("%-14s %8d %10d %8d %8d\n", "TOTAL", totalFound, totalDL, totalSkip, totalErr)

	if len(zips) > 0 {
		sort.Strings(zips)
		log("\nZip archives (%d):\n", len(zips))
		for _, z := range zips {
			info, _ := os.Stat(z)
			log("  %s  (%.1f MB)\n", filepath.Base(z), float64(info.Size())/1_048_576)
		}
	}

	log("\nFiles are in: %s/\n", *flagOut)
	if *flagLog != "" {
		log("Full log at   : %s\n", *flagLog)
	}
	if totalErr > 0 {
		log("\nWARN  %d file(s) failed — rerun same command to resume from checkpoints\n", totalErr)
		log("      Many HTTP 429? Try: -delay 1000 -workers 4\n")
		log("      Many timeouts?  Try: -delay 500  -retries 5\n")
		os.Exit(2)
	}
	log("Next step: python scripts/ingest.py\n")
}