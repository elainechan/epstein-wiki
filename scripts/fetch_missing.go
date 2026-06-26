// scripts/fetch_missing.go
//
// Downloads a hardcoded list of specific missing files.
// No scraping, no checkpoints — just fetches each URL directly.
//
// Build:  go build -o fetch_missing scripts/fetch_missing.go
// Run:    ./fetch_missing -out /Volumes/Bones/epstein-files -cookie "ak_bmsc=..."

package main

import (
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"time"
)

var (
	flagOut    = flag.String("out", ".", "Output directory for downloaded files")
	flagCookie = flag.String("cookie", "", "Cookie header (paste from browser DevTools)")
	flagDelay  = flag.Int("delay", 800, "Delay between requests in ms")
)

const userAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

// ── Add missing URLs here ─────────────────────────────────────────────

var missingFiles = []string{
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822476.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822477.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822504.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822507.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822508.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822509.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822668.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822670.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822689.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822690.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822692.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822694.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822715.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822717.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822731.pdf",
	"https://www.justice.gov/epstein/files/Court%20Records/Maxwell%20v.%20United%20States%2C%20No.%2024-1073%20%28U.S.%202025%29%20%28petition%20for%20cert.%29/EFTA02822732.pdf",
}

// ─────────────────────────────────────────────────────────────────────

var client = &http.Client{Timeout: 90 * time.Second}

func fetch(rawURL, outDir, cookie string) (string, error) {
	parsed, _ := url.Parse(rawURL)
	filename := filepath.Base(parsed.Path)
	outPath := filepath.Join(outDir, filename)

	if _, err := os.Stat(outPath); err == nil {
		return filename, nil // already exists
	}

	req, _ := http.NewRequest("GET", rawURL, nil)
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", "application/pdf,*/*")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	if cookie != "" {
		req.Header.Set("Cookie", cookie)
	}

	resp, err := client.Do(req)
	if err != nil {
		return filename, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return filename, fmt.Errorf("HTTP %d", resp.StatusCode)
	}

	tmp := outPath + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return filename, fmt.Errorf("create file: %w", err)
	}
	_, copyErr := io.Copy(f, resp.Body)
	closeErr := f.Close()
	if copyErr != nil {
		os.Remove(tmp)
		return filename, fmt.Errorf("write: %w", copyErr)
	}
	if closeErr != nil {
		os.Remove(tmp)
		return filename, fmt.Errorf("close: %w", closeErr)
	}
	if err := os.Rename(tmp, outPath); err != nil {
		os.Remove(tmp)
		return filename, fmt.Errorf("rename: %w", err)
	}
	return filename, nil
}

func main() {
	flag.Parse()

	if err := os.MkdirAll(*flagOut, 0755); err != nil {
		fmt.Fprintf(os.Stderr, "ERROR cannot create output dir: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("Downloading %d files → %s/\n", len(missingFiles), *flagOut)
	if *flagCookie == "" {
		fmt.Println("WARN  no -cookie set — may hit age gate")
	}
	fmt.Println()

	ok, skip, fail := 0, 0, 0
	for i, rawURL := range missingFiles {
		parsed, _ := url.Parse(rawURL)
		filename := filepath.Base(parsed.Path)

		// Skip if already on disk
		if _, err := os.Stat(filepath.Join(*flagOut, filename)); err == nil {
			fmt.Printf("[%d/%d] skip  %s (already exists)\n", i+1, len(missingFiles), filename)
			skip++
			continue
		}

		fmt.Printf("[%d/%d] ...   %s\n", i+1, len(missingFiles), filename)
		name, err := fetch(rawURL, *flagOut, *flagCookie)
		if err != nil {
			fmt.Fprintf(os.Stderr, "[%d/%d] FAIL  %s — %v\n", i+1, len(missingFiles), name, err)
			fail++
		} else {
			fmt.Printf("[%d/%d] ✓     %s\n", i+1, len(missingFiles), name)
			ok++
		}

		if i < len(missingFiles)-1 {
			time.Sleep(time.Duration(*flagDelay) * time.Millisecond)
		}
	}

	fmt.Printf("\nDone — %d downloaded, %d skipped, %d failed\n", ok, skip, fail)
	if fail > 0 {
		fmt.Println("Rerun the same command to retry failed files.")
		os.Exit(1)
	}
}
