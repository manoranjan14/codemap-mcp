package main

import (
	"fmt"
	"github.com/acme/app/internal/scoring"
)

type Runner struct {
	rules *scoring.Rules
}

func (ru *Runner) Go(x int) int {
	return ru.rules.Apply(x)
}

func callsAcrossPackages() int {
	return scoring.Compute(1)
}

func callsSiblingFile() int {
	return scoring.Sibling()
}

func callsStdlib() {
	fmt.Println("hi")
}

func callsLocal() int {
	return local()
}

func local() int {
	return 2
}

func main() {
	callsAcrossPackages()
}
