package main

import sc "github.com/acme/app/internal/scoring"

func usesAlias() int {
	return sc.Compute(3)
}
