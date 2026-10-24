// scanner-sdl-version derives the official version only; no wallet, network or deploy command.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"

	"pkg.akt.dev/go/sdl"
)

func derive(raw []byte) ([]byte, error) {
	if len(raw) == 0 || len(raw) > 65536 || bytes.Contains(raw, []byte("ac-secret://")) {
		return nil, fmt.Errorf("unsupported SDL")
	}
	doc, err := sdl.Read(raw)
	if err != nil {
		return nil, fmt.Errorf("invalid SDL")
	}
	manifest, err := doc.Manifest()
	if err != nil {
		return nil, fmt.Errorf("invalid manifest")
	}
	encoded, err := json.Marshal(manifest)
	if err != nil || bytes.Contains(encoded, []byte("ac-secret://")) {
		return nil, fmt.Errorf("unsupported reference")
	}
	version, err := doc.Version()
	if err != nil || len(version) != 32 {
		return nil, fmt.Errorf("invalid manifest version")
	}
	return version, nil
}

func run() int {
	raw, err := io.ReadAll(io.LimitReader(os.Stdin, 65537))
	if err != nil {
		return 2
	}
	version, err := derive(raw)
	if err != nil {
		return 2
	}
	wire := sha256.Sum256(raw)
	row := map[string]string{"schema": "just-akash/official-sdl-version/v1", "library": "pkg.akt.dev/go/sdl@v0.4.5-rc0", "manifest_version": base64.StdEncoding.EncodeToString(version), "wire_request_sha256": hex.EncodeToString(wire[:])}
	if err = json.NewEncoder(os.Stdout).Encode(row); err != nil {
		return 2
	}
	return 0
}

func main() {
	code := 2
	func() {
		defer func() {
			if recover() != nil {
				code = 2
			}
		}()
		code = run()
	}()
	if code != 0 {
		fmt.Fprintln(os.Stderr, "Official SDL version could not be verified")
	}
	os.Exit(code)
}
