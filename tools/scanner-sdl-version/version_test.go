package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"os"
	"testing"
)

func fixture(t *testing.T) []byte {
	t.Helper()
	raw, e := os.ReadFile("testdata/e71-with-endpoint.yaml")
	if e != nil {
		t.Fatal(e)
	}
	return raw
}
func TestOfficialVersionKnownPublicFixture(t *testing.T) {
	raw := fixture(t)
	v, e := derive(raw)
	if e != nil {
		t.Fatal(e)
	}
	if base64.StdEncoding.EncodeToString(v) != "1s4ifNc1TSMdDSxnRkJJeB3jidGjyhdDisURhZLxf8M=" {
		t.Fatal("official version mismatch")
	}
}
func TestOfficialVersionIsNotRawYAMLSHA(t *testing.T) {
	raw := fixture(t)
	a, e := derive(raw)
	if e != nil {
		t.Fatal(e)
	}
	changed := append([]byte("# public harmless comment\n"), raw...)
	b, e := derive(changed)
	if e != nil {
		t.Fatal(e)
	}
	if !bytes.Equal(a, b) || sha256.Sum256(raw) == sha256.Sum256(changed) {
		t.Fatal("version and exact wire commitment confused")
	}
}
func TestOfficialVersionBindsLiteralJITAndPullPayload(t *testing.T) {
	raw := fixture(t)
	a, e := derive(raw)
	if e != nil {
		t.Fatal(e)
	}
	for _, needle := range []string{"SYNTHETIC_JIT_SECRET", "SYNTHETIC_PULL_SECRET"} {
		t.Run(needle, func(t *testing.T) {
			changed := bytes.ReplaceAll(raw, []byte(needle), []byte("OTHER_SYNTHETIC_LITERAL"))
			b, e := derive(changed)
			if e != nil {
				t.Fatal(e)
			}
			if bytes.Equal(a, b) {
				t.Fatal("literal payload did not change manifest version")
			}
		})
	}
}
func TestOfficialVersionRefusesUnsupportedInput(t *testing.T) {
	for _, raw := range [][]byte{nil, bytes.Repeat([]byte("x"), 65537), []byte("ac-secret://NAME"), []byte("not: [valid")} {
		if _, e := derive(raw); e == nil {
			t.Fatal("unsupported input admitted")
		}
	}
}

func TestOfficialVersionRefusesEscapedSemanticReferences(t *testing.T) {
	raw := fixture(t)
	changed := bytes.ReplaceAll(raw, []byte("SYNTHETIC_PULL_SECRET"), []byte("\"ac-\\u0073ecret://NAME\""))
	if bytes.Contains(changed, []byte("ac-secret://")) {
		t.Fatal("fixture must exercise escaped input")
	}
	if _, e := derive(changed); e == nil {
		t.Fatal("semantic Console secret reference accepted")
	}
}
